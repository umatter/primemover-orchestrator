# PrimeMover Orchestrator

Unified control plane for the PrimeMover ecosystem - manage, monitor, and debug all components from a single CLI.

## Overview

The orchestrator provides centralized management for:
- **primemover_api** (Laravel) - Experiment management API with MySQL, Redis
- **primemover_runner** (Java/Spring Boot) - Browser automation runner
- **bot-decision-yt** (Python Lambda) - YouTube video selection
- **Monitoring Stack** - Grafana, Loki, Prometheus, Tempo

It also includes the **Experiment Designer** (`pmspec`) - a YAML-based DSL for defining experiments with factorial designs, reusable templates, and automated crawler generation.

## Quick Start: New VM Deployment

Deploy PrimeMover on a fresh Ubuntu VM with a single command:

```bash
# SSH into your VM, then run:
curl -fsSL https://raw.githubusercontent.com/umatter/primemover-orchestrator/master/scripts/setup-server.sh | bash
```

This automated script installs everything: Docker, Java, Maven, XFCE desktop, VNC, Multilogin X, and all PrimeMover components.

**After setup completes (~15-20 min), verify everything works:**
```bash
cd ~/tools/primemover-orchestrator
source venv/bin/activate
orchestrator test          # Run e2e tests (no Multilogin required)
```

**Configure required credentials** (edit `.env`):
```bash
cd ~/tools/primemover-orchestrator
nano .env  # Set PM_API_EMAIL, PM_API_PASSWORD, GRAFANA_ADMIN_PASSWORD
```

**Access services:**

| Service | URL | Credentials |
|---------|-----|-------------|
| PrimeMover GUI | `http://VM-IP:8090/gui` | (set in .env: PM_API_EMAIL/PASSWORD) |
| API Docs | `http://VM-IP:8090/api/documentation` | - |
| Grafana | `http://VM-IP:3000` | admin / (set in .env: GRAFANA_ADMIN_PASSWORD) |

**First-time Multilogin setup (connect via VNC):**
1. VNC to your VM (port 5901, password set during install)
2. Launch Multilogin X from the applications menu
3. Log in with your Multilogin account credentials

**Daily operations:**
```bash
cd ~/tools/primemover-orchestrator
source venv/bin/activate
orchestrator status    # Check health
orchestrator up        # Start all components
orchestrator down      # Stop all components
```

**Use Claude Code remotely from your laptop:**
```bash
# On VM: Start MCP server for remote access
./scripts/mcp-server.sh --http --port 8100

# On laptop: Create SSH tunnel (secure)
ssh -L 8100:localhost:8100 user@your-vm

# On laptop: Add MCP server to Claude Code
claude mcp add --transport http primemover http://localhost:8100/mcp
```

