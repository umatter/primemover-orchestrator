# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

PrimeMover Orchestrator is a unified monitoring, debugging, and experiment management platform for the PrimeMover ecosystem. It aggregates logs and metrics from three components:
- **primemover_api** (Laravel) - Experiment management API
- **primemover_runner** (Java/Spring Boot) - Browser automation runner
- **bot-decision-yt** (Python Lambda) - YouTube video selection

## Quick Reference

### CLI Commands

```bash
# Setup (new developer onboarding)
orchestrator setup                  # Full setup: clone repos, build, start
orchestrator setup --skip-clone     # Skip cloning (repos already exist)
orchestrator setup --skip-build     # Skip Maven build
orchestrator setup --skip-start     # Don't start after setup

# Lifecycle
orchestrator up                     # Start all components
orchestrator up --no-runner         # Start without runner
orchestrator down                   # Stop all components
orchestrator down --volumes         # Stop and remove data
orchestrator restart                # Restart all
orchestrator restart --runner       # Restart just runner

# Status
orchestrator status -v              # System health check
orchestrator doctor                 # Diagnose setup issues
orchestrator config show            # Show configuration

# Database
orchestrator db migrate --seed      # Run migrations + seed
orchestrator db reset               # Fresh migration + seed
orchestrator db init                # Full Laravel initialization
orchestrator db clear-test          # Clear test data (keeps users)

# Logs
orchestrator logs query -c api -l error -t 1h
orchestrator logs errors --time-range 24h
orchestrator watch -c all -e <experiment_id>

# Tracing
orchestrator trace experiment <id>
orchestrator trace crawler <id>
orchestrator trace job <id>

# Lambda (HAR files)
orchestrator lambda invocations -t 1h
orchestrator lambda decisions <experiment_id>
orchestrator lambda show <job_id>

# Lambda (CloudWatch)
orchestrator lambda logs -t 1h
orchestrator lambda metrics
orchestrator lambda errors
orchestrator lambda invocation <request_id>

# Debug (comprehensive job diagnostics)
orchestrator debug job <id>              # Full debug report
orchestrator debug job <id> -v           # Verbose output
orchestrator debug job <id> --export report.json
orchestrator debug job <id> --no-cloudwatch

# E2E Tests (verify setup without Multilogin)
orchestrator test                        # Run all e2e tests
orchestrator test -v                     # Verbose output
orchestrator test --quick                # Quick health checks only
orchestrator test -k "api"               # Filter by pattern

# Full E2E Test (with Multilogin browser automation)
orchestrator e2e                         # Run full browser automation test
orchestrator e2e -v                      # Verbose output
orchestrator e2e --timeout 300           # Custom timeout (default: 180s)

# Experiments
orchestrator experiment validate spec.yaml
orchestrator experiment preview spec.yaml
orchestrator experiment generate spec.yaml -o out.json
orchestrator experiment upload spec.yaml --dry-run
orchestrator experiment list

# Experiment Spec CLI (pmspec) - alternative to `orchestrator experiment`
pmspec validate spec.yaml --seed 42      # Validate with reproducible randomization
pmspec preview spec.yaml                  # Preview factorial design expansion
pmspec generate spec.yaml -o out.json     # Generate API JSON
pmspec upload spec.yaml --dry-run         # Upload to API
pmspec summary spec.yaml                  # Generate experiment summary
pmspec schema jobs                        # List all job types
pmspec schema job <type>                  # Show job type details
```

### Running Tests

```bash
# Run all tests
pytest

# Run specific test module
pytest orchestrator/tests/test_correlation.py -v
pytest experiment_designer/tests/test_spec.py -v

# Run with coverage
pytest --cov=orchestrator --cov=experiment_designer
```

### Development Setup

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install in development mode
pip install -e ".[dev]"

# Set up pre-commit hooks
pre-commit install

# Format code
black orchestrator/ experiment_designer/
ruff check orchestrator/ experiment_designer/ --fix

