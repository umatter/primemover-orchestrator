# Integration Contract

This document defines the integration points between the PrimeMover Orchestrator and the other repositories in the ecosystem. **Changes to these contracts require coordinated updates across repositories.**

## Overview

The orchestrator integrates with:
- **primemover_api** (Laravel) - via Docker, logs, and REST API
- **primemover_runner** (Java/Spring Boot) - via process management and logs
- **bot-decision-yt** (AWS Lambda, or the local Lambda server on `127.0.0.1:5001`) -
  via CloudWatch logs and HAR files

The two contracts that decide whether an experiment's data is trustworthy are
**§3.4 Job & Queue Status Contract** and **§7.3 Decision-Function Response
Contract**. Both are normative; `orchestrator/status.py` is their executable copy.
Before shipping either of them, read **§0 Deploy order** — the order is part of
the contract. It records what is deployed on ex63 today, the order for the work
in flight, and the one historical pairing that recorded *false* data rather than
merely incomplete data.

---

## 0. Deploy Order

**Status:** Critical - **normative** whenever a change to §3.4 or §7.3 is in
flight across repositories.

### The 2026-08-27/28 overhaul is deployed (verified 2026-09-07)

This section used to open "nothing in that change is deployed". That has been
false since 2026-09-03 and reading it as current would send someone off to
redeploy a stack that is already current. The six steps of that rollout, and how
each was checked on ex63:

| # | step | state on ex63 | how it was verified |
|---|---|---|---|
| 1 | `bot-decision-yt` -> local Lambda server | **done** 2026-09-03 10:46 CEST | `systemctl show -p ActiveEnterTimestamp primemover-lambda`; `readlink /proc/$(systemctl show -p MainPID --value primemover-lambda)/cwd` ends in `/bot-decision-EN`, so `ensure_decision_cwd()` took effect and the `pi=1` arm is live |
| 2 | promtail | **done** 2026-08-28 17:05 CEST | `docker inspect pm-orchestrator-promtail` |
| 3 | runner jar | **done** 2026-09-03 | jar `primemover-runner/target/primemover-runner-2.2.1.jar` built 10:47, unit restarted 10:48 |
| 4 | API | nothing to deploy | unchanged: `status_code` is `nullable` with no `in:` rule |
| 5 | GUI image | **done** 2026-09-03 09:31 CEST | `docker ps`: `ex63-primemover-1` runs `ghcr.io/umatter/primemover_gui:sha-593a027` |
| 6 | Grafana | **done** 2026-08-27 17:35 CEST | container start postdates `infrastructure/grafana/provisioning/alerting/` (written 15:00) |

The database is the confirmation that the vocabulary is actually flowing, not
merely deployed. On 2026-09-07:

```
jobs    success 19860  failure 1607  failed 256  skipped 4419  warning  547  NULL 4885
queues  success   358  failure   26  failed 277  skipped    1  warning   73
```

547 `warning` jobs and 73 `warning` queues are states that could not be recorded
at all before 2026-08-28. The 256 + 277 legacy `failed` rows are why every reader
still has to go through the alias table (§3.4).

**The mixed-version hazard of that rollout is therefore gone**, and the pairing
worth remembering is the one that made the order matter: a **new decision
function against an old runner jar** records `success` for a session whose
payload says `treatment_lost: true`. If a jar is ever rolled back, that is what
happens - the API keeps the wrong answer and every analysis joins on it.

### The order for this change set (persisted decision verdict, §7.3)

The current work adds four nullable columns to `jobs`, the rollup counts and
filters in §3.4, and the runner-side writes that populate them. Three
repositories, one order:

| # | deploy | why it sits here |
|---|---|---|
| 1 | **API** - migration adding `decision_degraded`, `decision_treatment_lost`, `decision_selection_mode`, `decision_report` (+ the new counts, filters, `sortBy=severity`, `/experiments/{id}/health`) | Somewhere for the verdict to land. Alone it changes nothing anyone sees: every column is NULL and NULL reads as unknown. |
| 2 | **GUI** - "Treatment lost" / "Degraded" / "no verdict" badges, `filter[bad]`, health strip | Renders a verdict for the jobs that have one and "no verdict" for the rest - which, until step 3, is all of them. |
| 3 | **runner jar** - the four fields on the status PUT | Starts producing verdicts. Build in a throwaway clone (the live runner executes from `target/`), restart while nothing is in flight. |

**Never the runner first.** Its verdict travels on the same PUT as
`status_code`. Against an API that has not migrated, the best case is that the
four keys are dropped and every verdict in that window is lost with no trace;
the worse case is that the PUT is rejected and the job it was reporting on stays
`running` for ever. §3.4 records that the API deliberately has no `in:` rule on
`status_code` for exactly this reason - a status PUT must never be refusable.

