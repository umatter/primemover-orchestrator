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
- `orchestrator/logs/aggregator.py` - Unified log interface across Loki, files, HAR, CloudWatch
- `orchestrator/tracing/trace_builder.py` - Reconstructs execution traces as hierarchical `TraceSpan` trees
- `orchestrator/debug/job_debugger.py` - Multi-source job diagnostics
- `orchestrator/mcp_server/server.py` - FastMCP server with all tools
- `experiment_designer/spec/converter.py` - YAML-to-JSON conversion pipeline
- `experiment_designer/spec/design_expander.py` - Factorial design expansion
- `experiment_designer/spec/job_registry.py` - Job type definitions and validation

### Correlation IDs

All components use a hierarchical correlation ID format for cross-component tracing:
```
exp-{experiment_id}|cr-{crawler_id}|q-{queue_id}|j-{job_id}
```

### Log Aggregation

`LogAggregator` unifies four log sources: Loki (primary), direct file parsing (fallback), HAR files (Lambda outputs from runner), and CloudWatch (Lambda execution logs).

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

### Adding a New Log Parser

1. Create parser in `orchestrator/logs/parsers/`
2. Return list of `LogEntry` objects
3. Register in `LogAggregator._query_log_files()`

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

## Related Repositories

- `primemover_api` - Laravel API (cloned as sibling `../primemover_api`)
- `primemover_runner` - Java runner (cloned as sibling `../primemover_runner`)
- `bot-decision-yt` - AWS Lambda (remote only, not cloned locally)

Integration contract details: `docs/integration-contract.md`
