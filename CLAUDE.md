# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

PrimeMover Orchestrator is a unified monitoring, debugging, and experiment management platform for the PrimeMover ecosystem. It aggregates logs and metrics from three external components:
- **primemover_api** (Laravel) - Experiment management API
- **primemover_runner** (Java/Spring Boot) - Browser automation runner
- **bot-decision-yt** (Python Lambda) - YouTube video selection

This repo contains two Python packages sharing a monorepo:
- **`orchestrator`** - CLI for lifecycle management, log aggregation, tracing, debugging, and MCP server
- **`pmspec`** - the experiment designer: YAML DSL, factorial expansion, locks, upload/verify/audit (package, CLI and env-var prefix all share the name; older docs called it the "Experiment Designer")

Both are installed as CLI entry points: `orchestrator` and `pmspec` (defined in `pyproject.toml`).

## Build & Test Commands

```bash
# Install in development mode (requires Python 3.10+)
pip install -e ".[dev]"

# Run all unit tests (e2e tests are skipped by default)
pytest

# Run a single test file or specific test
pytest orchestrator/tests/test_correlation.py -v
pytest pmspec/tests/test_spec.py::TestSpecParser::test_parse -v

# Run e2e tests (requires live infrastructure)
pytest --e2e

# Coverage
pytest --cov=orchestrator --cov=pmspec

# Lint and format (line length: 100)
black orchestrator/ pmspec/
ruff check orchestrator/ pmspec/ --fix
mypy orchestrator/ pmspec/ --ignore-missing-imports

# All checks at once
make check

# Install git hooks — do this once per clone
make hooks
```

`make hooks` installs **both** stages: `pre-commit` runs black/ruff/mypy, and
`pre-push` runs the full test suite. They are not installed automatically, and
while they were missing, type errors reached CI three times in one day that
either hook would have caught locally. If you are about to push and have not run
`make hooks`, run `make check` first.

`detect-secrets` excludes known-benign placeholder **values** (`password`,
`testpass`, `PROXY_PASSWORD_PLACEHOLDER`, …) rather than excluding the files that
contain them. Every file therefore stays scanned — a real token pasted into
`docs/troubleshooting.md` is still caught, verified by planting one — while the
baseline holds no entries. That last part is the point: the baseline stores line
numbers, so any edit above a match used to rewrite it and block the commit until
it was re-staged, which CLAUDE.md alone triggered on 12 of its last 90 days'
commits. Nothing baselined means nothing to churn.

