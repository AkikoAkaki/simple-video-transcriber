"""How the transcription pipeline describes failure."""

from __future__ import annotations


class TranscriptionError(RuntimeError):
    """A job-level failure that must not terminate the persistent worker."""


def is_out_of_memory(exc: BaseException | None) -> bool:
    while exc is not None:
        message = str(exc).lower()
        # onnxruntime reports a full GPU as a failed allocation.
        if isinstance(exc, MemoryError) or "out of memory" in message or "failed to allocate memory" in message:
            return True
        exc = exc.__cause__
    return False