# Type check
mypy orchestrator/ experiment_designer/ --ignore-missing-imports
```

### Docker

```bash
docker build -t primemover-orchestrator .
docker run --rm primemover-orchestrator --help
```

## Architecture

```
orchestrator/
├── cli.py                    # Click-based CLI (setup, up, down, etc.)
├── config/
│   └── settings.py           # Pydantic settings from .env
├── components/
│   ├── api.py                # API Docker Compose management
│   ├── runner.py             # Runner process management (native Java)
│   ├── monitoring.py         # Grafana/Loki/Prometheus stack
│   └── manager.py            # Orchestrates all components
├── clients/
│   ├── loki.py               # Loki log queries
│   └── cloudwatch.py         # AWS CloudWatch queries
├── logs/
│   ├── aggregator.py         # Unified log interface
│   └── parsers/
│       ├── laravel.py        # Laravel log parsing
│       └── runner.py         # Spring Boot log parsing
├── tracing/
│   ├── correlation.py        # Correlation ID utilities
│   └── trace_builder.py      # Execution trace reconstruction
├── debug/
│   ├── har_parser.py         # HAR file selective parsing
│   └── job_debugger.py       # Multi-source job diagnostics
├── e2e/
│   ├── runner.py             # End-to-end test runner
│   ├── reporter.py           # Test result formatting
│   └── test_experiment.json  # Test experiment template
└── mcp_server/
    └── server.py             # FastMCP server for Claude

experiment_designer/          # YAML experiment DSL
├── cli.py                    # pmspec CLI
├── spec/
│   ├── parser.py             # YAML parsing
│   ├── converter.py          # JSON conversion
│   ├── design_expander.py    # Factorial design expansion
│   ├── distributions.py      # Parameter distributions
│   ├── data_pools.py         # Data pool scoring
│   ├── job_registry.py       # Job type definitions
│   └── validator.py          # Spec validation
└── summary/                  # Experiment summary generation

infrastructure/               # Docker service configs
├── grafana/provisioning/     # Dashboard provisioning
├── loki/                     # Loki config
├── prometheus/               # Prometheus config
├── promtail/                 # Log collection config
└── tempo/                    # Distributed tracing config
```

## Key Concepts

### Correlation IDs

All components use a hierarchical correlation ID format:
```
exp-{experiment_id}|cr-{crawler_id}|q-{queue_id}|j-{job_id}
```

This allows tracing requests across API, Runner, and Lambda.

### Log Aggregation

The `LogAggregator` class unifies logs from multiple sources:
1. **Loki** - Primary source when available
2. **Files** - Direct log file parsing (fallback)
3. **HAR** - Lambda outputs from runner captures
4. **CloudWatch** - Lambda execution logs

### Trace Building

The `TraceBuilder` class reconstructs execution traces by:
1. Fetching entity data from PrimeMover API
2. Querying related logs from Loki/files
3. Building hierarchical `TraceSpan` structures
4. Computing duration, status, and errors

## Common Tasks

### Adding a New CLI Command

1. Add command function in `orchestrator/cli.py`
2. Use Click decorators for options
3. Import required modules lazily to improve CLI startup time

```python
@cli.command()
@click.option("-t", "--time-range", default="1h")
@click.pass_context
def my_command(ctx: click.Context, time_range: str) -> None:
    """Command description."""
    from orchestrator.some_module import SomeClass
    # ... implementation
```

### MCP Server

The orchestrator provides the **single, consolidated MCP server** for all PrimeMover operations.
The API repo's MCP server is deprecated - use this one for all operations.

**Available MCP Tools:**

| Category | Tools |
|----------|-------|
| **API - List** | `list_experiments`, `list_all_experiments`, `list_crawlers`, `list_queues` |
| **API - Get** | `get_experiment`, `get_crawler`, `get_queue`, `get_job` |
| **API - Export** | `download_experiment`, `download_report` |
| **Logs** | `query_logs`, `get_error_summary`, `system_status`, `tail_logs` |
| **Tracing** | `trace_experiment`, `trace_crawler`, `trace_job`, `find_failed_executions` |
| **Lambda** | `get_lambda_invocations`, `analyze_lambda_decisions`, `get_lambda_cloudwatch_logs` |
| **Design** | `validate_experiment_spec`, `preview_factorial_design`, `convert_spec_to_json`, `upload_experiment` |
| **Summary** | `summarize_yaml_spec`, `summarize_experiment`, `compare_experiments` |
| **Schema** | `list_job_types`, `get_job_type_schema`, `get_spec_template` |

**Running the MCP server:**
```bash
# Local (stdio mode - default)
./scripts/mcp-server.sh

# Remote (HTTP mode - binds to localhost by default)
./scripts/mcp-server.sh --http --port 8100

# Remote with external access (use with SSH tunnel or firewall)
./scripts/mcp-server.sh --http --port 8100 --host 0.0.0.0
```

**Remote MCP Access (Cloud VM → Laptop):**

When deployed on a cloud VM, use HTTP transport:

```bash
# On VM: Start in HTTP mode (localhost only by default - secure)
./scripts/mcp-server.sh --http --port 8100