**Why every intermediate state is safe: NULL means unknown, not clean.** That
one rule is what removes the "wrong data" pairings from this rollout entirely.
Old runner + new API: NULL, rendered as "no verdict". New GUI + old API: the
field is absent, which is the same answer. New runner + old GUI: the verdict is
stored and simply not drawn yet, and `orchestrator debug job` and `trace job`
will show it in the meantime. At no point does any layer assert that a treatment
applied when nobody measured whether it did - which is the failure mode the
whole overhaul exists to prevent, and the one a `NOT NULL DEFAULT false`
migration would have re-created the moment it ran, across all 31,574 job rows
already in the table.

The one thing the order does **not** make safe is **counting** with a nullable
boolean. These columns have three states and SQL's three-valued logic silently
drops the third from any `WHERE`: `decision_treatment_lost = 1` and
`decision_treatment_lost = 0` are both correct as filters, but they do not
partition the table, and `COUNT(*) - COUNT(lost)` is not "the ones that were
fine". Tally all three explicitly -

```sql
SUM(decision_treatment_lost IS TRUE), SUM(decision_treatment_lost IS FALSE),
SUM(decision_treatment_lost IS NULL)
```

- and check they sum to the total, the same rule §3.4 puts on the `unknown`
bucket of `/experiments/{id}/health`. An unknown that vanishes from a
denominator is the `status_code = 'failure'` mistake wearing a different hat.

### The correlation-id spelling changed at the 2026-09-03 runner restart

The jar deployed before it wrote `correlationId=cr-244|jl-838`; the one running
now writes the canonical `cr-244|q-838`, plus `|j-<id>` for the duration of a job
(§1). Archived logs hold both. **Both forms are handled by the query layer** -
`correlation_line_filters()` in `orchestrator/clients/loki.py` emits a filter for
each spelling, and the entry parser matches `(?:q|jl)-(\d+)` - so a search by id
keeps working across the boundary. Anything that hard-codes one spelling does
not; the Grafana alert-rule annotations name both, and
`infrastructure/grafana/provisioning/dashboards/json/primemover-overview.json`
still documents only `jl-` in two help strings and should be updated to say
"`jl-838` or `q-838`".

---

## 1. Correlation ID Format

**Status:** Critical - used for cross-component tracing

All components must use this format for correlation IDs:

```
exp-{experiment_id}|cr-{crawler_id}|q-{queue_id}|j-{job_id}
```

**Examples:**
- Full: `exp-15|cr-42|q-123|j-456`
- Partial: `cr-42|j-456`
- Job only: `j-456`

**Orchestrator file:** `orchestrator/tracing/correlation.py`

**Validation regex:**
```python
r"exp-(\d+)"      # experiment_id
r"cr-(\d+)"       # crawler_id
r"(?:q|jl)-(\d+)" # queue_id (q- or jl- prefix)
r"j-(\d+)"        # job_id
```

---

## 2. Log Formats

### 2.1 Laravel (primemover_api)

**Status:** Critical - parsed for log aggregation and tracing

**Expected format:**
```
[YYYY-MM-DD HH:MM:SS] environment.LEVEL: message {"context": "json"}
```

**Example:**
```
[2025-01-15 10:30:45] production.ERROR: Job failed {"experiment_id": 15, "job_id": 456}
```

**Orchestrator file:** `orchestrator/logs/parsers/laravel.py`

**Required context fields** (when applicable):
- `experiment_id`
- `crawler_id`
- `queue_id` or `joblist_id`
- `job_id`

**Log location:** `storage/logs/` directory

### 2.2 Spring Boot (primemover_runner)

**Status:** Critical - parsed for log aggregation and tracing

**Expected format:**
```
YYYY-MM-DDTHH:MM:SS.mmm+ZZ:ZZ  LEVEL PID --- [application] [thread] logger  : message
```

**Example:**
```
2025-01-15T10:30:45.123+00:00  INFO 12345 --- [primemover-runner] [main] c.p.r.Runner  : Processing crawlerId=42
```

**Orchestrator file:** `orchestrator/logs/parsers/runner.py`

**ID extraction patterns in messages:**
- `crawlerId=N` or `crawler_id=N` or `crawler.id=N`
- `jobId=N` or `job_id=N` or `job.id=N`
- `correlationId=exp-N|cr-N|j-N`

**Log location:** `primemover-runner/log/` directory

---

## 3. API Endpoints

**Status:** Critical - used for data retrieval and experiment upload

### 3.1 Authentication

**Endpoint:** `POST /api/v1/login`

**Request:**
```json
{
  "email": "user@example.com",
  "password": "password"
}
```

**Response:**
```json
{
  "access_token": "eyJ..."
}
```

**Usage:** Bearer token in `Authorization` header

### 3.2 Data Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/experiments` | GET | List experiments |
| `/api/v1/experiments/{id}` | GET | Get experiment details |
| `/api/v1/crawlers/{id}` | GET | Get crawler details |
| `/api/v1/jobs/{id}` | GET | Get job details |
| `/api/v1/experiments` | POST | Create experiment |

**Orchestrator file:** `orchestrator/tracing/trace_builder.py`

### 3.3 Health Check

**Endpoint:** `GET /api/v1/health` or `GET /`

**Expected:** HTTP 200 response

### 3.4 Job & Queue Status Contract