See [Prerequisites](#prerequisites) for manual installation or [Remote MCP Access](#remote-mcp-access-cloud-vm--laptop) for more options.

## Prerequisites

Install these system dependencies before running `make setup`:

### Ubuntu/Debian

```bash
# Required
sudo apt update
sudo apt install -y git docker.io docker-compose python3 python3-venv openjdk-17-jdk maven

# Add yourself to docker group (run docker without sudo)
sudo usermod -aG docker $USER
newgrp docker  # or log out and back in

# Optional: GUI development (not needed - GUI builds with Docker)
# sudo apt install -y nodejs npm

# Optional: Browser automation
wget -qO- https://dl.google.com/linux/linux_signing_key.pub | sudo gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" | sudo tee /etc/apt/sources.list.d/google-chrome.list
sudo apt update && sudo apt install -y google-chrome-stable
```

### macOS

```bash
# Required
brew install git docker openjdk@17 maven python@3.11

# Optional: GUI development (not needed - GUI builds with Docker)
# brew install node

# Optional: Browser automation
brew install --cask google-chrome
```

### Ubuntu Server / VM (with GUI + VNC)

For running browser automation with Multilogin on a headless server, use our automated setup script:

```bash
# SSH into your server, then run:
curl -fsSL https://raw.githubusercontent.com/umatter/primemover-orchestrator/master/scripts/setup-server.sh | bash
```

Or clone first and run locally:
```bash
git clone https://github.com/umatter/primemover-orchestrator.git
cd primemover-orchestrator
./scripts/setup-server.sh
```

This script installs and configures everything via SSH:
- XFCE desktop + TigerVNC server (for Multilogin)
- All prerequisites (Docker, Java, Maven, Node.js, Chrome)
- Multilogin X (browser fingerprint management)
- Clones and runs `make setup` (builds and starts PrimeMover)

After the script completes, PrimeMover is running. Connect via VNC only when you need to:
- Log into Multilogin X (first-time setup)
- Visually monitor browser automation

**Multilogin X First-Time Setup** (connect via VNC):
1. Launch Multilogin X from the applications menu
2. Log in with your Multilogin account credentials
3. For API automation, generate an automation token in your account settings

### Verify Prerequisites

```bash
make prereqs  # Check all prerequisites
```

## Quick Start (New Developer Onboarding)

The orchestrator is the **entry point** for the PrimeMover ecosystem:

```bash
git clone git@github.com:umatter/primemover-orchestrator.git
cd primemover-orchestrator
make prereqs   # Verify prerequisites are installed
make setup     # Full setup: clone repos, build, start
```

This single `make setup` command will:
1. Create a Python virtual environment
2. Install the orchestrator package and dependencies
3. Clone `primemover_api`, `primemover_runner`, `primemover_gui` as siblings
4. Create `.env` configuration
5. Build the runner JAR (requires Maven)
6. Start all components (API, Runner, Monitoring stack)
7. Initialize database with seed data

### Setup Options

```bash
# Full setup (default)
make setup

# Quick setup when repos already exist
make setup-quick

# Or use orchestrator directly for more options:
orchestrator setup --skip-clone     # Skip cloning (repos exist)
orchestrator setup --skip-build     # Skip Maven build
orchestrator setup --skip-start     # Don't start after setup
```

### After Setup

```bash
# Activate the virtual environment (if not already active)
source venv/bin/activate

# Check status
make status          # or: orchestrator status -v

# Start/stop components
make up              # or: orchestrator up
make down            # or: orchestrator down
make restart         # or: orchestrator restart
```

### Access Services

| Service | URL | Credentials |
|---------|-----|-------------|
| PrimeMover API | http://localhost:8090 | - |
| PrimeMover GUI | http://localhost:8090/gui | (set in .env: PM_API_EMAIL/PASSWORD) |
| API Documentation | http://localhost:8090/api/documentation | - |
| Grafana | http://localhost:3000 | admin / (set in .env: GRAFANA_ADMIN_PASSWORD) |
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

### End-to-End Testing

```bash
# Run all e2e tests (verifies setup without Multilogin)
orchestrator test

# Verbose output
orchestrator test -v

# Quick health checks only
orchestrator test --quick

# Filter by test name pattern
orchestrator test -k "api"
orchestrator test -k "monitoring"
```

E2E tests verify:
- **API stack**: Laravel, MySQL, Redis accessible, authentication works
- **Monitoring**: Grafana, Loki, Prometheus healthy
- **Log aggregation**: Log paths exist, queries work
- **Runner**: JAR exists, configuration present

These tests do NOT require Multilogin X - they only test infrastructure.

## Experiment Designer (YAML DSL)

The orchestrator includes a YAML-based Domain-Specific Language for defining experiments with factorial designs, parameter distributions, and data pools.

### Quick Example

```yaml
version: "1.0"

experiment:
  name: "YouTube Recommendation Study"

design:
  factors:
    political_orientation:
      levels: [left, center, right]
      maps_to:
        params.pi: {left: -0.5, center: 0, right: 0.5}
    content_type:
      levels: [news, entertainment]
  replications: 5
  # Generates: 3 × 2 × 5 = 30 crawlers

crawlers:
  - from_design: true
    name: "yt_{political_orientation}_{content_type}_{replica:02d}"
    session:
      start: now  # Relative timestamps: now, +5m, +1h, +1d
      workflow:
        - visit: https://youtube.com
        - wait: 5s
        - template: cookie_consent
```

### pmspec CLI

The `pmspec` command provides the experiment specification tooling:

```bash
# Validate specification
pmspec validate spec.yaml --seed 42

# Preview factorial design expansion
pmspec preview spec.yaml

# Generate API-compatible JSON
pmspec generate spec.yaml -o output.json --seed 42

# Upload to PrimeMover API
pmspec upload spec.yaml --dry-run
pmspec upload spec.yaml --seed 42

# Generate experiment summary
pmspec summary spec.yaml

# Explore available job types
pmspec schema jobs
pmspec schema job lambda_select
```

### Key Features

| Feature | Description |
|---------|-------------|
| **Factorial Designs** | Cross factors with levels, automatic crawler generation |
| **Proportional Levels** | Stratified sampling (e.g., 25%/25%/50% split) |
| **Parameter Distributions** | `uniform`, `normal`, `discrete`, `beta`, `gumbel` |
| **Data Pools** | Select items by scoring expression (e.g., closest to crawler's `pi`) |
| **Templates** | Reusable workflow components |
| **Relative Timestamps** | `now`, `+5m`, `+1h`, `+1d` for session scheduling |
| **Reproducibility** | `--seed` option for deterministic randomization |

### Workflow

1. Describe experiment in plain English
2. Draft YAML spec (use Claude to help translate)
3. Validate: `pmspec validate spec.yaml --seed 42`
4. Preview: `pmspec preview spec.yaml`
5. Upload via **one of**:
   - **CLI**: `pmspec upload spec.yaml` (direct upload)
   - **GUI**: Generate JSON first, then upload via GUI:
     ```bash
     pmspec generate spec.yaml -o output.json --seed 42
     # Then upload output.json via GUI at http://localhost:8090/gui
     ```

See [docs/experiment-designer.md](docs/experiment-designer.md) for full documentation.

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
│  │ MySQL     :3307 │  │      │          │  │ Prometheus:9090 │         │
│  │ Redis     :8889 │  │      │ HTTP     │  │ Tempo     :3200 │         │
│  │ Memcached      │  │      ▼          │  │ Promtail       │         │
│  └─────────────────┘  └──────┼──────────┘  └─────────────────┘         │
│                              │                                           │
└──────────────────────────────┼──────────────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │   AWS Lambda        │
                    │  (bot-decision-yt)  │
                    │                     │
                    │  YouTube video      │
                    │  selection logic    │
                    └─────────────────────┘
```

**Note on Lambda**: The `bot-decision-yt` Lambda runs in AWS, not locally. The Runner invokes it via HTTP during job execution to select YouTube videos. No local setup is required - the orchestrator provides commands to view logs and analyze decisions from the deployed Lambda.

> **Integration Contract**: The orchestrator depends on specific log formats, API endpoints, and directory structures in the API and Runner repositories. Before making changes to those repos, consult [docs/integration-contract.md](docs/integration-contract.md) to avoid breaking the orchestrator.

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

# REQUIRED: API Credentials (no defaults - must be set)
PM_API_BASE_URL=http://localhost:8090/api/v1
PM_API_EMAIL=your-email@example.com
PM_API_PASSWORD=your-password

# REQUIRED: Grafana password (no default - must be set)
GRAFANA_ADMIN_PASSWORD=your-strong-password

# Optional: For health checks (match your docker-compose)
PM_MYSQL_PASSWORD=
PM_REDIS_PASSWORD=

# AWS (for Lambda CloudWatch logs)
AWS_DEFAULT_REGION=eu-central-1
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
LAMBDA_LOG_GROUP=/aws/lambda/bot-decision-yt
```

> **Security Note**: Credentials are no longer hardcoded with defaults. You must configure `PM_API_EMAIL`, `PM_API_PASSWORD`, and `GRAFANA_ADMIN_PASSWORD` before starting services.

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

## AI Integration (Claude)

The orchestrator provides tools for AI-assisted experiment design and monitoring.

### Using Claude Code (CLI)

Claude Code can directly use the `pmspec` and `orchestrator` CLI tools:

```bash
# Design experiments with Claude Code
pmspec validate my-spec.yaml    # Validate YAML spec
pmspec preview my-spec.yaml     # Preview factorial design
pmspec generate my-spec.yaml    # Generate API JSON
pmspec upload my-spec.yaml      # Upload to API

# Monitor and debug
orchestrator logs query -c runner -t 1h
orchestrator trace experiment 123
```

Claude Code doesn't need MCP configuration - it calls CLI tools directly.

### Using Claude Desktop (MCP)

For Claude Desktop, configure the MCP server (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "primemover-orchestrator": {
      "command": "/path/to/primemover-orchestrator/scripts/mcp-server.sh",
      "env": {
        "PM_API_BASE_URL": "http://localhost:8090/api/v1",
        "PM_API_EMAIL": "your-email@example.com",
        "PM_API_PASSWORD": "your-password"
      }
    }
  }
}
```

> **Note**: Replace placeholder credentials with your actual values. Never commit credentials to version control.

### Remote MCP Access (Cloud VM → Laptop)

When PrimeMover is deployed on a cloud VM, you can access the MCP server from Claude Code running on your laptop using HTTP transport:

**1. Start MCP server in HTTP mode on the VM:**

```bash
# Via script (binds to localhost by default - secure)
./scripts/mcp-server.sh --http --port 8100

# To allow external connections (use with SSH tunnel or firewall):
./scripts/mcp-server.sh --http --port 8100 --host 0.0.0.0

# Or via Python directly
python -m orchestrator.mcp_server.server --http --port 8100

# Or via environment variables
MCP_TRANSPORT=http MCP_PORT=8100 ./scripts/mcp-server.sh
```

> **Security Note**: By default, HTTP mode binds to `127.0.0.1` (localhost only). Use `--host 0.0.0.0` only when you have proper network security (SSH tunnel or firewall).

**2. Configure firewall (allow MCP port):**

```bash
# Ubuntu UFW
sudo ufw allow 8100/tcp

# Or use SSH tunnel for secure access (recommended):
ssh -L 8100:localhost:8100 user@your-vm-host
```

**3. Add to Claude Code on your laptop:**

```bash
# If using SSH tunnel (localhost)
claude mcp add --transport http primemover http://localhost:8100/mcp

# If VM has public IP and firewall configured
claude mcp add --transport http primemover http://your-vm-ip:8100/mcp
```

**Security note**: For production deployments, we recommend using an SSH tunnel rather than exposing the MCP port directly to the internet. The SSH tunnel encrypts all traffic and requires authentication.

### MCP Tools

The orchestrator MCP server is the **single, consolidated MCP server** for all PrimeMover operations:

| Category | Tools |
|----------|-------|
| **API - List** | `list_experiments`, `list_all_experiments`, `list_crawlers`, `list_queues` |
| **API - Get** | `get_experiment`, `get_crawler`, `get_queue`, `get_job` |
| **API - Export** | `download_experiment`, `download_report` |
| **Logs** | `query_logs`, `get_error_summary`, `system_status`, `tail_logs` |
| **Tracing** | `trace_experiment`, `trace_crawler`, `trace_job`, `find_failed_executions` |
| **Lambda** | `get_lambda_invocations`, `analyze_lambda_decisions`, `get_lambda_cloudwatch_logs`, `get_lambda_execution_metrics` |
| **Design** | `validate_experiment_spec`, `preview_factorial_design`, `convert_spec_to_json`, `upload_experiment` |
| **Summary** | `summarize_yaml_spec`, `summarize_experiment`, `compare_experiments` |
| **Schema** | `list_job_types`, `get_job_type_schema`, `get_spec_template` |

> **Note**: The `primemover_api` repo has a legacy MCP server that is now deprecated. Use this orchestrator MCP server for all operations.

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

# 5. Verify status
orchestrator status -v

# 6. Run e2e tests (optional - verifies everything works)
orchestrator test
```

**Note:** The `orchestrator test` command runs end-to-end tests that verify all infrastructure is working (API, database, monitoring). These tests do NOT require Multilogin X to be set up.

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
# Comprehensive debug report for a specific job (recommended)
orchestrator debug job <job_id>           # Full report
orchestrator debug job <job_id> -v        # Verbose output
orchestrator debug job <job_id> --export report.json

# Find recent errors
orchestrator logs errors -t 1h

# Trace specific experiment
orchestrator trace experiment 123 --jobs

# Check Lambda decisions
orchestrator lambda show <job_id>
```

The `debug job` command combines data from all sources into a single diagnostic report:
- Job metadata from API (status, timing, errors)
- Lambda decision output (selected video, xpath, warnings)
- HAR analysis (failed/slow HTTP requests)
- CloudWatch Lambda execution metrics
- Related logs from Loki/files

### Reset Test Environment

```bash
# Clear test data (keeps users)
orchestrator db clear-test

# Or full reset
orchestrator db reset
```

## Development

### Using Make (Recommended)

```bash
make install         # Create venv and install with dev dependencies
make hooks           # Install pre-commit hooks

make test            # Run tests
make test-cov        # Run tests with coverage
make format          # Format code (black + ruff --fix)
make lint            # Lint code (ruff)
make typecheck       # Type check (mypy)
make check           # Run all checks (format, lint, typecheck, test)

make clean           # Remove build artifacts and caches
```

### Manual Commands

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Set up pre-commit hooks
pre-commit install

# Run tests
pytest

# Run tests with coverage
pytest --cov=orchestrator --cov=experiment_designer
```

## Docker

Build and run the orchestrator CLI in a container:

```bash
# Build image
make docker-build    # or: docker build -t primemover-orchestrator .

# Run CLI commands
make docker-run ARGS="--help"
make docker-run ARGS="status"

# Or directly with docker:
docker run --rm primemover-orchestrator --help

# Run with environment variables
docker run --rm \
  -e PM_API_BASE_URL=http://host.docker.internal:8090/api/v1 \
  primemover-orchestrator experiment list

# Mount local .env file
docker run --rm \
  -v $(pwd)/.env:/app/.env:ro \
  primemover-orchestrator config show
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