# On laptop: Create SSH tunnel (recommended)
ssh -L 8100:localhost:8100 user@your-vm-host

# On laptop: Add to Claude Code
claude mcp add --transport http primemover http://localhost:8100/mcp
```

> **Security Note**: HTTP mode defaults to `127.0.0.1`. Use `--host 0.0.0.0` only with proper network security.

### Adding a New MCP Tool

1. Add function in `orchestrator/mcp_server/server.py`
2. Use `@mcp.tool()` decorator
3. Return JSON-serialized results

```python
@mcp.tool()
def my_tool(param: str = "default") -> str:
    """Tool description for Claude."""
    try:
        # ... implementation
        return json.dumps({"result": "..."})
    except Exception as e:
        return json.dumps({"error": str(e)})
```

### Adding a New Log Parser

1. Create parser in `orchestrator/logs/parsers/`
2. Return list of `LogEntry` objects
3. Register in `LogAggregator._query_log_files()`

### Adding a New Grafana Dashboard

1. Create JSON in `grafana/dashboards/`
2. Use consistent UIDs: `primemover-{name}`
3. Add variables for filtering
4. Update dashboard provisioning if needed

## Environment Variables

### Required Credentials (No Defaults)

These must be set in `.env` before starting services:

| Variable | Description |
|----------|-------------|
| `PM_API_EMAIL` | API login email (required) |
| `PM_API_PASSWORD` | API login password (required) |
| `GRAFANA_ADMIN_PASSWORD` | Grafana admin password (required) |
| `PM_MYSQL_PASSWORD` | MySQL password for health checks (optional) |
| `PM_REDIS_PASSWORD` | Redis password for health checks (optional) |

### Service Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ENV` | development | Environment name |
| `GRAFANA_URL` | http://localhost:3000 | Grafana URL |
| `LOKI_URL` | http://localhost:3100 | Loki URL |
| `PROMETHEUS_URL` | http://localhost:9090 | Prometheus URL |
| `TEMPO_URL` | http://localhost:3200 | Tempo URL |
| `PM_API_BASE_URL` | http://localhost:8090/api/v1 | PrimeMover API |
| `PM_API_LOG_PATH` | ../primemover_api/storage/logs | API log directory |
| `PM_RUNNER_LOG_PATH` | ../primemover_runner/.../log | Runner log directory |
| `PM_RUNNER_HAR_PATH` | ../primemover_runner/.../output | HAR output directory |
| `AWS_DEFAULT_REGION` | eu-central-1 | AWS region |
| `LAMBDA_LOG_GROUP` | /aws/lambda/bot-decision-yt | CloudWatch log group |
| `LAMBDA_FUNCTION_NAME` | bot-decision-yt | Lambda function name |

## Experiment Designer

The experiment designer uses YAML to define experiments with factorial designs.

### Key Features
- Factorial design expansion
- Reusable templates
- Parameter distributions
- Data pools with scoring
- Relative timestamps (`now`, `+5m`)

### Quick Example

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

## Testing Notes

- Use `pytest` for running tests
- Mock external services (API, Loki, CloudWatch)
- Use `@pytest.fixture` for test data
- Tests are in `orchestrator/tests/` and `experiment_designer/tests/`

## Common Issues

**ImportError: No module named 'mcp'**
- The MCP library is optional for the MCP server
- Install with: `pip install mcp`

**CloudWatch not available**
- Ensure AWS credentials are configured
- Check `AWS_DEFAULT_REGION` is set
- Verify Lambda log group exists

**Loki connection refused**
- Start Loki: `docker-compose up -d loki`
- Check `LOKI_URL` configuration

**Grafana won't start**
- Set `GRAFANA_ADMIN_PASSWORD` in `.env` (required, no default)
- Check Docker logs: `docker logs pm-orchestrator-grafana`

**API authentication failed**
- Verify `PM_API_EMAIL` and `PM_API_PASSWORD` are set in `.env` (no defaults)
- Check API is running and accessible
- Ensure credentials match those configured in the API's database

## CLI Entry Points

Two CLI tools are installed via `pip install -e .`:
- **`orchestrator`** - Main CLI for lifecycle management, logs, tracing, database ops
- **`pmspec`** - Experiment specification tool (YAML DSL for experiment design)

Both are defined in `pyproject.toml` under `[project.scripts]`.

## Related Repositories

- `primemover_api` - Laravel API (experiment management)
- `primemover_runner` - Java browser automation
- `bot-decision-yt` - Lambda video selection
