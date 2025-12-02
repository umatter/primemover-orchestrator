# CloudWatch Integration

The orchestrator integrates with AWS CloudWatch to query Lambda execution logs and metrics for the `bot-decision-yt` function.

## Overview

CloudWatch provides:
1. **Execution Logs** - All Lambda console output
2. **Invocation Metrics** - Duration, memory, errors
3. **Error Tracking** - Stack traces and error messages

## Prerequisites

### AWS Credentials

Configure AWS credentials using any standard method:

```bash
# Option 1: Environment variables
export AWS_ACCESS_KEY_ID=your_key
export AWS_SECRET_ACCESS_KEY=your_secret
export AWS_DEFAULT_REGION=eu-central-1

# Option 2: AWS credentials file (~/.aws/credentials)
[default]
aws_access_key_id = your_key
aws_secret_access_key = your_secret

# Option 3: IAM role (when running on AWS)
```

### Required IAM Permissions

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:FilterLogEvents",
        "logs:GetLogEvents",
        "logs:DescribeLogStreams"
      ],
      "Resource": "arn:aws:logs:*:*:log-group:/aws/lambda/bot-decision-yt:*"
    }
  ]
}
```

## CLI Usage

### Query Logs

```bash
# Recent logs
orchestrator lambda logs -t 1h

# Filter by request ID
orchestrator lambda logs -r "abc123-def456-..."

# Filter by job ID
orchestrator lambda logs -j 789

# Search pattern
orchestrator lambda logs -s "error"

# Output formats
orchestrator lambda logs -f plain   # Default
orchestrator lambda logs -f table
orchestrator lambda logs -f json
```

### View Metrics

```bash
# Execution metrics summary
orchestrator lambda metrics -t 1h

# JSON output
orchestrator lambda metrics -f json
```

### List Errors

```bash
# Recent errors
orchestrator lambda errors -t 24h

# Limit results
orchestrator lambda errors -n 10
```

### Invocation Details

```bash
# Get specific invocation
orchestrator lambda invocation abc123-def456-...

# JSON output
orchestrator lambda invocation abc123-def456-... --json
```

## Python API

### CloudWatchClient

```python
from orchestrator.clients.cloudwatch import CloudWatchClient

client = CloudWatchClient()

# Check availability
if client.is_available():
    # Query logs
    result = client.query_logs(
        time_range="1h",
        request_id="abc123...",
        job_id=789,
        filter_pattern='"error"',
        limit=100
    )

    for entry in result.entries:
        print(f"{entry.timestamp} [{entry.level}] {entry.message}")

client.close()
```

### Query Logs

```python
result = client.query_logs(
    time_range="1h",           # 15m, 1h, 24h, 7d
    start_time=datetime(...),  # Or explicit start
    end_time=datetime(...),    # Or explicit end
    request_id="...",          # Filter by request ID
    job_id=123,                # Filter by job ID
    filter_pattern='"error"',  # CloudWatch filter pattern
    limit=100                  # Max results
)

print(f"Found {len(result.entries)} entries")
```

### Get Invocation Details

```python
invocation = client.get_invocation(request_id="abc123...")

if invocation:
    print(f"Request ID: {invocation.request_id}")
    print(f"Duration: {invocation.duration_ms}ms")
    print(f"Memory: {invocation.max_memory_used_mb}MB / {invocation.memory_size_mb}MB")
    print(f"Cold Start: {invocation.is_cold_start}")
    print(f"Error: {invocation.error}")

    for log in invocation.logs:
        print(f"  {log.timestamp}: {log.message}")
```

### Get Metrics Summary

```python
metrics = client.get_metrics_summary(time_range="24h")

print(f"Invocations: {metrics['invocation_count']}")
print(f"Errors: {metrics['error_count']}")
print(f"Cold Starts: {metrics['cold_start_count']}")
print(f"Avg Duration: {metrics['duration']['avg']}ms")
print(f"P99 Duration: {metrics['duration']['p99']}ms")
print(f"Max Memory: {metrics['memory']['max_used']}MB")
```

### Get Recent Errors

```python
errors = client.get_recent_errors(time_range="24h", limit=20)

for inv in errors:
    print(f"{inv.start_time}: {inv.error}")
