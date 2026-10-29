"""Load the recognition and diarization models once and keep them resident across a job queue."""

from __future__ import annotations

import contextlib
import functools
import gc
import math
import os
import sys
import threading
import types
from pathlib import Path

import config


DIARIZATION_PIPELINE_ID = "pyannote/speaker-diarization-3.1"
# English speech on the GPU goes to Parakeet; on lectures it keeps Whisper's content at 2.5x the speed.
PARAKEET_LABEL = "Parakeet TDT 0.6B v3"
_PARAKEET_REPO = "istupakov/parakeet-tdt-0.6b-v3-onnx"
_PARAKEET_FILES = ("config.json", "vocab.txt", "nemo128.onnx", "encoder-model.onnx",
                   "encoder-model.onnx.data", "decoder_joint-model.onnx")
# A small multilingual Whisper decides which recognizer an auto-detect job uses.
LANGUAGE_ID_MODEL = "base"


def compute_type(device: str) -> str:
    return "float16" if device == "cuda" else "int8"


@contextlib.contextmanager
def _allow_unsafe_torch_load():
    """Temporarily allow weights_only=False — needed for pyannote checkpoints only."""
    import torch
    orig = torch.load

    @functools.wraps(orig)
    def _unsafe(*args, **kwargs):
        kwargs["weights_only"] = False
        return orig(*args, **kwargs)

    torch.load = _unsafe
    try:
        yield
    finally:
        torch.load = orig


def _accept_use_auth_token() -> None:
    """pyannote.audio 3.x downloads with use_auth_token, which huggingface_hub 1.x renamed to token.

    pyannote binds hf_hub_download when it is imported, so this runs on import, before pyannote is.
    """
    import huggingface_hub.file_download as hf_download
    orig = hf_download.hf_hub_download

    @functools.wraps(orig)
    def _download(*args, **kwargs):
        if "use_auth_token" in kwargs:
            kwargs["token"] = kwargs.pop("use_auth_token")
        return orig(*args, **kwargs)

    hf_download.hf_hub_download = _download


_accept_use_auth_token()


def empty_cuda_cache() -> None:
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()


_CUDA_DLL_HANDLES = []


def _load_cuda_libraries():
    """Load one complete cuDNN suite before CTranslate2 loads its bundled core."""
    if sys.platform != "win32" or _CUDA_DLL_HANDLES:
        return
    import ctypes
    import torch

    lib = Path(torch.__file__).parent / "lib"
    handles = [os.add_dll_directory(str(lib))]
    # PATH alone does not prevent mixing CTranslate2's core with Torch's
    # sub-libraries, which aborts the process at cudnnGetLibConfig.
    handles.extend(ctypes.WinDLL(str(path)) for path in sorted(lib.glob("cudnn*64_*.dll")))
    os.environ["PATH"] = str(lib) + os.pathsep + os.environ.get("PATH", "")
    _CUDA_DLL_HANDLES.extend(handles)


def resolve_device(device: str | None = None) -> str:
    device = device or config.DEVICE
    if device != "cpu":
        _load_cuda_libraries()
    if device == "auto":
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda"
        except Exception:
            pass
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
        except Exception:
            pass
        return "cpu"
    return device


def _load_whisper(model_name: str, device: str):
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model
    if device == "cuda":
        _load_cuda_libraries()
    try:
        # A downloaded model loads without the HuggingFace update check, which costs about 2s per load.
        model_path = download_model(model_name, local_files_only=True)
    except Exception:
        model_path = model_name
    return WhisperModel(model_path, device=device, compute_type=compute_type(device))


def _load_parakeet():
    """Parakeet through onnx-asr on CUDA, returning token timestamps."""
    import onnx_asr
    import onnxruntime
    from huggingface_hub import snapshot_download

    from appdata import APP_DATA_DIR
    # A plain folder: onnxruntime rejects external weights reached through HuggingFace cache links.
    folder = APP_DATA_DIR / "models" / "parakeet-tdt-0.6b-v3"
    if not all((folder / name).is_file() for name in _PARAKEET_FILES):
        snapshot_download(_PARAKEET_REPO, allow_patterns=list(_PARAKEET_FILES), local_dir=folder)
    _load_cuda_libraries()
    # Hide per-node placement warnings; onnxruntime deliberately keeps shape ops on the CPU.
    onnxruntime.set_default_logger_severity(3)
    model = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3", folder, providers=["CUDAExecutionProvider"])
    return model.with_timestamps()


