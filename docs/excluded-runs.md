# Excluded runs

Runs that must not be analysed, and the reason. A flag in the database says
*that* a run was excluded; this file says *why*, which is the part that matters
when someone revisits the data months later.

Set `active = 0` on the queue when adding an entry here, so the crawler's
`queues()` relation stops returning it.

---

## Queues 843–847 — experiment 42, 2026-08-27

**Excluded.** Five 113-job YouTube runs, crawlers 249–253. Do not analyse them,
and do not salvage the completed prefixes.

Three independent reasons, each on its own sufficient:

**1. Every session was truncated, and unevenly across arms.** All five renderers
died of a V8 heap OOM part-way through (`Reached heap limit`, confirmed in the
Chrome crash dumps). Completion by arm:

| crawler | arm | completed |
|---|---|---|
| 249 | control | 70% |
| 252 | control | 79% |
| 250 | psi=1 kappa=1 tau=1 beta=−1 | 77% |
| 253 | psi=1 kappa=1 tau=1 beta=−1 | 77% |
| 251 | kappa=1 beta=−1 | 61% |

That is an 18-point spread. The treatment is cumulative — a bot that stopped at
61% did not receive the treatment it was assigned — so session length is itself
outcome-relevant and the attrition is differential across arms. This is the
reason the prefixes cannot be salvaged: truncating every arm to the shortest
would discard most of the data and still leave the arms unbalanced on exposure.

**2. The decision function changed mid-sample.** `bot-decision-yt` PR #5 merged
at 15:00:22 and `primemover-lambda` restarted at 15:06:18. Crawlers 249/250/251
ran 06:54–08:19 on the old code; 252/253 ran 15:34–16:35 on the new. The old
code degraded a failed LLM filter to a uniform random pick behind an HTTP 200,
so the two halves of the sample are not comparable on treatment fidelity.

**3. One exit IP across every arm.** Proxy records 247–252 are six distinct rows
in the API that are byte-identical: `103.195.116.28:5432`, same credentials, a
US host (Los Angeles, AS26658, a hosting ASN rather than residential). Control
and both treatment arms egressed from the same address concurrently. That is
both a detection signal and a confound across arms — see the proxies section of
`CLAUDE.md`.

Additional context, not itself disqualifying: every run reported queue
`status_code = success` despite carrying 12–22 failed and 12–22 skipped jobs,
because `TaskOperator` set the joblist result unconditionally. Anything that
filters on queue status alone would have ingested all five as clean.

**Reversal.** The queues were deactivated via `PUT /api/v1/queues/{id}` with
`{"active": 0}`; nothing else about them was modified and no job data was
deleted. The pre-change records are in the session scratchpad as
`queues-843-847-before.json`. Setting `active` back to 1 restores them.

**Before re-running:** land the renderer-OOM fix (`primemover_runner` #67), mint
one sticky proxy session per bot with `scripts/mlx-proxy-gen.py --count N
--verify`, and decide the exit country deliberately — a CH/EU exit turns on a
YouTube consent path that has never handled a real consent bump in production.