**Status:** Critical - **normative**. This is the runner -> API -> GUI/orchestrator
contract for "how did it go". Changing any value here is a breaking change across
four repositories.

**Executable copy:** `orchestrator/status.py`. That module is the single source of
truth in code (vocabulary, aliases, severity ordering, field extraction); this
section is the prose copy. If they ever disagree, the module is right and this
section is stale - fix it.

This section exists because the contract was written down nowhere. The document
you are reading listed log formats, routes, auth and the MDC correlation format
as breaking-change surface, and said nothing about status values. The result was
**five divergent status vocabularies inside the orchestrator alone**
(`e2e/job_status.py`, `e2e/runner.py`, `tracing/trace_builder.py`,
`debug/job_debugger.py`, `cli.py`), several of which read a `status` field the
API has never sent. Silence in a contract document is not neutrality; it is a
licence for everyone to invent their own.

#### Endpoints

| Endpoint | Method | Written by | Purpose |
|----------|--------|-----------|---------|
| `/api/v1/runner/jobs/{job}` | PUT | runner | report job state |
| `/api/v1/runner/queues/{queue}` | PUT | runner | report queue (run) state |

#### Fields

| Field | Type | Notes |
|---|---|---|
| `status_code` | string | the vocabulary below. **The field is `status_code`. There is no `status` field on the wire** - anything reading `status` is reading a fixture, not the API. |
| `status_message` | string, nullable | see "Message format" below |
| `started_at` / `finished_at` | ISO-8601 with offset | the API sends `finished_at`; `completed_at` exists only in older orchestrator code and fixtures |

Neither field is validated by the API: `RunnerJobUpdateRequest` marks
`status_code` `nullable` with no `in:` rule, so the API will store whatever it is
sent. Nothing rejects a typo. That is why the vocabulary has to be enforced by
the writers and normalised by the readers.

#### Job `status_code`

| value | terminal | meaning |
|---|---|---|
| `running` | no | started, or storing data |
| `success` | yes | completed, nothing degraded |
| `warning` | yes | **completed, but something the experiment cares about degraded** |
| `failure` | yes | did not complete |
| `skipped` | yes | not attempted, normally because an earlier job in the run failed |

#### Queue `status_code`

| value | terminal | meaning |
|---|---|---|
| `running` | no | in progress |
| `success` | yes | every job finished cleanly |
| `warning` | yes | **completed, but lost jobs along the way** |
| `failure` | yes | nothing usable came out of the run |
| `skipped` | yes | never attempted (inactive, already reviewed, or timed out) |

#### Queue `warning` (new 2026-08-28)

A run that lost jobs is neither a success nor a total failure, and calling it
either one is what hid five truncated sessions on 2026-08-27 and two more on
2026-08-28. `TaskOperator` used to set the joblist result unconditionally after
the job loop, so **the queue reported `success` however many of its jobs had
died**. Measured on experiment 145:

```
q849  status_code=success  status_message="done"   ok=8/204 (4%)  fail=49 skip=147
q850  status_code=success  status_message="done"   ok=8/204 (4%)  fail=49 skip=147
```

Anything filtering on queue status alone ingested those as clean sessions.

The runner now counts per-job outcomes (`JobListOutcome`) and reports
`DONE_JOBSWARNING` when the run completed but lost work, with the counts in
`status_message`:

```
status_code=warning  status_message="8/204 jobs succeeded (0 warning, 49 failed, 147 skipped)"
```

`failure` is reserved for a run that produced nothing at all. Note the queue's
own status and its per-job rollup (`jobs_succeeded_count` and friends) answer
different questions: the status says whether the *run* is trustworthy, the rollup
says how much of it survived.

#### Job `warning` (new 2026-08-27)

The job ran to completion, but either

- its job data (HAR / job result) could not be written, or
- the decision function reported a **degraded** decision (§7.3): a *requested*
  filter did not apply, and in the worst case `treatment_lost` - nothing
  requested applied at all.

**A control crawler whose video was picked at random is not this case.** Nothing
was requested and nothing failed, so `degraded` is false and the job stays
`success`. Mapping `selection_mode == "random"` onto `warning` would have turned
~82% of all `lambdaselect` jobs yellow; see §7.3.

Both were previously reported as `success` - the runner's `JobState` enum mapped
`WARNING` and `WARNING_JOBDATA` onto the code `"success"`. That is precisely what
hid silent treatment-arm degradation: the run finished green, the experiment
looked complete, and the affected bots had quietly produced control-arm data.

**Consumers must not treat `warning` as success.** The predicate for "a human
should look at this" is `failure OR warning` (`status.is_bad`), not `failure`.

#### Legacy values

| stored value | canonical | why it exists |
|---|---|---|
| `skpped` | `skipped` | a typo in the runner's `JoblistState` for SKIPPED_INACTIVE / SKIPPED_REVIEWED |
| `failed` | `failure` | older spelling |

Readers must accept both. This is not hypothetical. Counted on the ex63 database
on **2026-09-07**:

