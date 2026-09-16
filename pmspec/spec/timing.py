"""Seeded timing: wait ranges and session start jitter.

Two things a generator used to have to draw for itself, and therefore outside
the lock's one seed:

``wait: {min: 30s, max: 60s}``
    The parser cannot draw the number -- templates expand before design
    expansion, so the same workflow item becomes a job on every crawler -- so
    it emits a deferred token (``%%pmspec-wait:<min>:<max>%%``, seconds) and
    the converter resolves it per (crawler i, queue j, job k) from
    ``random.Random(f"{seed}:wait:{i}:{j}:{k}")``. A surviving token is a
    strict-contract drop (see ``drops.py``), never a literal shipped to a bot.

``start_at: {from: "+1h", jitter: "6h"}``
    Kept as a dict all the way through conversion and resolved per (crawler i,
    session j) from ``random.Random(f"{seed}:start:{i}:{j}")``, *before*
    ``pmspec.lock.build`` derives the anchor -- so by the time anything reads
    ``start_at`` it is the ordinary string a spec could have written by hand,
    and verify, audit and diff need nothing new.

Both draws are integer seconds, uniform and inclusive of both bounds.

The relative-offset grammar (``+90s``, ``-5m``) is ``time_utils.OFFSET_RE``,
the one ``is_relative_time`` and ``pmspec.lock.anchor`` also read -- it lives
there rather than here because ``pmspec.spec`` does not depend on
``pmspec.lock`` (the dependency runs the other way: a lock is built from a
converted spec).
"""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta
from typing import Any

from .time_utils import OFFSET_RE, UNIT_SECONDS, _to_api_wall_clock, is_relative_time

#: A resolvable wait-range token: both bounds in whole seconds.
WAIT_TOKEN_RE = re.compile(r"%%pmspec-wait:(\d+):(\d+)%%")

#: Anything that *looks like* a wait token, well-formed or not. The strict scan
#: uses this one, for the same reason ``draws.ANY_DRAW_TOKEN_RE`` exists: a
#: token the parser cannot read is *more* certain to reach the bot as a
#: literal, not less.
ANY_WAIT_TOKEN_RE = re.compile(r"%%pmspec-wait:[^%]*%%")

_WAIT_TOKEN_FMT = "%%pmspec-wait:{min_s}:{max_s}%%"

_DURATION_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(s|sec|seconds?|m|min|minutes?|h|hr|hours?|d|days?)?$"
)


def wait_token(min_s: int, max_s: int) -> str:
    """Build a deferred wait-range token."""
    return _WAIT_TOKEN_FMT.format(min_s=int(min_s), max_s=int(max_s))


