"""Results of the expensive stages, kept per source so a retry does not repeat them."""

from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path

from paths import write_atomic
from pipeline.reporting import log


_SCHEMA_VERSION = 2


def _cache_key(kind: str, params: dict) -> str:
    payload = {"kind": kind, "schema_version": _SCHEMA_VERSION, **params}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


class StageCache:
    """One stage's result for one source; ``params`` are its inputs and quality-affecting options."""

    def __init__(self, path: Path, kind: str, **params):
        self.path = path
        self.kind = kind
        self.key = _cache_key(kind, params)

    def read(self) -> dict | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError):
            log(f"[WARN] {self.kind} cache is corrupt — re-running...")
            with contextlib.suppress(OSError):
                self.path.unlink()
            return None
        if (not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION or
                payload.get("kind") != self.kind or payload.get("cache_key") != self.key):
            return None
        # Older versions cached empty results from transiently silent or corrupt audio.
        return payload if isinstance(payload.get("result"), list) and payload["result"] else None

    def write(self, result: list[dict], **metadata) -> dict:
        payload = {"schema_version": _SCHEMA_VERSION, "kind": self.kind, "cache_key": self.key,
                   "result": result, **metadata}
        write_atomic(self.path, json.dumps(payload, ensure_ascii=False))
        return payload