The black/ruff/mypy hooks are deliberately `repo: local`, running the venv's own
tools with the same arguments and the same scope as `make check`. The pinned
upstream mirrors disagreed with it badly enough to be worse than nothing: the
`mirrors-mypy` pin reported 10 errors on a tree CI called clean, and `psf/black`
pinned at 24.3.0 (against the venv's 26.5.1) reformatted four files that
`make check` immediately wanted to change back. **A hook that contradicts the
canonical check is a hook people learn to bypass with `--no-verify`.**

`scripts/` is covered too, as of 2026-08-21 — Python by black/ruff/mypy, and the
13 shell scripts by **shellcheck at warning level** (`make lint-sh`, part of
`make check`, enforced in CI). shellcheck is not a Python dependency, so the
target uses a local install, else docker, else skips loudly rather than passing
silently.

That shell is load-bearing — it boots the stack, drains HAR data and runs the
boot health check — and had never been linted. The four bare `except:` clauses
removed from `scripts/multilogin-login.py` in the same pass were swallowing
`KeyboardInterrupt`, so Ctrl-C during an agent login did nothing.

## CLI Reference

```bash
# Setup (new developer onboarding)
# For a fresh SERVER (accounts, secrets, symlink, MinIO key, systemd), follow
# docs/new-server-setup.md -- `orchestrator doctor` preflights those pieces.
orchestrator setup                  # Full setup: clone repos, build, start
orchestrator setup --skip-clone     # Skip cloning (repos already exist)

# Lifecycle
orchestrator up                     # Start all components (incl. local Lambda server)
orchestrator up --no-runner         # Start without runner
orchestrator up --no-lambda-local   # Use AWS Lambda instead of the local server
orchestrator down [--volumes]       # Stop all components (optionally remove data)
orchestrator restart [--runner|--lambda-local]

# Status
orchestrator status -v              # System health check
orchestrator doctor                 # Diagnose setup issues
orchestrator config show

# Database
orchestrator db migrate --seed      # Run migrations + seed
orchestrator db reset               # Fresh migration + seed
orchestrator db init                # Full Laravel initialization
orchestrator db clear-test          # Clear test data (keeps users)

# Logs / tracing / debugging
orchestrator logs query -c api -l error -t 1h
orchestrator logs errors --time-range 24h
orchestrator watch -c all -e <experiment_id>
orchestrator trace experiment|crawler|job <id>
orchestrator debug job <id> [-v] [--export report.json] [--no-cloudwatch]

# Lambda
orchestrator lambda invocations -t 1h        # HAR-derived
orchestrator lambda decisions <experiment_id>
orchestrator lambda logs|metrics|errors      # CloudWatch

# Tests
orchestrator test [-v] [--quick] [-k PATTERN]   # e2e without Multilogin
orchestrator e2e [-v] [--timeout 300]           # full browser automation
orchestrator e2e --youtube                      # YouTube workflow, CONTROL arm
orchestrator e2e --youtube-treatment            # TREATED arm; fails if untreated

# Experiments (equivalently: pmspec <cmd>)
orchestrator experiment validate|preview|generate|upload|list
pmspec schema jobs                   # List all job types
pmspec schema job <type>             # Show job type details
```

## Running Docker Compose

**Always run `docker compose` from this repository's root directory.** The compose
file uses relative bind-mount sources (`./infrastructure/loki/loki-config.yaml`,
`./infrastructure/prometheus/prometheus.yml`, etc.). If you launch from anywhere else,
the Docker daemon silently creates those missing source paths as **empty directories**
in your current working directory — scattering broken config/log/data folders outside
the repo. (This already happened once: empty `infrastructure/*` and `storage/*` dirs
appeared at `~/Code/`.)

```bash
cd /path/to/primemover-orchestrator   # NOT the parent folder
docker compose up
```

The sibling-repo and log paths (`PM_API_PATH`, `PM_*_LOG_PATH`, …) are set to
**absolute** paths in `.env` so those mounts survive a wrong CWD, but the in-repo
`./infrastructure/...` config mounts are not env-overridable — so the CWD rule still
applies.

## Architecture

### Two Packages

**`orchestrator/`** handles ops: starting/stopping Docker services, querying logs from Loki/CloudWatch/files, building execution traces from API data, debugging jobs via HAR files, and exposing everything through both CLI (Click) and MCP server (FastMCP).

**`pmspec/`** handles experiment design: parsing YAML specs, expanding factorial designs into crawler configurations, validating against job type schemas, and converting to API-compatible JSON for upload.

### Key Source Locations

- `orchestrator/cli.py` - Click CLI entry point, all subcommands
- `orchestrator/status.py` - **canonical** job/queue status vocabulary + the decision-function degradation helpers; the executable copy of the contract in `docs/integration-contract.md` §3.4/§7.3
- `orchestrator/config/settings.py` - Pydantic Settings, loaded from `.env` with `get_settings()` (lru_cached)
- `orchestrator/components/manager.py` - Orchestrates API (Docker), Runner (native Java), Monitoring (Docker)
- `orchestrator/components/lambda_server.py` - Local Lambda server lifecycle (subprocess)
- `orchestrator/lambda_service/server.py` - Flask wrapper around the decision function
- `orchestrator/clients/` - Service clients: `api.py` (PrimeMover API), `loki.py` (log queries), `cloudwatch.py` (Lambda logs)
- `orchestrator/logs/aggregator.py` - Unified log interface across Loki, files, HAR, CloudWatch
- `orchestrator/logs/parsers/` - Log parsers: `laravel.py` (API), `runner.py` (Java runner)
- `orchestrator/tracing/trace_builder.py` - Reconstructs execution traces as hierarchical `TraceSpan` trees
- `orchestrator/debug/job_debugger.py` - Multi-source job diagnostics
- `orchestrator/mcp_server/server.py` - FastMCP server with all tools (stdio + HTTP transport)
- `pmspec/cli.py` - `pmspec` Click CLI entry point
- `pmspec/spec/converter.py` - YAML-to-JSON conversion pipeline
- `pmspec/spec/design_expander.py` - Factorial design expansion
- `pmspec/spec/job_registry.py` - Job type definitions and validation
- `scripts/har-drain.sh` - job-data archiver (MinIO → Nextcloud); copies, never deletes
- `scripts/mlx-proxy-gen.py` - generate N Multilogin sticky-session proxy records (one exit IP per bot)
- `scripts/monitor-memory.sh` - per-component **PSS** memory + browser-instance count + disk; `--json` for a sampler. Uses PSS (shared pages counted once), not summed RSS, which over-counted the browser total ~5x at 19 concurrent browsers. `system.used_mb`/`available_mb` from `free` are the box-level ground truth.
- `scripts/reap-orphan-browsers.sh` - kill Mimic browsers older than a session could be (orphan sweep)
- `scripts/churn-soak.py` - drive many short session lifecycles to expose slow leaks over churn
- `infrastructure/systemd/` - VM service units (lambda, multilogin, health-check, har-drain)
- `infrastructure/grafana/provisioning/alerting/` - the **only** working alerting: Grafana unified-alerting rules over Loki, plus a README on what is deliberately not wired

### Proxies / exit IPs

A personalization study needs roughly one stable exit IP per bot; a single shared
proxy IP is both a detection signal and an experimental confound across arms. Two
sources:

- **privateproxy** — the `103.195.116.28:5432` record in the `proxies` table.
  One static endpoint (one exit IP), fine for a single bot; not enough for
  parallel personas.
- **Multilogin proxy** — part of the workspace plan (verified 2026-08-24 on the
  `AutomateS` plan). `scripts/mlx-proxy-gen.py --count N --verify` mints N sticky
  residential sessions via `profile-proxy.multilogin.com`, each a plain
  `host:port:user:pass` on `gate.multilogin.com` with its own exit IP and a 24h
  TTL. They drop straight into `proxies` records — no runner or MLX-profile
  change. Verified end to end: 3 bots each egressed from exactly their assigned
  exit (read back from the captured IP-echo page in MinIO). Budget is GB-metered
  (~450 MB per bot-hour measured; a 15-bot 1h run ≈ 6-7 GB); the balance is only
  visible in the Multilogin app, not the API. **Freshly-minted sessions have a
  meaningful dead-on-arrival rate** (~1 in 4 in a small sample), which is why the
  generator's `--verify` resolves each exit and rejects duplicates/failures
  before the credentials are used.

### Parallel-bot capacity (measured 2026-08-24, ex63: 20 cores / 62 GB)

Concurrency is capped by `runner.MaxParallelExecution` (the fixed browser
thread-pool size). Measured limits, in the order they actually bind:

1. **Multilogin `/profile/create` rate limit — the first real ceiling.** The
   workspace allows 50 API ops/min (`workspace/restrictions.ratelimit`). Twenty
   bots launching in the *same second* overran it: one of 20 failed init with a
   `429`, the rest ran fine. So **~15 simultaneous launches is clean; beyond that,
   stagger `start_at` over 60-90s** so profile-creates stay under 50/min. This
   limits launch *rate*, not how many can *run*.
2. **RAM is a distant second.** Real per-bot cost is **~0.85 GB (PSS/`free`),
   not the ~1.8 GB an earlier summed-RSS reading suggested** — 19 concurrent bots
   used ~30 GB of 62, leaving 33 GB free. By memory alone the box holds ~40-50.
   Always judge headroom from `free` / PSS, never from summed RSS.
3. **CPU, `/dev/shm`, task limits: not binding below ~30 bots.** VA-API decode
   keeps each browser mostly idle-waiting; `/dev/shm` ran ~70 MB per browser of
   its 32 GB.

Proven clean at 15 concurrent (twice). 19 ran with only the one rate-limit
casualty. Net: **~15 with instant launch, ~30-40 with staggered launch**, at
which point the binding constraint is exit-IP supply and GB budget, not the box.

### Correlation IDs

All components use a hierarchical correlation ID format for cross-component tracing:
```
exp-{experiment_id}|cr-{crawler_id}|q-{queue_id}|j-{job_id}
```

### Error & warning handling

The status vocabulary is **normative** and lives in `orchestrator/status.py`
(executable) and `docs/integration-contract.md` §3.4 (prose). Read one of them
before writing any status check.

```
job    status_code: running | success | warning | failure | skipped
queue  status_code: running | success | warning | failure | skipped
```

The field is `status_code`. **There is no `status` field on the wire** — code
that reads `status` is reading a fixture. Legacy rows are real and must be
accepted: `failed` == `failure`, `skpped` (a runner typo) == `skipped`. That is
not pedantry — counted 2026-09-07, `jobs` holds **256 `failed` beside 1607
`failure`**, and `queues` holds **277 `failed` beside 26 `failure`**, so
`WHERE status_code = 'failure'` misses 14% of failed jobs and **91% of failed
runs**. An unrecognised value is *unknown*, never success — as are the 4885 jobs
with a NULL `status_code`.

**`warning` is new (2026-08-27): completed, but degraded.** The job ran to the
end and something the experiment cares about did not happen — job data was not
written, or the decision function reported a **degraded** decision. Both were
previously reported as `success`, because the runner's `JobState` enum mapped
`WARNING` and `WARNING_JOBDATA` onto the code `"success"`.
That single mapping is what hid silent treatment-arm degradation for as long as
it existed: the run finished green and the affected bots had quietly produced
control-arm data. The predicate for "a human should look at this" is
`failure OR warning` (`status.is_bad`), never `is_failure` alone.

**`status_message` now carries a real diagnostic** — `"<state text> - <detail>"`,
single-line and truncated. Until this change it was always one of about seven
fixed enum strings; the entire `jobs` table contains only six distinct values,
which is why diagnosing anything always meant leaving the API for the logs.

**The decision function's degradation is invisible from HTTP.** It returns
**200** even when every requested filter failed. `docs/integration-contract.md`
§7.3 has the full response contract; three fields carry it, and each answers
exactly one question:

| field | meaning | gate on it for |
|---|---|---|
| `degraded` | a **requested** filter failed to apply, or nothing could be selected | "a human should look at this decision" |
| `treatment_lost` | `degraded` **and** nothing requested applied — a bot configured to be treated produced control-arm data | "this session's data is void": the alarm, `pm-lambda-treatment-lost`, critical |
| `selection_mode` | *description only* — `"random"` means no LLM rating informed the pick | **nothing. Never alert on it.** |

Plus `filters: {name: "ok" | "skipped" | "failed: …"}` for the six filters, where
`"skipped"` means **not requested** and must never be counted as a failure, and
`llm: {available, error_type, error}`.

**`selection_mode == "random"` is not an error, and an earlier version of this
file said it was.** A control crawler (`pi=psi=kappa=tau=0`) requests no LLM
filter, so `random` is its *correct* output. Measured before shipping: 295 of the
358 lambda payloads in this box's rotated runner logs carry no rating column, and
**236 of the 243 `params` rows in the live database are control-arm** (7 treated).
Gating on the mode alone would have marked ~82% of every `lambdaselect` job
`warning` and pinned a `severity=critical` alert on permanently — the precise
opposite of the point of the change. `treatment_lost` exists so that two-term
gate (`degraded` AND nothing applied) is computed **once, at the source**, and no
consumer can get it wrong.

A payload with **no** degradation report is a legacy payload: absence reads as
*unknown*, never as degraded. It is also the *rare* case, and an earlier version
of this file said otherwise. Decoding all **6682** `lambda-jobdata` objects in
MinIO on 2026-09-08: **5901 carry the full report** (timestamps `20260827-153849`
onward), **361 are the report-less** `{present, procedure, selected, timestamps,
warnings, xpath}` shape (`20260819-160358` .. `20260827-081831`), **419 are
zero-byte objects** with no payload at all, and one holds only the lambda input
(5901 + 361 + 419 + 1 = 6682). The two shapes do not interleave, so the
report has been emitted since **2026-08-27 ~15:38**, a week before the 09-03
lambda restart this file used to credit it to — which matters if you are sizing a
backfill, because those pre-09-03 objects are recoverable, not lost. Every reader
still has to tell "clean" from "cannot tell": use
`status.lambda_report_present()` / `lambda_verdict()`, never a bare boolean.

`already_watched` reported **`not_implemented`** on every successful invocation
for the first year of its life — the filter was an empty TO-DO stub and saying
`ok` would have been a lie. **It reports `ok` as of 2026-09-10**, when the
history loop was closed (`docs/integration-contract.md` §7.3.1). Both states are
healthy and neither may be counted as a failure: `status._FILTER_OK_STATES` says
so, and the API and runner match.

The meaning of `not_implemented` changed with it. It no longer means "the filter
is a stub"; it means **the caller supplied no history** — an un-redeployed
runner, or one whose lookup failed. Nothing was filtered either way, so the
handling is unchanged, but a reader diagnosing one should look at the caller now
rather than at the decision function.

**The history loop, since it spans all three components** (contract §7.3.1):

| step | component |
|---|---|
| `GET /crawlers/{id}/watched` → `{"data": ["<videoId>", …]}` | API |
| reads it, sends it as `already_watched` in the lambda payload | runner |
| filters those ids out, reports `already_watched: "ok"` | decision function |
| reports `selected.id` back as `decision_selected_id` on the status PUT | runner |

Scope is **"selected by this bot, ever"** — every run, every experiment. The
decision function does **not** call the API; it is a pure function over page
source and config and must stay one.

**`null` and `[]` are different answers, and every layer keeps them apart.**
Absent/`null` means the history is *unknown*: nothing is filtered and the filter
reports `not_implemented`, exactly as an un-redeployed runner produces today. An
empty **list** means "this bot has watched nothing" — a real answer, and the
filter counts as applied. Every failure path produces the first, never the
second: the API answers **409** rather than an empty list when the column is
missing, the runner returns null on any error and omits the field, and the
decision function treats a non-list as unknown. An empty list on failure would
report a filter as applied while filtering nothing.

The last step is what makes the scope honest. Before it the ids reached the table
only via `decisions:backfill` out of MinIO, so the filter could not see a choice
the same session had just made. Measured before the loop was closed: **150
bot/video pairs had been watched more than once**.

Note the shape of the real failure: valid xpath, real video clicked, job
finished, run completed. Every other indicator says fine.

**This is all deployed** — an earlier version of this file said it was not, and
that has been wrong since 2026-09-03. Verified 2026-09-07: `primemover-lambda`
restarted 09-03 10:46 with its cwd inside `bot-decision-EN`, the runner jar
rebuilt and restarted 09-03 10:47/10:48, the GUI on
`ghcr.io/umatter/primemover_gui:sha-593a027`, promtail restarted 08-28, Grafana
08-27. The database is the proof it flows: **547 `warning` jobs and 73 `warning`
queues**, states nothing could record before 08-28. The one pairing that records
*false* data rather than merely incomplete data — new decision function + old jar
→ `treatment_lost: true` in the payload and `success` in the database, which is
what every analysis joins on — is what to avoid if a jar is ever rolled back.
`docs/integration-contract.md` **§0** has the verification table and the deploy
order for the work now in flight; read it before restarting anything.

**Persisting the verdict (deployed 2026-09-07/10).**
The verdict used to live only in a log line and a MinIO object, so nothing could
join on it. It is on the job row now, as **nullable** columns —
`decision_degraded`, `decision_treatment_lost`, `decision_selection_mode`,
`decision_report`, and since 2026-09-10 **`decision_selected_id`**, which records
*which video was chosen* — read by `status.job_decision_verdict()` and friends, with
`GET /api/v1/jobs/{id}/decision` serving the raw payload for the 6682 historical
objects. **NULL means unknown, never clean and never false**: a pre-migration
job, a non-lambda job and an un-redeployed runner are indistinguishable, and all
three are "we cannot tell". That single rule is what makes every *version*
pairing of the rollout safe, and the rollout order is **API migration → API code
→ GUI → runner jar, never the runner first** (the verdict rides the same PUT as
`status_code`; ahead of the API it is lost at best and can leave the job stuck at
`running` at worst).

**The API step is two events, not one, and their order is the whole hazard.** The
API source is **bind-mounted** into the running php container
(`/opt/primemover/primemover_api → /var/www/html`), so its code is live the
instant it lands on disk — a checkout *is* the deploy — while `php artisan
migrate` is a separate command somebody has to type. New code against an
un-migrated schema is the one state that is not safe: the rollups ride on
`Crawler::$withCount`, so every crawler and queue read answers `SQLSTATE[42S22]
Unknown column 'jobs.decision_treatment_lost'` → 500, including
`GET /crawlers-unprocessed`, the runner's only work feed. That is not
hypothetical — it took the API's crawler and queue reads down for **107 minutes,
2026-09-07 22:44 to 2026-09-08 00:31**, ended by reverting the checkout rather
than by migrating. The API now
guards with `Job::hasDecisionColumns()` and **omits** the counts when the columns
are missing (absent = unknown; never `0`, which would claim nothing was lost),
but a guard is a net, not a licence: migrate first. `docs/integration-contract.md`
§0 has the full table.

When counting, remember the column has **three** states and SQL drops the third
from any `WHERE`: tally `IS TRUE` / `IS FALSE` / `IS NULL` separately and check
they sum to the total, rather than treating "not lost" as everything minus the
losses.

The same change set adds, on the read side: `latest_queues_warning` and
`latest_queues_treatment_lost` per crawler, `jobs_degraded_count` and
`jobs_treatment_lost_count` per queue, `runs_bad_count` per experiment,
`filter[status_code]=failure,warning` and `filter[bad]=1` (alias-expanded, so
legacy `failed` rows are included), `sortBy=severity` (`status.severity_rank()`,
which ranks **unknown between running and warning** so unreported rows cannot
hide at the clean end), and `GET /api/v1/experiments/{id}/health?hours=24`.
§3.4 and §7.3 of the contract are normative for all of it.

**Alerting** is Grafana unified alerting, provisioned from
`infrastructure/grafana/provisioning/alerting/` over the **Loki** datasource, and
it takes effect only on `docker compose restart grafana`. Rules fire and are
visible in the UI with nothing configured; delivery stays inert until SMTP is set
up. Read that directory's `README.md` before touching it — in particular:
`prometheus/rules/primemover-alerts.yml` is **dead code** that has never been
loaded by anything (`rule_files: []`, never mounted, no Alertmanager, and its
expressions reference metrics no component exports), and an invalid contact point
takes the *whole* alerting provisioning down with it, rules included.

### Log Aggregation

`LogAggregator` unifies four log sources: Loki (primary), direct file parsing (fallback), HAR files (Lambda outputs from runner), and CloudWatch (Lambda execution logs).

### Where job data goes — MinIO, local disk, Nextcloud, AWS

Every job's HAR, final page source, cookies and lambda payload is written as a
zip. There are four possible destinations and only one of them is live; the
confusion is worth stating plainly, because the config keys are misleading.

| destination | state | what it is for |
|---|---|---|
| **MinIO** (`s3.qamelab.org`) | **live — the system of record** | everything the runner captures |
| local disk (`out/har/<jobId>/`) | **off** | historical buffer; only re-enable to debug the writer itself |
| Nextcloud (via `har-drain.sh`) | off-host **archive** of MinIO | disaster recovery, long-term retention |
| AWS S3 | **never used** | only if data must leave the box |

**The API's OWN file storage is a different disk, and a different bucket.** Job
data (above) is the `s3runner` disk, read-only, bucket `primemoverrunner`, served
by `GET /file/{report}`. What the *API itself* creates — experiment export zips
from `GET /experiments/{id}/download`, and `POST /upload` — is the plain `s3`
disk, bucket **`primemover-api`**, with its own scoped read-write key (set up
2026-09-10; runbook in `primemover_api/docs/minio-api-bucket.md`). Neither
credential can touch the other's bucket, which is the point: the API has no
business writing to 39 GB of experimental job data.

`AWS_MOCK` is the trap in that config. Despite the name it is not a test double —
it selects **where the API's own storage lives**: `false` = the `s3` disk,
`true` = the local disk inside the php container. On a box with no AWS account
`true` is a perfectly correct *production* value, and it was the setting for half
a day before the MinIO bucket existed. `S3Service::getTargetDisk()` is its only
reader and it keys on `filesystems.disks.s3.mock`, so `s3runner` is unaffected
either way. `AWS_DEFAULT_REGION` is **required** by both disks even though MinIO
ignores the value — the SDK refuses to construct a client without one.

**The runner's `awsS3.*` settings are not AWS.** `awsS3.endpoint` in
`config/secrets.properties` is `https://s3.qamelab.org`, which resolves to this
box and terminates at the local MinIO (bucket `primemoverrunner`). No AWS
account, key or cost is involved — do not go looking for one, and do not
"fix" an apparently-empty `awsS3.endpoint` in `application.properties`: the
value in `secrets.properties` overrides it.

**Local disk is off by design.** `primemover_runner` `cadb15e` set
`storage.local.enabled=false` ("run S3-only on the VM"), which stops
`Config.java`'s `@ConditionalOnProperty` from ever constructing
`FileStorageService`. So `out/har` is never created. That was deliberate — it
also stops the empty-captcha writer's local failure flipping jobs to
`WARNING_JOBDATA`. Re-enabling it duplicates every object and reopens that bug.

**The layout is flat, not a directory per job.** Two prefixes, job id as the
trailing token:

```
reports/lambda-jobdata-<jobtype>-<task>-<jobId>      the decision payload
reports/static-jobdata-<jobtype>-<task>-<jobId>      the HAR
reports/static-captcha-<jobtype>-<task>-<jobId>
finalsource/<ts>-page-finalsource-<jobtype>-<task>-<jobId>   rendered HTML
```

The API's `reports` table holds one pointer row per object
(`mimetype = primemover/s3`, `filecontent` NULL) — the bytes are in MinIO.

**Reading it.** `LogAggregator` reads MinIO through
`orchestrator/logs/jobdata_s3.py` when `JOBDATA_S3_ACCESS_KEY` /
`JOBDATA_S3_SECRET_KEY` are set, and falls back to `PM_RUNNER_HAR_PATH`
otherwise. Use a **scoped, read-only** MinIO access key, not the root
credentials. Without those settings every HAR-backed reader returns an empty
list — which is exactly the trap that hid this for a month: the writer moved to
MinIO in July and no reader followed, so `orchestrator debug job`,
`lambda invocations` and the HAR log source answered "nothing" instead of
failing. An unreachable store is missing evidence about a job, never evidence
that the job was fine.

In a pinch the objects are readable straight off the MinIO filesystem with no
credentials — small ones are inlined in `xl.meta` as a zip:

```bash
docker exec ex63-minio-1 sh -c 'ls -d /data/primemoverrunner/*/*<jobId>'
```

### Local Lambda Server

To avoid AWS Lambda costs, the orchestrator can run the `bot-decision-yt`
decision function locally as a Flask HTTP service that mirrors the AWS Lambda
invocation contract. This is **on by default** (`orchestrator up`); pass
`--no-lambda-local` to fall back to AWS.

- **Service**: `orchestrator/lambda_service/server.py` — exposes `GET /health`
  and `POST /invoke` on `127.0.0.1:5001`. `/invoke` calls
  `select_recommendation(json.dumps(event))` and returns its dict as JSON.
- **One server, N decision functions, addressed by name.** The runner has always
  named the function it wants — the `lambda_select` behaviour's `function`
  field, which AWS mode passes straight to `LambdaCaller.invokeAws`. Locally
  that name used to be dropped and one directory served, so a box could host
  exactly one study. `invokeLocal` now sends it as **`X-Function-Name`** and the
  server dispatches on it against a registry
  (`orchestrator/lambda_service/registry.py`):
  - **The registry is box-specific and outside git, like `secrets.toml`**:
    `DECISION_FUNCTIONS_FILE` (env or `.env`), default
    `~/.config/primemover/decision-functions.toml`, a `default = "<name>"` plus
    one `[functions.<name>]` table per function with `path` (the directory
    holding `lambda_function.py`) and an optional
    `entry = "lambda_function:select_recommendation"`. Template:
    `infrastructure/decision-functions.example.toml`.
  - **No file is not an error** — it is exactly the old behaviour: the
    `orchestrator/lambda_service/decision` symlink is the one function, named
    `bot-decision-yt`, and `/health` says `"registry": "absent"`. That fallback
    is what lets this repo deploy before the runner jar. A *malformed* file is a
    different thing and never falls back silently: serving some other study's
    function would be invisible in every indicator the system has, so every
    `/invoke` then answers the degraded shape and `/health` goes degraded.
  - **No header → the `default`. An unknown name → HTTP 200** with
    `degraded: true`, `treatment_lost: true` and an `error` naming what *is*
    registered, plus one WARNING carrying the correlation id — never a 5xx,
    which the runner would retry as a transport failure instead of recording a
    decision that did not apply.
  - **Each package is imported under its own module name** (`pm_decision_<name>`)
    with its directory on `sys.path` only for that import, and the modules it
    pulled out of that directory are re-keyed under the same prefix. Two
    packages both called `lambda_function`, both with a same-named helper next
    to them, would otherwise shadow each other — and that failure does not
    raise, it returns a plausible recommendation computed by the wrong study's
    code.
  - **`/health` is per function**: `{status, registry, default, functions:
    {<name>: {path, entry, loaded, error, health}}, open_files, cwd}`, degraded
    if any function fails to import or its own `health()` hook reports a
    problem. `orchestrator doctor` prints one line per function, checks each
    path holds `lambda_function.py`, and adds the `journalctl -u
    primemover-lambda` line when the running service reports one un-importable.
- **Served by waitress, not `app.run`.** `main()` serves the Flask app with
  **waitress** in production and uses werkzeug's `app.run` only under `--debug`.
  This is load-bearing, not a preference: werkzeug's development server leaks an
  epoll fd per connection under concurrency (reproduced with a bare threaded
  Flask app: 0 → 17 epoll over 90 concurrent requests). Under 12 concurrent bots
  that reached **844 leaked fds in one soak round**, crossing the process's fd
  ceiling mid-run, after which every OpenAI call in the decision function failed
  with `APIConnectionError` and the children/entertainment filter silently
  stopped applying to ~40% of selects. waitress holds no per-connection epoll
  (fixed thread pool + one asyncore loop). **Do not revert to `app.run`** — it
  reintroduces the leak, and it only shows under concurrency, so a sequential
  test will not catch it. `waitress>=3.0.0` is a declared dependency.
- **`/health` reports open file descriptors** (`open_files: {open, soft_limit,
  near_limit}`) and flips `status` to `degraded` at ≥80% of the soft limit — so
  an fd leak is a glance, not an hour of forensics. The systemd unit sets
  `LimitNOFILE=65536` (the default 1024 was the ceiling the leak hit); the
  running process must show that limit after any unit change + `daemon-reload`.
- **A fd leak that climbs ~1 per select and only under concurrency is the
  werkzeug one above.** A *socket* leak (per invocation, sequential too) instead
  points at the decision function's OpenAI client — it is created once and
  reused (`_get_shared_openai_client`), so per-call socket growth means that
  reuse was broken. Check `open_files` under a *concurrent* burst, never a
  sequential one.
- **Decision code**: `orchestrator/lambda_service/decision`, a **symlink** to
  `bot-decision-yt/bot-decision-EN`. Since the registry it is the *fallback*
  function — required only on a box with no registry file, which is every dev
  checkout. `LambdaServerComponent.validate()` errors if it (or
  `lambda_function.py` inside it) is missing **when no registry names the
  functions**; with a registry it validates those entries instead. Recreate it
  **relative**, from the
  repo root, so the checkout stays portable across machines and paths:
  `ln -s ../../../bot-decision-yt/bot-decision-EN orchestrator/lambda_service/decision`
  Note this points at `bot-decision-EN` specifically. The sibling
  `bot-decision-EN-arm` and `bot-decision-DE` directories are stale forks that
  still contain the pre-`364beae` xpath bug; never repoint the symlink at them.
- **cwd decides whether the `pi` treatment arm works at all — and it was wrong
  on this VM, verified 2026-08-27.** `lambda_function.py` reads its data files
  by bare relative path (`pandas.read_csv('mainstream_media_channels.csv')`), so
  the process cwd is load-bearing. `LambdaServerComponent` always passed
  `cwd=…/lambda_service/decision` to `Popen`; **`primemover-lambda.service` did
  not** — its `WorkingDirectory` is the repo root, where no such CSV exists. On
  a box where systemd owns the process (the VM default) the read therefore
  raised, the decision function's `except Exception` swallowed it, and the
  `pi=1` mainstream-media arm ran as a **second control arm**: HTTP 200,
  plausible recommendations, no failed job, nothing at error level. Two
  supervisors, two different sciences, from the same code.
  `server.py` closed that hole by chdiring itself rather than trusting its
  launcher, and **the registry closed it properly**: N functions cannot share
  one cwd, so a function now reads its data files relative to its own file
  (`Path(__file__).resolve().parent / …`), which bot-decision-yt does as of its
  own PR. **The server no longer chdirs** — except in the no-registry fallback,
  where it still does it once, so a checkout predating that change keeps
  working through the transition. Consequences for anyone debugging an old
  box: `readlink /proc/$(systemctl show -p MainPID --value
  primemover-lambda)/cwd` ending in `/decision` is now evidence of the fallback,
  not of health, and `/health` reports the cwd for information only — what to
  read instead is `functions.<name>.health`, the project's own hook (for
  bot-decision-yt, the CSV integrity check that used to live in this server).
