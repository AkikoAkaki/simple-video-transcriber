"""The GPU runtime, and models kept loaded across a job queue."""

from __future__ import annotations

import gc
import os
import sys
import threading
from pathlib import Path


_CUDA_DLL_HANDLES = []


def _load_cuda_libraries() -> None:
    """Load one complete cuDNN suite before CTranslate2 loads its bundled core."""
    if _CUDA_DLL_HANDLES:
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


def resolve_device(device: str) -> str:
    """Turn a requested device into a usable one, readying CUDA first; models load only after this."""
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


def empty_cuda_cache() -> None:
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()


_RESIDENTS: list[Resident] = []


class Resident:
    """One model kept loaded between jobs, reloaded when its parameters change.

    A preload thread and a job stage can ask for the same model; the second caller waits for the
    first load instead of loading it again.
    """

    def __init__(self, load):
        self._load = load
        self._lock = threading.RLock()
        self._entry = None
        _RESIDENTS.append(self)

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


def release_models() -> None:
    for model in _RESIDENTS:
        model.release()
