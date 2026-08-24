#!/usr/bin/env python3
"""Churn soak: drive many short bot session lifecycles to expose slow leaks.

The burst tests prove the working set (what N live browsers cost at once). They
do NOT prove the churn regime -- bots stopping and starting for weeks, where a
per-lifecycle leak accumulates. This harness exercises that: it repeatedly
creates a small experiment, lets it run to completion (create MLX profiles ->
launch browsers -> set up recorders -> tear all of it down), and moves on.

Read the RESULT as a trend of TROUGHS, not peaks: sample memory continuously
(scripts/monitor-memory.sh --json), and look at RAM and browser-instance count
at the quiet moment between cycles. A flat trough line means teardown fully
releases and the box is churn-safe. A rising trough is a leak, and
browser_instances > 0 at a trough is an orphaned-process leak specifically.

Uses the static privateproxy (E2E_PROXY_* in .env), not the metered Multilogin
proxy: the leak vectors -- recorder proxy, browser process, MLX profile, JVM
heap/off-heap -- are identical regardless of the upstream exit, so there is no
reason to spend residential GB on a memory test.

Sessions are deliberately SHORT (consent + one visit + a brief wait, no video
watching): the point is many lifecycles per hour, not long ones. Every leak
vector is exercised on each create/teardown regardless of session length.

Usage:
    scripts/churn-soak.py --hours 2 --bots 3
    scripts/churn-soak.py --minutes 30 --bots 4     # a quick shakeout
"""

from __future__ import annotations

import argparse
import copy
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

REPO = Path(__file__).resolve().parent.parent
API = "http://localhost:8090/api/v1"


def _env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (REPO / ".env").read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v.strip().strip('"')
    return out


def _token(env: dict[str, str]) -> str:
    r = httpx.post(
        f"{API}/login",
        json={"email": env["PM_API_EMAIL"], "password": env["PM_API_PASSWORD"]},
        timeout=30,
    )
    r.raise_for_status()
    tok: str = r.json()["access_token"]
    return tok


def _zurich_start(seconds_ahead: int) -> str:
    n = datetime.now(ZoneInfo("Europe/Zurich")) + timedelta(seconds=seconds_ahead)
    off = n.strftime("%z")
    return n.strftime("%Y-%m-%dT%H:%M:%S.00") + off[:3] + ":" + off[3:]


def _scenario(env: dict[str, str], bots: int, cycle: int) -> str:
    tpl = json.loads((REPO / "orchestrator" / "e2e" / "test_experiment_youtube.json").read_text())
    base = tpl[0]
    # short low-traffic session: consent visit, wait, one visit, a brief wait.
    base["queues"][0]["jobs"] = base["queues"][0]["jobs"][:6]
    start = _zurich_start(90)

    crawlers = []
    for n in range(1, bots + 1):
        c = copy.deepcopy(base)
        c["name"] = f"churn c{cycle:03d} b{n}"
        c["description"] = "churn soak lifecycle probe"
        c["queues"][0]["name"] = f"churn c{cycle:03d} q{n}"
        crawlers.append(c)

    s = json.dumps(crawlers)
    s = s.replace("PROXY_HOST_PLACEHOLDER", env["E2E_PROXY_HOST"])
    s = s.replace("PROXY_USER_PLACEHOLDER", env["E2E_PROXY_USERNAME"])
    s = s.replace("PROXY_PASSWORD_PLACEHOLDER", env["E2E_PROXY_PASSWORD"])
    s = s.replace('"port": 5432', f'"port": {env["E2E_PROXY_PORT"]}')
    s = s.replace("%%START_AT%%", start)
    return s


def _run_cycle(env: dict[str, str], bots: int, cycle: int, log) -> None:
    tok = _token(env)
    h = {"Authorization": f"Bearer {tok}"}
    eid = httpx.post(
        f"{API}/experiments",
        headers=h,
        json={"name": f"CHURN cycle {cycle}", "description": "churn soak", "active": 1},
        timeout=30,
    ).json()["data"]["id"]
    httpx.post(
        f"{API}/experiments/{eid}/upload",
        headers={**h, "Accept": "application/json"},
        files={"json": ("s.json", _scenario(env, bots, cycle).encode(), "application/json")},
        timeout=120,
    )
    log(f"cycle {cycle}: uploaded experiment {eid} ({bots} bots)")

    # poll until all queues processed, or a generous per-cycle cap
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        time.sleep(20)
        got = httpx.get(
            f"{API}/experiments/{eid}/queues-processed",
            headers={**h, "Accept": "application/json"},
            timeout=30,
        )
        done = len(got.json().get("data", [])) if got.status_code == 200 else 0
        if done >= bots:
            log(f"cycle {cycle}: all {bots} queues processed")
            return
    log(f"cycle {cycle}: TIMED OUT waiting for completion")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    dur = ap.add_mutually_exclusive_group()
    dur.add_argument("--hours", type=float)
    dur.add_argument("--minutes", type=float)
    ap.add_argument("--bots", type=int, default=3, help="concurrent bots per cycle")
    ap.add_argument("--log", default="/tmp/claude-1001/churn/churn.log")
    args = ap.parse_args()

    seconds = int((args.hours or 0) * 3600 + (args.minutes or 0) * 60) or 7200
    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    logf = open(args.log, "a", buffering=1)

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        logf.write(line + "\n")

    env = _env()
    end = time.monotonic() + seconds
    log(f"churn soak start: {seconds // 60} min, {args.bots} bots/cycle, static proxy")
    cycle = 0
    while time.monotonic() < end:
        cycle += 1
        try:
            _run_cycle(env, args.bots, cycle, log)
        except Exception as e:  # noqa: BLE001 -- a bad cycle must not end the soak
            log(f"cycle {cycle}: ERROR {type(e).__name__}: {e}")
        # brief settle so a trough (fully torn down, nothing running) is sampled
        time.sleep(30)
    log(f"churn soak done after {cycle} cycles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
