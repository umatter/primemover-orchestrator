# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

PrimeMover Orchestrator is a unified monitoring, debugging, and experiment management platform for the PrimeMover ecosystem. It aggregates logs and metrics from three external components:
- **primemover_api** (Laravel) - Experiment management API
- **primemover_runner** (Java/Spring Boot) - Browser automation runner
- **bot-decision-yt** (Python Lambda) - YouTube video selection

This repo contains two Python packages sharing a monorepo:
- **`orchestrator`** - CLI for lifecycle management, log aggregation, tracing, debugging, and MCP server
- **`experiment_designer`** - YAML DSL for defining experiments with factorial designs

Both are installed as CLI entry points: `orchestrator` and `pmspec` (defined in `pyproject.toml`).

## Build & Test Commands

```bash
# Install in development mode (requires Python 3.10+)
pip install -e ".[dev]"

# Run all unit tests (e2e tests are skipped by default)
pytest

# Run a single test file or specific test
pytest orchestrator/tests/test_correlation.py -v
pytest experiment_designer/tests/test_spec.py::TestSpecParser::test_parse -v

# Run e2e tests (requires live infrastructure)
pytest --e2e

# Coverage
pytest --cov=orchestrator --cov=experiment_designer

# Lint and format (line length: 100)
black orchestrator/ experiment_designer/
ruff check orchestrator/ experiment_designer/ --fix
mypy orchestrator/ experiment_designer/ --ignore-missing-imports

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

**`experiment_designer/`** handles experiment design: parsing YAML specs, expanding factorial designs into crawler configurations, validating against job type schemas, and converting to API-compatible JSON for upload.

### Key Source Locations

- `orchestrator/cli.py` - Click CLI entry point, all subcommands
- `orchestrator/config/settings.py` - Pydantic Settings, loaded from `.env` with `get_settings()` (lru_cached)
- `orchestrator/components/manager.py` - Orchestrates API (Docker), Runner (native Java), Monitoring (Docker)
- `orchestrator/components/lambda_server.py` - Local Lambda server lifecycle (subprocess)
- `orchestrator/lambda_service/server.py` - Flask wrapper around the decision function
- `orchestrator/logs/aggregator.py` - Unified log interface across Loki, files, HAR, CloudWatch
- `orchestrator/tracing/trace_builder.py` - Reconstructs execution traces as hierarchical `TraceSpan` trees
- `orchestrator/debug/job_debugger.py` - Multi-source job diagnostics
- `orchestrator/mcp_server/server.py` - FastMCP server with all tools
- `experiment_designer/spec/converter.py` - YAML-to-JSON conversion pipeline
- `experiment_designer/spec/design_expander.py` - Factorial design expansion
- `experiment_designer/spec/job_registry.py` - Job type definitions and validation
- `scripts/har-drain.sh` - HAR/job-data spool-and-drain sidecar (buffer → Nextcloud)
- `scripts/mlx-proxy-gen.py` - generate N Multilogin sticky-session proxy records (one exit IP per bot)
- `scripts/monitor-memory.sh` - per-component RSS + browser-instance count + disk; `--json` for a sampler
- `scripts/reap-orphan-browsers.sh` - kill Mimic browsers older than a session could be (orphan sweep)
- `scripts/churn-soak.py` - drive many short session lifecycles to expose slow leaks over churn
- `infrastructure/systemd/` - VM service units (lambda, multilogin, health-check, har-drain)

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

### Correlation IDs

All components use a hierarchical correlation ID format for cross-component tracing:
```
exp-{experiment_id}|cr-{crawler_id}|q-{queue_id}|j-{job_id}
```

### Log Aggregation

`LogAggregator` unifies four log sources: Loki (primary), direct file parsing (fallback), HAR files (Lambda outputs from runner), and CloudWatch (Lambda execution logs).

### Local Lambda Server

To avoid AWS Lambda costs, the orchestrator can run the `bot-decision-yt`
decision function locally as a Flask HTTP service that mirrors the AWS Lambda
invocation contract. This is **on by default** (`orchestrator up`); pass
`--no-lambda-local` to fall back to AWS.

- **Service**: `orchestrator/lambda_service/server.py` — exposes `GET /health`
  and `POST /invoke` on `127.0.0.1:5001`. `/invoke` calls
  `select_recommendation(json.dumps(event))` and returns its dict as JSON.
- **Decision code**: imported from `orchestrator/lambda_service/decision`,
  a **symlink** to `bot-decision-yt/bot-decision-EN`. The symlink is a
  prerequisite — `LambdaServerComponent.validate()` errors if it (or
  `lambda_function.py` inside it) is missing. Recreate it **relative**, from the
  repo root, so the checkout stays portable across machines and paths:
  `ln -s ../../../bot-decision-yt/bot-decision-EN orchestrator/lambda_service/decision`
  Note this points at `bot-decision-EN` specifically. The sibling
  `bot-decision-EN-arm` and `bot-decision-DE` directories are stale forks that
  still contain the pre-`364beae` xpath bug; never repoint the symlink at them.
- **cwd matters**: the server runs with cwd set to the `decision/` directory so
  relative file reads (e.g. `mainstream_media_channels.csv`) resolve the same
  way they do under the AWS Lambda task root.
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

MCP tools in `orchestrator/mcp_server/server.py` use `@mcp.tool()` decorator and return `json.dumps(...)`. The orchestrator MCP server is the single consolidated server for all PrimeMover operations (the API repo's MCP server is deprecated).

There are **read/observe** tools (unprefixed: `list_experiments`, `list_crawlers`, `list_queues`, `list_library`, `get_*`, `trace_*`, `find_failed_executions`, `query_logs`, `list_job_types`, …) and **write/control** tools (`pm_` prefix: `pm_create_experiment`, `pm_add_bot`, `pm_create_run`, `pm_add_job`, `pm_set_behaviour`, `pm_activate_run`, `pm_delete`, and the composite `pm_compose_run`).

Write-tool conventions (see `docs/specs/mcp-control-surface.md`). These apply to **every tool that can
mutate**, not only the `pm_`-prefixed ones:
- **Dry-run by default** — previews the change; pass `confirm=true` to apply (`pm_` tools) or
  `dry_run=false` (`upload_experiment`, which predates the convention and keeps its own parameter name).
- **Local-only** — `_require_local_write_target()` refuses to mutate anything unless the API is
  `localhost:8090`, even with `confirm`.
- All API calls go through `_api_request` (auth at `/login`, supports GET/POST/PUT/DELETE) — except
  `upload_experiment`, which uses `requests` directly and therefore has to call the guard itself.

`upload_experiment` was the exception to all three until 2026-08-21: it defaulted to `dry_run=False`
and never called the guard, so it applied by default and could write to a remote API. Both are fixed.
`test_mcp_write_guard.py` now walks the AST for any tool calling a write verb at a non-login endpoint
and asserts it is guarded, so a new tool cannot reintroduce the gap — a name-based check would not
have caught this one, since the tool is not `pm_`-prefixed.

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

# Remote (HTTP mode - binds to 127.0.0.1 by default)
./scripts/mcp-server.sh --http --port 8100
```

