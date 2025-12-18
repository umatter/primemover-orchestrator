# Log Aggregation

The PrimeMover Orchestrator provides unified log querying across all ecosystem components.

## Security Considerations

### Docker Socket Access

Promtail mounts the Docker socket (`/var/run/docker.sock`) in read-only mode for container log discovery. This has security implications:

- **What it exposes**: Promtail can read container metadata, logs, and Docker daemon information
- **Risk level**: Medium - read-only access limits damage but exposes operational data
- **Mitigation applied**:
  - Read-only mount
  - Dropped all capabilities except `DAC_READ_SEARCH`
  - `no-new-privileges` security option enabled

**For high-security environments**, consider:
1. Removing Docker socket mount and using file-based log collection only
2. Running Promtail on a dedicated log aggregation network
3. Using Docker's logging driver to send logs directly to Loki

### Query Input Validation

All log queries are validated and escaped to prevent LogQL injection:
- Search strings are escaped using `escape_logql_string()`
- Component names are validated against a whitelist
- ID parameters are validated as integers

## Overview

The `LogAggregator` class aggregates logs from multiple sources:

1. **Loki** - Primary log storage (when available)
2. **Files** - Direct log file parsing (fallback)
3. **HAR Files** - Lambda outputs from runner captures
4. **CloudWatch** - Lambda execution logs from AWS
5. **Docker** - Container stdout/stderr via Docker socket

## Supported Components

| Component | Type | Source | Description |
|-----------|------|--------|-------------|
| `api` | Application | Loki/Files | Laravel API logs |
| `runner` | Application | Loki/Files | Java Runner logs |
| `lambda` | Application | CloudWatch/HAR | AWS Lambda execution & decisions |
| `apache` | Infrastructure | Loki/Docker | Web server access & error logs |
| `mysql` | Infrastructure | Loki/Docker | Database error & slow query logs |
| `redis` | Infrastructure | Loki/Docker | Cache server logs |
| `php` | Infrastructure | Loki/Docker | PHP-FPM error logs |
| `memcached` | Infrastructure | Docker | Memcached logs |

## Log Sources

### Loki

When Loki is available, it serves as the primary log source:

```python
from orchestrator.logs.aggregator import LogAggregator

aggregator = LogAggregator()

# Check if Loki is available
if aggregator.is_loki_available():
    result = aggregator.query(
        component="api",
        level="error",
        time_range="1h",
        limit=100
    )
```

**Loki Labels:**
- `component` - Source component (api, runner, apache, mysql, redis, php, etc.)
- `level` - Log level (debug, info, warning, error)
- `log_type` - Log type (access, error, slow_query for some components)
- `experiment_id` - Experiment ID (when available)
- `crawler_id` - Crawler ID (when available)
- `job_id` - Job ID (when available)

### File-based Logs

When Loki is unavailable, logs are read directly from files:

**API Logs (Laravel):**
- Path: `PM_API_LOG_PATH` (default: `../primemover_api/storage/logs`)
- Format: Laravel log format (`[YYYY-MM-DD HH:MM:SS] environment.LEVEL: message`)

**Runner Logs (Spring Boot):**
- Path: `PM_RUNNER_LOG_PATH` (default: `../primemover_runner/.../log`)
- Format: Logback pattern (`YYYY-MM-DD HH:MM:SS.mmm LEVEL ...`)

### Job Reports (Lambda Output)

The Runner writes job reports containing Lambda decision outputs:

- Path: `PM_RUNNER_HAR_PATH` (default: `../primemover_runner/.../output`)
- Structure:
  ```
  {job_id}/
  ├── *.har              # Browser HTTP traffic (HAR format)
  ├── screenshot*.png    # Page screenshots
  └── *lambda*.zip       # Lambda output
      └── output.json    # Decision details (selected video, scoring, etc.)
  ```

The orchestrator extracts `output.json` from the Lambda zip files to show decision details.

### CloudWatch Logs

Lambda execution logs from AWS CloudWatch:

- Log Group: `LAMBDA_LOG_GROUP` (default: `/aws/lambda/bot-decision-yt`)
- Requires AWS credentials

### Docker Container Logs

Infrastructure components (MySQL, Redis, Apache, PHP, Memcached) running in Docker containers are collected via the Docker socket:

- Promtail uses Docker service discovery
- Containers are identified by `com.docker.compose.project=primemover` label
- Logs are tagged with `component` based on container name
- No file mounts required for container stdout/stderr

**Note**: This requires the Docker socket to be mounted in Promtail and the primemover-network to be accessible.

## CLI Usage

### Query Logs

```bash
# Query all components
orchestrator logs query -t 1h

# Filter by component (application)
orchestrator logs query -c api -t 24h
orchestrator logs query -c runner -l error
orchestrator logs query -c lambda -t 1h

# Filter by component (infrastructure)
orchestrator logs query -c apache -t 1h        # Web server logs
orchestrator logs query -c mysql -l error       # Database errors
orchestrator logs query -c redis -t 24h         # Cache logs
orchestrator logs query -c php -l error         # PHP-FPM errors

# Search pattern
orchestrator logs query -s "connection failed"

# Filter by experiment
orchestrator logs query -e 123 -t 24h

# Filter by crawler
orchestrator logs query --crawler-id 456

# Filter by job
orchestrator logs query --job-id 789

# Output formats
orchestrator logs query -f plain   # Default
orchestrator logs query -f table
orchestrator logs query -f json
```

