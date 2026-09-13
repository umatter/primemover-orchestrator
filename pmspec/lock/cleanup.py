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

from pmspec.spec.time_utils import API_TIMEZONE

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
    in_flight: bool = False


@dataclass
class CleanupResult:
    deleted: list[int]
    skipped_in_flight: list[int]


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

    A naive timestamp (no offset) is treated as unparseable rather than compared against an
    aware ``now`` -- that comparison would raise ``TypeError`` and take the whole cleanup
    down with it, and a value we cannot safely compare is exactly the "cannot tell" case
    ``older_than`` must not treat as "old enough".
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
        except (ValueError, TypeError):
            continue
        if parsed.tzinfo is None:
            continue
        if best is None or parsed > best[1]:
            best = (raw, parsed)
    return best


def _has_in_flight_queue(session: Any, experiment_id: int) -> bool:
    """True if the experiment has at least one unprocessed (in-flight) queue.

    ``per_page=1`` is enough: only presence/absence matters, never the count."""
    rows = session.get(f"experiments/{experiment_id}/queues-unprocessed", params={"per_page": 1})
    return bool(rows)


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

    Every surviving candidate is also checked for in-flight (unprocessed) queues via ``GET
    experiments/{id}/queues-unprocessed`` -- ``delete_experiments`` refuses those unless
    told to ``force``, so callers need to know before they ever try.
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
        # "cannot tell -> keep": an experiment ever appended to in a non-test mode is not a
        # candidate, even though it also carries pmspec.mode == "test" from an earlier
        # upload. Requiring every upload's mode to be "test" is what keeps a listing honest
        # instead of hard-coding "test" and hiding the mix.
        if modes != {"test"}:
            continue

        latest = _latest_uploaded_at(preferences)
        if older_than is not None:
            if latest is None or now - latest[1] < older_than:
                continue

        experiment_id = int(row["id"])
        candidates.append(
            CleanupCandidate(
                experiment_id=experiment_id,
                name=detail.get("name", name),
                uploaded_at=latest[0] if latest else None,
                mode=",".join(sorted(modes)),
            )
        )

    # A second pass, after every candidate is known: keeps call order predictable
    # (listing, then one detail GET per test--named row, then one in-flight check per
    # surviving candidate) instead of interleaving the two kinds of follow-up GET.
    for candidate in candidates:
        candidate.in_flight = _has_in_flight_queue(session, candidate.experiment_id)
    return candidates


def delete_experiments(
    session: Any,
    ids: list[int],
    *,
    candidates: list[CleanupCandidate],
    force: bool = False,
) -> CleanupResult:
    """``DELETE experiments/{id}/destroy`` (the cascading route) for each id, in order.

    An id whose candidate is in-flight (an unprocessed queue exists) is skipped rather than
    deleted, unless ``force=True`` -- deleting an experiment mid-run would pull the rug out
    from under a runner that is still working it. ``candidates`` must include a
    :class:`CleanupCandidate` for every id in ``ids``.

    Stops at the first failure among the ids actually attempted and re-raises as
    :class:`CleanupError`, which carries the ids already deleted -- callers must never retry
    the whole list, which would attempt to re-delete experiments that are already gone.
    """
    in_flight_by_id = {c.experiment_id: c.in_flight for c in candidates}
    deleted: list[int] = []
    skipped_in_flight: list[int] = []
    for experiment_id in ids:
        if in_flight_by_id.get(experiment_id, False) and not force:
            skipped_in_flight.append(experiment_id)
            continue
        try:
            session.delete(f"experiments/{experiment_id}/destroy")
        except Exception as e:
            raise CleanupError(deleted, experiment_id, e) from e
        deleted.append(experiment_id)
    return CleanupResult(deleted=deleted, skipped_in_flight=skipped_in_flight)
