---
name: primemover-debug
description: "Use when the user wants to diagnose a failing or stuck PrimeMover experiment, run, job, or bot — e.g. \"why did run 40 fail?\", \"debug job 525\", \"this crawler isn't producing data\", \"trace experiment 30\". Uses the orchestrator MCP read/trace/log tools to find the root cause. Read-only — never mutates."
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
- **Queue/Run**: `DONE_SUCCESS`, `DONE_FAILURE`, `DONE_INITFAILED`, `DONE_JOBSFAILURE`,
  `DONE_PROCESSING_TIMEOUT`, or a `RUNNING_*` (in progress).
- **Job**: `SUCCESS` / `FAILURE` / `WARNING_JOBDATA` / `SKIPPED`. A `SKIPPED` job usually means an
  **earlier** job failed (jobs skip after a failure unless failsave) — so trace back to the first
  non-success job.

## Playbook
Run in order. Summarize each finding in a sentence. **Stop and report once you have a root cause.**

### Step 1 — Scope it
If the user named a run/job/experiment, start there. Otherwise find recent failures:
```
find_failed_executions(time_range="24h")
```
Pick the failing experiment/crawler/queue and note its ids.

### Step 2 — Read the run
```
get_queue(queue_id)
```
Look at `status_code`/`status_message` and the job list. Identify the **first job that is not
SUCCESS** — that's the failure point (later ones are likely SKIPPED).
- `DONE_INITFAILED` → browser/profile/proxy setup failed, not a job. Jump to Step 4 (logs) and look
  at Multilogin/proxy startup lines; check the bot's agent/proxy config with `get_crawler`.
- `DONE_PROCESSING_TIMEOUT` → run exceeded `ProcessingTimeoutMinutes`; look for a hung job (Wait/Scroll).

### Step 3 — Inspect the failing job
```
get_job(job_id)
trace_job(job_id, time_range="7d")
```
Check `status_message`, `captcha_found`/`captcha_solved`, and whether it has the behaviours it needs
(a `visitjob` with no `url`, a `clickjob` with a stale `selector`, etc.). Cross-check the expected
parameters with `get_job_type_schema(<type>)`.

### Step 4 — Logs for the exact correlation id
```
query_logs(time_range="24h", search="j-<job_id>")     # or cr-<id> / q-<id> / exp-<id>
get_error_summary(time_range="24h", component="all")
```
Read the stack/exception around the failure. Common signatures:
- `selector` not found / element not interactable → the page changed or the selector is wrong.
- proxy `connection refused` / auth → proxy block misconfigured (`get_crawler`).
- Multilogin `Can't set core older than X` → agent `multilogin_profile` needs `auto_update_core:true`.
- captcha appeared and wasn't solved → `captcha_found` set, `captcha_solved` false.

### Step 5 — Lambda-selected jobs (if relevant)
For `lambdasingleselectjob` / video-selection experiments:
```
analyze_lambda_decisions(experiment_id)
get_lambda_recent_errors(time_range="24h")
```

### Step 6 — Report
State the root cause, the correlation id, and the concrete fix (which behaviour/config to change).
If the fix is a data change (a behaviour value, a bot's proxy), you may offer to apply it via the
**primemover-compose** skill's `pm_*` write tools (dry-run → confirm) — but only on the local API.

Reference: `primemover-orchestrator/docs/integration-contract.md`, `docs/specs/runs-and-bots.md`.
