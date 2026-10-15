"""Load and reuse the Whisper and pyannote models, keeping at most one of them resident."""

from __future__ import annotations

import contextlib
import functools
import gc
import os
import sys
from pathlib import Path

import config


DIARIZATION_PIPELINE_ID = "pyannote/speaker-diarization-3.1"


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
    return WhisperModel(model_name, device=device, compute_type=compute_type(device))


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
    return pipeline, device


_whisper = None  # ((model_name, device), model)
_diarization = None  # ((token, requested device), pipeline, device it runs on)


def get_whisper_model(model_name: str, device: str):
    global _whisper
    release_diarize_pipeline()
    params = (model_name, device)
    if _whisper is None or _whisper[0] != params:
        release_whisper_model()
        if device == "cuda":
            _load_cuda_libraries()
        _whisper = (params, _load_whisper(model_name, device))
    return _whisper[1]


def get_diarize_pipeline(token: str, device: str):
    """Return the pipeline and the device it runs on."""
    global _diarization
    release_whisper_model()
    params = (token, device)
    if _diarization is None or _diarization[0] != params:
        release_diarize_pipeline()
        _diarization = (params, *_load_diarization(token, device))
    return _diarization[1], _diarization[2]


def release_whisper_model() -> None:
    global _whisper
    if _whisper is not None:
        _whisper = None
        gc.collect()
        empty_cuda_cache()


def release_diarize_pipeline() -> None:
    global _diarization
    if _diarization is not None:
        _diarization = None
        empty_cuda_cache()


def release_models() -> None:
    release_whisper_model()
    release_diarize_pipeline()