### Error Summary

```bash
# Get error summary for last 24h
orchestrator logs errors -t 24h

# Filter by component
orchestrator logs errors -c runner
```

### Watch Logs (Real-time)

```bash
# Watch all logs
orchestrator watch

# Watch specific component
orchestrator watch -c api

# Watch with filter
orchestrator watch -e 123 -l warning
```

## Python API

### Basic Query

```python
from orchestrator.logs.aggregator import LogAggregator

aggregator = LogAggregator()

result = aggregator.query(
    component="runner",      # api, runner, lambda, or None for all
    level="error",           # debug, info, warning, error, or None
    search="timeout",        # Text pattern
    experiment_id=123,       # Filter by experiment
    crawler_id=456,          # Filter by crawler
    job_id=789,              # Filter by job
    time_range="1h",         # 15m, 1h, 24h, 7d
    limit=100,               # Max results
    include_lambda=True      # Include Lambda HAR/CloudWatch
)

print(f"Found {result.total_count} entries")
print(f"Sources: {result.sources}")

for entry in result.entries:
    print(f"{entry.timestamp} [{entry.level}] {entry.component}: {entry.message}")

aggregator.close()
```

### Lambda Output

```python
# Get full Lambda output for a job
output = aggregator.get_lambda_output(job_id=789)

if output:
    print(f"Selected: {output['selected']['title']}")
    print(f"XPath: {output['xpath']}")
```

### Real-time Tailing

```python
# Tail logs in real-time
for entry in aggregator.tail(component="api", level="error"):
    print(f"{entry.timestamp}: {entry.message}")
```

## MCP Tools

### query_logs

```json
{
  "component": "api",
  "time_range": "1h",
  "level": "error",
  "search": "pattern",
  "experiment_id": 123,
  "limit": 100
}
```

### get_error_summary

```json
{
  "time_range": "24h",
  "component": "all"
}
```

### tail_logs

```json
{
  "component": "runner",
  "filter_text": "error",
  "lines": 50
}
```

## Log Entry Structure

```python
@dataclass
class LogEntry:
    timestamp: datetime        # Log timestamp
    component: str             # Source component
    level: str                 # Log level
    message: str               # Log message
    labels: dict               # Additional labels
    raw: str                   # Raw log line

    # Correlation
    experiment_id: Optional[int]
    crawler_id: Optional[int]
    job_id: Optional[int]
```

## Aggregated Result Structure

```python
@dataclass
class AggregatedResult:
    entries: list[LogEntry]    # Log entries
    sources: dict[str, int]    # Source -> count
    total_count: int           # Total entries
    query_params: dict         # Query parameters used
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LOKI_URL` | http://localhost:3100 | Loki server URL |
| `PM_API_LOG_PATH` | ../primemover_api/storage/logs | API log directory |
| `PM_RUNNER_LOG_PATH` | ../primemover_runner/.../log | Runner log directory |
| `PM_RUNNER_HAR_PATH` | ../primemover_runner/.../output | HAR output directory |
| `PM_MYSQL_LOG_PATH` | ../primemover_api/storage/logs/mysql | MySQL log directory |
| `PM_APACHE_LOG_PATH` | ../primemover_api/storage/logs/apache | Apache log directory |
| `PM_PHP_LOG_PATH` | ../primemover_api/storage/logs/php | PHP-FPM log directory |

## Log Parsers

### Laravel Log Parser

Parses Laravel log format:
```
[2024-01-15 10:30:45] production.ERROR: Connection failed {"exception":"..."}
```

Features:
- Extracts timestamp, level, message
- Parses JSON context data
- Handles multi-line stack traces

### Spring Boot Log Parser

Parses Logback format:
```
2024-01-15 10:30:45.123 ERROR 12345 --- [main] c.p.r.JobRunner : Job failed
```

Features:
- Extracts timestamp, level, logger, message
- Handles thread and process info
- Parses correlation IDs from MDC

## Debug Command

For comprehensive job diagnostics, use the debug command which combines log aggregation with other data sources:

```bash
# Full debug report for a job
orchestrator debug job 12345

# Verbose output with more details
orchestrator debug job 12345 -v

# Export to JSON for further analysis
orchestrator debug job 12345 --export report.json

# Skip certain data sources
orchestrator debug job 12345 --no-cloudwatch --no-har
```

The debug command collects:
- **Job metadata** from API (status, timing, experiment/crawler info)
- **Lambda decision** output (selected video, xpath, scoring, warnings)
- **HAR analysis** (failed HTTP requests, slow requests, domain stats)
- **Logs** from Loki/files and CloudWatch

This is the recommended approach for debugging failed jobs, as it provides a unified view of all relevant information.

## Best Practices

1. **Use correlation IDs**: Include experiment/crawler/job IDs for traceability
2. **Query specific components**: Avoid querying all components when possible
3. **Limit time ranges**: Shorter ranges improve performance
4. **Use search patterns**: Text search narrows results effectively
5. **Close aggregator**: Call `aggregator.close()` to release resources
6. **Use debug command for jobs**: For job-specific issues, `orchestrator debug job` provides comprehensive diagnostics
