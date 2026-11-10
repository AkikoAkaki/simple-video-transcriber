"""The source recording as the 16 kHz mono audio every model reads."""

from __future__ import annotations

import contextlib
import subprocess
import threading
import wave
from pathlib import Path

from paths import partial_path
from pipeline.errors import TranscriptionError
from pipeline.reporting import Reporter, log


SAMPLE_RATE = 16000
_MIN_WAV_BYTES = 1024


def get_wav_duration(wav_path: Path) -> float:
    try:
        with wave.open(str(wav_path)) as wav:
            return wav.getnframes() / wav.getframerate()
    except (OSError, EOFError, wave.Error, ZeroDivisionError):
        return 0.0


class SharedAudio:
    """The 16 kHz WAV decoded once and shared by recognition and diarization."""

    def __init__(self, wav_path: Path):
        self.path = wav_path
        self._lock = threading.Lock()
        self._samples = None

    def samples(self):
        with self._lock:
            if self._samples is None:
                from faster_whisper.audio import decode_audio
                self._samples = decode_audio(str(self.path), sampling_rate=SAMPLE_RATE)
            return self._samples


def convert_to_wav(input_path: Path, output_path: Path, report: Reporter) -> None:
    if output_path.exists():
        if output_path.stat().st_size < _MIN_WAV_BYTES:
            output_path.unlink()
        else:
            report.stage("converting", 1.0, f"Using cached 16 kHz WAV {output_path.name}")
            return
    partial = partial_path(output_path)
    with contextlib.suppress(OSError):
        partial.unlink(missing_ok=True)
    report.stage("converting", 0.0, "Converting audio to 16 kHz mono WAV")
    with report.heartbeat("converting", "Audio conversion is still running"):
        try:
            result = subprocess.run(
                ["ffmpeg", "-nostdin", "-y", "-i", str(input_path), "-vn", "-sn", "-dn",
                 "-ac", "1", "-ar", str(SAMPLE_RATE), str(partial)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            raise TranscriptionError("ffmpeg not found in PATH; install it from https://ffmpeg.org/download.html")
    if result.returncode != 0:
        partial.unlink(missing_ok=True)
        log(f"ffmpeg exited with code {result.returncode}:")
        for line in result.stderr.splitlines()[-20:]:
            log(f"       {line}")
        raise TranscriptionError(f"ffmpeg failed with exit code {result.returncode}")
    if not partial.is_file() or partial.stat().st_size < _MIN_WAV_BYTES:
        partial.unlink(missing_ok=True)
        raise TranscriptionError("ffmpeg produced no usable WAV output")
    partial.replace(output_path)
    report.stage("converting", 1.0, f"Audio conversion completed ({output_path.stat().st_size / 1024 / 1024:.1f} MB)")
