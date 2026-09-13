"""Time parsing utilities for experiment specs.

Everything here exists to produce one thing correctly: a queue's ``start_at``.

The API does NOT honour the offset it is sent. It keeps the wall-clock digits
and discards the zone, so ``2030-07-15T03:00:00Z`` is stored as ``03:00`` and
runs two hours early in summer, and ``2030-07-15T05:00:00-05:00`` is stored as
``05:00`` and runs seven hours early. Probed against the live API, all six
cases. Nothing errors; the queue simply starts at the wrong time, or lands in
the past and has its jobs marked skipped.

So the rule here is: whatever instant the caller means, emit the API's OWN wall
clock. That used to be ``datetime.now().astimezone()`` -- the *host's* zone --
which is right only because every machine that has run this so far happens to
sit in CET/CEST. From a laptop in London it would schedule an hour early and
from one in Boston six, silently.
"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

#: The zone the API interprets a naive ``start_at`` in.
#:
#: Laravel's APP_TIMEZONE. Named here rather than assumed, because the whole
#: scheme rested on every client happening to share it.
API_TIMEZONE = ZoneInfo("Europe/Zurich")


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
        # The API's zone, not the host's. See the module docstring: the offset
        # is discarded on ingest, so the digits have to be the API's wall clock.
        base_time = datetime.now(API_TIMEZONE)

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

    # An absolute timestamp. Two readings, and they are not the same:
    #
    #   naive ("2026-09-10T05:00:00")  -- wall clock as written. Passed through,
    #       because that is exactly how the API will read it.
    #   offset-bearing ("...Z", "...-05:00") -- an INSTANT. Converted to the
    #       API's wall clock, because storing its digits verbatim would schedule
    #       it at that instant's time in the wrong zone.
    #
    # The second is the one that was silently wrong: an author writing Z meant a
    # moment, and got those digits read as Zurich local.
    return _to_api_wall_clock(value)


def _to_api_wall_clock(value: str) -> str:
    """Normalise an absolute ISO timestamp to the API's wall clock.

    A naive string is returned unchanged -- it already means what the API will
    read. A string carrying an offset is converted, so the instant survives.
    Anything unparseable is passed through untouched: this is a convenience, and
    refusing to schedule because a timestamp has an unusual shape would be worse
    than letting the API's own validation answer.
    """
    text = value.strip()
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text

    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return value

    if parsed.tzinfo is None:
        return value

    return format_iso8601(parsed.astimezone(API_TIMEZONE))


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
