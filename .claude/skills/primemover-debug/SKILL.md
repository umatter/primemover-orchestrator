---
name: primemover-debug
description: "Use when the user wants to diagnose a failing, stuck or silently degraded PrimeMover experiment, run, job, or bot — e.g. \"why did run 40 fail?\", \"debug job 525\", \"this crawler isn't producing data\", \"did the treatment arm actually run?\", \"trace experiment 30\". Uses the orchestrator MCP read/trace/log tools to find the root cause. Read-only — never mutates."
---

# /primemover-debug

Diagnose a failing PrimeMover execution using the **orchestrator MCP** read, trace, and log tools.
Strictly read-only. Work top-down: Experiment → Bot → Run → Job, stopping as soon as you have a
root cause.

## Prerequisites
- The `primemover` MCP server is connected (need `get_queue`, `get_job`, `trace_job`, `query_logs`,
  `find_failed_executions`). If absent, tell the user to start it and stop.
- For log-based steps, the monitoring stack (Loki) should be up (`orchestrator status` → Monitoring
  RUNNING). Log tools still fall back to file parsing if Loki is down.

## Correlation IDs
Everything is tied together by: `exp-{experiment_id}|cr-{crawler_id}|q-{queue_id}|j-{job_id}`.
Carry these down as you narrow the search.

## States to recognize

**Read `status_code`. These are the only values on the wire** — full contract in
`docs/integration-contract.md` §3.4, executable copy in `orchestrator/status.py`:

- **Job**: `running` · `success` · `warning` · `failure` · `skipped`
- **Run/Queue**: `running` · `success` · `failure` · `skipped` (no `warning`)

Two traps:

- **Do not look for `DONE_SUCCESS`, `DONE_INITFAILED`, `WARNING_JOBDATA`, `RUNNING_*` and friends.**
  Those are Java **enum constant names** in the runner's `JobState` / `JoblistState`. What is stored
  and returned is the enum's *code*, from the short list above. You will see the constant names in
  runner source and nowhere else. (An earlier version of this skill asserted the constant names as
  the vocabulary — every state check written from it silently matched nothing.)
- **Legacy values still in the database**: `failed` == `failure`, `skpped` (runner typo) == `skipped`.
  Filtering on `failure` alone under-reports: on ex63 the `jobs` table holds 256 `failed` rows
  alongside 170 `failure` rows.

**`warning` is new (2026-08-27) and is the whole point of half this skill.** The job ran to
completion, but something the experiment cares about degraded — job data was not written, or the
decision function reported a **degraded** decision (`degraded: true`, §7.3). These used to be
reported as `success`. A control crawler whose video was picked at random is **not** this: nothing
was requested, nothing failed, and it finishes `success`.
Treat `warning` like a failure for investigation purposes: `failure OR warning` is the "a human
should look at this" predicate.

`status_message` now carries a real diagnostic (`"<state text> - <detail>"`), not just one of the
seven fixed enum strings it was limited to before. Read it first — it may be the whole answer.

A `skipped` job usually means an **earlier** job failed (jobs skip after a failure unless failsave)
— so trace back to the first non-success job.

## Playbook
Run in order. Summarize each finding in a sentence. **Stop and report once you have a root cause.**

### Step 1 — Scope it
If the user named a run/job/experiment, start there. Otherwise:
```
find_failed_executions(time_range="24h")
```
This now asks the **API for recorded outcomes** — every recent job/queue whose `status_code` is
`failure` or `warning` — and returns them under `executions`, worst-first, with a `by_status`
count. The ERROR log lines come back separately under `log_signal` and are explicitly a
**secondary** signal.

Read them in that order, and do not substitute one for the other:
- a run can fail without ever logging an ERROR (`executions` sees it, logs do not);
- a degraded `warning` job normally logs nothing at error level at all;
- `log_signal` counts ERROR only — WARN needs `query_logs(level="warning")`.

If `executions.error` is set, the API query failed. That is **not** a clean run — say so rather
than reporting the log signal as if it were the answer.

### Step 2 — Read the run
```
get_queue(queue_id)
```
Look at `status_code`/`status_message` and the job list. Identify the **first job that is not
`success`** — that is the failure point (later ones are likely `skipped`).
- `failure` + `status_message` mentioning initialisation → browser/profile/proxy setup failed, not
  a job. Jump to Step 4 (logs), look at Multilogin/proxy startup lines, check the bot's
  agent/proxy config with `get_crawler`.
- `failure` + a timeout message → the run exceeded `ProcessingTimeoutMinutes`; look for a hung job
  (Wait/Scroll).
- All jobs `success`/`warning` and the run `success` → the run **worked**. If the user is here
  because the data looks wrong, go to Step 5; that is the degraded-treatment path, not a failure.

### Step 3 — Inspect the failing job
```
get_job(job_id)
trace_job(job_id, time_range="7d")
```
Check `status_message` first, then `captcha_found`/`captcha_solved`, and whether the job has the
behaviours it needs (a `visitjob` with no `url`, a `clickjob` with a stale `selector`). Cross-check
expected parameters with `get_job_type_schema(<type>)`.

