"""The contract between the background service and the transcription worker.

Both sides import this module, so it must stay free of ML and Qt imports.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields, replace
from typing import Literal, NamedTuple, get_args

import config


EVENT_PREFIX = "@@EVENT "
SERVER_READY = "SERVER_READY"
EVENT_COMPLETED = "completed"
EVENT_FAILED = "failed"
DEVICES = ("auto", "cuda", "cpu")
Mode = Literal["full", "transcribe", "diarize"]


class Stage(NamedTuple):
    step: int
    start: float
    end: float


STEP_LABELS = ("转换", "识别 · 分离", "合并写出")
STAGES = {
    "queued": Stage(0, 0.0, 0.0),
    "starting": Stage(0, 0.0, 0.02),
    "converting": Stage(0, 0.02, 0.1),
    "loading_whisper": Stage(1, 0.1, 0.12),
    "transcribing": Stage(1, 0.12, 0.85),
    "loading_diarization": Stage(1, 0.85, 0.85),
    "diarizing": Stage(1, 0.85, 0.9),
    "merging": Stage(2, 0.9, 0.95),
    "writing": Stage(2, 0.95, 1.0),
    "completed": Stage(2, 1.0, 1.0),
}


def step_of(stage: str) -> int:
    return STAGES[stage].step if stage in STAGES else 0


def overall_progress(stage: str, progress: float | None) -> float:
    """Map a stage's own 0–1 progress onto one bar for the whole job."""
    fraction = max(0.0, min(1.0, progress or 0.0))
    if stage not in STAGES:
        return fraction
    span = STAGES[stage]
    return span.start + (span.end - span.start) * fraction


def format_event(record: dict) -> str:
    return EVENT_PREFIX + json.dumps(record, ensure_ascii=False)


def parse_event(line: str) -> dict | None:
    if not line.startswith(EVENT_PREFIX):
        return None
    try:
        event = json.loads(line.removeprefix(EVENT_PREFIX))
    except json.JSONDecodeError:
        return {"event": "parse_error", "message": "Malformed worker event", "raw": line}
    if not isinstance(event, dict) or not event.get("event"):
        return {"event": "parse_error", "message": "Worker event is not an object", "raw": line}
    return event


@dataclass(frozen=True)
class JobOptions:
    model: str = config.WHISPER_MODEL
    device: str = config.DEVICE
    language: str | None = config.LANGUAGE
    max_speakers: int | None = None
    num_speakers: int | None = None
    hotwords: str = ""
    mode: Mode = "full"
    title: str = ""

    def __post_init__(self) -> None:
        if self.model not in config.SUPPORTED_MODELS:
            raise ValueError(f"Unsupported model: {self.model} "
                             f"(expected one of: {', '.join(config.SUPPORTED_MODELS)})")
        if self.device not in DEVICES:
            raise ValueError(f"Unsupported device: {self.device} (expected one of: {', '.join(DEVICES)})")
        if self.mode not in get_args(Mode):
            raise ValueError(f"Unsupported mode: {self.mode}")
        for count in (self.max_speakers, self.num_speakers):
            if count is not None and (not isinstance(count, int) or count < 1):
                raise ValueError(f"Speaker count must be a positive integer: {count}")

    @classmethod
    def parse(cls, data: dict) -> JobOptions:
        """Build options from CLI values or a saved row, which may predate some fields.

        Missing fields take their defaults; "auto" means "let the model decide".
        """
        values = {f.name: data[f.name] for f in fields(cls) if f.name in data}
        for name in ("language", "max_speakers", "num_speakers"):
            if name in values and str(values[name]).strip().lower() in {"", "auto", "none"}:
                values[name] = None
        for name in ("max_speakers", "num_speakers"):
            if values.get(name) is not None:
                values[name] = int(values[name])
        if "hotwords" in values:
            values["hotwords"] = values["hotwords"] or ""
        if "title" in values:
            values["title"] = " ".join(str(values["title"] or "").split())
        return replace(cls(), **values)


@dataclass(frozen=True)
class JobRequest:
    """One transcription for the worker: what the CLI and the service both hand to a job."""

    job_id: str
    input_path: str
    output_dir: str
    options: JobOptions
    token: str = ""
    source_fingerprint: str | None = None

    def to_line(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_line(cls, line: str) -> JobRequest:
        data = json.loads(line)
        return cls(**{**data, "options": JobOptions.parse(data["options"])})
