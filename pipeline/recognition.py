"""Turn speech into timestamped segments with Whisper or Parakeet."""

from __future__ import annotations

import time
from typing import NamedTuple

import appdata
import config
from pipeline.audio import SAMPLE_RATE, SharedAudio
from pipeline.errors import TranscriptionError, is_out_of_memory
from pipeline.models import Resident
from pipeline.reporting import Reporter, log
from pipeline.transcript import format_time


_VAD_MIN_SILENCE_MS = 2000
# Auto-detect jobs use Parakeet only when the language ID is at least this sure the speech is English.
_ENGLISH_CONFIDENCE = 0.8
# A small multilingual Whisper decides which recognizer an auto-detect job uses.
_LANGUAGE_ID_MODEL = "base"
_INITIAL_PROMPTS = {
    "zh": "以下是普通话的会议记录，包含完整的标点符号。",
    "en": "Here is a transcript of the meeting with complete punctuation.",
    "ja": "これは会議の書き起こしです。句読点を含めます。",
    "ko": "다음은 회의 녹취록이며 완전한 구두점이 포함되어 있습니다.",
}

# English speech on the GPU goes to Parakeet; on lectures it keeps Whisper's content at 2.5x the speed.
_PARAKEET_LABEL = "Parakeet TDT 0.6B v3"
_PARAKEET_REPO = "istupakov/parakeet-tdt-0.6b-v3-onnx"
_PARAKEET_FILES = ("config.json", "vocab.txt", "nemo128.onnx", "encoder-model.onnx",
                   "encoder-model.onnx.data", "decoder_joint-model.onnx")
# Parakeet attends over the whole clip, so speech is cut into spans of at most this length.
_PARAKEET_MAX_SPAN_SEC = 25
# Batch 16 was slower than 8 on an 8 GB card and nearly filled it.
_PARAKEET_BATCH = 8
# onnx-asr reports when each token starts; a word ends at most this long after its last token.
_WORD_TAIL_SEC = 0.24


def _compute_type(device: str) -> str:
    return "float16" if device == "cuda" else "int8"


def _load_whisper(model_name: str, device: str):
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model
    try:
        # A downloaded model loads without the HuggingFace update check, which costs about 2s per load.
        model_path = download_model(model_name, local_files_only=True)
    except Exception:
        model_path = model_name
    return WhisperModel(model_path, device=device, compute_type=_compute_type(device))


def _load_parakeet():
    """Parakeet through onnx-asr on CUDA, returning token timestamps."""
    import onnx_asr
    import onnxruntime
    from huggingface_hub import snapshot_download

    # A plain folder: onnxruntime rejects external weights reached through HuggingFace cache links.
    folder = appdata.models_dir() / "parakeet-tdt-0.6b-v3"
    if not all((folder / name).is_file() for name in _PARAKEET_FILES):
        snapshot_download(_PARAKEET_REPO, allow_patterns=list(_PARAKEET_FILES), local_dir=folder)
    # Hide per-node placement warnings; onnxruntime deliberately keeps shape ops on the CPU.
    onnxruntime.set_default_logger_severity(3)
    model = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3", folder, providers=["CUDAExecutionProvider"])
    return model.with_timestamps()


_whisper = Resident(_load_whisper)
_language_id = Resident(lambda device: _load_whisper(_LANGUAGE_ID_MODEL, device))
_parakeet = Resident(_load_parakeet)


class Recognized(NamedTuple):
    segments: list[dict]
    total_sec: float | None
    model_label: str