### Step 4 — Logs for the exact correlation id
```
query_logs(time_range="24h", job_id=<job_id>)        # or crawler_id= / queue_id= / experiment_id=
get_error_summary(time_range="24h", component="all")
```
Read the stack/exception around the failure. Common signatures:
- `selector` not found / element not interactable → the page changed or the selector is wrong.
- proxy `connection refused` / auth → proxy block misconfigured (`get_crawler`).
- Multilogin `Can't set core older than X` → agent `multilogin_profile` needs `auto_update_core:true`.
- captcha appeared and wasn't solved → `captcha_found` set, `captcha_solved` false.
- `Lambda selected no recommendation (empty xpath)` → the analysed page was not a watch page
  (consent wall, bot check, network-error page). Look at the payload warnings and captured source,
  not at the xpath.

### Step 5 — Degraded decisions: did the treatment arm actually run?
**Run this whenever a video-selection experiment's data looks wrong, even if every job succeeded.**
A degraded decision returns HTTP 200, produces a valid xpath, clicks a real video and finishes the
job green. Nothing fails. The only evidence is in the response payload, which the runner logs in
full:

```
query_logs(component="runner", time_range="24h", search="treatment_lost")
query_logs(component="runner", time_range="24h", search="Full Lambda response payload")
query_logs(component="runner", time_range="24h", search="Aversion to mainstream media")
```

**Ask the three questions in this order** (contract in `docs/integration-contract.md` §7.3):

1. **Is there a degradation report at all?** If the payload has no `degraded` / `treatment_lost` /
   `filters` keys, it came from the decision function currently deployed and the answer is
   **unknown** — say "cannot tell", never "fine" and never "degraded". Every payload on this box
   today is that shape until the lambda service is restarted (§0 deploy order).
2. **Is `treatment_lost` true?** That is the alarm: the decision degraded AND no requested
   treatment applied, so the bot produced control-arm data whatever its assignment says. Report
   those sessions as scientifically void.
3. **Is `degraded` true?** Milder: part of the treatment did not happen; `filters` names which.

| what you see | what it means |
|---|---|
| `"treatment_lost":true` | **The alarm.** A bot configured to be treated produced control-arm data. Name the affected crawlers/sessions and say their data carries no treatment signal. Runner-side twin: an ERROR line `Lambda decision LOST THE TREATMENT for job <id>`. |
| `"degraded":true`, `"treatment_lost":false` | At least one requested filter failed; the rest of the treatment applied. `filters` names the failure. Runner logs it at WARN. |
| `"selection_mode":"random"` **on its own** | **Not an error.** It means no LLM rating informed the pick, which is the correct output for a control crawler (`pi=psi=kappa=tau=0`) — 236 of the 243 bot configurations on this box are exactly that. Read `degraded`/`treatment_lost`; never conclude anything from the mode alone. |
| `"filters":{"political":"skipped"}` | `skipped` = **not requested**. Not a failure. Only `"failed: …"` is. |
| `"warnings":["Aversion to mainstream media channels ignored", …]` | The `pi=1` arm ran **without** the mainstream-media filter — it behaved as a control. Cause is either a corrupted `mainstream_media_channels.csv` or the lambda process running with the wrong cwd so the relative read never resolves. Check both: `cd ../bot-decision-yt && pytest tests/test_mainstream_media_csv.py`, and `readlink /proc/$(systemctl show -p MainPID --value primemover-lambda)/cwd` (must be the `decision/` directory). |
| `"llm":{"available":false,…}` | The LLM was unreachable — `error_type`/`error` say why (key, quota, timeout). Every filter that needed it degraded. |
| `"error": …` | A hard 4xx/5xx from the lambda server; this one *does* fail the job. |

**Do not report a control crawler as broken.** If `treatment_lost` is false and `degraded` is
false, the decision was fine even when `selection_mode` is `"random"` — look elsewhere.

Supporting tools: `get_lambda_recent_errors(time_range="24h")` and
`get_lambda_invocations(time_range="24h")` (HAR/log-derived).
**`analyze_lambda_decisions` is a stub** — it returns "Not yet implemented" and no data. Do not
build a conclusion on it.

If the local Lambda server is suspect, check the right log: under systemd (the VM default)
`journalctl -u primemover-lambda`, not `log/lambda-server.log`, which is stale there. Those lines
are **not in Loki** (promtail scrapes no journal unit), so `query_logs` will not find them — the
runner's echo of the payload is the searchable copy.

### Step 6 — Report
State the root cause, the correlation id, and the concrete fix (which behaviour/config to change).
If the finding is a **degraded arm** rather than a failure, say explicitly which crawlers/sessions
are affected and that their data carries no treatment signal — that is the actionable part, and it
will not be obvious to anyone reading a run that finished green.
If the fix is a data change (a behaviour value, a bot's proxy), you may offer to apply it via the
**primemover-compose** skill's `pm_*` write tools (dry-run → confirm) — but only on the local API.

Reference: `primemover-orchestrator/docs/integration-contract.md` (§3.4 status contract, §7.3
decision-function contract), `orchestrator/status.py`, `docs/specs/runs-and-bots.md`.