For cloud VM → laptop access, prefer an SSH tunnel over binding publicly:

```bash
ssh -L 8100:localhost:8100 user@your-vm-host          # on laptop
claude mcp add --transport http primemover http://localhost:8100/mcp
```

> **Security**: HTTP mode defaults to `127.0.0.1`. Use `--host 0.0.0.0` only behind a firewall.

### Adding a New Log Parser

1. Create parser in `orchestrator/logs/parsers/`
2. Return list of `LogEntry` objects
3. Register in `LogAggregator._query_log_files()`

## Uploading Experiments (JSON Scenario)

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

**Important:** `start_at` must be in the future for the runner to pick up the queue. The API
interprets timestamps as CET (+01:00). Reference implementation:
`orchestrator/e2e/runner.py:_upload_experiment()`.

## Experiment Designer

The experiment designer uses YAML to define experiments with factorial designs: factorial
expansion, reusable templates, parameter distributions, data pools with scoring, and relative
timestamps (`now`, `+5m`).

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
        params.pi: {control: 0, treatment: 1}
  replications: 5
crawlers:
  - from_design: true
    name: "bot_{condition}_{replica:02d}"
    session:
      workflow:
        - visit: https://example.com
        - wait: 5s
```

See `docs/experiment-designer.md` for full documentation.

## Testing

- Tests live in `orchestrator/tests/` and `experiment_designer/tests/`
- E2E tests (files matching `test_e2e*`) are **skipped by default**; pass `--e2e` to pytest to run them
- E2E tests require live infrastructure (API, monitoring stack running)
- Custom markers: `@pytest.mark.e2e`, `@pytest.mark.multilogin`
- Mock external services (API, Loki, CloudWatch) in unit tests
- Shared fixtures in `orchestrator/tests/conftest.py`: `cli_runner`, `mock_settings`, `mock_httpx_client`, `sample_log_entries`

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
  `pytest --cov=orchestrator --cov=experiment_designer` locally.
  Worth knowing what it currently says: **~39% overall**, with
  `mcp_server/server.py` at **0%** — every `pm_` write tool is untested —
  `logs/aggregator.py` at 13% and `tracing/trace_builder.py` at 23%.

## Formatting & Linting

- **Line length**: 100 (black + ruff)
- **Ruff rules**: E, F, W, I, N, UP, B, C4 (E501 ignored)
- **Target Python**: 3.10+
- **Pre-commit hooks**: trailing whitespace, black, ruff, mypy, detect-secrets (`.secrets.baseline`)

## Environment

Configuration via `.env` file (Pydantic Settings). Required credentials with no defaults:
- `PM_API_EMAIL`, `PM_API_PASSWORD` - API authentication
- `GRAFANA_ADMIN_PASSWORD` - Grafana admin

All other settings have sensible defaults (see `orchestrator/config/settings.py`). The API logs in CET timezone; Promtail config handles conversion to UTC for Loki.

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
- `har-drain.sh [--dry-run]` — the spool-and-drain sidecar; tars settled job
  folders and uploads via rclone WebDAV (see `docs/har-spool-drain.md`)
- `multilogin-login.py` — authenticates the Multilogin agent
- `boot-health-check.sh` / `monitor-memory.sh` — health and memory monitoring

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
- Verify the `orchestrator/lambda_service/decision` symlink resolves and that
  `decision/lambda_function.py` exists
- Suspect `mainstream_media_channels.csv` corruption if the mainstream-media
  arm looks inert (see "Local Lambda Server" above). Note it will **not** show
  up as a failure: `pi=1` crawlers keep returning 200 and behave like controls,
  and the only trace is `Aversion to mainstream media channels ignored` in the
  returned `warnings` list. Check with
  `cd ../bot-decision-yt && pytest tests/test_mainstream_media_csv.py`
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
- The V8 heap cap (`--max-old-space-size=512`, `ProfileService.java`) guards a
  ChromeDriver leak and is hardware-independent — keep it on any host
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
- Job data (incl. HAR) is written by the runner's storage backends
  (`FileStorageService` → `export.FileTargetFolder`, plus `AwsS3StorageService`).
  A single failing backend used to short-circuit the rest (dead AWS key → 403 →
  local write skipped); fixed in `primemover_runner` commit `82d6e72`. If jobs
  still land in `WARNING_JOBDATA` ("problems writing jobresult"), the AWS S3 beans
  are 403-ing — remove/repoint them, or run the spool-and-drain sidecar to
  Nextcloud instead (`docs/har-spool-drain.md`).

## Related Repositories

- `primemover_api` - Laravel API (cloned as sibling `../primemover_api`)
- `primemover_runner` - Java runner (cloned as sibling `../primemover_runner`)
- `bot-decision-yt` - Python decision function. Cloned as a sibling and
  **symlinked** into `orchestrator/lambda_service/decision` when running the
  local Lambda server; deployed to AWS Lambda otherwise.

Integration contract details: `docs/integration-contract.md`
