"""
simple-video-transcriber configuration
Defaults for the dashboard and CLI; saved dashboard settings take precedence.
"""
from pathlib import Path
import os

# ── Directories ──────────────────────────────────────────────────────────────
# Root of this repo (don't change)
ROOT_DIR = Path(__file__).parent.resolve()

# OBS recordings are watched by the tray service. Override this on another
# machine with MEETING_TRANSCRIBER_WATCH_DIR.
WATCH_DIR = Path(os.environ.get("MEETING_TRANSCRIBER_WATCH_DIR", Path.home() / "Videos"))

# Transcripts are written here as Markdown files
TRANSCRIPT_DIR = ROOT_DIR / "transcripts"

# Intermediate files (16k WAV, JSON caches)
CACHE_DIR = ROOT_DIR / "cache"

# ── Whisper ───────────────────────────────────────────────────────────────────
# Model size vs. quality trade-off:
#   large-v3-turbo       → fast, high accuracy (~1.6 GB, recommended)
#   large-v3             → best quality, needs ~6 GB VRAM
WHISPER_MODEL = "large-v3-turbo"

# Single source of truth for supported Whisper models.
SUPPORTED_MODELS = ("large-v3-turbo", "large-v3")


def normalize_whisper_model(value) -> str:
    """Map legacy/unsupported model names to the default supported model."""
    if isinstance(value, str) and value in SUPPORTED_MODELS:
        return value
    return WHISPER_MODEL

# English speech on an NVIDIA GPU is recognized with Parakeet TDT 0.6B v3 instead
# of Whisper: about 2.5x faster with the same content on lectures. Auto-detect jobs
# first identify the language; other languages, CPU runs, and jobs with names or
# terms (which Parakeet cannot use) keep Whisper. First use downloads about 2.5 GB.
PARAKEET_FOR_ENGLISH = True

# Transcription language:
#   None  → auto-detect (recommended for mixed-language audio)
#   "en"  → force English
#   "zh"  → force Chinese
#   "ja"  → force Japanese  (see https://en.wikipedia.org/wiki/List_of_ISO_639_language_codes)
LANGUAGE = None

# Compute device:
#   "auto"  → use CUDA if available, else CPU
#   "cuda"  → force GPU (faster, requires NVIDIA GPU)
#   "cpu"   → force CPU (slow but always works)
DEVICE = "auto"

# ── Speaker Diarization (pyannote) ───────────────────────────────────────────
# HuggingFace token — needed to download pyannote models.
# Leave empty to use the dashboard token store or HF_TOKEN environment variable.
# Get a free token at: https://hf.co/settings/tokens
# Then accept model terms at:
#   https://hf.co/pyannote/speaker-diarization-3.1
#   https://hf.co/pyannote/segmentation-3.0
HF_TOKEN = ""

# Upper limit on speaker count; use NUM_SPEAKERS for an exact count.
# None = auto-detect.
MAX_SPEAKERS = None

# Exact speaker count, when known for a recording.  Leave unset to let
# pyannote estimate the number of speakers automatically.
NUM_SPEAKERS = None

# Optional comma-separated names and technical terms passed to Whisper.
HOTWORDS = ""

# ── Local API ────────────────────────────────────────────────────────────────
# The Obsidian plugin and the Explorer "转录" menu reach the tray on this
# 127.0.0.1 port.
API_PORT = 47321

# ── Watcher ──────────────────────────────────────────────────────────────────
# Files smaller than this are ignored (filters out accidental tiny files).
MIN_FILE_SIZE_KB = 100

# Supported video/audio extensions the watcher will pick up.
WATCH_EXTENSIONS = {
    ".mp4", ".m4a", ".mov", ".mkv", ".webm", ".mp3", ".wav",
    ".ts", ".flac", ".aac", ".opus",
}
