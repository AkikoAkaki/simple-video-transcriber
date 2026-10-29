#!/usr/bin/env python3
"""
simple-video-transcriber — transcribe.py
Transcribe a video or audio file with speaker labels.

Usage:
  python transcribe.py <video>                    # full pipeline
  python transcribe.py <video> --language en      # force language
  python transcribe.py <video> --transcribe-only  # skip diarization
  python transcribe.py <video> --diarize-only     # reuse matching stage caches

Output: transcripts/<filename>_<source-fingerprint>.md
"""

import argparse
import contextlib
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

# Force UTF-8 stdout so Unicode characters (checkmarks, arrows, em-dashes)
# don't crash on Windows systems with GBK/CP936 console encoding.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import logging
import threading
import time
import warnings

# Suppress verbose/cosmetic warnings from pyannote and huggingface_hub
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
logging.getLogger("pyannote").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message=".*TensorFloat-32.*")
warnings.filterwarnings("ignore", message=r".*std\(\).*degrees of freedom.*")
warnings.filterwarnings("ignore", message=".*resume_download.*")

import config
from appdata import TokenStore
from models import (DIARIZATION_PIPELINE_ID, LANGUAGE_ID_MODEL, PARAKEET_LABEL, compute_type,
                    empty_cuda_cache, get_diarize_pipeline, get_language_id_model, get_parakeet_model,
                    get_whisper_model, release_models, resolve_device)
from paths import JobPaths, SourceChangedError, audio_cache_lock, job_paths, partial_path, source_fingerprint
from protocol import SERVER_READY, STAGE_COUNT, STAGE_STEPS, JobOptions, format_event
from transcript import format_time, generate_markdown, merge_results


CACHE_SCHEMA_VERSION = 2
SAMPLE_RATE = 16000
# Auto-detect jobs use Parakeet only when the language ID is at least this sure the speech is English.
ENGLISH_CONFIDENCE = 0.8
# Parakeet attends over the whole clip, so speech is cut into spans of at most this length.
PARAKEET_MAX_SPAN_SEC = 25
# Batch 16 was slower than 8 on an 8 GB card and nearly filled it.
PARAKEET_BATCH = 8
# onnx-asr reports when each token starts; a word ends at most this long after its last token.
_WORD_TAIL_SEC = 0.24


def _cache_key(kind: str, **params) -> str:
    """Return a stable key for a stage's inputs and quality-affecting options."""
    payload = {"kind": kind, "schema_version": CACHE_SCHEMA_VERSION, **params}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _read_stage_cache(path: Path, kind: str, cache_key: str) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        print(f"[WARN] {kind} cache is corrupt — re-running...", flush=True)
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    if (not isinstance(payload, dict) or payload.get("schema_version") != CACHE_SCHEMA_VERSION or
            payload.get("kind") != kind or payload.get("cache_key") != cache_key):
        return None
    # Older versions cached empty results from transiently silent or corrupt audio.
    return payload if isinstance(payload.get("result"), list) and payload["result"] else None


def _write_stage_cache(path: Path, kind: str, cache_key: str, result: list[dict],
                       metadata: dict | None = None) -> None:
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": kind,
        "cache_key": cache_key,
        "result": result,
        **(metadata or {}),
    }
    partial = partial_path(path)
    partial.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    partial.replace(path)


_INITIAL_PROMPTS = {
    "zh": "以下是普通话的会议记录，包含完整的标点符号。",
    "en": "Here is a transcript of the meeting with complete punctuation.",
    "ja": "これは会議の書き起こしです。句読点を含めます。",
    "ko": "다음은 회의 녹취록이며 완전한 구두점이 포함되어 있습니다.",
}


def select_initial_prompt(language: str | None) -> str | None:
    return _INITIAL_PROMPTS.get(language.lower()) if language else None


def _whisper_cache_key(model_name: str, device: str, language: str | None,
                       hotwords: str | None, word_timestamps: bool, recognizer: str = "whisper") -> str:
    params = dict(model=model_name, device=device, compute_type=compute_type(device),
                  language=language or "auto", hotwords=hotwords or "",
                  word_timestamps=bool(word_timestamps),
                  initial_prompt=select_initial_prompt(language) or "")
    if recognizer != "whisper":
        # An auto-detect job picks its recognizer from the audio, so the key records the rule.
        params["recognizer"] = f"{recognizer}:{PARAKEET_LABEL}:{LANGUAGE_ID_MODEL}>={ENGLISH_CONFIDENCE}"
    return _cache_key("whisper", **params)


