"""Identifier generation compatible with Super Productivity.

* Entity ids: 21-character nanoid (alphabet ``A-Za-z0-9_-``), like the app.
* Operation ids: UUID v7 (time-ordered), like the app's op log.
* Client ids: ``M_<6 base62>``. ``M`` marks the MCP server; the app decodes
  unknown prefixes as "unknown platform", which is harmless.
  ``is_valid_client_id`` mirrors the app's ``isValidClientIdFormat``
  (generate-client-id.ts), which deliberately also accepts any string of 10+
  characters so legacy ids are never orphaned. ``is_usable_client_id`` is the
  stricter rule for an id *we* put on the wire: the charset ``[A-Za-z0-9_-]``
  that the app itself mints and that SuperSync requires, length >= 5.
"""

from __future__ import annotations

import re
import secrets
import time

MIN_CLIENT_ID_LENGTH = 5  # operation-log.const.ts; incrementVectorClock throws below it
_USABLE_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_NANOID_ALPHABET = "useandom-26T198340PX75pxJACKVERYMINDBUSHWOLF_GQZbfghjklqvwyzrict"
_BASE62 = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def nanoid(size: int = 21) -> str:
    return "".join(secrets.choice(_NANOID_ALPHABET) for _ in range(size))


def uuid7(now_ms: int | None = None) -> str:
    ts = int(time.time() * 1000) if now_ms is None else int(now_ms)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (ts & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    hex_str = f"{value:032x}"
    return f"{hex_str[:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:]}"


def generate_client_id() -> str:
    return "M_" + "".join(secrets.choice(_BASE62) for _ in range(6))


def is_usable_client_id(value: object) -> bool:
    """An id this server may use as its own: the shape the app mints."""
    return isinstance(value, str) and len(value) >= MIN_CLIENT_ID_LENGTH and bool(_USABLE_CLIENT_ID_RE.match(value))


def is_valid_client_id(value: object) -> bool:
    """Port of the app's ``isValidClientIdFormat``: what a reader must accept."""
    if not isinstance(value, str):
        return False
    return len(value) >= 10 or is_usable_client_id(value)
