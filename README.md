# PrimeMover Orchestrator

Unified control plane for the PrimeMover ecosystem - manage, monitor, and debug all components from a single CLI.

## Overview

The orchestrator provides centralized management for:
- **primemover_api** (Laravel) - Experiment management API with MySQL, Redis
- **primemover_runner** (Java/Spring Boot) - Browser automation runner
- **bot-decision-yt** (Python Lambda) - YouTube video selection
- **Monitoring Stack** - Grafana, Loki, Prometheus, Tempo

## Quick Start (New Developer Onboarding)

The orchestrator is the **entry point** for the PrimeMover ecosystem. One command sets up everything:

```bash
# 1. Clone orchestrator
git clone git@github.com:umatter/primemover-orchestrator.git
cd primemover-orchestrator

# 2. Install Python package
pip install -e .

# 3. Run setup (clones repos, builds, starts everything)
orchestrator setup
```

The `setup` command will:
1. Clone `primemover_api`, `primemover_runner`, `primemover_gui` as siblings
2. Create `.env` configuration
3. Build the runner JAR (requires Maven)
4. Start all components
5. Initialize database with seed data

### Setup Options

```bash
# Full setup (default)
orchestrator setup

# Skip cloning (repos already exist)
orchestrator setup --skip-clone

# Skip building runner JAR
orchestrator setup --skip-build

# Don't start components (manual start later)
orchestrator setup --skip-start

# Custom repository URLs
orchestrator setup --api-repo git@github.com:myorg/api.git
```

### After Setup

```bash
# Check status
orchestrator status

# Start/stop components
orchestrator up
orchestrator down
```

### Access Services

| Service | URL | Credentials |
|---------|-----|-------------|
| PrimeMover API | http://localhost:8090 | - |
| PrimeMover GUI | http://localhost:8090/gui | admin@primemover.example / password |
| API Documentation | http://localhost:8090/api/documentation | - |
| Grafana | http://localhost:3000 | admin / admin |
| Prometheus | http://localhost:9090 | - |

## CLI Command Reference

### Lifecycle Commands

```bash
# Start all components (Monitoring -> API -> Runner)
orchestrator up

# Start specific components
orchestrator up --api --no-runner --no-monitoring

# Stop all components
orchestrator down

# Stop and remove data volumes (WARNING: data loss)
orchestrator down --volumes

# Restart components
orchestrator restart
orchestrator restart --runner  # Just restart runner
```

### Status & Health

```bash
# Quick status overview
orchestrator status

# Detailed status with container health
orchestrator status -v

# Diagnose setup issues
orchestrator doctor
```

### Configuration

```bash
# Initialize .env from template (interactive)
orchestrator config init

# Validate paths and ports
orchestrator config validate

# Show effective configuration
orchestrator config show
orchestrator config show --json
```

### Database Management

```bash
# Run pending migrations
orchestrator db migrate

# Fresh migration (drops all tables)
orchestrator db migrate --fresh

# Migrate and seed
orchestrator db migrate --seed

# Seed database
orchestrator db seed
orchestrator db seed --class=UserSeeder

# Full reset (fresh + seed)
orchestrator db reset

# Show migration status
orchestrator db status

# Clear test data (keeps users)
orchestrator db clear-test
```

### Log Queries

```bash
# Query logs from all components
orchestrator logs query -c runner -t 1h -l error

# Options:
#   -c, --component   all|api|runner|lambda
#   -t, --time-range  15m|1h|24h|7d
#   -l, --level       debug|info|warning|error
#   -s, --search      Search pattern
#   -e, --experiment-id  Filter by experiment
#   -f, --format      table|json|plain

# Get error summary
orchestrator logs errors --time-range 24h

# Watch logs in real-time (tail -f)
orchestrator watch -c all -l info
```

### Execution Tracing

```bash
# Trace experiment execution
orchestrator trace experiment 123
orchestrator trace experiment 123 --jobs  # Include job details

# Trace crawler
orchestrator trace crawler 456

# Trace specific job
orchestrator trace job 789 --include-logs
```

### Lambda Analysis

```bash
# From HAR files (local)
orchestrator lambda invocations -t 1h
orchestrator lambda decisions 123 --group-by crawler
orchestrator lambda show 789  # Show output for job

# From CloudWatch
orchestrator lambda logs -t 1h -s "error"
orchestrator lambda metrics -t 24h
orchestrator lambda errors -t 24h
orchestrator lambda invocation <request_id>
```

### Experiment Management

```bash
# List experiments from API
orchestrator experiment list

# Validate YAML spec
orchestrator experiment validate spec.yaml

# Preview factorial design
orchestrator experiment preview spec.yaml

# Generate API JSON
orchestrator experiment generate spec.yaml -o output.json

# Upload to API
orchestrator experiment upload spec.yaml
orchestrator experiment upload spec.yaml --dry-run
orchestrator experiment upload spec.yaml -e 123  # Add to existing
```

### Dashboard

