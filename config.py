"""Defaults for the tray and the CLI. The tray saves only its watch folder and whether it is watching."""
import json
import os
from pathlib import Path

# ── Directories ──────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).parent.resolve()

# OBS recordings are watched by the tray service. Override this on another
# machine with MEETING_TRANSCRIBER_WATCH_DIR.
WATCH_DIR = Path(os.environ.get("MEETING_TRANSCRIBER_WATCH_DIR", Path.home() / "Videos"))

# Transcripts are written here as Markdown files
TRANSCRIPT_DIR = ROOT_DIR / "transcripts"

# Intermediate files (16k WAV, JSON caches)
CACHE_DIR = ROOT_DIR / "cache"

# ── Whisper ───────────────────────────────────────────────────────────────────
#   large-v3-turbo       → fast, high accuracy (~1.6 GB, recommended)
#   large-v3             → best quality, needs ~6 GB VRAM
WHISPER_MODEL = "large-v3-turbo"
SUPPORTED_MODELS = ("large-v3-turbo", "large-v3")

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

# ── Local API ────────────────────────────────────────────────────────────────
# The Explorer "转录" menu and the Obsidian plugin reach the tray on this 127.0.0.1 port.
API_PORT = 47321

# ── Watcher ──────────────────────────────────────────────────────────────────
# Files smaller than this are ignored (filters out accidental tiny files).
MIN_FILE_SIZE_KB = 100

# A recording counts as finished once its size has held still this long.
STABLE_SECONDS = 15

# The Explorer menu and the Obsidian plugin offer only audio and video: .ts on a
# developer machine is far more often TypeScript than a video stream.
_MEDIA = json.loads((ROOT_DIR / "media_extensions.json").read_text(encoding="utf-8"))
WATCH_EXTENSIONS = {*_MEDIA["audio"], *_MEDIA["video"], *_MEDIA["watch_only"]}