def _load_diarization(token: str, device: str):
    """Return pyannote on ``device``, or on the CPU when the GPU cannot take it, and where it runs."""
    import torch
    from pyannote.audio import Pipeline
    with _allow_unsafe_torch_load():
        pipeline = Pipeline.from_pretrained(DIARIZATION_PIPELINE_ID, use_auth_token=token)
    try:
        pipeline.to(torch.device(device))
    except RuntimeError as exc:
        if device != "cuda":
            raise
        print(f"      GPU move failed ({exc}), falling back to CPU", flush=True)
        device = "cpu"
        pipeline.to(torch.device(device))
    pipeline.get_embeddings = types.MethodType(_embed_active_speakers, pipeline)
    if device == "cuda":
        _half_precision_embeddings(pipeline)
    return pipeline, device


def _embed_active_speakers(self, file, binary_segmentations, exclude_overlap=False, hook=None):
    """pyannote 3.x get_embeddings without the (chunk, speaker) pairs whose mask is empty.

    pyannote embeds every local speaker slot of every chunk, then clustering drops the empty ones
    and marks them inactive. Skipping them leaves the speaker turns unchanged at about half the cost.
    """
    import numpy as np
    import torch
    duration = binary_segmentations.sliding_window.duration
    num_chunks, num_frames, num_speakers = binary_segmentations.data.shape
    if exclude_overlap:
        num_samples = duration * self._embedding.sample_rate
        min_num_frames = math.ceil(num_frames * self._embedding.min_num_samples / num_samples)
        clean = binary_segmentations.data * (np.sum(binary_segmentations.data, axis=2, keepdims=True) < 2)
    else:
        min_num_frames, clean = -1, binary_segmentations.data
    embeddings = np.full((num_chunks * num_speakers, self._embedding.dimension), np.nan, dtype=np.float32)
    rows, waveforms, masks = [], [], []

    def flush():
        if rows:
            embeddings[rows] = self._embedding(torch.vstack(waveforms), masks=torch.vstack(masks))
            rows.clear()
            waveforms.clear()
            masks.clear()

    for c, (chunk, chunk_masks) in enumerate(binary_segmentations):
        chunk_masks = np.nan_to_num(chunk_masks, nan=0.0).astype(np.float32)
        clean_masks = np.nan_to_num(clean[c], nan=0.0).astype(np.float32)
        active = [s for s in range(num_speakers) if chunk_masks[:, s].sum() > 0]
        if not active:
            continue
        waveform, _ = self._audio.crop(file, chunk, duration=duration, mode="pad")
        for s in active:
            mask = clean_masks[:, s] if clean_masks[:, s].sum() > min_num_frames else chunk_masks[:, s]
            rows.append(c * num_speakers + s)
            waveforms.append(waveform[None])
            masks.append(torch.from_numpy(mask)[None])
            if len(rows) >= self.embedding_batch_size:
                flush()
    flush()
    return embeddings.reshape(num_chunks, num_speakers, -1)


def _half_precision_embeddings(pipeline) -> None:
    """Run the speaker-embedding ResNet in float16; filterbank features stay float32 to avoid overflow."""
    import torch
    resnet = pipeline._embedding.model_.resnet
    resnet.to(memory_format=torch.channels_last)
    forward = resnet.forward

    @functools.wraps(forward)
    def half_forward(*args, **kwargs):
        with torch.autocast("cuda", dtype=torch.float16):
            result = forward(*args, **kwargs)
        return tuple(x.float() for x in result) if isinstance(result, tuple) else result.float()

    resnet.forward = half_forward


class _Resident:
    """One model kept loaded between jobs, reloaded when its parameters change.

    A preload thread and a job stage can ask for the same model; the second caller waits for the
    first load instead of loading it again.
    """

    def __init__(self, load):
        self._load = load
        self._lock = threading.RLock()
        self._entry = None  # (params, model)

    def get(self, *params):
        with self._lock:
            if self._entry is None or self._entry[0] != params:
                self.release()
                self._entry = (params, self._load(*params))
            return self._entry[1]

    def release(self) -> None:
        with self._lock:
            if self._entry is not None:
                self._entry = None
                gc.collect()
                empty_cuda_cache()


# The loaders are looked up at call time so tests can replace them.
_whisper = _Resident(lambda model_name, device: _load_whisper(model_name, device))
_language_id = _Resident(lambda device: _load_whisper(LANGUAGE_ID_MODEL, device))
_parakeet = _Resident(lambda: _load_parakeet())
_diarization = _Resident(lambda token, device: _load_diarization(token, device))


def get_whisper_model(model_name: str, device: str):
    return _whisper.get(model_name, device)


def get_language_id_model(device: str):
    return _language_id.get(device)


def get_parakeet_model():
    return _parakeet.get()


def get_diarize_pipeline(token: str, device: str):
    """Return the pipeline and the device it runs on."""
    return _diarization.get(token, device)


def release_models() -> None:
    for model in (_whisper, _language_id, _parakeet, _diarization):
        model.release()