def parse_duration(value: Any) -> int | None:
    """A duration in whole seconds, or ``None`` when *value* is not one.

    Stricter than ``SpecParser._parse_duration``, which warns and falls back to
    0: a `jitter: soon` has to be refused, not silently turned into no jitter
    at all. Negative durations are refused for the same reason -- `-6h` reads
    as a range and means nothing here.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    match = _DURATION_RE.match(value.strip().lower())
    if match is None:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or "s")[0]
    return int(amount * UNIT_SECONDS[unit])


def _relative_seconds(value: str) -> int:
    """Seconds from the anchor for a relative `from` (`now`, `+1h`, `-5m`).

    A total, never ``None``: the caller has already asked ``is_relative_time``,
    which reads the same :data:`OFFSET_RE`, so anything arriving here either
    matches it or is `now`.
    """
    match = OFFSET_RE.match(value.strip().lower())
    if match is None:  # "now"
        return 0
    sign, amount, unit = match.groups()
    seconds = int(amount) * UNIT_SECONDS[unit]
    return -seconds if sign == "-" else seconds


def _format_seconds(total: int) -> str:
    return f"+{total}s" if total >= 0 else f"-{abs(total)}s"


def resolve_wait_tokens(api_crawlers: list[dict[str, Any]], seed: int | None) -> list[str]:
    """Resolve every wait-range token in `api_crawlers`, in place.

    Job `k` of queue `j` of crawler `i` draws from
    ``random.Random(f"{seed}:wait:{i}:{j}:{k}")``, so the number is a function
    of the lock's seed and the job's position and nothing else.

    Returns the errors found. Without a seed nothing is drawn and every token
    is left exactly as it is: the converter's strict scan then reports it as a
    drop, so a token can never reach a lock.
    """
    errors: list[str] = []
    found = False

    for i, crawler in enumerate(api_crawlers):
        for j, queue in enumerate(crawler.get("queues", [])):
            for k, job in enumerate(queue.get("jobs", [])):
                for behavior in job.get("behaviors", []):
                    value = behavior.get("value")
                    if not isinstance(value, str):
                        continue
                    match = WAIT_TOKEN_RE.fullmatch(value)
                    if match is None:
                        continue
                    found = True
                    if seed is None:
                        continue
                    low, high = int(match.group(1)), int(match.group(2))
                    rng = random.Random(f"{seed}:wait:{i}:{j}:{k}")
                    behavior["value"] = rng.randint(low, high)

    if found and seed is None:
        errors.append("wait range needs a seed: use pmspec compile, or set design.seed")
    return errors


def resolve_start_jitter(crawlers: list[dict[str, Any]], seed: int | None) -> list[str]:
    """Replace every ``start_at: {from, jitter}`` dict with a concrete start.

    Session `j` of crawler `i` draws ``randint(0, jitter)`` from
    ``random.Random(f"{seed}:start:{i}:{j}")``. A relative `from` is written
    back as ``+<total seconds>s`` and an absolute one as the ISO wall clock it
    shifts to, which is exactly what ``derive_anchor`` reads next.

    Returns the errors found, one per distinct mistake rather than one per
    session: a spec whose 500 crawlers share a bad `jitter:` says so once, with
    the count and the first concrete place. **Every** path replaces the dict
    with a string, an unresolvable one with the unjittered `from` (or `now`):
    the errors are what fails the compile, and a caller that reads `start_at`
    before checking them -- `derive_anchor`, `parse_relative_time` -- must never
    meet a dict.
    """
    errors: list[str] = []
    unseeded = False
    # One mistake in one spec is one message, however many sessions carry it:
    # every crawler of a 500-bot spec has the same `start_at:` block, so a bad
    # jitter used to print 500 lines differing only in the index and bury
    # whatever else went wrong. Grouped by message the way
    # `converter._scan_unresolved` groups: `[*]` in place of the indices, the
    # count, and the first concrete place. A group of one keeps its own path,
    # which is strictly more useful than a `[*]`.
    grouped: dict[str, list[str]] = {}

    for i, crawler in enumerate(crawlers):
        for j, queue in enumerate(crawler.get("queues", [])):
            start_at = queue.get("start_at")
            if not isinstance(start_at, dict):
                continue
            where = f"crawlers[{i}].queues[{j}]"
            source, jitter = start_at.get("from"), start_at.get("jitter")
            queue["start_at"] = source if isinstance(source, str) else "now"
            if source is None or jitter is None:
                grouped.setdefault("needs both from and jitter", []).append(where)
                continue
            span = parse_duration(jitter)
            if span is None:
                grouped.setdefault(f"jitter {jitter!r} is not a duration", []).append(where)
                continue
            if seed is None:
                unseeded = True
                continue
            drawn = random.Random(f"{seed}:start:{i}:{j}").randint(0, span)
            resolved = _shift(str(source), drawn)
            if resolved is None:
                grouped.setdefault(f"cannot parse start time {source!r}", []).append(where)
                continue
            queue["start_at"] = resolved

    for what, places in grouped.items():
        if len(places) == 1:
            errors.append(f"{places[0]}.start_at: {what}")
        else:
            errors.append(
                f"crawlers[*].queues[*].start_at: {what} "
                f"({len(places)} occurrence(s); first at {places[0]})"
            )

    # One message for the spec, not one per session: a spec with no seed has
    # every jittered session in the same position, and 500 copies of it buries
    # whatever else went wrong.
    if unseeded:
        errors.append("start jitter needs a seed: use pmspec compile, or set design.seed")
    return errors


def _shift(source: str, seconds: int) -> str | None:
    """`source` moved `seconds` later, in `source`'s own kind of time."""
    if is_relative_time(source):
        return _format_seconds(_relative_seconds(source) + seconds)
    try:
        parsed = datetime.fromisoformat(_to_api_wall_clock(source))
    except ValueError:
        return None
    return (parsed + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S")
