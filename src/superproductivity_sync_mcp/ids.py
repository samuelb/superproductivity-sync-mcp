"""Identifier generation compatible with Super Productivity.

* Entity ids: 21-character nanoid (alphabet ``A-Za-z0-9_-``), like the app.
* Operation ids: UUID v7 (time-ordered), like the app's op log.
* Client ids: ``M_<6 base62>`` — passes the app's ``isValidClientIdFormat``
  (charset ``[a-zA-Z0-9_-]``, length >= 5). ``M`` marks the MCP server; the
  app decodes unknown prefixes as "unknown platform", which is harmless.
"""

from __future__ import annotations

import secrets
import time

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


def is_valid_client_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if len(value) >= 10:
        return True
    return len(value) >= 5 and all(c.isalnum() or c in "_-" for c in value)