- **Data files are load-bearing**: `mainstream_media_channels.csv` must keep its
  `channel,verified` header — `lambda_function.py` does
  `drop_duplicates(subset=['channel', 'verified'])`. Never round-trip it through
  a spreadsheet application: doing so has previously renamed the header, added a
  trailing empty column, and replaced the `;` in HTML entities with `,`.

  **It does not crash — that is the problem.** The `read_csv` sits inside a
  `try` whose `except Exception` degrades to `warnings.warn('Aversion to
  mainstream media channels ignored...')`, and the consumer downstream guards on
  `if col in recommendations.columns`. So a corrupted file returns HTTP 200 with
  plausible recommendations, fails no job, and logs nothing at error level: it
  silently turns the mainstream-media-aversion **treatment arm into a second
  control arm**. Control crawlers never reach that branch, so the e2e suite does
  **not** catch it either.

  Guarded since 2026-08-21 by `bot-decision-yt/tests/test_mainstream_media_csv.py`,
  which runs in that repo's CI. Its `check_csv()` / `check_parsed()` helpers are
  deliberately importable so the same assertions can be reused elsewhere — worth
  doing, because the known incident was **never committed**: the damage lived
  only in the working copy on the VM, where CI cannot see it. The natural second
  home is `LambdaServerComponent.validate()`, so `orchestrator up` refuses to
  start on a corrupted copy.

  A second fragility on the same filter, **fixed 2026-09-09** in `bot-decision-yt`
  (#22, #23), and worth knowing because the symptom will recur if anything is
  rolled back: the per-card channel xpath ended in `/text()[1]` — the first
  *direct* text node — and a **collaboration** card ("The Daily Signal and
  Victor Davis Hanson") keeps its channel name in a nested `<span>`, so the
  selector matched nothing and `[0]` raised `IndexError`. That killed the whole
  `mainstream_media` filter for the invocation: job `warning`, nothing failed,
  the arm silently lost. Measured 1 in 3 lambda calls the day it was found; 7
  such cards in 219 across the runs since.
  The selector now ends in `//text()` and takes the first non-empty node, and
  extraction is per card so one odd card can no longer cost the arm. Matching is
  `_mainstream_flags` rather than a `pandas.merge`: exact match first, then the
  **longest** leading segment that is itself a listed channel followed by
  `" and "`. **Never "simplify" that to a split on `" and "`** — the CSV holds
  channels whose own names contain it (`The Globe and Mail`, `Amanpour and
  Company`, …), so splitting turns a mainstream channel into a non-match and
  causes the contamination the arm exists to prevent.
- **Lifecycle**: two different supervisors, and **they log to different places** —
  check which one is actually running before reading a log, or you will be
  reading a file that stopped being written weeks ago.

  | started by | PID | logs to |
  |---|---|---|
  | `orchestrator up` (`LambdaServerComponent`) | `.lambda_server.pid` | `log/lambda-server.log` |
  | `primemover-lambda.service` (the VM default) | systemd | `journalctl -u primemover-lambda` |

  On this VM systemd owns it, so `log/lambda-server.log` is **stale** and its
  last entry long predates the running process. `LambdaServerComponent` already
  tolerates the systemd case for liveness (it never finds a PID file and trusts
  the health endpoint instead). Component startup order is
  `monitoring → lambda_server → api → runner` (the runner depends on it).
- **Runner side**: the runner selects local vs AWS via `lambda.mode` in its
  `application.properties` (`local` → `POST http://localhost:5001/invoke`).
  `local` is also the **code** default since `primemover_runner` PR #53
  (`LambdaCaller`), and an unrecognised value now raises instead of falling
  through. Before that the code
  default was `aws`, so losing the properties line silently sent every decision
  to AWS — jobs kept succeeding while the answers came from a stale ECR image
  predating the camelCase-selector and aria-hidden fixes. If you ever need AWS,
  set it explicitly and check the deployed image is current first.
- The decision function's deps (`flask`, `lxml`, `pandas`, `openai`,
  `langdetect`) are declared in `pyproject.toml` so the orchestrator venv can
  run it directly.

## Code Patterns

### CLI: Lazy Imports

CLI commands use lazy imports inside the function body to keep CLI startup fast:

```python
@cli.command()
@click.option("-t", "--time-range", default="1h")
@click.pass_context
def my_command(ctx: click.Context, time_range: str) -> None:
    """Command description."""
    from orchestrator.some_module import SomeClass  # lazy import
    # ...
```

### Settings: Cached Singleton

`get_settings()` returns an `lru_cache`d `Settings` instance. In tests, the `conftest.py` `reset_settings_cache` fixture (autouse) calls `get_settings.cache_clear()` before/after each test. Use `mock_settings` fixture for test defaults.

### MCP Tools

MCP tools in `orchestrator/mcp_server/server.py` use `@mcp.tool()` decorator and return `json.dumps(...)`. The orchestrator MCP server is the single consolidated server for all PrimeMover operations (the API repo's MCP server is deprecated). The server supports two transport modes: stdio (default, for Claude Code local use) and HTTP (`--http --port 8100`, for remote access via SSH tunnel).

There are **read/observe** tools (unprefixed: `list_experiments`, `list_crawlers`, `list_queues`, `list_library`, `get_*`, `trace_*`, `find_failed_executions`, `query_logs`, `list_job_types`, …) and **write/control** tools (`pm_` prefix: `pm_create_experiment`, `pm_add_bot`, `pm_create_run`, `pm_add_job`, `pm_set_behaviour`, `pm_activate_run`, `pm_delete`, and the composite `pm_compose_run`).

Write-tool conventions (see `docs/specs/mcp-control-surface.md`). These apply to **every tool that can
mutate**, not only the `pm_`-prefixed ones:
- **Dry-run by default** — previews the change; pass `confirm=true` to apply (`pm_` tools) or
  `dry_run=false` (`upload_experiment`, which predates the convention and keeps its own parameter name).
- **Local-only** — `_require_local_write_target()` refuses to mutate anything unless the API is
  `localhost:8090`, even with `confirm`.
- All API calls go through `_api_request` (auth at `/login`, supports GET/POST/PUT/DELETE).
  `upload_experiment` reaches it through `_McpSession`, an `ApiSession`-shaped adapter whose
  `get`/`post`/`put` delegate to `_api_request` and whose `post_file`/`get_bytes` (multipart
  crawler upload, export download) use `httpx` with the same token; the tool calls
  `_require_local_write_target()` before `upload_lock` can mutate anything.

`upload_experiment` takes a lock (from `compile_experiment_spec` or `pmspec compile`) plus a
**required** `mode` (`test` = VM runner, `test-` name prefix; `real` = as in the lock, refuses a
dirty or unknown commit) and is dry-run by default (`dry_run=false` to apply). Before 2026-08-21
it defaulted to applying and never called the guard, so it could write to a remote API.
`test_mcp_write_guard.py` walks the AST for any tool calling a write verb at a non-login endpoint —
including an `upload_lock(...)` call or a `.post_file(...)` on a session — and asserts it is
guarded, so a new tool cannot reintroduce the gap; a name-based check would not have caught this
one, since the tool is not `pm_`-prefixed.

**Claude skills** live in `.claude/skills/`: `primemover` (orient/start-stop), `primemover-compose`
(build experiments/runs via the `pm_` tools), `primemover-debug` (diagnose failing runs/jobs). They
are project-scoped, so anyone running Claude Code in this repo gets them.

Ecosystem specs are in `docs/specs/` (`runs-and-bots.md`, `mcp-control-surface.md`).

### Running the MCP Server

```bash
# Local (stdio mode - default)
./scripts/mcp-server.sh

# Register once so the pm_ write tools are available in Claude Code
claude mcp add --scope user primemover /path/to/primemover-orchestrator/scripts/mcp-server.sh

# Remote (HTTP mode - binds to 127.0.0.1 by default; REQUIRES MCP_AUTH_TOKEN)
MCP_AUTH_TOKEN=$(openssl rand -hex 32) ./scripts/mcp-server.sh --http --port 8100
```

**HTTP mode is bearer-token protected and fails closed.** The server has no
users of its own: whoever reaches the port gets every tool, `pm_` writes
included. So `--http` refuses to start unless `MCP_AUTH_TOKENS` is set (env or
`.env`), every request to `/mcp` needs `Authorization: Bearer <token>` (else
401), and only `GET /health` is open. Network controls say who can *reach* it;
the token says who may *use* it. Both are required — `tailnet-only` on the edge
is not a substitute for the token, and the token is not a reason to bind
publicly.

`GET /health` answers JSON, not plain text: `{"status": "ok", "secrets_store": "ok" |
"missing" | "insecure" | "invalid"}`, reporting the `SecretStore` this process would
resolve `${secret:…}` placeholders from (path from `PMSPEC_SECRETS_FILE` or the default) —
`invalid` covers a store present and mode-600 but unparseable (bad TOML). The HTTP status
stays 200 in every case, since the edge's liveness check only reads the status code; the
path and any value are never included in the body.

Tokens are **named, one per person**: `MCP_AUTH_TOKENS=ulrich:<hex>,matthias:<hex>`.
Every request is logged with the name (`journalctl -u primemover-mcp` →
`mcp request user=matthias …`), and revoking someone is deleting their entry
and `systemctl restart primemover-mcp`; nobody else's token changes. To add a
colleague: they must be on the tailnet first (403 otherwise, before the token
is even read); mint with `openssl rand -hex 32`, append `name:token`, restart
the unit, and hand the token over end-to-end encrypted (Proton↔Proton, Signal,
a password-manager share) — never chat or plain email. A token grants the same
access as yours, `pm_` writes included; there is no read-only role.

**On ex63 it runs as `primemover-mcp.service`** (`infrastructure/systemd/`,
installed but not auto-enabled by `install-systemd-services.sh`) and is reached
at **`https://mcp.qamelab.org/primemover/mcp`** — one host for every qamelab MCP
server, each under its own path prefix. The pieces, because they span two repos
and a firewall:

| piece | where | note |
|---|---|---|
| `MCP_AUTH_TOKENS`, `MCP_HOST=172.19.0.1`, `MCP_PORT=8100` | this repo's `.env` | `172.19.0.1` is the host's address on the `ex63_internal` bridge: reachable from the edge Traefik container and the host, from nothing else |
| Traefik router + `stripPrefix` + service URL | `ex63-infra/config/traefik/dynamic/mcp.yml` | reference copy: `infrastructure/traefik/mcp.qamelab.org.yml`. File provider, not labels — the server is a host process, not a container |
| ufw rule | host | `ufw default deny incoming` also drops bridge→host traffic, so Traefik times out until `sudo ufw allow in on br-<ex63_internal id> to 172.19.0.1 port 8100 proto tcp` (`docker network inspect ex63_internal --format '{{.Id}}' \| cut -c1-12` gives the id) |
| DNS | Cloudflare | `mcp` A record → the tailnet IP, DNS-only |

Client side, from any tailnet machine:

```bash
claude mcp add --scope user --transport http primemover https://mcp.qamelab.org/primemover/mcp \
  --header "Authorization: Bearer <your token from MCP_AUTH_TOKENS in the ex63 .env>"
```

An SSH tunnel to `127.0.0.1:8100` still works for a one-off and needs the same
token. Adding another MCP server to the host is a second router/middleware/
service triple in `mcp.yml` with its own prefix and its own token.

### Composing as a specific user (personal MCP identity)

The shared MCP (`primemover-mcp.service`) authenticates to the API with **one**
identity — `admin@primemover.example` (user 1) from the repo `.env` — regardless of which
person's bearer token calls it. Users 1 and 3 are both **service accounts**
(`PM_SERVICE_ACCOUNT_IDS=1,3`; user 3 is the VM runner identity, not a
researcher login — researchers are users 2/5/6/7). Service accounts bypass
isolation and see everything, and everything the shared MCP composes is
**owned by user 1**, so it is invisible to individual researcher logins.

To have your MCP-composed experiments **owned by, and visible only to, your own
login** — while the runner still executes them — run a *personal* stdio MCP that
authenticates as your user. It must run **on ex63**, because the write-guard
(`_require_local_write_target`) requires the API target to be `localhost:8090`; a
laptop-run MCP would point at the remote API and be refused.

- Launcher (on ex63): `/home/primemover/.config/primemover/mcp-as-identity.sh
  <creds-file>` — sources the repo `.env`, then the creds file **last** so its
  `PM_API_EMAIL`/`PM_API_PASSWORD` override the shared admin identity while
  `PM_API_BASE_URL` (=`localhost:8090`, guard-satisfying) still comes from `.env`.
  Order matters: `mcp-server.sh` sources `.env` internally, so pre-set env vars do
  **not** survive — the override has to come after.
- Creds file: a **mode-600** file (e.g. `~/.config/primemover/mcp-<name>.env`)
  exporting your `PM_API_EMAIL` and `PM_API_PASSWORD`. Same trust boundary as the
  admin creds already in `.env` — anyone who is `primemover`/root on the box can
  read it.
- Register on the laptop (runs the launcher over SSH; no secret leaves ex63):
  ```bash
  claude mcp add --scope user primemover-me -- \
    ssh primemover@<ex63-tailnet-ip> \
    /home/primemover/.config/primemover/mcp-as-identity.sh \
    /home/primemover/.config/primemover/mcp-<name>.env
  ```

Consequences: as a **regular** (non-service) user, your MCP's read tools
(`list_experiments`, `query_logs`, `trace_*`) are **scoped to you** — keep the
shared HTTP MCP or the CLI for cross-user/admin visibility. Your experiments
**do run**: since 2026-09-02 execution is decided by `experiments.runner_user_id`,
not by owner (see `docs/integration-contract.md` "Runner assignment"). Leave it
unset and ex63 runs them; pass `runner_user_id=3` to `pm_create_experiment`, or
upload a lock with `mode='test'` (`upload_experiment` / `pmspec upload --mode test`,
which forces the VM runner and a `test-` name prefix) or with
`experiment.runner_user_id: 3` in the spec it was compiled from, to run them
on the VM instead — handy for test runs while a real study occupies ex63. This
user-scoped MCP is what lets Claude Code in any other repo on the laptop author,
validate, upload and trace experiments; the `primemover*` skills are symlinked
into `~/.claude/skills` for the same reason.

### Adding a New Log Parser

1. Create parser in `orchestrator/logs/parsers/`
2. Return list of `LogEntry` objects
3. Register in `LogAggregator._query_log_files()`

## Uploading Experiments (JSON Scenario)

For anything that is a study rather than a one-off scenario, use a **lock** (`pmspec compile` →
`pmspec upload --mode …` → `pmspec verify`); the raw JSON path below records no provenance.

The API supports uploading pre-built experiment JSON directly (as used by the e2e tests and
scenario files). This is separate from the YAML DSL workflow (`pmspec upload`).

1. Authenticate: `POST /api/v1/login` → get `access_token`
2. Create experiment: `POST /api/v1/experiments` with `{name, description, active: 1}`
3. Upload crawlers JSON: `POST /api/v1/experiments/{id}/upload` as multipart form with `json=@file.json`

```bash
API=http://localhost:8090/api/v1

TOKEN=$(curl -s -X POST $API/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@primemover.example","password":"password"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

EXP_ID=$(curl -s -X POST $API/experiments \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"My Experiment","description":"...","active":1}' | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['id'])")

# multipart form - note -F, not -d
curl -s -X POST "$API/experiments/$EXP_ID/upload" \
  -H "Authorization: Bearer $TOKEN" -H "Accept: application/json" \
  -F "json=@scenario.json;type=application/json"
```

**Scenario JSON format:** array of crawler objects, each with `queues` containing `jobs`.

**Important:** `start_at` must be in the future for the runner to pick up the queue.

It is **Europe/Zurich wall clock**, not CET and not an instant. `APP_TIMEZONE` is
pinned to `Europe/Zurich` (it was unset, defaulting to `Europe/Berlin`; they share
offsets year-round, which is why nothing had ever broken). Since 2026-09-10 the
API **converts an offset-bearing timestamp** to that zone rather than keeping the
digits and discarding the zone — before, `2030-07-15T03:00:00Z` was stored as
`03:00`, two hours early in summer, with no error anywhere.

Generate times from the API's zone, never the host's: `datetime.now().astimezone()`
is right only because every machine here happens to be CET/CEST, and would
schedule an hour early from London. `pmspec.spec.time_utils.API_TIMEZONE`
is the one declaration, shared by the DSL, the e2e and the canary. Reference
implementation: `orchestrator/e2e/runner.py:_upload_experiment()`.

## pmspec — the experiment designer

The experiment designer uses YAML to define experiments with factorial designs: factorial
expansion, reusable templates, parameter distributions, data pools with scoring, and relative
timestamps (`now`, `+5m`). A `with:` value on a `template:`/`repeat:` item can also be a per-bot
pool draw (`{from_pool, field, select: random|first}`), taken without replacement within each
crawler and seeded so the lock stays deterministic (`docs/pmspec.md` § Per-bot
template draws).

The reproducibility artifact is the *lock* (`docs/pmspec.md` § Locks): committed,
hashed, bound to the experiment via `pmspec.*` preferences, checked by `pmspec verify`.
`upload_experiment` takes a lock and a required `mode`. Format 2 (`pmspec_lock: 2`, the default
`compile` writes) factors each distinct queue/agent body once under `parts`, keyed by its own
content hash, instead of repeating it per crawler; `validate` still accepts format 1.

A lock has two **kinds**. A *fleet* lock is the randomization record, uploaded once. A **runs
lock** (`kind: runs`, from a spec carrying `runs_for: <fleet lock>`) appends *queues* to that
fleet's crawlers, matched by name — the way a longitudinal study adds a session per bot-day
without making crawler ≠ bot; it records the base lock's sha256, needs `-e <experiment id>` on
upload (`experiment_id` over MCP), skips a queue already live at the same name and wall-clock
start, and may change a preference only through an opt-in `updates:` block that records old→new.
`verify`/`audit` on one are scoped to the queues it wrote. Orthogonally, **`assign:`** draws one
data-pool row per crawler (`{panelist.person_id}`), without replacement and sharing that
bookkeeping with `proxy: {from_pool: …}`, which is how per-bot data — a panelist, an outlet list —
gets into a spec at all. `docs/pmspec.md` §§ "Longitudinal studies", "Per-crawler assignment".

**Where each step runs.** Design, `validate`, `preview` and `compile` are the CLI *in the research
repo* — that is where the provenance a lock records (`git_commit`, `spec_path`, pool sha256s) is
read from, and where pools resolve; commit the lock there. Upload, verify, status, audit and
cleanup go through the MCP on ex63 (`upload_experiment`, `verify_experiment`, `experiment_status`,
`audit_experiment`, `pm_cleanup_test_experiments`), which passes the lock as text: the write-guard
only allows the local API and the `${secret:…}` store is a mode-600 file on the box, so nothing
secret leaves it. `pmspec secrets init|check` is a one-time shell step on ex63 and deliberately
not an MCP tool. On a machine that runs the stack itself the CLI does both halves.

Day to day, once a study is running: `pmspec status <id> [lock] [--hours N] [--deep]` (provenance +
health always; with a lock, also `verify`), `pmspec diff <lock_a> <lock_b>` (compare two locks, no
live experiment involved), `pmspec audit <id> <lock> [--hours N] [--sample K] [--strict]` (did each
finished lambda job apply the treatment the lock says it should, per the decision function's own
verdict), and `pmspec cleanup [--older-than 7d] [--dry-run] [--yes]`. `cleanup`
never deletes by name — a candidate must have a name starting `test-` **and** `pmspec.mode ==
"test"` in its preferences, the prefix only narrows the fetch. MCP equivalents: `experiment_status`,
`diff_locks`, `audit_experiment`, `pm_cleanup_test_experiments`.

`pmspec compile` (and MCP `compile_experiment_spec`) is **strict by default**
(`docs/pmspec.md` § The strict contract): a construct the pipeline would silently
drop, ignore, or leave unresolved is a compile error, not a warning, so a spec that "compiles with
some warnings" is a spec with drops nobody fixed. `additional_sessions` (extra queues on one bot)
and `proxy.from_pool` (one CSV row, hence one exit IP, per bot) both now compile and are checked
by the same validator. `--lax` downgrades drops to warnings-plus-a-count and records
`provenance.lax` / `pmspec.lax` on the resulting lock — use it only to see what a broken spec
would have produced, never to upload a real study.

> **The four selection parameters are switches, not intensities.** `pi`, `psi`,
> `kappa` and `tau` are read by the decision function as
> `int(round(aux.get('pi', 0)))` and then compared `== 1`, so **use only `0` or
> `1`**. A fractional value does not scale the treatment — it rounds. And because
> Python rounds half to even, `0.5` rounds to **`0`**, silently disabling the very
> arm it looks like it configures, with no error anywhere. (An earlier version of
> this example used `treatment: 0.5`; every value stored in the database has only
> ever been `0` or `1`, so no experiment was affected.) If graded intensities are
> ever wanted, that needs a change in `lambda_function.py`, not in the spec.

```yaml
version: "1.0"
experiment:
  name: "My Study"
design:
  factors:
    condition:
      levels: [control, treatment]
      maps_to:
        configuration.params.pi: {control: 0, treatment: 1}
  replications: 5
crawlers:
  - from_design: true
    name: "bot_{condition}_{replica:02d}"
    session:
      workflow:
        - visit: https://example.com
        - wait: 5s
```

See `docs/pmspec.md` for full documentation.

> **`cookie_pref` is a treatment, and the runner acts on it.** Since
> `primemover_runner` PR #61 (2026-08-26) the runner answers cookie/GDPR
> dialogs — the YouTube consent bump first, then OneTrust, Cookiebot, Didomi,
> Usercentrics, TrustArc, Quantcast, Sourcepoint and a generic banner, in
> EN/DE/FR/IT/ES — after every visit and lambda-select navigation. Which
> button it clicks comes from the bot's `cookie_pref` preference
> (`{"accept_all": true}` → "Accept all", `false` → "Reject all"), overridable
> per job with the `consent` behaviour (`accept | reject | ignore`), else the
> runner's `consent.choice` (default `accept`). "Reject all" switches off the
> signed-out personalisation a study measures, so choose it deliberately.
> The outcome is in the runner log per job (`consent dialog (youtube)
> answered: …`). Before PR #61 nothing consumed `cookie_pref`, and no bot had
> ever seen the dialog because every run egressed from a **US** exit IP; from a
> CH/EU exit it appears on every session.

## Testing

- Tests live in `orchestrator/tests/` and `pmspec/tests/`
- E2E tests (files matching `test_e2e*`) are **skipped by default**; pass `--e2e` to pytest to run them
- E2E tests require live infrastructure (API, monitoring stack running)
- Custom markers: `@pytest.mark.e2e`, `@pytest.mark.multilogin`
- Mock external services (API, Loki, CloudWatch) in unit tests
- Shared fixtures in `orchestrator/tests/conftest.py`: `cli_runner`, `mock_settings`, `mock_httpx_client`, `sample_log_entries`
- `pmspec/tests/` has no conftest.py; its tests are self-contained
- CI runs tests + lint + type-check on Python 3.10, 3.11, 3.12 (`.github/workflows/ci.yml`)

**A green `--youtube` e2e says nothing about whether any treatment works.** That
scenario is `pi=psi=kappa=tau=0`, a control crawler. It does call OpenAI — the
`children_entertainment` rejection filter is requested unconditionally — but a
rejection filter produces no rating column, so `selection_mode` is correctly
`random` and no filter that *weights* the draw is exercised. Meanwhile the
decision function degrades by **omission**: a filter it cannot apply is dropped,
the recommendations stay plausible, the call is 200 and the job succeeds.

`orchestrator e2e --youtube-treatment` is the one that can see it. It runs
`pi=1` (mainstream_media, the CSV-backed filter, where the cwd and
CSV-corruption failures land) and `kappa=1` (similar, an LLM **rating** filter,
the only kind that makes `selection_mode` `llm` at all), then **fails the run
unless the treatment actually applied** — checking that a verdict was recorded
at all, the selection mode, each requested filter being `ok` (`skipped` means
NOT REQUESTED, so a skipped filter in a scenario that asked for it is the silent
failure itself), and the verdict's own `degraded` / `treatment_lost`.
It found a real defect on its first run; see the collaboration-card note under
"Local Lambda Server". Note `_job_ok()` counts `warning` as a pass by design, so
without this check a treated run that lost its arm still goes green.

**It also runs unattended, as a standing canary.** Experiment **177**
("Treatment canary 2026-09") on ex63 is one treated bot (`pi=1`, `kappa=1`) with
one session a day at 05:00 Europe/Zurich, generated by
`scripts/treatment-canary.py`. No cron is involved and none is wanted: `start_at`
already schedules work — a crawler carries many queues, each with its own start
time — so the canary is one uploaded experiment and the runner picks up each
session itself. Checking it is one request:

```
GET /api/v1/experiments/177/health?hours=24   # treatment_lost_jobs and degraded_jobs should be 0
```

The script reads its workflow from the same scenario `--youtube-treatment`
uploads, so canary and test cannot drift, and it **refuses to build from a
crawler that is not pi=1/kappa=1** — a canary built from a control passes for
ever and means nothing. Sessions run out **2026-10-07**; topping up means
generating a fresh batch and uploading it as a new experiment.

### CI across the ecosystem

As of 2026-08-21 all five repos have GitHub Actions workflows. Before that only
this repo and the API did, so "mergeable" on a GUI, runner or decision-function
PR meant *no merge conflicts* and nothing more.

| repo | workflow | what it runs |
|---|---|---|
| `primemover-orchestrator` | `ci.yml`, `compatibility-check.yml` | pytest 3.10–3.12, ruff, black, mypy |
| `primemover_api` | `laravel.yml`, `php.yml` | PHPUnit (249), pint, composer validate |
| `primemover_gui` | `ci.yml` | eslint, karma, prod build, **image + endpoint-injection smoke test** |
| `primemover_runner` | `ci.yml` | `mvn verify` on JDK **17 and 25** |
| `bot-decision-yt` | `tests.yml` | pytest on **3.9** (Lambda parity), 3.12/3.13 advisory |

Non-obvious constraints, each of which cost a red build to find:

- **GUI**: the image job asserts on the bytes the container *serves*, not on the
  build. Its two historical bugs — a bash-ism under busybox `ash`, and Angular's
  minifier rewriting `content=""` to a bare `content` — were both invisible to
  review and to `ng build`.
- **runner**: the poms compile at `release=17` but production runs **Java 25**;
  the matrix exists so CI tests the JVM that actually runs the jar. Never build
  in the working copy while the runner is live — it executes from
  `primemover-runner/target/`, and rebuilding the fat jar underneath it throws
  `EOFException`. Build in a throwaway clone.
- **bot-decision-yt**: pinned to `ubuntu-24.04`, not `ubuntu-latest` —
  `actions/python-versions` has no 3.9 build for 26.04. The pinned
  `requirements.txt` cannot install on 3.12+ (`numpy==1.25.2`, no cp312 wheel),
  which is why those jobs are advisory and unpinned.
- All actions are on **node24-native** majors (checkout v7, setup-python v7,
  setup-node v7, setup-java v5, cache v6, upload-artifact v7, github-script v9,
  codecov v7). Node 20 leaves GitHub-hosted runners in autumn 2026. When adding
  a step, check `action.yml`'s `using:` — `upload-artifact@v5` still declares
  node20.
- Coverage is printed to the job log, not uploaded. A Codecov step used to
  follow the pytest run and **could not succeed** — this repo is private,
  tokenless upload is public-repo-only, and no `CODECOV_TOKEN` exists — so it
  failed on every run and was green only because `fail_ci_if_error: false`.
  Removed rather than tokenised: the number is one `pytest --cov` away, and the
  history/PR-comment features it sells assume a team large enough for coverage
  to drift unnoticed. Read it from the job log, or run
  `pytest --cov=orchestrator --cov=pmspec` locally.
  Worth knowing what it says — **measured 2026-09-10, and the previous figures
  here were badly stale**: **70% overall**, `mcp_server/server.py` at **76%**
  (the `pm_` write tools have 49 tests in `test_mcp_write_tools.py` and 20 more
  in `test_mcp_write_guard.py`; the old note claiming 0% and "every `pm_` write
  tool is untested" was wrong by the time anyone read it), `tracing/trace_builder.py`
  at 82%, `logs/aggregator.py` at 43%, `clients/api.py` at 70%.

  Where it is genuinely thin is `components/*` — `manager.py` 37%, `runner.py`
  41%, `api.py` 24%, `lambda_server.py` 33% — which drive Docker and
  subprocesses and are awkward to unit test, and
  `pmspec/summary/visualizer.py` at 9%. Re-measure before acting on
  any of these numbers: they drift, and a stale coverage note costs somebody an
  afternoon writing tests for code that already had them.

## Formatting & Linting

- **Line length**: 100 (black + ruff)
- **Ruff rules**: E, F, W, I, N, UP, B, C4 (E501 ignored)
- **Target Python**: 3.10+
- **Pre-commit hooks**: trailing whitespace, black, ruff, mypy, detect-secrets (`.secrets.baseline`)
- **`make check`** runs in order: format → lint → typecheck → test (all must pass)
- **Branch**: `master` is the main branch; PRs target `master`

## Environment

Configuration via `.env` file (Pydantic Settings). Required credentials with no defaults:
- `PM_API_EMAIL`, `PM_API_PASSWORD` - API authentication
- `GRAFANA_ADMIN_PASSWORD` - Grafana admin

All other settings have sensible defaults (see `orchestrator/config/settings.py`). The API logs in CET timezone; Promtail config handles conversion to UTC for Loki. `PM_DEFAULT_RUNNER_USER_ID` (defaults to 1, ex63) and `PM_SERVICE_ACCOUNT_IDS` (`1,3`) drive the runner-assignment mechanism used by "Composing as a specific user" above.

## Deployment (VM / systemd)

On the production headless VM, components run as systemd units rather than via
`orchestrator up`. Definitions live in `infrastructure/systemd/` and are
installed with `sudo scripts/install-systemd-services.sh`:

- `primemover-lambda.service` — local Lambda server (`python -m
  orchestrator.lambda_service.server`), `Restart=always`
- `multilogin-agent.service` / `multilogin-desktop.service` — Multilogin X,
  require the `vncserver@1` display (`DISPLAY=:1`)
- `primemover-runner.service` — the Java runner, `Restart=always`. Installed by
  `install-systemd-services.sh` but **not auto-enabled**: it needs a built jar
  and a logged-in Multilogin X, so enabling it on a fresh machine yields a
  restart loop rather than a runner. Enable with
  `sudo systemctl enable --now primemover-runner` once both are in place.

  Added 2026-08-21. Before it, the runner was only ever started by
  `full-startup.sh` as a bare `nohup java`, which meant **it did not survive a
  reboot** — and since it inherited `DISPLAY=:1` from the interactive session it
  was launched in, there was nothing to inherit from at boot anyway. That is why
  `primemover-health-check.service` had been sitting in `failed` since 2026-08-13,
  passing every check and then reporting `✗ Runner (Java)`. It was right.

  Stopping mid-job leaves queues flagged `processing=1, processed=0` — SIGTERM
  does not drain gracefully, hence `TimeoutStopSec=180`. Prefer stopping when
  nothing is in flight.

  **`orchestrator doctor` reports the state of every PrimeMover unit**, with the
  timestamp of a failure and the `journalctl` line to run. That exists because a
  failed unit was otherwise visible only via `systemctl status`, which nobody
  runs during normal work — so the boot health check sat `failed` for over a week
  while correctly reporting three genuinely broken things. A check nobody reads
  is worth about as much as no check.

  Note a one-shot keeps its last result until it runs again: fixing the script
  does **not** clear a `failed` unit. `sudo systemctl start primemover-health-check`
  does.
- `primemover-health-check.service` — one-shot boot check running
  `scripts/boot-health-check.sh --quick` after everything else. It had been
  `failed` from 2026-08-13 until 2026-08-21 for three independent reasons, all
  now fixed: the runner had no unit (above), and two checks had gone stale
  against Multilogin X v12 — it looked for a standalone `/opt/mlx/agent.bin`
  that no longer exists (v12 embeds the agent; only `/opt/mlxapp` is present),
  and it required a 2xx from the launcher's `/api/v2`, which now returns 404
  even though the launcher is healthy. The launcher check is liveness-only now:
  any HTTP response counts, since `curl`'s `%{http_code}` is `000` only when the
  connection itself fails. Treat a red boot check as real — it was right all
  three times.
- `primemover-har-drain.service` + `.timer` — spool-and-drain sidecar (every 5
  min) that ships the runner's local HAR/job-data buffer to Nextcloud. Resource-
  capped (`CPUQuota=50%`, idle IO, `MemoryMax`) so it never contends with the
  runner. Not installed by `install-systemd-services.sh` — see
  `docs/har-spool-drain.md` for setup. Optional; enable when off-host HAR
  archival is wanted.

Helper scripts in `scripts/`:
- `full-startup.sh [--no-runner|--check]` — brings up VNC, Docker, monitoring,
  Multilogin, the Lambda service, and the runner in order (alternative to
  systemd for manual starts). Note its runner step redirects stdout to
  `log/runner-stdout.log` with **no rotation** — that file had reached 208 MB.
  `primemover-runner.service` sends stdout to the journal instead; logback's own
  rotated `log/primemover-runner.log` is unaffected either way, and that is the
  file promtail scrapes.
- `har-drain.sh [--dry-run]` — off-host archival; copies a time window of
  objects out of MinIO, tars them and uploads via rclone WebDAV. It is an
  archiver, not a drain: the source is the system of record and is never
  deleted (see `docs/har-spool-drain.md`)
- `multilogin-login.py` — authenticates the Multilogin agent
- `boot-health-check.sh` / `monitor-memory.sh` — health and memory monitoring

### Reaching things from off the box (ex63 edge)

Everything on ex63 that a browser should reach goes through the Traefik edge in
`/opt/ex63-infra` — tailnet-only, real certificates, DNS pointing at the box's
Tailscale IP. Nothing is public. The pieces, and where each is wired:

| URL | what | wired in |
|---|---|---|
| `primemover.qamelab.org` | the GUI (`ghcr.io/umatter/primemover_gui:sha-…`) | `ex63-infra/compose/apps.yml` |
| `primemover.qamelab.org/api` | the API, same origin as the GUI, so no CORS and no `API_ENDPOINT` | `primemover_api/docker-compose.ex63.yml` (opt-in overlay) |
| `pm-ops.qamelab.org` | **this repo's Grafana** — runner/Laravel file logs, HAR data, Tempo traces | `docker-compose.ex63.yml` here (opt-in overlay) |
| `ops.qamelab.org` | the *infra* Grafana — node/cAdvisor, container stdout | `ex63-infra/compose/observability.yml` |

The overlays are opt-in on purpose: the base compose files stay portable and
bind to `127.0.0.1`. Apply with
`docker compose -f docker-compose.yml -f docker-compose.ex63.yml up -d grafana`
(and the API's equivalent). Two traps, both hit once: the overlays interpolate
`DOMAIN` from *that repo's* `.env`, and without it the rule renders as
``Host(`pm-ops.`)`` with no error; and a Traefik router's default priority is
its rule **length**, so an explicit priority meant to win must exceed ~30, not
merely be non-zero. Testing from the box itself gives `403` via `127.0.0.1`
(the `tailnet-only` allow-list sees the docker bridge address) — use
`--resolve host:443:<ex63-tailnet-ip>` instead.

Loki, Prometheus and Tempo have no auth of their own and are deliberately
**not** routed; reach them through Grafana, or over an SSH tunnel.

**Do not deploy the API's `docker-compose.yml` from a branch that predates the
post-incident hardening.** MySQL and Redis must bind to `127.0.0.1` only and
`MYSQL_ALLOW_EMPTY_PASSWORD` must be absent. Verify before `docker compose up`:

```bash
grep -nE '127\.0\.0\.1:.*(3307|8889)|ALLOW_EMPTY' ../primemover_api/docker-compose.yml
```

## Common Issues

**ImportError: No module named 'mcp'** — install with `pip install mcp`.

**CloudWatch not available** — check AWS credentials, `AWS_DEFAULT_REGION`, and that the Lambda log group exists.

**Loki connection refused** — start Loki (`docker compose up -d loki`), check `LOKI_URL`.

**Grafana won't start** — set `GRAFANA_ADMIN_PASSWORD` in `.env` (required, no default); check `docker logs pm-orchestrator-grafana`.

**API authentication failed** — verify `PM_API_EMAIL` / `PM_API_PASSWORD` in `.env` match the API database.

**API logs not appearing in Loki** — the API logs in CET, Loki runs in UTC. Promtail config sets `location: "Europe/Zurich"`. Check `docker logs pm-orchestrator-promtail 2>&1 | grep "timestamp too new"`.

**Local Lambda server fails to start / `/invoke` returns 500**
- Read the log of whichever supervisor is running it — they differ, and the
  wrong one looks alarmingly quiet rather than erroring:
  `systemctl is-active primemover-lambda && journalctl -u primemover-lambda -n 50`
  under systemd (the VM default), or `log/lambda-server.log` when it was started
  by `orchestrator up`. Neither logs to the console.
- `orchestrator doctor` lists the registry and every decision function it names,
  including whether the running service could import each one. With no registry,
  verify the `orchestrator/lambda_service/decision` symlink resolves and that
  `decision/lambda_function.py` exists
- **A job whose decision came back `degraded` with `unknown decision function
  '<name>'`** is not a broken function — it is a name this box does not host.
  Compare the bot's `lambda_select` behaviour `function` value with
  `GET :5001/health`'s `functions` keys (and remember an un-rebuilt runner sends
  no name at all, so it lands on `default`)
- Suspect `mainstream_media_channels.csv` corruption if the mainstream-media
  arm looks inert (see "Local Lambda Server" above). Note it will **not** show
  up as a failure: `pi=1` crawlers keep returning 200 and behave like controls,
  and the only trace is `Aversion to mainstream media channels ignored` in the
  returned `warnings` list. Check with
  `cd ../bot-decision-yt && pytest tests/test_mainstream_media_csv.py`, or read
  `functions."bot-decision-yt".health` from `GET :5001/health`, which is that
  same check run by the package itself. Historically the actual cause on this VM
  was the **cwd** — the file was read by bare relative path while
  `primemover-lambda.service` ran from the repo root — and a passing CSV test
  plus a wrong cwd looked exactly like a healthy system. bot-decision-yt reads
  relative to `__file__` now, so that cause is gone for a current checkout; on an
  old one, check `readlink /proc/$(systemctl show -p MainPID --value
  primemover-lambda)/cwd`.
- The same filter used to silently lose a treatment via an `IndexError` on a
  collaboration card's channel xpath, unrelated to CSV corruption. Fixed
  2026-09-09 (`bot-decision-yt` #22/#23) — see "Data files are load-bearing"
  above for what it was and what must not be re-simplified.
- Port 5001 in use: stop the stale process (`.lambda_server.pid`) or check the
  `primemover-lambda` systemd unit isn't already running it

**Chrome renderer becomes unresponsive on the headless VM after many jobs**
- Full history, measurements and current status: `docs/chrome-browser-death-fix.txt`
  (rewritten 2026-08-24 — the old version described two fixes and a 1024 MB heap
  cap; reality was three H.264 injection vectors, an effective cap of 512, and an
  undocumented video teardown that cost ~2/3 of the bots' watch time)
- The software-decode half of the root cause is **gone on this host**: VA-API
  hardware decode works as of 2026-08-22 (iHD driver + `render`/`video` group
  membership for `primemover`; groups are read at process start, so restart
  `primemover-runner` and `multilogin-desktop` after changing them)
- The V8 heap cap (`ProfileService.java`) guards a ChromeDriver leak and is
  hardware-independent — keep it on any host. **It is 2048 since 2026-08-28, not
  512, and the renderer is now recycled every 25 navigations**
  (`runner.RendererRecycleAfterNavigations`, `primemover.util.RendererRecycler`).
  Both were needed, and the reason is the unit: the leak is **per navigation**
  (~14 MB of old-space per same-site SPA navigation, Selenium #13510/#13533), not
  per minute. Five 113-job runs died on 2026-08-27 at 27/32/33/36/37
  navigations — tight — but anywhere between 34 and 69 *minutes* — not tight.
  Nothing had regressed: the same workflow at 8-13 navigations per session ran
  1828 jobs on 2026-08-24 with zero crashes. The queues simply got long enough
  (55 navigations) to cross a threshold that was always there, and 512 MB
  tolerates about 36.
  A bigger cap only rescales that ceiling; the recycle removes it, because
  navigating cross-origin (`chrome://version` — **not** `about:blank`, which can
  inherit the SiteInstance and leave the leaking renderer alive) makes Chrome
  tear the renderer down and reclaim the whole heap. Peak heap becomes O(1) in
  queue length instead of O(navigations), for ~2-3 s and no Multilogin API call.
  Verified in production 2026-08-28: renderer PIDs appear at exactly the recycle
  timestamps, and bots passed 38 navigations with zero crashes where 5 of 5 had
  died by 37 the day before.
- **Never add GL flags (`--use-gl=egl` …) to the MLX profile**: Chrome 151 only
  allows `(gl=egl-angle, angle=default)`; anything else kills the GPU process
- `scripts/monitor-memory.sh` watches renderer RSS to catch regressions

**No HAR files / job data on disk, or every job shows `problems writing jobresult`**
- HAR recording is toggled by `recorder.enabled` in the runner's
  `application.properties` (default **off** → `NoRecordingProxy`, a pass-through;
  residential proxying still works, only capture is lost). Set `true` to record.
  When on, the MITM recorder chains through the residential proxy and impersonates
  TLS per host — verified working; mind the recorder cert (`cert/2024/ca-cert.p12`)
  shelf life.
- **`out/har` being absent is normal and not the bug.** `storage.local.enabled=false`
  means the local writer bean is never constructed; job data goes to MinIO. See
  "Where job data goes" above before chasing this. If a *reader* is empty, the
  fix is `JOBDATA_S3_ACCESS_KEY` / `JOBDATA_S3_SECRET_KEY`, not re-enabling the
  local write.
- Job data is written by `AwsS3StorageService` (to MinIO, despite the name) and,
  when `storage.local.enabled=true`, `FileStorageService` → `export.FileTargetFolder`.
  A single failing backend used to short-circuit the rest (dead key → 403 → local
  write skipped); fixed in `primemover_runner` commit `82d6e72`. If jobs land in
  `WARNING_JOBDATA` ("problems writing jobresult"), check the MinIO endpoint and
  the bucket's credentials.

## Related Repositories

- `primemover_api` - Laravel API (cloned as sibling `../primemover_api`)
- `primemover_runner` - Java runner (cloned as sibling `../primemover_runner`)
- `bot-decision-yt` - Python decision function. Cloned as a sibling and
  **symlinked** into `orchestrator/lambda_service/decision` when running the
  local Lambda server; deployed to AWS Lambda otherwise.

Integration contract details: `docs/integration-contract.md`
