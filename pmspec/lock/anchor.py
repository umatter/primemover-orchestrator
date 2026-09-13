"""One anchor, many offsets. See the spec §1 "schedule.anchor + start_offset"."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from pmspec.spec.time_utils import (
    API_TIMEZONE,
    _to_api_wall_clock,
    format_iso8601,
    is_relative_time,
)

UPLOAD_ANCHOR = "upload"
_OFFSET_RE = re.compile(r"^([+-])(\d+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class AnchorError(ValueError):
    pass


def parse_offset(text: str) -> timedelta:
    m = _OFFSET_RE.match(text.strip().lower())
    if not m:
        raise ValueError(f"not an offset: {text!r}")
    sign, amount, unit = m.groups()
    seconds = int(amount) * _UNIT_SECONDS[unit]
    return timedelta(seconds=-seconds if sign == "-" else seconds)


def format_offset(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    sign = "-" if total < 0 else "+"
    total = abs(total)
    for unit in ("d", "h", "m"):
        if total and total % _UNIT_SECONDS[unit] == 0:
            return f"{sign}{total // _UNIT_SECONDS[unit]}{unit}"
    return f"{sign}{total}s"


def _parse_absolute(value: str) -> datetime:
    """Naive Europe/Zurich wall clock as a tz-aware datetime."""
    wall = _to_api_wall_clock(value)
    try:
        parsed = datetime.fromisoformat(wall)
    except ValueError as e:
        raise AnchorError(f"cannot parse start time {value!r}") from e
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(API_TIMEZONE).replace(tzinfo=None)
    return parsed.replace(tzinfo=API_TIMEZONE)


def _wall_clock(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def derive_anchor(raw_starts: list[str], override: str | None = None) -> tuple[str, list[str]]:
    relative = [s for s in raw_starts if is_relative_time(s)]
    absolute = [s for s in raw_starts if not is_relative_time(s)]
    if relative and absolute:
        raise AnchorError(
            "mixed relative and absolute session starts; use one kind, or pass --anchor"
        )

    if absolute:
        instants = [_parse_absolute(s) for s in absolute]
        if override == UPLOAD_ANCHOR:
            # Keep the spacing, drop the date: offsets from the earliest, anchored at upload.
            base = min(instants)
            return UPLOAD_ANCHOR, [format_offset(_naive_delta(base, dt)) for dt in instants]
        anchor_dt = _parse_absolute(override) if override else min(instants)
        offsets = [format_offset(_naive_delta(anchor_dt, dt)) for dt in instants]
        return _wall_clock(anchor_dt), offsets

    offsets = []
    for s in relative:
        offsets.append("+0s" if s.strip().lower() == "now" else format_offset(parse_offset(s)))
    if override and override != UPLOAD_ANCHOR:
        return _wall_clock(_parse_absolute(override)), offsets
    return UPLOAD_ANCHOR, offsets


def _naive_delta(a: datetime, b: datetime) -> timedelta:
    # Wall-clock difference: the API stores digits, not instants.
    return b.replace(tzinfo=None) - a.replace(tzinfo=None)


def resolve_anchor(anchor: str, now: datetime | None = None) -> datetime:
    if anchor == UPLOAD_ANCHOR:
        if now is None:
            now = datetime.now(API_TIMEZONE)
        elif now.tzinfo is None:
            raise AnchorError("now must be timezone-aware")
        else:
            now = now.astimezone(API_TIMEZONE)
        return now.replace(microsecond=0)
    return _parse_absolute(anchor)


def resolve_start_at(anchor_dt: datetime, offset: str) -> str:
    naive = anchor_dt.replace(tzinfo=None) + parse_offset(offset)
    return format_iso8601(naive.replace(tzinfo=API_TIMEZONE))