| table | `success` | `failure` | `failed` | `skipped` | `warning` | NULL |
|---|---|---|---|---|---|---|
| `jobs` | 19860 | 1607 | 256 | 4419 | 547 | 4885 |
| `queues` | 358 | 26 | 277 | 1 | 73 | 0 |

So `WHERE status_code = 'failure'` misses 14% of failed jobs and, on queues,
**misses 91% of them** - the legacy spelling is the *majority* there. `skpped`
currently has 0 rows, but the enum still emits it, so it can reappear at any
time. The API's `statusVariants()` helper and `orchestrator.status.normalize()`
are the two implementations of this table; use one of them rather than writing
the alias out again.

An unrecognised value must be treated as **unknown**, never coerced to success.
The 4885 jobs with a NULL `status_code` are unknown too: they are not failures,
and they are certainly not successes.

#### Message format

`status_message` is either the bare state text (`"job started"`,
`"successfully finished job"`) or, when there is a diagnostic to attach:

```
<state text> - <single-line, truncated detail>
```

Until 2026-08-27 it was **always** one of about seven fixed enum strings. The
whole `jobs` table contains only six distinct values - `successfully finished
job`, `error during execution`, `previous task was not executed`, `Message`,
`Random`, and NULL - which is why debugging a failure always meant leaving the
API and going to the logs. The runner now attaches the real cause. The detail
must stay single-line (it travels through log lines and table cells) and
truncated (it is a message, not a stack trace; the stack trace belongs in the
runner log under the same correlation id).

#### Rollup counts on the read endpoints (new 2026-09)

The status of one run answers "is this run trustworthy". It does not answer "how
much of it survived", and the two genuinely disagree: a `warning` run may have
lost one job of 204 or, as measured on experiment 145 above, 196 of them. So the
read endpoints append counts, and every consumer
(GUI, orchestrator, analysis) must read the same names.

**Per queue** (`GET /queues`, `GET /queues/{id}`, and the `queues-*` list
endpoints), counting that run's jobs:

| field | counts jobs where |
|---|---|
| `jobs_count` | all |
| `jobs_succeeded_count` | `status_code` ∈ variants(`success`) |
| `jobs_warning_count` | ∈ variants(`warning`) |
| `jobs_failed_count` | ∈ variants(`failure`) - **includes the legacy `failed` spelling** |
| `jobs_skipped_count` | ∈ variants(`skipped`) - includes `skpped` |
| `jobs_degraded_count` | **new** - `decision_degraded = true` (§7.3) |
| `jobs_treatment_lost_count` | **new** - `decision_treatment_lost = true` (§7.3) |

The last two are counts of *verdicts*, not of statuses, and a NULL column is not
counted in either direction: a run with `jobs_count = 40` and
`jobs_treatment_lost_count = 0` may have forty verdicts saying "fine" or forty
NULLs saying nothing. Do not render `0` as a clean bill of health without
checking that a verdict exists.

**Per crawler (bot)**, over a rolling window (24h by default; the window is a
`?hours=` query parameter):

| field | counts runs where |
|---|---|
| `latest_queues_count` | all runs in window |
| `latest_queues_succeeded` | the run's own `status_code` ∈ variants(`success`) |
| `latest_queues_failed` | ∈ variants(`failure`) |
| `latest_queues_warning` | **new** - ∈ variants(`warning`), the run's **own** status |
| `latest_queues_degraded` | the run has ≥ 1 job with `status_code` ∈ variants(`warning`) |
| `latest_queues_treatment_lost` | **new** - the run has ≥ 1 job with `decision_treatment_lost = true` |

`latest_queues_warning` and `latest_queues_degraded` are **different questions**,
and the second one alone had a hole: `degraded` is derived from *jobs*, so a run
that the runner marked `warning` at the queue level with no `warning` job among
its children was counted by none of the four original fields, and a consumer
reconstructing "what happened to this bot" from them got `running` - for ever.
`warning` closes that hole; keep both, because a run can be one without the
other.

**Per experiment** (`GET /experiments`): `runs_bad_count` - **new** - runs in the
window whose own `status_code` is `failure` **or** `warning`, i.e. the contract's
`is_bad`, so an experiment card can show a dot without one request per bot.

#### Filtering and sorting by status (new 2026-09)

| parameter | meaning |
|---|---|
| `filter[status_code]=failure,warning` | comma-separated list, each term **expanded through the alias table** before matching, so `failure` also matches the 256 legacy `failed` job rows and the 277 queue ones |
| `filter[bad]=1` | exactly `variants(failure) ∪ variants(warning)` - the contract's `is_bad`, in one parameter |
| `sortBy=severity` / `sortBy=-severity` | orders by the severity rank below rather than by the raw string; `-severity` is worst-first, which is the one a triage view wants |

`filter[bad]` exists so a client never has to know the alias table to ask the one
question that matters. A GUI that builds `filter[status_code]=failure` by hand
gets a list that is missing most of its failed runs, and nothing tells it so.

**Severity sort rank** - and note it is *not* the same table as the one used to
roll a parent status up from its children:

