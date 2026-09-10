"""Date helpers mirroring the app's ``getDbDateStr`` / start-of-next-day logic."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Largest ``+N`` accepted; anything further is a typo, and unbounded offsets
# overflow ``timedelta`` / ``date`` with an OverflowError instead of a ValueError.
MAX_DAY_OFFSET = 3650


class InputError(ValueError):
    """A day or time string supplied by the caller is not valid."""


def get_start_of_next_day_diff_ms(global_config: dict[str, Any] | None) -> int:
    """Port of ``getStartOfNextDayDiffMs`` (util/start-of-next-day.util.ts)."""
    misc = (global_config or {}).get("misc") or {}
    time_str = misc.get("startOfNextDayTime")
    hour = misc.get("startOfNextDay")
    if isinstance(time_str, str):
        m = _TIME_RE.match(time_str)
        if not m:
            return 0
        minutes = int(m.group(1)) * 60 + int(m.group(2))
        minutes = max(0, min(23 * 60 + 59, minutes))
        return minutes * 60 * 1000
    if isinstance(hour, (int, float)) and 0 <= hour <= 23:
        return int(hour) * 60 * 60 * 1000
    return 0


def db_date_str(ms: int | float, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ms / 1000, tz).strftime("%Y-%m-%d")


def today_str(now_ms: int, tz: ZoneInfo, start_of_next_day_diff_ms: int = 0) -> str:
    return db_date_str(now_ms - start_of_next_day_diff_ms, tz)


def is_valid_day(s: str) -> bool:
    if not _DAY_RE.match(s):
        return False
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def resolve_day(value: str, today: str) -> str:
    """Accept ``today``, ``tomorrow``, ``+N`` or ``YYYY-MM-DD``."""
    v = value.strip().lower()
    base = date.fromisoformat(today)
    if v == "today":
        return today
    if v == "tomorrow":
        return (base + timedelta(days=1)).isoformat()
    if v.startswith("+") and v[1:].isdigit():
        offset = int(v[1:])
        if offset > MAX_DAY_OFFSET:
            raise InputError(f"Day offset {value!r} is too large; at most +{MAX_DAY_OFFSET} days")
        return (base + timedelta(days=offset)).isoformat()
    if is_valid_day(v):
        return v
    raise InputError(f"Invalid day {value!r}; use YYYY-MM-DD, 'today', 'tomorrow' or '+N'")


def day_time_to_ms(day: str, hhmm: str, tz: ZoneInfo) -> int:
    m = _TIME_RE.match(hhmm.strip())
    if not m:
        raise InputError(f"Invalid time {hhmm!r}; use HH:MM (24h)")
    d = date.fromisoformat(day)
    dt = datetime(d.year, d.month, d.day, int(m.group(1)), int(m.group(2)), tzinfo=tz)
    return int(dt.timestamp() * 1000)


def ms_to_iso(ms: int | float | None, tz: ZoneInfo) -> str | None:
    if ms is None or not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz).isoformat(timespec="minutes")