```bash
# Open Grafana in browser
orchestrator dashboard
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         ORCHESTRATOR CLI                                 │
│                    (orchestrator up/down/status)                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐         │
│  │   API Stack     │  │     Runner      │  │   Monitoring    │         │
│  │  (Docker)       │  │    (Native)     │  │   (Docker)      │         │
│  ├─────────────────┤  ├─────────────────┤  ├─────────────────┤         │
│  │ Apache    :8090 │  │ Java Process    │  │ Grafana   :3000 │         │
│  │ PHP-FPM        │  │ PID tracking    │  │ Loki      :3100 │         │
│  │ MySQL     :3307 │  │                 │  │ Prometheus:9090 │         │
│  │ Redis     :8889 │  │                 │  │ Tempo     :3200 │         │
│  │ Memcached      │  │                 │  │ Promtail       │         │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘         │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

## Configuration

### Environment Variables

Copy `.env.example` to `.env` and customize:

```bash
# Component Paths
PM_API_PATH=../primemover_api
PM_RUNNER_PATH=../primemover_runner

# API Ports
PM_API_HTTP_PORT=8090
PM_API_MYSQL_PORT=3307
PM_API_REDIS_PORT=8889

# Monitoring Ports
GRAFANA_PORT=3000
LOKI_PORT=3100
PROMETHEUS_PORT=9090

# API Credentials
PM_API_BASE_URL=http://localhost:8090/api/v1
PM_API_EMAIL=admin@primemover.example
PM_API_PASSWORD=password

# AWS (for Lambda CloudWatch logs)
AWS_DEFAULT_REGION=eu-central-1
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
LAMBDA_LOG_GROUP=/aws/lambda/bot-decision-yt
```

### Directory Structure

```
primemover-orchestrator/
├── orchestrator/           # Python package
│   ├── cli.py             # CLI commands
│   ├── components/        # Component managers
│   │   ├── api.py         # API Docker Compose
│   │   ├── runner.py      # Runner process
│   │   ├── monitoring.py  # Monitoring stack
│   │   └── manager.py     # Orchestration
│   ├── logs/              # Log aggregation
│   └── tracing/           # Execution tracing
├── grafana/               # Grafana config
│   ├── dashboards/        # Pre-built dashboards
│   └── provisioning/      # Auto-provisioning
├── prometheus/            # Prometheus config
│   └── rules/             # Alert rules
├── docker-compose.yml     # Monitoring stack
└── .env                   # Configuration
```

## MCP Integration

Configure Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "primemover-orchestrator": {
      "command": "/path/to/primemover-orchestrator/scripts/mcp-server.sh",
      "env": {
        "PM_API_BASE_URL": "http://localhost:8090/api/v1",
        "PM_API_EMAIL": "admin@primemover.example",
        "PM_API_PASSWORD": "password"
      }
    }
  }
}
```

### MCP Tools

| Category | Tools |
|----------|-------|
| **Logs** | `query_logs`, `get_error_summary`, `system_status`, `tail_logs` |
| **Tracing** | `trace_experiment`, `trace_crawler`, `trace_job`, `find_failed_executions` |
| **Lambda (HAR)** | `get_lambda_invocations`, `analyze_lambda_decisions` |
| **Lambda (CloudWatch)** | `get_lambda_cloudwatch_logs`, `get_lambda_execution_metrics`, `get_lambda_invocation_details` |
| **Experiments** | `validate_experiment_spec`, `preview_factorial_design`, `convert_spec_to_json`, `upload_experiment` |

## Grafana Dashboards

Pre-built dashboards in `grafana/dashboards/`:

- **Experiment Overview** - API requests, job completion, error rates
- **Lambda Performance** - Invocations, duration, memory, errors
- **Log Explorer** - Full-text search, component filtering

## Alerting Rules

Prometheus alerts in `prometheus/rules/primemover-alerts.yml`:

- **API**: High error rate, API down
- **Runner**: Job failures, no activity
- **Lambda**: High duration, memory issues, throttling
- **Infrastructure**: Loki/Grafana down

## Common Workflows

### Fresh Setup

```bash
# 1. Initialize configuration
orchestrator config init

# 2. Validate setup
orchestrator doctor

# 3. Start components
orchestrator up

# 4. Initialize database
orchestrator db migrate --seed

# 5. Verify
orchestrator status -v
```

### Daily Development

```bash
# Start everything
orchestrator up

# Watch logs while testing
orchestrator watch -c runner -l info

# After changes, restart runner
orchestrator restart --runner --no-api --no-monitoring
```

### Debugging Failed Jobs

```bash
# Find recent errors
orchestrator logs errors -t 1h

# Trace specific experiment
orchestrator trace experiment 123 --jobs

# Check Lambda decisions
orchestrator lambda show <job_id>
```

### Reset Test Environment

```bash
# Clear test data (keeps users)
orchestrator db clear-test

# Or full reset
orchestrator db reset
```

## Development

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Format code
black orchestrator/
ruff check orchestrator/
```

## Troubleshooting

### API won't start
```bash
orchestrator doctor          # Check prerequisites
orchestrator config validate # Verify paths
```

### Database connection errors
```bash
orchestrator status -v       # Check MySQL health
orchestrator db status       # Check migrations
```

### Logs not appearing
```bash
orchestrator config show     # Verify log paths
ls -la $(orchestrator config show --json | jq -r .paths.api)/storage/logs
```

### Port conflicts
```bash
orchestrator config validate  # Shows port status
lsof -i :8090                # Check what's using port
```

## License

MIT
