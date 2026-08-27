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
the contract, because two of the pairings record *false* data rather than merely
incomplete data.

---

## 0. Deploy Order

**Status:** Critical - **normative**, for as long as the 2026-08-27 error-handling
change is only half deployed.

Nothing in that change is deployed at the time of writing: the runner jar is not
rebuilt, `primemover-lambda` has not been restarted, the GUI image is not rebuilt,
promtail and Grafana have not been restarted. The five components will therefore
run mixed versions for a while. This order is the one in which a mismatch loses
*information* instead of asserting something *false*.

| # | deploy | how | why it sits here |
|---|---|---|---|
| 1 | **`bot-decision-yt` → local Lambda server** | `sudo systemctl restart primemover-lambda` | The source of `degraded` / `treatment_lost` / `filters` (§7.3). Nothing downstream can report a degradation the payload does not carry. The restart also picks up `ensure_decision_cwd()`, which is what makes the `pi=1` arm work on this VM at all - so this step **changes what the treatment does**, see below. |
| 2 | **promtail** | `docker compose restart promtail` (from the repo root) | Runner-line regex and the `job.id` MDC key. Ship the labels before anything is keyed on them; a restart only affects lines read after it. |
| 3 | **runner jar** | build in a **throwaway clone** (never in the live tree - it executes from `target/`), then `sudo systemctl restart primemover-runner` while nothing is in flight | Turns the payload's report into `status_code=warning` (§3.4) and into the WARN/ERROR outcome lines the alert rules read. Needs (1) to have anything to read. |
| 4 | **API** | *nothing to deploy* | It stores whatever `status_code` it is sent - `nullable`, no `in:` rule - and this change adds no migration. Version-agnostic, which is why it is not a gate. |
| 5 | **GUI image** | rebuild, repoint the pin | Renders the `warning` pill and the real `status_message`. Harmless early (the value simply never arrives); pointless before (3). |
| 6 | **Grafana** | `docker compose restart grafana` (from the repo root) | Provisioning is read at startup **only**. Last, because every rule keys on lines steps 1-3 produce. |

### Pairings that produce WRONG data rather than missing data

- **(1) without (3) - new decision function, old runner jar. This is the one to
  avoid.** The payload says `treatment_lost: true`; the deployed jar neither reads
  the field nor owns a `warning` code (its `JobState` maps `WARNING` and
  `WARNING_JOBDATA` onto `"success"`). The API therefore records **`success` for a
  session that carries no treatment signal**, and every analysis that joins on job
  status - which is most of them - reads it as clean treated data. The
  contradicting evidence exists only inside a HAR payload nobody joins on. Keep
  the window between (1) and (3) short and treat every `lambdaselect` job run
  inside it as unverified.
- **(3) without (1) is the safe direction.** `LambdaSelectJobResult` reads a
  payload with no degradation report as *unknown*, not degraded, and keeps
  reporting `success` exactly as it did before. You lose the new signal; you are
  not told anything false.
- **(6) before (1)-(3) - Grafana armed first.** Every rule evaluates against lines
  that cannot exist yet, and `noDataState: OK` paints that green. A green board
  over a window that was never measured is a wrong answer, not a missing one.
- **Restarting `primemover-lambda` in the middle of a run changes the science.**
  Until that restart the server keeps the cwd its systemd unit handed it and the
  `pi=1` mainstream-media arm is inert (§7.3); after it, the arm works. A run
  straddling the restart holds bots from two different treatments under one
  experiment id. Restart between runs, not during one.

### The correlation-id spelling changes at step (3)

The deployed jar writes `correlationId=cr-244|jl-838`; the rebuilt one writes the
canonical `cr-244|q-838`, plus `|j-<id>` for the duration of a job (§1). **Both
forms are handled by the query layer** - `correlation_line_filters()` in
`orchestrator/clients/loki.py` emits a filter for each spelling, and the entry
parser matches `(?:q|jl)-(\d+)` - so a search by id keeps working across the
boundary and over archived logs. Anything that hard-codes one spelling does not;
the Grafana alert-rule annotations name both, and
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

`running` | `success` | `failure` | `skipped`. A queue has no `warning` state: a
run containing a degraded job is still a run that finished, and the degradation
is recorded per job where it can be acted on.

#### `warning` (new 2026-08-27)

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

Readers must accept both. This is not hypothetical: on the ex63 database today
the `jobs` table holds **256 rows of `failed`** (all written 2026-06-19) beside
**170 of `failure`** (2026-08-18 onward), so any query written as
`status_code = 'failure'` silently under-reports historical failures by more than
half. `skpped` currently has 0 rows, but the enum still emits it, so it can
reappear at any time.

An unrecognised value must be treated as **unknown**, never coerced to success.

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

#### Note for API maintainers

The OpenAPI annotation on `RunnerJobController::update` documents a **sixth**
vocabulary that matches nothing in this contract or in the database -
`{pending, running, completed, failed, cancelled}`. It is documentation only and
generates no validation, but it will mislead anyone who trusts it. It should be
corrected to the five values above.

---

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
`selection_mode`, no `filters`, no `treatment_lost` - came from the decision
function currently deployed. Readers must report it as **unknown**. Reading
absence as degradation turns every archived HAR into an incident; reading it as
health asserts something nobody measured.

All 358 payloads on this box today are exactly that shape - their key set is
`{present, procedure, selected, timestamps, warnings, xpath}` - so this is the
normal case until §0 step (1) is done, not an edge case.

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
