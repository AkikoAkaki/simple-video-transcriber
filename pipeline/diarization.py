"""Label who speaks when with pyannote."""

from __future__ import annotations

import contextlib
import functools
import math
import types

from pipeline.audio import SAMPLE_RATE, SharedAudio
from pipeline.errors import TranscriptionError, is_out_of_memory
from pipeline.models import Resident, empty_cuda_cache
from pipeline.reporting import Reporter, log


PIPELINE_ID = "pyannote/speaker-diarization-3.1"


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


@functools.cache
def _accept_use_auth_token() -> None:
    """pyannote.audio 3.x downloads with use_auth_token, which huggingface_hub 1.x renamed to token.

    pyannote binds hf_hub_download when it is imported, so this must run before that import.
    """
    import huggingface_hub.file_download as hf_download
    orig = hf_download.hf_hub_download

    @functools.wraps(orig)
    def _download(*args, **kwargs):
        if "use_auth_token" in kwargs:
            kwargs["token"] = kwargs.pop("use_auth_token")
        return orig(*args, **kwargs)

    hf_download.hf_hub_download = _download


def _load(token: str, device: str):
    """Return pyannote on ``device``, or on the CPU when the GPU cannot take it, and where it runs."""
    _accept_use_auth_token()
    import torch
    from pyannote.audio import Pipeline
    with _allow_unsafe_torch_load():
        pipeline = Pipeline.from_pretrained(PIPELINE_ID, use_auth_token=token)
    try:
        pipeline.to(torch.device(device))
    except RuntimeError as exc:
        if device != "cuda":
            raise
        log(f"      GPU move failed ({exc}), falling back to CPU")
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


_pipeline = Resident(_load)


def diarization_cache_params(max_speakers: int | None, num_speakers: int | None) -> dict:
    return dict(pipeline=PIPELINE_ID, max_speakers=max_speakers, num_speakers=num_speakers)


def preload(token: str, device: str) -> None:
    _pipeline.get(token, device)


def run_diarization(audio: SharedAudio, token: str, *, num_speakers: int | None, max_speakers: int | None,
                    device: str, report: Reporter) -> list[dict]:
    import torch

    report.stage("loading_diarization", 0.0, f"Loading {PIPELINE_ID}")
    try:
        pipeline, device = _pipeline.get(token, device)
    except Exception as e:
        err = str(e)
        if any(k in err for k in ("401", "403", "gated", "unauthorized", "PermissionError")):
            raise TranscriptionError(
                f"HuggingFace denied access to the diarization model ({err}). Check the token and accept "
                f"the terms at https://hf.co/{PIPELINE_ID} and https://hf.co/pyannote/segmentation-3.0"
            ) from e
        raise TranscriptionError(f"Speaker diarization model could not be loaded: {err}") from e

    speakers = ({"num_speakers": num_speakers} if num_speakers
                else {"max_speakers": max_speakers} if max_speakers else {})
    detail = "".join(f", {name}={value}" for name, value in speakers.items())
    report.stage("diarizing", 0.0, f"Running speaker diarization on {device}{detail}")
    waveform = {"waveform": torch.from_numpy(audio.samples())[None], "sample_rate": SAMPLE_RATE}
    with report.heartbeat("diarizing", "Speaker diarization is still running"):
        try:
            diarization = pipeline(waveform, **speakers)
        except RuntimeError as e:
            hint = "; retry on the CPU" if is_out_of_memory(e) else ""
            raise TranscriptionError(f"Speaker diarization failed: {e}{hint}") from e

    turns = [{"start": round(turn.start, 2), "end": round(turn.end, 2), "speaker": str(speaker)}
             for turn, _, speaker in diarization.itertracks(yield_label=True)]
    if not turns:
        raise TranscriptionError("Speaker diarization returned no speaker turns")
    report.stage("diarizing", 1.0, f"Identified {len(turns)} speaker turns")
    empty_cuda_cache()
    return turns