class Recognition:
    """Which recognizer a job uses and everything that follows from that choice."""

    def __init__(self, model_name: str, device: str, language: str | None, hotwords: str | None):
        self.model_name = model_name
        self.device = device
        self.language = language
        self.hotwords = hotwords or ""
        self.engine = self._choose_engine()

    def _choose_engine(self) -> str:
        """"whisper", "parakeet", or "detect" (identify the language from the audio first)."""
        # Parakeet takes no prompt, so a job with names or terms keeps Whisper.
        if self.device != "cuda" or not config.PARAKEET_FOR_ENGLISH or self.hotwords.strip():
            return "whisper"
        if not self.language:
            return "detect"
        return "parakeet" if self.language.lower() == "en" else "whisper"

    @property
    def whisper_label(self) -> str:
        return f"Whisper {self.model_name}"

    def _initial_prompt(self) -> str | None:
        return _INITIAL_PROMPTS.get(self.language.lower()) if self.language else None

    def cache_params(self) -> dict:
        params = dict(model=self.model_name, device=self.device, compute_type=_compute_type(self.device),
                      language=self.language or "auto", hotwords=self.hotwords, word_timestamps=True,
                      initial_prompt=self._initial_prompt() or "")
        if self.engine != "whisper":
            params["recognizer"] = f"{self.engine}:{_PARAKEET_LABEL}:{_LANGUAGE_ID_MODEL}>={_ENGLISH_CONFIDENCE}"
        return params

    def preload(self) -> None:
        """Load what this job is certain to need. A detecting job waits to learn which recognizer it
        wants, to avoid retaining an unused Parakeet beside Whisper and diarization on the GPU."""
        if self.engine == "detect":
            _language_id.get(self.device)
        elif self.engine == "parakeet":
            _parakeet.get()
        else:
            _whisper.get(self.model_name, self.device)

    def run(self, audio: SharedAudio, report: Reporter) -> Recognized:
        engine = self.engine
        if engine == "detect":
            language, probability = self._identify_language(audio)
            engine = "parakeet" if language == "en" and probability >= _ENGLISH_CONFIDENCE else "whisper"
            report.stage("transcribing", 0.0, f"Detected {language} ({probability:.0%}); recognizing with "
                                              f"{_PARAKEET_LABEL if engine == 'parakeet' else 'Whisper'}")
        if engine == "parakeet":
            segments, duration = self._run_parakeet(audio, report)
            label = _PARAKEET_LABEL
        else:
            segments, duration = self._run_whisper(audio, report)
            label = self.whisper_label
        if not segments:
            raise ValueError("No speech detected in audio")
        return Recognized(segments, duration, label)

    def _identify_language(self, audio: SharedAudio) -> tuple[str, float]:
        """Language of the first speech in the recording, from up to three 30 s windows."""
        from faster_whisper.vad import VadOptions

        language, probability, _ = _language_id.get(self.device).detect_language(
            audio.samples()[:300 * SAMPLE_RATE], vad_filter=True,
            vad_parameters=VadOptions(min_silence_duration_ms=_VAD_MIN_SILENCE_MS), language_detection_segments=3)
        return language, probability

    def _run_whisper(self, audio: SharedAudio, report: Reporter) -> tuple[list[dict], float | None]:
        report.stage("loading_whisper", 0.0,
                     f"Loading Whisper {self.model_name} on {self.device} ({_compute_type(self.device)})")
        try:
            model = _whisper.get(self.model_name, self.device)
        except RuntimeError as e:
            hint = "; try large-v3-turbo or the CPU" if is_out_of_memory(e) else ""
            raise TranscriptionError(f"Failed to load Whisper model: {e}{hint}") from e

        report.stage("transcribing", 0.0, f"Transcribing [{self.language or 'auto-detect'}]")
        segments = []
        seg_iter, info = model.transcribe(
            audio.samples(),
            language=self.language,
            word_timestamps=True,
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=_VAD_MIN_SILENCE_MS),
            hotwords=self.hotwords or None,
            initial_prompt=self._initial_prompt(),
        )

        duration = info.duration
        last_progress = -1.0
        last_progress_emit = 0.0
        for s in seg_iter:
            text = s.text.strip()
            if not text:
                continue
            segment = {"start": round(s.start, 2), "end": round(s.end, 2), "text": text}
            words = [{"start": round(float(w.start), 2), "end": round(float(w.end), 2), "word": w.word}
                     for w in s.words or () if w.word.strip()]
            if words:
                segment["words"] = words
            segments.append(segment)
            progress = min(max(float(s.end) / float(duration), 0.0), 0.99) if duration and duration > 0 else None
            now = time.monotonic()
            if (progress is not None and progress - last_progress >= 0.01) or now - last_progress_emit >= 1.0 or last_progress < 0:
                report.progress("transcribing", progress, f"Transcribed through {format_time(s.end)}")
                last_progress = progress if progress is not None else last_progress
                last_progress_emit = now
            if len(segments) % 20 == 0:
                log(f"      ... {len(segments)} segments, up to {format_time(s.end)}")

        report.stage("transcribing", 1.0, f"Transcribed {len(segments)} segments; detected {info.language} "
                                          f"({info.language_probability:.0%})")
        return segments, duration

    def _run_parakeet(self, audio: SharedAudio, report: Reporter) -> tuple[list[dict], float]:
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        report.stage("loading_whisper", 0.0, f"Loading {_PARAKEET_LABEL}")
        try:
            model = _parakeet.get()
        except Exception as e:
            raise TranscriptionError(f"Failed to load {_PARAKEET_LABEL}: {e}") from e

        samples = audio.samples()
        report.stage("transcribing", 0.0, f"Transcribing [en] with {_PARAKEET_LABEL}")
        # Whisper's VAD settings; onnx-asr's own VAD splitting garbled whole passages of a lecture.
        spans = get_speech_timestamps(samples, VadOptions(min_silence_duration_ms=_VAD_MIN_SILENCE_MS,
                                                          max_speech_duration_s=_PARAKEET_MAX_SPAN_SEC))
        order = sorted(range(len(spans)), key=lambda i: spans[i]["end"] - spans[i]["start"])
        results = [None] * len(spans)
        total = sum(span["end"] - span["start"] for span in spans) or 1
        done = 0
        for first in range(0, len(order), _PARAKEET_BATCH):
            batch = order[first:first + _PARAKEET_BATCH]
            clips = [samples[spans[i]["start"]:spans[i]["end"]] for i in batch]
            for i, result in zip(batch, model.recognize(clips, sample_rate=SAMPLE_RATE), strict=True):
                results[i] = result
            done += sum(len(clip) for clip in clips)
            report.progress("transcribing", min(done / total, 0.99), f"Transcribed {done / total:.0%} of the speech")

        segments = [segment for span, result in zip(spans, results)
                    for segment in _sentence_segments(span["start"] / SAMPLE_RATE, span["end"] / SAMPLE_RATE, result)]
        report.stage("transcribing", 1.0, f"Transcribed {len(segments)} segments with {_PARAKEET_LABEL}")
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
