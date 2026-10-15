"""The contract between the background service and the transcription worker.

Both sides import this module, so it must stay free of ML and Qt imports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace
from typing import Literal, get_args

import config


EVENT_PREFIX = "@@EVENT "
SERVER_READY = "SERVER_READY"
DEVICES = ("auto", "cuda", "cpu")
Mode = Literal["full", "transcribe", "diarize"]

STAGE_STEPS = {
    "converting": 1, "loading_whisper": 2, "transcribing": 2,
    "loading_diarization": 3, "diarizing": 3, "merging": 4, "writing": 4,
}
STAGE_COUNT = max(STAGE_STEPS.values())
_ERROR_MARKERS = ("error", "traceback", "could not load", "cannot load", "invalid handle", "fatal")

# Pipeline labels stored by older dashboards in place of ``mode``; any other label ran the full pipeline.
_LEGACY_MODES = {"Transcribe only": "transcribe", "Re-diarize only": "diarize"}


def looks_like_error(line: str) -> bool:
    lower = line.lower()
    return any(marker in lower for marker in _ERROR_MARKERS)


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
    max_speakers: int | None = config.MAX_SPEAKERS
    num_speakers: int | None = config.NUM_SPEAKERS
    hotwords: str = config.HOTWORDS
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
    def parse(cls, data: dict, base: JobOptions | None = None) -> JobOptions:
        """Build options from worker JSON, CLI values or a row saved by an older version.

        Missing fields come from ``base``; "auto" means "let the model decide".
        """
        values = {f.name: data[f.name] for f in fields(cls) if f.name in data}
        if "mode" not in values and "pipeline" in data:
            values["mode"] = _LEGACY_MODES.get(data["pipeline"], "full")
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
        return replace(base or cls(), **values)
