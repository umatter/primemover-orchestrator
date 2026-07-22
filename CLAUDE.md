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
```

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
- `infrastructure/systemd/` - VM service units (lambda, multilogin, health-check)

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
  `lambda_function.py` inside it) is missing. Recreate with:
  `ln -s /home/primemover/bot-decision-yt/bot-decision-EN orchestrator/lambda_service/decision`
- **cwd matters**: the server runs with cwd set to the `decision/` directory so
  relative file reads (e.g. `mainstream_media_channels.csv`) resolve the same
  way they do under the AWS Lambda task root.
- **Data files are load-bearing**: `mainstream_media_channels.csv` must keep its
  `channel,verified` header — `lambda_function.py` does
  `drop_duplicates(subset=['channel', 'verified'])`. Never round-trip it through
  a spreadsheet application: doing so has previously renamed the header, added a
  trailing empty column, and replaced the `;` in HTML entities with `,`, which
  raises `KeyError` on every invocation with `pi >= 0.5` (the whole
  mainstream-media-aversion treatment arm). Control crawlers never reach that
  branch, so the e2e suite does **not** catch it.
- **Lifecycle**: `LambdaServerComponent` (`orchestrator/components/lambda_server.py`)
  manages it as a detached subprocess. PID is tracked in `.lambda_server.pid`,
  logs go to `log/lambda-server.log`. Component startup order is
  `monitoring → lambda_server → api → runner` (the runner depends on it).
- **Runner side**: the runner selects local vs AWS via `lambda.mode` in its
  `application.properties` (`local` → `POST http://localhost:5001/invoke`).
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

Write-tool conventions (see `docs/specs/mcp-control-surface.md`):
- **Dry-run by default** — every `pm_` tool previews the change; pass `confirm=true` to apply.
- **Local-only** — `_require_local_write_target()` refuses to mutate anything unless the API is
  `localhost:8090`, even with `confirm`.
- All API calls go through `_api_request` (auth at `/login`, supports GET/POST/PUT/DELETE).

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

```yaml
version: "1.0"
experiment:
  name: "My Study"
design:
  factors:
    condition:
      levels: [control, treatment]
      maps_to:
        params.pi: {control: 0, treatment: 0.5}
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
- `primemover-health-check.service` — one-shot boot check running
  `scripts/boot-health-check.sh --quick` after everything else

Helper scripts in `scripts/`:
- `full-startup.sh [--no-runner|--check]` — brings up VNC, Docker, monitoring,
  Multilogin, the Lambda service, and the runner in order (alternative to
  systemd for manual starts)
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
- Check `log/lambda-server.log` (the subprocess logs there, not to the console)
- Verify the `orchestrator/lambda_service/decision` symlink resolves and that
  `decision/lambda_function.py` exists
- If only `pi >= 0.5` crawlers fail, suspect `mainstream_media_channels.csv`
  corruption (see "Local Lambda Server" above)
- Port 5001 in use: stop the stale process (`.lambda_server.pid`) or check the
  `primemover-lambda` systemd unit isn't already running it

**Chrome renderer becomes unresponsive on the headless VM after many jobs**
- Root cause and fix are documented in `docs/chrome-browser-death-fix.txt`
- Software VP9/AV1 decode + a ChromeDriver V8 heap leak saturate the renderer.
  Fix lives in `primemover_runner` (H.264 codec override in `VisitJobHandler.java`
  + `--max-old-space-size=1024` flag), not in this repo
- `scripts/monitor-memory.sh` watches renderer RSS to catch regressions

## Related Repositories

- `primemover_api` - Laravel API (cloned as sibling `../primemover_api`)
- `primemover_runner` - Java runner (cloned as sibling `../primemover_runner`)
- `bot-decision-yt` - Python decision function. Cloned as a sibling and
  **symlinked** into `orchestrator/lambda_service/decision` when running the
  local Lambda server; deployed to AWS Lambda otherwise.

Integration contract details: `docs/integration-contract.md`