| status | rank |
|---|---|
| `success` | 0 |
| `skipped` | 1 |
| `running` | 2 |
| **unknown** | **3** |
| `warning` | 4 |
| `failure` | 5 |

`unknown` covers an unrecognised code **and a NULL `status_code`** (4885 job rows
today), and it sits above `running` deliberately: put it at 0 and every
unreported row hides at the clean end of the list, which is where nobody looks.
The executable copy is `orchestrator.status.SEVERITY_SORT_RANK` /
`severity_rank()`. It differs on purpose from `orchestrator.status.SEVERITY`,
which has no `unknown` entry because it answers a different question ("which
child status does the parent inherit?") and other code pins its numbers -
`SEVERITY` must not gain one.

#### Experiment health (new 2026-09)

```
GET /api/v1/experiments/{id}/health?hours=24
```

`hours` defaults to 24 and is capped at 720 (30 days). One grouped, alias-aware
query over the experiment's queues:

```json
{
  "bots": 12,
  "runs":  {"total": 48, "running": 2, "success": 31, "warning": 9,
            "failure": 5, "skipped": 0, "unknown": 1},
  "jobs":  {"total": 4820, "running": 0, "success": 4102, "warning": 547,
            "failure": 90, "skipped": 60, "unknown": 21},
  "bad_runs": 14,
  "last_finished_at": "2026-09-06T22:14:03+02:00"
}
```

`unknown` is a first-class bucket in both tallies, not a residue to be dropped:
the counts must sum to `total`, otherwise the endpoint is quietly deciding that
rows it does not understand did not happen. `bad_runs` is `warning + failure`
after alias expansion - the same predicate as `filter[bad]` and
`runs_bad_count`, computed once so the three cannot disagree.

#### Note for API maintainers

The OpenAPI annotation on `RunnerJobController::update` documents a **sixth**
vocabulary that matches nothing in this contract or in the database -
`{pending, running, completed, failed, cancelled}`. It is documentation only and
generates no validation, but it will mislead anyone who trusts it. It should be
corrected to the five values above.

---

### 3.5 Runner assignment (API PR #59, not yet merged/rolled out)

With API PR #59, `experiments.runner_user_id` (nullable, FK → users) names the
runner account that executes an experiment. `NULL` means the default runner,
`PM_DEFAULT_RUNNER_USER_ID` on the API (1 = ex63). Valid targets are exactly the
accounts in `PM_SERVICE_ACCOUNT_IDS`; the rollout target on ex63 is `1,3` (ex63's
runner, the VM's runner). Rollout status on ex63: see
`docs/runner-vm-migration-handoff.md`.

Only two endpoints are partitioned by it, the two the Java runner fetches work from:
`GET /crawlers-unprocessed` and `GET /crawlers-testing` return crawlers whose
experiment is assigned to the calling runner (plus unassigned ones for the default
runner). The filter applies regardless of isolation bypass. Every other endpoint —
including the runner's status PUTs — is unaffected.

Ownership (`user_id`) is visibility only. Researchers create experiments under their
own login and pick the runner with `runner_user_id` (create/update requests; the
MCP `pm_create_experiment` and the YAML `experiment.runner_user_id`).

## 4. Directory Structure

### 4.1 primemover_api

```
primemover_api/
├── docker-compose.yml        # Required - orchestrator runs this
├── .env                      # Configuration
├── storage/
│   └── logs/                 # Laravel logs (*.log)
│       └── laravel.log
└── public/
    └── gui/                  # GUI static files (optional)
```

### 4.2 primemover_runner

```
primemover_runner/
├── pom.xml                   # Maven parent POM
├── primemover-runner/
│   ├── pom.xml
│   ├── target/
│   │   └── primemover-runner-*.jar   # Built JAR (excludes -sources, -tests)
│   ├── config/
│   │   └── application.properties    # Spring config
│   ├── log/                          # Runner logs
│   └── out/
│       └── har/                      # HAR output files
│           └── {job_id}/
│               └── *lambda*.zip      # Lambda response archives
└── primemover-generated/
    └── pom.xml
```

**Critical paths:**
- JAR: `primemover-runner/target/primemover-runner-*.jar`
- Config: `primemover-runner/config/`
- Logs: `primemover-runner/log/`
- HAR: `primemover-runner/out/har/`

---

## 5. Docker Configuration

### 5.1 Container Names

The API's docker-compose.yml must use these container name prefixes:

| Service | Container Name |
|---------|---------------|
| Apache | `primemover-apache` |
| PHP | `primemover-php` |
| MySQL | `primemover-mysql` |
| Redis | `primemover-redis` |
| Memcached | `primemover-memcached` |

**Orchestrator file:** `orchestrator/components/api.py`

### 5.2 Default Ports

| Service | Port | Environment Variable |
|---------|------|---------------------|
| HTTP (Apache) | 8090 | `PM_API_HTTP_PORT` |
| MySQL | 3307 | `PM_API_MYSQL_PORT` |
| Redis | 8889 | `PM_API_REDIS_PORT` |

### 5.3 Artisan Commands

The orchestrator executes Laravel Artisan commands via:

```bash
docker-compose exec -T php php artisan {command}
```

**Required commands:**
- `migrate`
- `db:seed`
- `cache:clear`
- `config:cache`

---

## 6. Runner Process Management

### 6.1 JAR Discovery

**Pattern:** `primemover-runner/target/primemover-runner-*.jar`

**Exclusions:** Files containing `-sources` or `-tests`

**Orchestrator file:** `orchestrator/components/runner.py`

### 6.2 Startup Command

```bash
java {JAVA_OPTS} -jar {jar_path} \
  --spring.profiles.active={profile} \
  --spring.config.location={config_path}/
```

**Defaults:**
- `JAVA_OPTS`: `-Xmx2g -Xms512m`
- `profile`: `local`
- `config_path`: `primemover-runner/config/`

### 6.3 Startup Detection

The orchestrator waits for one of these log messages:
- `Started PrimemoverRunnerApplication`
- `Tomcat started on port`

**Timeout:** 30 seconds

### 6.4 Java Version

**Required:** Java 17 or higher

---

## 7. Lambda Integration

### 7.1 AWS Configuration

| Setting | Default | Environment Variable |
|---------|---------|---------------------|
| Region | `eu-central-1` | `AWS_DEFAULT_REGION` |
| Function | `bot-decision-yt` | `LAMBDA_FUNCTION_NAME` |
| Log Group | `/aws/lambda/bot-decision-yt` | `LAMBDA_LOG_GROUP` |

### 7.2 HAR Output Format

Lambda responses are stored in ZIP files within the HAR directory:

**Path:** `{PM_RUNNER_HAR_PATH}/{job_id}/*lambda*.zip`

**ZIP contents:** `output.json` (or similar)

**Expected JSON fields:** see §7.3 - the HAR/ZIP copy is the same payload the
runner received from the invocation.

### 7.3 Decision-Function Response Contract

**Status:** Critical - **normative**. This is what `bot-decision-yt` returns,
whether invoked on AWS or through the local Lambda server
(`POST http://localhost:5001/invoke`).

**The response is HTTP 200 even when the decision was degraded.** The status code
tells you the *call* worked; it never tells you the *treatment* worked. Every
consumer that cares about arm integrity has to read the fields below.

| field | type | since | meaning |
|---|---|---|---|
| `xpath` | string | existing | selector for the chosen recommendation; **empty means nothing was selected** (usually the page was a consent wall, a bot check or a network-error page, not a watch page) |
| `present` | object | existing | the video the bot is currently on |
| `selected` | object | existing | the video chosen |
| `procedure` | object | existing | per-candidate filter scores |
| `timestamps` | object | existing | timing |
| `warnings` | string[] | existing | human-readable degradation notes; **empty array is the healthy state** |
| `degraded` | bool | **new** | true iff at least one **requested** filter failed to apply, or selection failed outright (no video could be chosen). The authoritative "something went wrong" flag. |
| `selection_mode` | `"llm"` \| `"random"` | **new** | a **neutral description** of how the video was chosen. `"random"` = no LLM rating informed the pick. Expected and correct for a control crawler. **Not an error signal on its own.** |
| `treatment_lost` | bool | **new** | true iff `degraded` **and** no requested treatment actually applied - the bot was configured to be treated and produced control-arm data. The single unambiguous alarm condition. Always false for a control crawler. |
| `filters` | object | **new** | `{name: "ok" \| "skipped" \| "failed: <ExcClass>: <msg>"}`, six keys in fixed order: `already_watched`, `mainstream_media`, `children_entertainment`, `similar`, `political`, `fringe`. **`"skipped"` means NOT REQUESTED and must never be counted as a failure.** |
| `llm` | object | **new** | `{available: bool, error_type: str\|null, error: str\|null}` |
| `error` | string | **new** | present **only** on a hard 4xx/5xx from the lambda server |

#### Consumer rules - identical at every layer

| question | gate |
|---|---|
| a human should look at this decision | `degraded == true` |
| this session's data is scientifically void | `treatment_lost == true` - **alert here** |
| how was the video chosen | `selection_mode`, for description only |

**Never branch on `selection_mode` alone.** Not in the runner, not in
`status.py`, not in the GUI, not in an alert rule, not in an analysis script.

#### Why `treatment_lost` exists - the correction, 2026-08-27

An earlier draft of this section said `selection_mode == "random"` meant the
treatment arm had silently become a control arm. **That is false**, and it is
false in the direction that would have inverted the purpose of the whole change.

A genuine control crawler (`pi=psi=kappa=tau=0`) requests no LLM rating filter at
all. It therefore draws uniformly and correctly reports `selection_mode: "random"`
with `degraded: false`. Nothing went wrong; nothing was requested. Measured on
ex63 before this was shipped:

- **295 of the 358** lambda payloads in the runner's rotated logs carry no rating
  column at all - 82% (a plain `grep -c rating` over the same 358 files says 350,
  98%; the conclusion does not depend on which count you take);
- **236 of the 243** `params` rows in the live database are control-arm
  (`pi=psi=kappa=tau=0`), against 7 treated.

A `selection_mode`-only gate would therefore have marked ~82% of every
`lambdaselect` job `warning` and held a `severity=critical` alert on permanently
over entirely healthy traffic - burying the handful of real losses and training
everyone to ignore the alert. It reads zero *today* only because the deployed
decision function emits no `selection_mode` at all; it would have gone critical
the moment step (1) of §0 landed.

`treatment_lost` is that two-term gate - *degraded* **and** *nothing requested
applied* - computed **once, at the source**, so no consumer can get it wrong.
Consumers read one boolean.

`degraded: true` with `treatment_lost: false` is the milder, still-real case:
part of the treatment did not happen, and `filters` names which part.

#### Legacy payloads: absence is "unknown", never "degraded"

A payload with **no degradation report at all** - no `degraded`, no
`selection_mode`, no `filters`, no `treatment_lost` - came from a decision
function deployed before the report existed. Its key set is
`{present, procedure, selected, timestamps, warnings, xpath}`. Readers must
report it as **unknown**. Reading absence as degradation turns every archived HAR
into an incident; reading it as health asserts something nobody measured.

**This is now the historical case, not the normal one.** Since the 2026-09-03
restart (§0 step 1) the deployed function emits the full report on every
invocation - verified 2026-09-07 on the MinIO object for job 34226, whose payload
carries `degraded`, `treatment_lost`, `selection_mode`, `filters` and `llm`. But
the archive predating that restart is large and is exactly what a
`GET /jobs/{id}/decision` reader will hit, so the distinction stays load-bearing.

#### Reading it in code

`orchestrator/status.py` provides `lambda_is_degraded()`, `lambda_treatment_lost()`,
`lambda_selection_mode()`, `lambda_warnings()` and `lambda_error()`; the runner's
equivalent is `LambdaSelectJobResult`. Both recompute the two-term gate from
`degraded` + `selection_mode` when `treatment_lost` is absent but the rest of the
report is present, and neither ever falls back to `selection_mode` alone.

"Clean" and "we cannot tell" are different answers, and the booleans cannot carry
both: `lambda_is_degraded()` returns `False` for a payload with no report. When
the difference matters - and for anything reading archived HAR it always does -
use `lambda_report_present()`, or `lambda_verdict()`, which keeps the two apart in
one value. The runner's `LambdaSelectJobResult` exposes the same distinction.

Reading the same verdict off a **job row** rather than a payload:
`job_decision_verdict()` (and `job_decision_reported()` /
`job_decision_degraded()` / `job_decision_treatment_lost()`), with the column
names as `status.DECISION_FIELDS`. They delegate to the helpers above, so there
is one implementation of the rules and two sources for it.

#### The verdict is persisted on the job row (new 2026-09)

Until now the verdict existed in exactly two places: a line in the runner log,
and the `reports/lambda-jobdata-*` object in MinIO. Neither is joinable, and the
one every analysis *does* join on - the `jobs` table - said nothing at all. That
is why 6682 archived decisions (counted 2026-09-07; one more lands with every
`lambdaselect` job) had never been read: answering "did the treatment apply?"
for one job meant fetching an artifact, and for a study meant fetching
thousands.

The runner now copies the verdict onto the status PUT and the API stores it on
`jobs`:

| column | type | source field | meaning |
|---|---|---|---|
| `decision_degraded` | bool, **nullable** | `degraded` | a requested filter did not apply |
| `decision_treatment_lost` | bool, **nullable** | `treatment_lost` | the bot was treated and produced control-arm data - **the alarm** |
| `decision_selection_mode` | varchar(32), nullable | `selection_mode` | `"llm"` / `"random"`. A descriptor. Never branch on it alone, in SQL either. |
| `decision_report` | JSON, nullable | — | `{"filters": {...}, "llm": {...}, "warnings": [...], "applied": [...], "requested": [...]}` |

The two booleans are indexed; the rollups in §3.4
(`jobs_treatment_lost_count`, `jobs_degraded_count`,
`latest_queues_treatment_lost`) are counted off them.

**NULL means unknown. It never means clean, and it never means false.** Three
different jobs are indistinguishable through these columns and all three are
unknown:

- a job that predates the migration (every one of the 31,574 rows already in the
  table);
- a job that never called the decision function at all (a `visit`, a `wait`, a
  `type` job - most jobs);
- a `lambdaselect` job run by a runner that has not been redeployed, or against a
  decision function that emits no report (§ "Legacy payloads" above).

A renderer must show "no verdict", not a green tick, and an analysis must count
unknowns as their own bucket rather than letting them fall out of a `WHERE`
clause (§0). `orchestrator.status.job_decision_verdict()` returns a
`LambdaVerdict` whose `.unknown` is `True` for all three, and it gets there by
rebuilding the payload from the columns and handing it to the same `lambda_*`
helpers that read a MinIO object - one implementation of the rules, two sources,
so a job row and the artifact it came from cannot disagree.

**Reading the raw payload:**

```
GET /api/v1/jobs/{id}/decision
```

serves the parsed `lambda-jobdata` object for that job straight from MinIO. This
is what makes the **historical** verdicts readable without a backfill: the
columns only ever describe jobs run after the runner deploy, while the objects go
back to the start.

**Deploy order: API migration, then GUI, then the runner jar. Never the runner
first.** §0 has the full table and the reasoning; the short version is that the
verdict travels on the same PUT as `status_code`, so a runner ahead of the
migration either loses every verdict silently or risks a rejected status PUT that
leaves the job stuck at `running`. Every intermediate state of that order is safe
for exactly one reason: the columns are nullable and null means unknown, so no
layer is ever forced to invent a verdict it does not have. A `NOT NULL DEFAULT
false` migration would have asserted, at the moment it ran, that all 31,574
historical jobs had applied their treatments - a claim nobody measured, in the
one table everybody joins on. §0 has the counting rule that follows from a
three-state column.

#### Data files are part of this contract

`mainstream_media_channels.csv` must keep its `channel,verified` header, and it
must be **resolvable from the decision function's working directory** - it is
read with a bare relative path. A failure of either kind is swallowed by an
`except Exception` that degrades to the warning `Aversion to mainstream media
channels ignored`, so the `pi=1` arm silently becomes a second control arm while
still returning HTTP 200. See CLAUDE.md, "Local Lambda Server", for the cwd trap
and its current state on the VM.

---

## 8. Breaking Change Checklist

Before making changes to the API or Runner repositories, check if any of these are affected:

### primemover_api

- [ ] Log format in `config/logging.php`
- [ ] Docker container names in `docker-compose.yml`
- [ ] API endpoint routes in `routes/api.php`
- [ ] Authentication response format
- [ ] Context fields in log messages
- [ ] **Job / queue `status_code` vocabulary (§3.4)** - nothing validates it, so a
      change here is silent
- [ ] **`status_message` semantics (§3.4)** - column width and single-line assumption
- [ ] **Field names `status_code` / `status_message` / `started_at` / `finished_at`**
      (§3.4) - renaming any of them breaks every reader

### primemover_runner

- [ ] Logback pattern in `src/main/resources/logback.xml`
- [ ] JAR artifact name in `pom.xml`
- [ ] Directory structure (`config/`, `log/`, `out/har/`)
- [ ] Spring profiles configuration
- [ ] Startup log messages
- [ ] Correlation ID format in MDC
- [ ] **`JobState` / `JoblistState` enum *codes* (§3.4)** - the code, not the Java
      constant name, is what goes on the wire
- [ ] **Whether a degraded outcome maps to `warning` rather than `success`** (§3.4)
- [ ] **The log lines the alert rules match**, all of them in the runner's own
      log: `Full Lambda response payload: {…}` (INFO), `Lambda decision LOST THE
      TREATMENT for job …` (ERROR), `Done running job with failure <id>` (WARN),
      `Exception executing job <id>` (ERROR), `Done running job with warning …`
      (WARN). Renaming any of them makes a Grafana rule read **zero** rather than
      error. Do not move an alerted line to DEBUG either: `primemover.service`
      inherits root, which is DEBUG only under `dev,staging,test,local` - the
      first version of the job-status rules matched a DEBUG line and could never
      have fired under `production`. See
      `infrastructure/grafana/provisioning/alerting/README.md`
- [ ] **Correlation-id spelling in the MDC** - `cr-N|jl-M` becomes `cr-N|q-M`
      (§0). Readers accept both; hard-coded consumers do not

### bot-decision-yt

- [ ] Lambda function name
- [ ] CloudWatch log group name
- [ ] Output JSON structure
- [ ] ZIP file naming pattern
- [ ] **`degraded` / `treatment_lost` / `selection_mode` / `filters` / `llm`
      fields (§7.3)** - dropping or renaming `treatment_lost` removes the only
      unambiguous detector of a silently inert treatment arm, and forces every
      consumer back onto the two-term gate it exists to centralise
- [ ] **The meaning of `filters[...] == "skipped"`** (§7.3) - it means *not
      requested*. Anything that starts emitting it for a filter that was requested
      and failed silently converts a treatment loss into a non-event
- [ ] **The `warnings[]` strings that alert rules match**, in particular
      `Aversion to mainstream media channels ignored`
- [ ] **Relative data-file reads** (`mainstream_media_channels.csv`) - adding one
      adds a cwd dependency (§7.3)

---

## 9. Testing Integration Changes

After making changes to integrated components:

```bash
# 1. Run orchestrator tests
make test

# 2. Check log parsing still works
orchestrator logs query -c api -t 1h
orchestrator logs query -c runner -t 1h

# 3. Verify tracing works
orchestrator trace experiment {id}

# 4. Check component health
orchestrator status -v
orchestrator doctor
```

---

## 10. Version Compatibility

| Orchestrator | API | Runner | Notes |
|--------------|-----|--------|-------|
| 0.1.x | Any | Any | Initial release |

*Update this table when breaking changes are made.*