```

## Data Structures

### LambdaLogEntry

```python
@dataclass
class LambdaLogEntry:
    timestamp: datetime
    message: str
    log_stream: str
    request_id: Optional[str]
    level: str  # debug, info, warning, error

    # Metrics (from REPORT line)
    duration_ms: Optional[float]
    billed_duration_ms: Optional[float]
    memory_used_mb: Optional[float]
    memory_size_mb: Optional[float]
    init_duration_ms: Optional[float]

    # Error info
    error_type: Optional[str]
    error_message: Optional[str]

    # Correlation
    job_id: Optional[int]
    crawler_id: Optional[int]
    experiment_id: Optional[int]
```

### LambdaInvocation

```python
@dataclass
class LambdaInvocation:
    request_id: str
    start_time: Optional[datetime]
    end_time: Optional[datetime]

    # Metrics
    duration_ms: Optional[float]
    billed_duration_ms: Optional[float]
    memory_used_mb: Optional[float]  # Actually max_memory_used_mb
    memory_size_mb: Optional[float]
    init_duration_ms: Optional[float]

    # Status
    status: str  # success, error, timeout
    error_type: Optional[str]
    error_message: Optional[str]

    # Properties
    is_cold_start: bool
    is_error: bool
    error: Optional[str]

    # All logs for this invocation
    logs: list[LambdaLogEntry]
```

## MCP Tools

### get_lambda_cloudwatch_logs

Query Lambda logs from CloudWatch.

```json
{
  "time_range": "1h",
  "request_id": "abc123...",
  "job_id": 789,
  "search": "error",
  "limit": 100
}
```

### get_lambda_execution_metrics

Get execution metrics.

```json
{
  "time_range": "24h"
}
```

**Response:**
```json
{
  "invocation_count": 150,
  "error_count": 3,
  "cold_start_count": 12,
  "duration": {
    "avg": 2500,
    "min": 800,
    "max": 15000,
    "p99": 8000
  },
  "memory": {
    "configured": 1024,
    "avg_used": 450,
    "max_used": 680
  }
}
```

### get_lambda_invocation_details

Get details for specific invocation.

```json
{
  "request_id": "abc123-def456-..."
}
```

### get_lambda_recent_errors

List recent error invocations.

```json
{
  "time_range": "24h",
  "limit": 20
}
```

## Log Parsing

The CloudWatch client parses Lambda log patterns:

### START Line
```
START RequestId: abc123-def456-... Version: $LATEST
```

### END Line
```
END RequestId: abc123-def456-...
```

### REPORT Line
```
REPORT RequestId: abc123-def456-... Duration: 2500.00 ms Billed Duration: 2500 ms Memory Size: 1024 MB Max Memory Used: 450 MB Init Duration: 350.00 ms
```

### Error Detection

Errors are detected from:
- Lines containing `ERROR`, `Error`, or `error`
- Lines containing `Exception` or `Traceback`
- REPORT lines with anomalous durations

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_DEFAULT_REGION` | eu-central-1 | AWS region |
| `LAMBDA_LOG_GROUP` | /aws/lambda/bot-decision-yt | CloudWatch log group |
| `LAMBDA_FUNCTION_NAME` | bot-decision-yt | Lambda function name |

## Grafana Dashboard

The Lambda Performance dashboard (`grafana/dashboards/lambda-performance.json`) provides:

- Total invocations and errors (stat panels)
- Average duration and memory utilization
- Invocations over time
- Duration distribution (avg, p99, max)
- Errors and throttles
- Memory usage trends
- Log stream

**Required datasource:** CloudWatch (uid: `cloudwatch`)

## Troubleshooting

### CloudWatch not available

```
CloudWatch is not available
```

**Solutions:**
1. Check AWS credentials are configured
2. Verify `AWS_DEFAULT_REGION` is set
3. Ensure IAM permissions are correct

### No logs found

**Solutions:**
1. Verify Lambda function name matches `LAMBDA_FUNCTION_NAME`
2. Check log group exists: `LAMBDA_LOG_GROUP`
3. Extend time range (logs may be older)
4. CloudWatch has ~5 second delay for recent logs

### Permission denied

```
AccessDeniedException: User is not authorized to perform logs:FilterLogEvents
```

**Solutions:**
1. Add required IAM permissions
2. Check IAM role/user has access to log group

### Timeout errors

**Solutions:**
1. Reduce time range
2. Reduce result limit
3. Use more specific filters (request_id, job_id)
