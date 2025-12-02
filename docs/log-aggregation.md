# Log Aggregation

The PrimeMover Orchestrator provides unified log querying across all ecosystem components.

## Overview

The `LogAggregator` class aggregates logs from multiple sources:

1. **Loki** - Primary log storage (when available)
2. **Files** - Direct log file parsing (fallback)
3. **HAR Files** - Lambda outputs from runner captures
4. **CloudWatch** - Lambda execution logs from AWS

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
- `component` - Source component (api, runner, lambda)
- `level` - Log level (debug, info, warning, error)
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

### HAR Files (Lambda Output)

Lambda decision outputs are extracted from HAR files:

- Path: `PM_RUNNER_HAR_PATH` (default: `../primemover_runner/.../output`)
- Structure: `{job_id}/*lambda*.zip` containing `output.json`

### CloudWatch Logs

Lambda execution logs from AWS CloudWatch:

- Log Group: `LAMBDA_LOG_GROUP` (default: `/aws/lambda/bot-decision-yt`)
- Requires AWS credentials

## CLI Usage

### Query Logs

```bash
# Query all components
orchestrator logs query -t 1h

# Filter by component
orchestrator logs query -c api -t 24h

# Filter by level
orchestrator logs query -c runner -l error

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

## Best Practices

1. **Use correlation IDs**: Include experiment/crawler/job IDs for traceability
2. **Query specific components**: Avoid querying all components when possible
3. **Limit time ranges**: Shorter ranges improve performance
4. **Use search patterns**: Text search narrows results effectively
5. **Close aggregator**: Call `aggregator.close()` to release resources
