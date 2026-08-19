"""Time parsing utilities for experiment specs."""

import re
from datetime import datetime, timedelta


def parse_relative_time(value: str, base_time: datetime | None = None) -> str:
    """
    Parse relative or absolute time to ISO 8601 string.

    Supports:
        - "now" -> current time
        - "+5m", "+30s", "+1h", "+1d" -> offset from now
        - ISO 8601 strings -> passed through unchanged

    Args:
        value: Time specification string
        base_time: Base time for relative calculations (defaults to now)

    Returns:
        ISO 8601 formatted string with timezone offset.

    Examples:
        >>> parse_relative_time("now")
        "2025-11-30T21:30:00+01:00"
        >>> parse_relative_time("+5m")
        "2025-11-30T21:35:00+01:00"
        >>> parse_relative_time("2025-02-01T10:00:00Z")
        "2025-02-01T10:00:00Z"
    """
    if base_time is None:
        # Use local time with timezone info - the API expects local time
        base_time = datetime.now().astimezone()

    value = value.strip()

    # Handle "now"
    if value.lower() == "now":
        return format_iso8601(base_time)

    # Handle relative offsets: +5m, +30s, +1h, +1d
    match = re.match(r"^([+-])(\d+)([smhd])$", value.lower())
    if match:
        sign, amount, unit = match.groups()
        amount = int(amount)
        if sign == "-":
            amount = -amount

        units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
        delta = timedelta(**{units[unit]: amount})
        return format_iso8601(base_time + delta)

    # Assume it's already an ISO 8601 timestamp - pass through unchanged
    return value


def format_iso8601(dt: datetime) -> str:
    """
    Format datetime as ISO 8601 with timezone offset.

    Args:
        dt: Datetime object to format

    Returns:
        ISO 8601 formatted string with offset (e.g., "2025-11-30T21:30:00+01:00")
    """
    if dt.tzinfo is None:
        # If no timezone, assume local time
        dt = dt.astimezone()

    # Format with explicit timezone offset (not 'Z' which can be misinterpreted)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + format_tz_offset(dt)


def format_tz_offset(dt: datetime) -> str:
    """
    Format timezone offset as +HH:MM or -HH:MM.

    Args:
        dt: Datetime object with timezone info

    Returns:
        Timezone offset string (e.g., "+01:00", "-05:00")
    """
    if dt.tzinfo is None:
        return "+00:00"

    offset = dt.utcoffset()
    if offset is None:
        return "+00:00"

    total_seconds = int(offset.total_seconds())
    sign = "+" if total_seconds >= 0 else "-"
    total_seconds = abs(total_seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60

    return f"{sign}{hours:02d}:{minutes:02d}"


def is_relative_time(value: str) -> bool:
    """
    Check if a time value is a relative time specification.

    Args:
        value: Time specification string

    Returns:
        True if the value is 'now' or a relative offset (+5m, -1h, etc.)
    """
    value = value.strip().lower()
    if value == "now":
        return True
    if re.match(r"^[+-]\d+[smhd]$", value):
        return True
    return False
