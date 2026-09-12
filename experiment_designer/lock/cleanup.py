"""Find and delete pmspec TEST experiments -- test-only, opt-in cleanup.

This tool deletes research data, so the safety property is the one thing that matters: a
candidate must satisfy BOTH --

* its name starts with ``"test-"`` (this only narrows which experiments get fetched via
  ``GET experiments/{id}``, it never authorises anything on its own), AND
* its preferences carry ``pmspec.mode == "test"`` (this is the actual authorisation).

A misleadingly-named real experiment (``"test-looks-like-one"`` whose ``pmspec.mode`` is
``"real"``) is never a candidate, and neither is a ``"test-"``-named experiment pmspec never
touched (no ``pmspec.mode`` preference at all). The name is a hint that saves a fetch; the
preference is the lock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from experiment_designer.spec.time_utils import API_TIMEZONE

_AGE_RE = re.compile(r"^(\d+)([smhd])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_age(text: str) -> timedelta:
    """Parse an age string such as ``"7d"``, ``"36h"``, ``"90m"`` into a ``timedelta``."""
    match = _AGE_RE.match(text.strip().lower())
    if not match:
        raise ValueError(f"invalid age {text!r}; expected e.g. '7d', '36h', '90m'")
    amount, unit = match.groups()
    return timedelta(**{_UNITS[unit]: int(amount)})


@dataclass
class CleanupCandidate:
    experiment_id: int
    name: str
    uploaded_at: str | None
    mode: str


class CleanupError(RuntimeError):
    """Raised by :func:`delete_experiments` when a DELETE fails partway through the list.

    Carries what was already deleted, so a caller can report real progress instead of an
    all-or-nothing failure that hides the experiments that are, in fact, already gone.
    """

    def __init__(self, deleted: list[int], failed_id: int, cause: Exception) -> None:
        super().__init__(f"failed to delete experiment {failed_id}: {cause}")
        self.deleted = deleted
        self.failed_id = failed_id
        self.cause = cause


def _latest_uploaded_at(preferences: list[dict[str, Any]]) -> tuple[str, datetime] | None:
    """The ``pmspec.uploaded_at`` preference with the latest parseable timestamp, or ``None``.

    An experiment appended to more than once carries one ``pmspec.uploaded_at`` preference
    per upload (each labelled "upload N" in its description), so the age check is against
    whichever upload is most recent -- an experiment last touched yesterday is not stale
    just because it was first created a month ago.
    """
    best: tuple[str, datetime] | None = None
    for pref in preferences:
        if pref.get("name") != "pmspec.uploaded_at":
            continue
        raw = pref.get("value")
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if best is None or parsed > best[1]:
            best = (raw, parsed)
    return best


def find_test_experiments(
    session: Any, *, older_than: timedelta | None = None, now: datetime | None = None
) -> list[CleanupCandidate]:
    """List pmspec test experiments, optionally filtered to ones at least ``older_than`` old.

    Reads ``GET experiments/experiments-list`` for the id/name pairs, then ``GET
    experiments/{id}`` only for names starting with ``"test-"`` -- a non-matching name is
    never fetched. Of those, only experiments whose preferences carry ``pmspec.mode ==
    "test"`` are kept.

    ``older_than`` filters on the latest ``pmspec.uploaded_at``. An experiment whose
    uploaded_at cannot be determined (never uploaded by pmspec, or an unparseable value) is
    kept only when ``older_than`` is ``None`` -- with an age filter given, "we cannot tell
    its age" must never be treated as "old enough to delete".
    """
    now = now or datetime.now(API_TIMEZONE)
    listing = session.get("experiments/experiments-list") or []

    candidates: list[CleanupCandidate] = []
    for row in listing:
        name = row.get("name") or ""
        if not name.startswith("test-"):
            continue

        detail = session.get(f"experiments/{row['id']}")
        preferences = detail.get("preferences") or []
        modes = {p.get("value") for p in preferences if p.get("name") == "pmspec.mode"}
        if "test" not in modes:
            continue

        latest = _latest_uploaded_at(preferences)
        if older_than is not None:
            if latest is None or now - latest[1] < older_than:
                continue

        candidates.append(
            CleanupCandidate(
                experiment_id=int(row["id"]),
                name=detail.get("name", name),
                uploaded_at=latest[0] if latest else None,
                mode="test",
            )
        )
    return candidates


def delete_experiments(session: Any, ids: list[int]) -> list[int]:
    """``DELETE experiments/{id}/destroy`` (the cascading route) for each id, in order.

    Stops at the first failure and re-raises as :class:`CleanupError`, which carries the ids
    already deleted -- callers must never retry the whole list, which would attempt to
    re-delete experiments that are already gone.
    """
    deleted: list[int] = []
    for experiment_id in ids:
        try:
            session.delete(f"experiments/{experiment_id}/destroy")
        except Exception as e:
            raise CleanupError(deleted, experiment_id, e) from e
        deleted.append(experiment_id)
    return deleted
