#!/usr/bin/env python3
"""Create a standing treatment canary: one treated bot, one session a day.

WHY THIS EXISTS
---------------
The decision function degrades by omission. A filter it cannot apply is
dropped, the remaining recommendations stay plausible, the HTTP call is 200 and
the job succeeds -- so a bot configured to be treated can produce control-arm
data through a completely green run. `orchestrator e2e --youtube-treatment`
catches that, but only when a human runs it, and the failure it guards is
exactly the kind nobody thinks to check for.

WHY NOT CRON
------------
Because `start_at` already schedules work. A crawler carries many queues, each
with its own start time, so a recurring canary is one uploaded experiment and
the runner picks up each session on its own. Nothing external has to fire.

What is left over is reading the result, and the API answers that directly:

    GET /api/v1/experiments/{id}/health?hours=24
      -> bad_runs, bad_jobs, treatment_lost_jobs, degraded_jobs

so checking the canary is one request, not a test harness.

THE WORKFLOW IS NOT DUPLICATED HERE
-----------------------------------
It is read from orchestrator/e2e/test_experiment_youtube_treatment.json, the
same scenario `--youtube-treatment` uploads. If the treated arm's definition
changes, both move together; a canary that had drifted from the test would be
worse than no canary.

TIMING
------
`start_at` is Europe/Zurich WALL CLOCK. The API (APP_TIMEZONE=Europe/Zurich)
strips the offset and reads the naive datetime as local time, so sessions are
generated as local times rather than by adding 24h to an instant -- which is
the difference between "05:00 every day" and "05:00 until the DST change, then
04:00". The 2026-10-25 boundary falls inside a 28-day schedule.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.config.settings import get_settings  # noqa: E402
from orchestrator.e2e.proxy import substitute_proxy  # noqa: E402

SCENARIO = REPO / "orchestrator" / "e2e" / "test_experiment_youtube_treatment.json"
API_ZONE = ZoneInfo("Europe/Zurich")


def local_start(day: date, at: time) -> str:
    """One `start_at`, as Europe/Zurich wall clock with its true offset.

    The offset is stripped by the API, but a string that disagrees with the
    wall clock it encodes is a trap for the next reader.
    """
    stamp = datetime.combine(day, at, tzinfo=API_ZONE)
    offset = stamp.strftime("%z")
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.00") + offset[:3] + ":" + offset[3:]


def sessions(first: date, days: int, at: time) -> list[str]:
    return [local_start(first + timedelta(days=n), at) for n in range(days)]


def build(days: int, at: time, first: date, settings: Any = None) -> list[dict]:
    """The canary's crawler: the treated scenario, one queue per day.

    ``settings`` is optional so the schedule can be built and inspected without
    proxy credentials; pass them (as main() does) to fill the scenario's
    placeholders. Building is pure, and only the thing that actually uploads
    needs secrets.
    """
    crawlers: list[dict] = json.loads(SCENARIO.read_text())
    crawler = crawlers[0]

    params = crawler["configuration"]["params"]
    if int(params.get("pi", 0)) != 1 or int(params.get("kappa", 0)) != 1:
        raise SystemExit(
            f"{SCENARIO.name} is no longer the treated scenario "
            f"(pi={params.get('pi')}, kappa={params.get('kappa')}). "
            "A canary built from a control crawler would pass for ever and mean nothing."
        )

    template = crawler["queues"][0]
    crawler["queues"] = []

    for index, start_at in enumerate(sessions(first, days, at), start=1):
        queue = copy.deepcopy(template)
        queue["name"] = f"canary session {index:02d}"
        queue["description"] = f"Treated canary, {start_at[:10]}"
        queue["start_at"] = start_at
        crawler["queues"].append(queue)

    # Same substitution the e2e does, from the same untracked settings. Without
    # it the crawler ships PROXY_*_PLACEHOLDER, Multilogin fails the profile's
    # proxy check on every session, and the canary reports a browser that never
    # started rather than anything about treatments.
    if settings is not None:
        substitute_proxy(crawler, settings)

    crawler["name"] = f"treatment-canary-{first:%Y-%m-%d}"
    crawler["description"] = (
        "Standing treated canary (pi=1 mainstream_media, kappa=1 similar). "
        "One session a day; check with GET /experiments/{id}/health?hours=24."
    )
    return crawlers


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=28, help="sessions to schedule (default 28)")
    ap.add_argument("--at", default="05:00", help="Europe/Zurich wall clock (default 05:00)")
    ap.add_argument(
        "--first", default=None, help="first session date, YYYY-MM-DD (default tomorrow)"
    )
    ap.add_argument("--out", default=None, help="write the crawlers JSON here instead of stdout")
    args = ap.parse_args()

    hour, _, minute = args.at.partition(":")
    at = time(int(hour), int(minute or 0))
    first = (
        date.fromisoformat(args.first)
        if args.first
        else (datetime.now(API_ZONE).date() + timedelta(days=1))
    )

    crawlers = build(args.days, at, first, settings=get_settings())
    payload = json.dumps(crawlers, indent=2)

    starts = [q["start_at"] for q in crawlers[0]["queues"]]
    print(
        f"{len(starts)} sessions, {starts[0]} .. {starts[-1]}",
        file=sys.stderr,
    )
    offsets = sorted({s[-6:] for s in starts})
    if len(offsets) > 1:
        print(
            f"  crosses a DST boundary; offsets {offsets} — wall clock stays {args.at}",
            file=sys.stderr,
        )

    if args.out:
        Path(args.out).write_text(payload + "\n")
        print(f"  wrote {args.out}", file=sys.stderr)
    else:
        print(payload)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
