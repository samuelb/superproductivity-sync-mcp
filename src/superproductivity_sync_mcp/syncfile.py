"""The ``sync-data.json`` envelope (``FileBasedSyncData`` v2) and vector clocks."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .codec import PrefixFlags

SYNC_FILE = "sync-data.json"
BACKUP_FILE = "sync-data.json.bak"
FILE_VERSION = 2
MAX_RECENT_OPS = 2000

VectorClock = dict[str, int]


class SyncFormatError(Exception):
    pass


def merge_clocks(a: VectorClock, b: VectorClock) -> VectorClock:
    merged = dict(a)
    for k, v in b.items():
        merged[k] = max(merged.get(k, 0), v)
    return merged


def increment_clock(clock: VectorClock, client_id: str) -> VectorClock:
    if not client_id or len(client_id) < 5:
        raise ValueError("Invalid client id for vector clock increment")
    new = dict(clock)
    new[client_id] = new.get(client_id, 0) + 1
    return new


def sanitize_clock(raw: Any) -> VectorClock:
    if not isinstance(raw, dict):
        return {}
    out: VectorClock = {}
    for k, v in raw.items():
        if isinstance(k, str) and k and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            out[k] = v
    return out


@dataclass
class SyncFile:
    """A downloaded, decoded sync file plus the revision it was read at."""

    raw: str
    flags: PrefixFlags
    data: dict[str, Any]
    rev: str
    strong_etag: str | None
    fetched_at: float = field(default_factory=time.monotonic)

    @property
    def state(self) -> dict[str, Any]:
        return self.data["state"]

    @property
    def recent_ops(self) -> list[dict[str, Any]]:
        return self.data.setdefault("recentOps", [])

    @property
    def vector_clock(self) -> VectorClock:
        return sanitize_clock(self.data.get("vectorClock"))

    @property
    def sync_version(self) -> int:
        return int(self.data.get("syncVersion") or 0)

    @property
    def schema_version(self) -> int:
        return int(self.data.get("schemaVersion") or 1)


def validate_envelope(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SyncFormatError("Sync file body is not a JSON object")
    version = data.get("version")
    if version == 3:
        if data.get("format") == "split":
            raise SyncFormatError(
                "This sync folder uses the split-file ('Surgical sync') format, which is "
                "not supported. Disable 'Surgical sync' in Super Productivity or use a "
                "different sync folder."
            )
        raise SyncFormatError("Unsupported sync file version 3")
    if version != FILE_VERSION:
        raise SyncFormatError(f"Unsupported sync file version {version!r} (expected {FILE_VERSION})")
    state = data.get("state")
    if not isinstance(state, dict) or not any(k in state for k in ("task", "project", "tag")):
        raise SyncFormatError("Sync file has no usable state snapshot")
    if not isinstance(data.get("recentOps", []), list):
        raise SyncFormatError("recentOps is not a list")
    return data
