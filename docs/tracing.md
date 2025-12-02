# Execution Tracing

The orchestrator provides execution tracing to reconstruct and visualize the flow of experiments, crawlers, and jobs across all components.

## Overview

Tracing combines:
1. **API Data** - Experiment, crawler, queue, job metadata from PrimeMover API
2. **Logs** - Related log entries from all components
3. **Lambda Output** - Decision outputs from HAR files

This creates a hierarchical view of execution with timing, status, and errors.

## Correlation IDs

All components use a hierarchical correlation ID format:

```
exp-{experiment_id}|cr-{crawler_id}|q-{queue_id}|j-{job_id}
```

**Examples:**
- `exp-123` - Experiment-level
- `exp-123|cr-456` - Crawler within experiment
- `exp-123|cr-456|q-789` - Queue within crawler
- `exp-123|cr-456|q-789|j-101` - Job within queue

### Using Correlation IDs

```python
from orchestrator.tracing import parse_correlation_id, build_correlation_id

# Parse a correlation ID
cid = parse_correlation_id("exp-123|cr-456|j-789")
print(cid.experiment_id)  # 123
print(cid.crawler_id)     # 456
print(cid.job_id)         # 789

# Build a correlation ID
cid_str = build_correlation_id(experiment_id=1, crawler_id=2, job_id=3)
print(cid_str)  # "exp-1|cr-2|j-3"

# Extract from log message
from orchestrator.tracing import extract_correlation_from_message
cid = extract_correlation_from_message("Processing job exp-5|cr-10|j-20 started")
print(cid.job_id)  # 20
```

## CLI Usage

### Trace Experiment

```bash
# Basic trace
orchestrator trace experiment 123

# Include job-level details
orchestrator trace experiment 123 --jobs

# Output formats
orchestrator trace experiment 123 -f tree    # Default (visual tree)
orchestrator trace experiment 123 -f summary # Statistics only
orchestrator trace experiment 123 -f json    # Full JSON output
```

### Trace Crawler

```bash
# Trace specific crawler
orchestrator trace crawler 456

# Include job details (default)
orchestrator trace crawler 456 --jobs

# Exclude jobs
orchestrator trace crawler 456 --no-jobs
```

### Trace Job

```bash
# Trace specific job
orchestrator trace job 789

# Include log entries
orchestrator trace job 789 --include-logs
```

## Python API

### TraceBuilder

```python
from orchestrator.tracing import TraceBuilder

builder = TraceBuilder()

# Trace an experiment
trace = builder.trace_experiment(
    experiment_id=123,
    include_crawlers=True,
    include_jobs=False,
    time_range="7d"
)

print(f"Entity: {trace.entity_type} {trace.entity_id}")
print(f"Status: {trace.root_span.status}")

# Export
print(trace.to_json())
print(trace.to_dict())

builder.close()
```

### Trace Results

```python
# ExecutionTrace structure
trace.entity_type      # "experiment", "crawler", or "job"
trace.entity_id        # ID of the entity
trace.entity_data      # Entity metadata (name, status, etc.)
trace.root_span        # Root TraceSpan
trace.correlation_id   # Correlation ID

# TraceSpan structure
span.name              # Span name
span.component         # Component (api, runner, lambda)
span.status            # SpanStatus (success, failed, running, pending)
span.start_time        # Start timestamp
span.end_time          # End timestamp
span.duration_ms       # Duration in milliseconds
span.children          # Child spans
span.logs              # Associated log entries
span.errors            # Error messages
span.metadata          # Additional metadata
```

## Trace Output Formats

### Tree Format (Default)

```
Experiment 123 - My Study
├── ✓ Crawler 456 [runner] (2.5m) cr:456
│   ├── ✓ Job 789 [runner] (45s) job:789
│   │   └── ✓ Lambda Decision [lambda] (1.2s)
│   └── ✓ Job 790 [runner] (52s) job:790
│       └── ✗ Lambda Decision [lambda] (0.8s)
│           ERROR: Invalid video selection...
└── ○ Crawler 457 [runner] (pending) cr:457

Spans: 6 | Logs: 45 | Errors: 1
```

### Summary Format

```
Experiment: 123
Name: My Study

Total Spans:  6
Total Logs:   45
Errors:       1
First Event:  2024-01-15T10:00:00
Last Event:   2024-01-15T10:15:00
Duration:     15.0 minutes

Errors:
  - Invalid video selection at position 3
```

### JSON Format

```json
{
  "entity_type": "experiment",
  "entity_id": 123,
  "entity_data": {
    "name": "My Study",
    "active": true
  },
  "correlation_id": "exp-123",
  "root_span": {
    "name": "Experiment 123",
    "component": "api",
    "status": "success",
    "start_time": "2024-01-15T10:00:00Z",
    "duration_ms": 900000,
    "children": [...],
    "logs": [...],
    "errors": []
  },
  "summary": {
    "total_spans": 6,
    "total_logs": 45,
    "error_count": 1
  }
}
```

## MCP Tools

### trace_experiment

```json
{
  "experiment_id": 123,
  "include_crawlers": true,
  "include_jobs": false,
  "time_range": "7d"
}
```

### trace_crawler

```json
{
  "crawler_id": 456,
  "include_jobs": true,
  "time_range": "7d"
}
```

### trace_job

```json
{
  "job_id": 789,
  "time_range": "7d"
}
```

### find_failed_executions

```json
{
  "time_range": "24h",
  "experiment_id": 123
}
```

## Trace Building Process

1. **Fetch Entity Data**
   - Query PrimeMover API for experiment/crawler/job details
   - Get related entities (crawlers for experiment, jobs for crawler)

2. **Build Correlation ID**
   - Construct correlation ID for the entity
   - Used to filter related logs

3. **Query Logs**
   - Fetch logs from LogAggregator with correlation filter
   - Group logs by sub-entity (crawler/job)

4. **Build Spans**
   - Create root span for the entity
   - Create child spans for related entities
   - Attach logs to appropriate spans

5. **Compute Status**
   - Determine status from entity data and logs
   - Calculate duration from timestamps
   - Collect error messages

## Span Status Values

| Status | Description |
|--------|-------------|
| `success` | Completed successfully |
| `failed` | Completed with errors |
| `running` | Currently executing |
| `pending` | Not yet started |
| `unknown` | Status cannot be determined |

## Configuration

| Variable | Description |
|----------|-------------|
| `PM_API_BASE_URL` | PrimeMover API URL |
| `PM_API_EMAIL` | API authentication email |
| `PM_API_PASSWORD` | API authentication password |
| `LOKI_URL` | Loki URL for log queries |

## Best Practices

1. **Use appropriate scope**: Trace experiments for overview, jobs for detail
2. **Adjust time range**: Longer ranges find more logs but take longer
3. **Check error summary**: `--format summary` gives quick error overview
4. **Export for analysis**: `--format json` for programmatic analysis
5. **Include logs sparingly**: `--include-logs` adds detail but increases output

## Troubleshooting

**No data returned**
- Check API credentials are configured
- Verify entity ID exists
- Adjust time range to cover execution period

**Missing logs**
- Ensure Loki is available or log files are accessible
- Check correlation ID is being logged by components

**Incomplete trace**
- Some components may not log correlation IDs
- Lambda output requires HAR files
