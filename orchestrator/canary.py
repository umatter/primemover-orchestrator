"""How much runway the standing treatment canary has left.

The canary is one experiment holding a fixed list of queues, one per day (see
``scripts/treatment-canary.py``). That is deliberate -- ``start_at`` already
schedules work, so no cron is involved -- but it has a consequence worth
guarding: **a fixed list runs out**. When the last session has run the canary
simply goes quiet, and a canary that goes quiet without saying so is worse than
no canary, because silence reads as health.

So ``orchestrator doctor`` warns while there is still time to act. The threshold
is days of runway rather than a count of sessions: what matters is how long you
have to notice, not how many rows remain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pmspec.spec.time_utils import API_TIMEZONE

#: A crawler whose name starts with this is a canary. Set by
#: scripts/treatment-canary.py as ``treatment-canary-<YYYY-MM-DD>``.
CANARY_CRAWLER_PREFIX = "treatment-canary-"

#: Warn below this many days of remaining runway.
#:
#: A week is enough to top up without hurrying and short enough that the warning
#: does not become background noise for a month.
RUNWAY_WARNING_DAYS = 7


@dataclass
class CanaryRunway:
    """What is left of one canary's schedule."""

    experiment_id: int | None = None
    experiment_name: str = ""
    #: Sessions not yet processed, whatever their start time.
    remaining: int = 0
    #: The last scheduled start, or None when nothing is left.
    last_start: datetime | None = None
    #: Whole days from now until ``last_start``. Negative once it has passed.
    days_left: int | None = None
    #: True when no canary experiment could be found at all.
    missing: bool = True
    problems: list[str] = field(default_factory=list)

    @property
    def needs_attention(self) -> bool:
        """Whether a human should do something about this.

        A missing canary is deliberately NOT a problem here: most deployments do
        not run one, and doctor should not nag a laptop about infrastructure that
        only ex63 has.
        """
        if self.missing:
            return False
        if self.remaining == 0:
            return True
        return self.days_left is not None and self.days_left <= RUNWAY_WARNING_DAYS


def parse_start(value: Any) -> datetime | None:
    """One ``start_at`` as an aware datetime, or None.

    The column is a varchar and the API stores wall clock in its own zone, so a
    naive string is interpreted as Europe/Zurich rather than as UTC or as the
    host's zone -- the same rule the spec DSL and the e2e follow.
    """
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip().replace(" ", "T", 1)
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    return parsed.replace(tzinfo=API_TIMEZONE) if parsed.tzinfo is None else parsed


def runway_from_queues(
    queues: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> tuple[int, datetime | None, int | None]:
    """``(remaining, last_start, days_left)`` for one canary's queues.

    ``remaining`` counts every unprocessed session, including any whose start has
    already passed -- those are a different problem (the runner is not picking
    them up) and are not silently discounted here.
    """
    now = now or datetime.now(API_TIMEZONE)

    pending = [q for q in queues if not q.get("processed")]
    starts = [s for s in (parse_start(q.get("start_at")) for q in pending) if s is not None]

    if not starts:
        return len(pending), None, None

    last = max(starts)
    return len(pending), last, (last - now).days


def find_canary(client: Any, *, scan_experiments: int = 40) -> CanaryRunway:
    """Locate the standing canary and measure its runway.

    Matched on the CRAWLER name (``treatment-canary-<date>``), which
    scripts/treatment-canary.py sets, rather than on the experiment name, which
    is typed by hand at upload time and will eventually be typed differently.

    Newest experiments first, so a freshly topped-up canary is found before the
    exhausted one it replaces.

    Never raises: doctor reports on a system that may be half-configured, and an
    unreachable API is already reported by its own check.
    """
    runway = CanaryRunway()

    try:
        experiments = client.list_experiments(per_page=scan_experiments)
    except Exception as exc:  # noqa: BLE001 - doctor reports, it does not crash
        runway.problems.append(f"could not list experiments: {str(exc)[:80]}")
        return runway

    for experiment in sorted(experiments, key=lambda e: e.get("id") or 0, reverse=True):
        experiment_id = experiment.get("id")
        if experiment_id is None:
            continue

        try:
            detail = client.get_experiment(experiment_id)
        except Exception:  # noqa: BLE001, S112 - one unreadable experiment is not fatal
            continue

        for crawler in detail.get("crawlers") or []:
            if not str(crawler.get("name", "")).startswith(CANARY_CRAWLER_PREFIX):
                continue

            crawler_id = crawler.get("id")
            if crawler_id is None:
                continue

            try:
                queues = client.list_queues_for_crawler(crawler_id)
            except Exception as exc:  # noqa: BLE001
                runway.problems.append(f"could not read canary queues: {str(exc)[:80]}")
                queues = []

            remaining, last_start, days_left = runway_from_queues(queues)

            return CanaryRunway(
                experiment_id=experiment_id,
                experiment_name=str(detail.get("name", "")),
                remaining=remaining,
                last_start=last_start,
                days_left=days_left,
                missing=False,
                problems=runway.problems,
            )

    return runway