def choose_recognizer(language: str | None, hotwords: str | None, device: str) -> str:
    """Return "whisper", "parakeet", or "detect" (identify the language from the audio first)."""
    # Parakeet takes no prompt, so a job with names or terms keeps Whisper.
    if device != "cuda" or not config.PARAKEET_FOR_ENGLISH or (hotwords or "").strip():
        return "whisper"
    if not language:
        return "detect"
    return "parakeet" if language.lower() == "en" else "whisper"


def _diarization_cache_key(max_speakers: int | None, num_speakers: int | None) -> str:
    return _cache_key(
        "diarization", pipeline=DIARIZATION_PIPELINE_ID,
        max_speakers=max_speakers, num_speakers=num_speakers,
    )


class TranscriptionError(RuntimeError):
    """A job-level failure that must not terminate the persistent worker."""


_EVENT_LOCK = threading.Lock()
# Set while the server runs a job; progress then goes to its controller as events instead of CLI lines.
_current_job_id = ""


def _print(line: str) -> None:
    """Recognition and diarization may report at once; keep each output line whole."""
    with _EVENT_LOCK:
        print(line, flush=True)


def _emit_event(event: str, **payload) -> None:
    if not _current_job_id:
        return
    record = {"event": event, "timestamp": time.time(), "job_id": _current_job_id, **payload}
    _print(format_event(record))


def _report(stage: str, progress: float, message: str, *, event: str = "stage", **extra) -> None:
    if _current_job_id:
        _emit_event(event, stage=stage, progress=progress, message=message, **extra)
    else:
        step = STAGE_STEPS.get(stage)
        _print(f"[{step}/{STAGE_COUNT}] {message}" if step else message)


@contextlib.contextmanager
def _heartbeat(stage: str, message: str, interval: float = 10.0):
    stop = threading.Event()
    started = time.monotonic()

    def _run():
        while not stop.wait(interval):
            _emit_event("heartbeat", stage=stage,
                        elapsed_sec=round(time.monotonic() - started, 1),
                        message=message)

    thread = threading.Thread(target=_run, name=f"{stage}-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)


def get_wav_duration(wav_path: Path) -> float:
    """Read WAV file header directly to calculate duration without loading torchaudio."""
    try:
        with open(wav_path, "rb") as f:
            riff_header = f.read(12)
            if riff_header[:4] != b"RIFF" or riff_header[8:12] != b"WAVE":
                return 0.0

            sample_rate = 16000
            channels = 1
            bits_per_sample = 16
            data_size = 0

            while True:
                chunk_header = f.read(8)
                if len(chunk_header) < 8:
                    break
                chunk_id = chunk_header[:4]
                chunk_len = struct.unpack("<I", chunk_header[4:8])[0]

                if chunk_id == b"fmt ":
                    fmt_data = f.read(chunk_len)
                    if len(fmt_data) >= 16:
                        channels = struct.unpack("<H", fmt_data[2:4])[0]
                        sample_rate = struct.unpack("<I", fmt_data[4:8])[0]
                        bits_per_sample = struct.unpack("<H", fmt_data[14:16])[0]
                    if chunk_len & 1:
                        f.seek(1, 1)
                elif chunk_id == b"data":
                    data_size = chunk_len
                    break
                else:
                    f.seek(chunk_len + (chunk_len & 1), 1)

            bytes_per_second = sample_rate * channels * (bits_per_sample // 8)
            if bytes_per_second > 0:
                return data_size / bytes_per_second
    except (OSError, struct.error):
        pass
    return 0.0


def decode_audio(wav_path: Path):
    from faster_whisper.audio import decode_audio as decode
    return decode(str(wav_path), sampling_rate=SAMPLE_RATE)


class SharedAudio:
    """The 16 kHz WAV decoded once and shared by Whisper and pyannote."""

    def __init__(self, wav_path: Path):
        self.path = wav_path
        self._lock = threading.Lock()
        self._samples = None

    def samples(self):
        with self._lock:
            if self._samples is None:
                self._samples = decode_audio(self.path)
            return self._samples


def prepare_paths(input_path: Path, output_dir: Path | None = None, title: str = "",
                  fingerprint: str | None = None) -> JobPaths:
    paths = job_paths(input_path, config.CACHE_DIR, output_dir or config.TRANSCRIPT_DIR,
                      fingerprint or source_fingerprint(input_path), title)
    paths.wav.parent.mkdir(parents=True, exist_ok=True)
    paths.output_md.parent.mkdir(parents=True, exist_ok=True)
    return paths


def get_hf_token() -> str:
    if config.HF_TOKEN:
        return config.HF_TOKEN
    token = os.environ.get("HF_TOKEN", "").strip()
    if token:
        return token
    stored = TokenStore().get()
    if stored:
        return stored
    print("[INFO] No HuggingFace token found.", flush=True)
    print("       To enable: save a token in the tray dashboard (or set HF_TOKEN env var)", flush=True)
    print(f"       Accept model terms at: https://hf.co/{DIARIZATION_PIPELINE_ID}", flush=True)
    return ""


# ── Step 1: Audio conversion ──────────────────────────────────────────────────

def convert_to_wav(input_path: Path, output_path: Path):
    if output_path.exists():
        if output_path.stat().st_size < 1024:
            output_path.unlink()
        else:
            _report("converting", 1.0, f"Using cached 16 kHz WAV {output_path.name}")
            return
    partial = partial_path(output_path)
    with contextlib.suppress(OSError):
        partial.unlink(missing_ok=True)
    _report("converting", 0.0, "Converting audio to 16 kHz mono WAV")
    with _heartbeat("converting", "Audio conversion is still running"):
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-y", "-i", str(input_path),
                    "-vn", "-sn", "-dn",
                    "-ac", "1", "-ar", "16000", str(partial),
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            raise TranscriptionError("ffmpeg not found in PATH; install it from https://ffmpeg.org/download.html")
    if result.returncode != 0:
        partial.unlink(missing_ok=True)
        print(f"ffmpeg exited with code {result.returncode}:", flush=True)
        for line in result.stderr.splitlines()[-20:]:
            print(f"       {line}", flush=True)
        raise TranscriptionError(f"ffmpeg failed with exit code {result.returncode}")
    if not partial.is_file() or partial.stat().st_size < 1024:
        partial.unlink(missing_ok=True)
        raise TranscriptionError("ffmpeg produced no usable WAV output")
    partial.replace(output_path)
    _report("converting", 1.0,
            f"Audio conversion completed ({output_path.stat().st_size / 1024 / 1024:.1f} MB)")


# ── Step 2: Whisper transcription ─────────────────────────────────────────────

def run_whisper(audio: SharedAudio, language: str | None, hotwords: str, *,
                model_name: str, device: str) -> tuple[list[dict], float | None]:
    """Recognize speech; return the segments and the audio duration Whisper measured."""
    lang_display = language or "auto-detect"
    _report("loading_whisper", 0.0, f"Loading Whisper {model_name} on {device} ({compute_type(device)})")
    try:
        model = get_whisper_model(model_name, device)
    except RuntimeError as e:
        hint = "; try large-v3-turbo or the CPU" if "out of memory" in str(e).lower() else ""
        raise TranscriptionError(f"Failed to load Whisper model: {e}{hint}") from e

    _report("transcribing", 0.0, f"Transcribing [{lang_display}]")
    segments = []
    seg_iter, info = model.transcribe(
        audio.samples(),
        language=language,
        word_timestamps=True,
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=2000),
        hotwords=hotwords or None,
        initial_prompt=select_initial_prompt(language),
    )

    last_progress = -1.0
    last_progress_emit = 0.0
    for s in seg_iter:
        if not s.text.strip():
            continue
        segment = {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
        words = []
        for word in getattr(s, "words", None) or []:
            word_text = str(getattr(word, "word", ""))
            word_start = getattr(word, "start", None)
            word_end = getattr(word, "end", None)
            if word_text.strip() and word_start is not None and word_end is not None:
                words.append({
                    "start": round(float(word_start), 2),
                    "end": round(float(word_end), 2),
                    "word": word_text,
                })
        if words:
            segment["words"] = words
        segments.append(segment)
        duration = getattr(info, "duration", None)
        progress = None
        if duration and duration > 0:
            progress = min(max(float(s.end) / float(duration), 0.0), 0.99)
        now = time.monotonic()
        if (progress is not None and progress - last_progress >= 0.01) or now - last_progress_emit >= 1.0 or last_progress < 0:
            _emit_event("progress", stage="transcribing", progress=progress,
                        segments=len(segments), audio_position_sec=round(float(s.end), 2),
                        message=f"Transcribed through {format_time(s.end)}",
                        preview=s.text.strip())
            last_progress = progress if progress is not None else last_progress
            last_progress_emit = now
        if len(segments) % 20 == 0:
            _print(f"      ... {len(segments)} segments, up to {format_time(s.end)}")

    _report("transcribing", 1.0, f"Transcribed {len(segments)} segments; detected {info.language} "
                                 f"({info.language_probability:.0%})", segments=len(segments))
    return segments, getattr(info, "duration", None)


def identify_language(audio: SharedAudio, device: str) -> tuple[str, float]:
    """Language of the first speech in the recording, from up to three 30 s windows."""
    from faster_whisper.vad import VadOptions

    model = get_language_id_model(device)
    language, probability, _ = model.detect_language(
        audio.samples()[:300 * SAMPLE_RATE], vad_filter=True,
        vad_parameters=VadOptions(min_silence_duration_ms=2000), language_detection_segments=3)
    return language, probability


def run_parakeet(audio: SharedAudio) -> tuple[list[dict], float]:
    """Recognize English speech with Parakeet; return Whisper-shaped segments and the duration."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    _report("loading_whisper", 0.0, f"Loading {PARAKEET_LABEL}")
    try:
        model = get_parakeet_model()
    except Exception as e:
        raise TranscriptionError(f"Failed to load {PARAKEET_LABEL}: {e}") from e

    samples = audio.samples()
    _report("transcribing", 0.0, f"Transcribing [en] with {PARAKEET_LABEL}")
    # Whisper's VAD settings; onnx-asr's own VAD splitting garbled whole passages of a lecture.
    spans = get_speech_timestamps(samples, VadOptions(min_silence_duration_ms=2000,
                                                      max_speech_duration_s=PARAKEET_MAX_SPAN_SEC))
    # Similar lengths batch with little padding; results go back to their spans afterwards.
    order = sorted(range(len(spans)), key=lambda i: spans[i]["end"] - spans[i]["start"])
    results = [None] * len(spans)
    total = sum(span["end"] - span["start"] for span in spans) or 1
    done = 0
    for first in range(0, len(order), PARAKEET_BATCH):
        batch = order[first:first + PARAKEET_BATCH]
        clips = [samples[spans[i]["start"]:spans[i]["end"]] for i in batch]
        for i, result in zip(batch, model.recognize(clips, sample_rate=SAMPLE_RATE), strict=True):
            results[i] = result
        done += sum(len(clip) for clip in clips)
        _emit_event("progress", stage="transcribing", progress=min(done / total, 0.99),
                    message=f"Transcribed {done / total:.0%} of the speech")

    segments = [segment for span, result in zip(spans, results)
                for segment in _sentence_segments(span["start"] / SAMPLE_RATE, span["end"] / SAMPLE_RATE, result)]
    _report("transcribing", 1.0, f"Transcribed {len(segments)} segments with {PARAKEET_LABEL}",
            segments=len(segments))
    return segments, len(samples) / SAMPLE_RATE


def _sentence_segments(start: float, end: float, result) -> list[dict]:
    """Split one recognized span into sentences with word timestamps, the shape Whisper produces."""
    words = []
    for token, offset in zip(result.tokens or [], result.timestamps or []):
        if token.startswith(" ") or not words:
            words.append({"start": start + offset, "last": start + offset, "word": " " + token.lstrip()})
        else:
            words[-1]["word"] += token
            words[-1]["last"] = start + offset
    segments, sentence = [], []
    for n, word in enumerate(words):
        following = words[n + 1]["start"] if n + 1 < len(words) else end
        word["end"] = max(word["start"], min(following, word.pop("last") + _WORD_TAIL_SEC))
        sentence.append(word)
        if word["word"].rstrip().endswith((".", "?", "!")) or n + 1 == len(words):
            text = "".join(w["word"] for w in sentence).strip()
            if text:
                segments.append({
                    "start": round(sentence[0]["start"], 2), "end": round(sentence[-1]["end"], 2), "text": text,
                    "words": [{"start": round(w["start"], 2), "end": round(w["end"], 2), "word": w["word"]}
                              for w in sentence],
                })
            sentence = []
    return segments


# ── Step 3: Speaker diarization (pyannote) ────────────────────────────────────

def run_diarization(audio: SharedAudio, token: str, *, num_speakers: int | None,
                    max_speakers: int | None, device: str, announce: bool = True) -> list[dict]:
    """``announce=False`` logs instead of reporting stages, while recognition owns the job's progress."""
    import torch

    def report(stage, progress, message, **extra):
        if announce:
            _report(stage, progress, message, **extra)
        else:
            _print(f"      [diarization] {message}")

    report("loading_diarization", 0.0, f"Loading {DIARIZATION_PIPELINE_ID}")
    try:
        pipeline, device = get_diarize_pipeline(token, device)
    except Exception as e:
        err = str(e)
        if any(k in err for k in ("401", "403", "gated", "unauthorized", "PermissionError")):
            raise TranscriptionError(
                f"HuggingFace denied access to the diarization model ({err}). Check the token and accept "
                f"the terms at https://hf.co/{DIARIZATION_PIPELINE_ID} and https://hf.co/pyannote/segmentation-3.0"
            ) from e
        raise TranscriptionError(f"Speaker diarization model could not be loaded: {err}") from e

    speakers = ({"num_speakers": num_speakers} if num_speakers
                else {"max_speakers": max_speakers} if max_speakers else {})
    detail = "".join(f", {name}={value}" for name, value in speakers.items())
    report("diarizing", 0.0, f"Running speaker diarization on {device}{detail}")
    waveform = {"waveform": torch.from_numpy(audio.samples())[None], "sample_rate": SAMPLE_RATE}
    heartbeat = _heartbeat("diarizing", "Speaker diarization is still running") if announce else contextlib.nullcontext()
    with heartbeat:
        try:
            diarization = pipeline(waveform, **speakers)
        except RuntimeError as e:
            hint = "; retry on the CPU" if "out of memory" in str(e).lower() else ""
            raise TranscriptionError(f"Speaker diarization failed: {e}{hint}") from e

    turns = []
    for t, _, spk in diarization.itertracks(yield_label=True):
        spk_str = str(spk)
        speaker_label = spk_str if spk_str.startswith("SPEAKER_") else f"SPEAKER_{spk_str}"
        turns.append({"start": round(t.start, 2), "end": round(t.end, 2), "speaker": speaker_label})
    if not turns:
        raise TranscriptionError("Speaker diarization returned no speaker turns")
    report("diarizing", 1.0, f"Identified {len(turns)} speaker turns", turns=len(turns))
    empty_cuda_cache()
    return turns


# ── Main ──────────────────────────────────────────────────────────────────────

def run_server():
    global _current_job_id
    print(SERVER_READY, flush=True)

    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            data = json.loads(line.strip())
            if data.get("command") != "transcribe":
                continue
            _current_job_id = data["job_id"]
            try:
                run_job_from_json(data)
            except Exception as e:
                if not isinstance(e, TranscriptionError):
                    traceback.print_exc(file=sys.stdout)
                release_models()
                _emit_event("failed", stage="worker", message=str(e), error=str(e))
            finally:
                _current_job_id = ""
        except Exception as e:
            print(f"ERROR: Server loop error: {e}", file=sys.stderr, flush=True)


def run_job_from_json(data: dict):
    for field in ("input_path", "output_dir"):
        if not data.get(field):
            raise ValueError(f"Job is missing required field: {field}")
    input_path = Path(data["input_path"]).resolve()
    output_dir = Path(data["output_dir"]).resolve()
    options = JobOptions.parse(data)
    if not input_path.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    fingerprint = source_fingerprint(input_path)
    expected = data.get("source_fingerprint")
    if expected and fingerprint != expected:
        raise SourceChangedError()
    paths = prepare_paths(input_path, output_dir, options.title, fingerprint)
    with audio_cache_lock(paths.wav):
        if source_fingerprint(input_path) != fingerprint:
            raise SourceChangedError()
        _run_job_impl(input_path, paths, options, data.get("token", ""))


def _run_job_impl(input_path: Path, paths: JobPaths, options: JobOptions, token: str):
    device = resolve_device(options.device)
    diarize = options.mode != "transcribe"
    recognizer = choose_recognizer(options.language, options.hotwords, device)
    whisper_key = _whisper_cache_key(options.model, device, options.language, options.hotwords,
                                     word_timestamps=True, recognizer=recognizer)
    diarization_key = _diarization_cache_key(options.max_speakers, options.num_speakers)

    whisper = _read_stage_cache(paths.whisper_json, "whisper", whisper_key)
    if options.mode == "diarize" and whisper is None:
        raise FileNotFoundError(f"No cached Whisper result for {input_path.name}")
    diarization = _read_stage_cache(paths.diarize_json, "diarization", diarization_key) if diarize else None
    if diarize and diarization is None and not token:
        raise TranscriptionError("Speaker diarization requires a HuggingFace token. "
                                 "Save one in the dashboard, or explicitly choose Transcribe only.")
    if whisper is None or (diarize and diarization is None):
        _preload_models(options.model, recognizer, device, token,
                        recognition=whisper is None, diarization=diarize and diarization is None)
        convert_to_wav(input_path, paths.wav)
    audio = SharedAudio(paths.wav)
    parallel = device == "cuda" and whisper is None and diarize and diarization is None

    def recognize():
        chosen = recognizer
        if chosen == "detect":
            language, probability = identify_language(audio, device)
            chosen = "parakeet" if language == "en" and probability >= ENGLISH_CONFIDENCE else "whisper"
            _report("transcribing", 0.0, f"Detected {language} ({probability:.0%}); recognizing with "
                                         f"{PARAKEET_LABEL if chosen == 'parakeet' else 'Whisper'}")
        if chosen == "parakeet":
            segments, duration = run_parakeet(audio)
            label = PARAKEET_LABEL
        else:
            segments, duration = run_whisper(audio, options.language, options.hotwords,
                                             model_name=options.model, device=device)
            label = f"Whisper {options.model}"
        if not segments:
            raise ValueError("No speech detected in audio")
        _write_stage_cache(paths.whisper_json, "whisper", whisper_key, segments,
                           metadata={"total_sec": duration, "model_label": label})
        return {"result": segments, "total_sec": duration, "model_label": label}

    def label_speakers():
        turns = run_diarization(audio, token, num_speakers=options.num_speakers,
                                max_speakers=options.max_speakers, device=device, announce=not parallel)
        _write_stage_cache(paths.diarize_json, "diarization", diarization_key, turns)
        return {"result": turns}

    stages = {}
    if whisper is None:
        stages["whisper"] = recognize
    else:
        _report("transcribing", 1.0, f"Loaded {len(whisper['result'])} Whisper segments from cache")
    if diarize and diarization is None:
        stages["diarization"] = label_speakers
    elif diarize:
        _report("diarizing", 1.0, f"Loaded {len(diarization['result'])} speaker turns from cache")
    results = _run_stages(stages, parallel)
    whisper = results.get("whisper", whisper)
    diarization = results.get("diarization", diarization)
    turns = diarization["result"] if diarization else []

    _report("merging", 0.0, "Merging transcript and speaker labels")
    segments = merge_results(whisper["result"], turns)

    total_sec = whisper.get("total_sec") or 0
    if total_sec <= 0 and paths.wav.is_file():
        total_sec = get_wav_duration(paths.wav)
    if total_sec <= 0 and segments:
        total_sec = segments[-1]["end"]

    _report("writing", 0.0, f"Writing {len(segments)} transcript blocks")
    content = generate_markdown(segments, input_path.name, total_sec,
                                DIARIZATION_PIPELINE_ID if turns else None, options.language,
                                title=options.title,
                                model_label=whisper.get("model_label") or f"Whisper {options.model}")
    write_markdown(paths.output_md, content)
    _report("completed", 1.0, f"✓ Done → {paths.output_md}", event="completed",
            output_path=str(paths.output_md), diarization=bool(turns))


def _preload_models(model_name: str, recognizer: str, device: str, token: str, *,
                    recognition: bool, diarization: bool):
    """Load the models while ffmpeg converts the audio, so the first job after a worker start does not
    wait for each in turn. A failed preload is ignored; the stage loads again and reports the error."""
    loaders = []
    if recognition and recognizer == "detect":
        loaders.append(lambda: get_language_id_model(device))
    if recognition and recognizer in ("detect", "parakeet"):
        loaders.append(get_parakeet_model)
    elif recognition:
        loaders.append(lambda: get_whisper_model(model_name, device))
    if diarization:
        loaders.append(lambda: get_diarize_pipeline(token, device))

    def load():
        for loader in loaders:
            with contextlib.suppress(Exception):
                loader()

    threading.Thread(target=load, name="model-preload", daemon=True).start()


def _out_of_memory(exc: BaseException | None) -> bool:
    while exc is not None:
        message = str(exc).lower()
        # onnxruntime reports a full GPU as a failed allocation.
        if isinstance(exc, MemoryError) or "out of memory" in message or "failed to allocate memory" in message:
            return True
        exc = exc.__cause__
    return False


def _run_stages(stages: dict, parallel: bool) -> dict:
    """Run recognition and diarization, at the same time on the GPU when ``parallel``.

    Both models stay loaded between jobs, so a stage that runs out of memory is retried once alone
    after every model is released. Any other failure is raised once the other stage has finished
    and cached its result.
    """
    names = list(stages)
    results, errors = {}, {}

    def run(name):
        try:
            results[name] = stages[name]()
        except Exception as exc:
            errors[name] = exc

    if parallel and len(names) > 1:
        helper = threading.Thread(target=run, args=(names[1],), name=f"{names[1]}-stage", daemon=True)
        helper.start()
        run(names[0])
        if helper.is_alive() and names[0] not in errors:
            _report("diarizing", 0.0, "Finishing speaker diarization")
        helper.join()
    else:
        for name in names:
            run(name)
            if name in errors and not _out_of_memory(errors[name]):
                break
    for name in [name for name in names if _out_of_memory(errors.get(name))]:
        _print(f"[WARN] {name} ran out of memory ({errors.pop(name)}); retrying it alone")
        release_models()
        run(name)
    for name in names:
        if name in errors:
            raise errors[name]
    return results


def write_markdown(path: Path, content: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.stem}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    try:
        _main()
    except KeyboardInterrupt:
        print("\nInterrupted.", flush=True)
        sys.exit(1)
    except Exception as e:
        print(f"\nFATAL: {e}", flush=True)
        traceback.print_exc(file=sys.stdout)
        sys.exit(1)


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transcribe a video or audio file with speaker labels")
    parser.add_argument("input", nargs="?", help="Path to video/audio file")
    parser.add_argument("--server", action="store_true", help="Run in persistent server mode")
    parser.add_argument("--language", default=None,
                        help="Language code (en/zh/ja/...). Default: auto-detect")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--transcribe-only", action="store_true",
                        help="Run Whisper only, skip diarization (no HF token needed)")
    mode.add_argument("--diarize-only", action="store_true",
                        help="Require matching Whisper cache; reuse matching diarization cache")
    parser.add_argument("--model", default=None,
                        choices=list(config.SUPPORTED_MODELS),
                        help="Whisper model size (large-v3-turbo/large-v3). Overrides config.py")
    parser.add_argument("--device", default=None,
                        help="Compute device (auto/cuda/cpu). Overrides config.py")
    parser.add_argument("--max-speakers", default=None, type=int,
                        help="Maximum number of speakers. Overrides config.py")
    parser.add_argument("--num-speakers", default=None, type=int,
                        help="Exact number of speakers, when known")
    parser.add_argument("--hotwords", default=None,
                        help="Comma-separated names or technical terms to bias Whisper")
    parser.add_argument("--output-dir", default=None,
                        help="Directory for output .md file. Overrides config.py TRANSCRIPT_DIR")
    parser.add_argument("--title", default="", help="Optional transcript title and filename")
    return parser


def _main():
    parser = _build_cli_parser()
    args = parser.parse_args()

    if args.server:
        run_server()
        return

    if not args.input:
        parser.error("the following arguments are required: input (unless running with --server)")

    overrides = {"model": args.model, "device": args.device, "language": args.language,
                 "max_speakers": args.max_speakers, "num_speakers": args.num_speakers,
                 "hotwords": args.hotwords}
    run_job_from_json({
        "input_path": args.input,
        "output_dir": args.output_dir or str(config.TRANSCRIPT_DIR),
        "title": args.title,
        "mode": "transcribe" if args.transcribe_only else "diarize" if args.diarize_only else "full",
        "token": "" if args.transcribe_only else get_hf_token(),
        **{name: value for name, value in overrides.items() if value is not None},
    })


if __name__ == "__main__":
    main()
