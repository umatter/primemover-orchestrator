# Troubleshooting Guide

This guide covers common issues and solutions for the PrimeMover Orchestrator.

## Installation Issues

### ModuleNotFoundError: No module named 'orchestrator'

```
ModuleNotFoundError: No module named 'orchestrator'
```

**Solution:**
```bash
# Install in development mode
pip install -e .

# Or install from pyproject.toml
pip install .
```

### ModuleNotFoundError: No module named 'mcp'

```
ModuleNotFoundError: No module named 'mcp'
```

**Solution:**
```bash
pip install mcp
```

Note: The MCP library is only required for running the MCP server, not for the CLI.

### ModuleNotFoundError: No module named 'boto3'

```
ModuleNotFoundError: No module named 'boto3'
```

**Solution:**
```bash
pip install boto3
```

Required for CloudWatch integration.

## Connection Issues

### Loki connection refused

```
Error querying logs: Connection refused
```

**Causes:**
1. Loki is not running
2. Wrong LOKI_URL configuration

**Solutions:**
```bash
# Check if Loki is running
curl http://localhost:3100/ready

# Start Loki with Docker Compose
docker-compose up -d loki

# Or check configuration
echo $LOKI_URL
```

### API authentication failed

```
Error: Authentication failed
401 Unauthorized
```

**Causes:**
1. Wrong credentials
2. API not running
3. Token expired

**Solutions:**
```bash
# Verify credentials
echo $PM_API_EMAIL
echo $PM_API_PASSWORD

# Test API connection
curl -X POST http://localhost:8090/api/v1/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@primemover.example","password":"password"}'

# Check API is running. There is no unauthenticated health route: every
# /api/v1 path is behind auth:api, so **401 is the healthy answer**. The
# Accept header is required -- without it Laravel redirects an unauthenticated
# request to a `login` route this API does not define and you get a 404.
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Accept: application/json" http://localhost:8090/api/v1/crawlers
```

### CloudWatch not available

```
CloudWatch is not available
Ensure AWS credentials are configured
```

**Causes:**
1. No AWS credentials
2. Wrong region
3. No permissions

**Solutions:**
```bash
# Check AWS configuration
aws configure list
aws sts get-caller-identity

# Set environment variables
export AWS_DEFAULT_REGION=eu-central-1
export AWS_ACCESS_KEY_ID=your_key
export AWS_SECRET_ACCESS_KEY=your_secret

# Verify Lambda log group exists
aws logs describe-log-groups --log-group-name-prefix /aws/lambda/bot-decision-yt
```

## Log Query Issues

### No logs found

```
No logs found matching criteria
```

**Causes:**
1. Time range too narrow
2. Wrong component filter
3. No logs generated yet
4. Log path misconfigured

**Solutions:**
```bash
# Expand time range
orchestrator logs query -t 7d

# Remove filters
orchestrator logs query

# Check log paths
ls -la $PM_API_LOG_PATH
ls -la $PM_RUNNER_LOG_PATH

# Verify Loki has data
curl 'http://localhost:3100/loki/api/v1/labels'
```

### Log file parsing errors

```
Error parsing log file: ...
```

**Causes:**
1. Corrupted log file
2. Unexpected log format
3. Encoding issues

**Solutions:**
```bash
# Check log file encoding
file /path/to/logfile.log

# View raw content
head -20 /path/to/logfile.log

# Clear corrupted files
# (backup first)
```

### Lambda HAR not found

```
No Lambda output found for job 789
Looked in: ../primemover_runner/.../output/789
```

**Causes:**
1. Job hasn't run yet
2. HAR capture disabled
3. Wrong path configuration

**Solutions:**
```bash
# Check HAR output directory
ls -la $PM_RUNNER_HAR_PATH

# Look for job directory
ls -la $PM_RUNNER_HAR_PATH/789/

# Check for Lambda zip files
find $PM_RUNNER_HAR_PATH -name "*lambda*.zip"
```

## Tracing Issues

### Entity not found

```
Error tracing experiment: Experiment 999 not found
```

**Causes:**
1. Wrong ID
2. Entity deleted
3. API error

**Solutions:**
```bash
# List experiments
orchestrator experiment list

# Check via API
curl http://localhost:8090/api/v1/experiments/999 \
  -H "Authorization: Bearer $TOKEN"
```

### Trace has no logs

```
Spans: 5 | Logs: 0 | Errors: 0
```

**Causes:**
1. Correlation IDs not logged
2. Time range doesn't cover execution
3. Loki not receiving logs

**Solutions:**
```bash
# Expand time range
orchestrator trace experiment 123 -t 30d

# Query logs directly
orchestrator logs query -e 123 -t 30d

# Check if correlation IDs are in logs
orchestrator logs query -s "exp-123"
```

## Experiment Designer Issues

### YAML parse error

```
Validation FAILED
YAML parse error: while parsing a block mapping
```

**Causes:**
1. Indentation errors
2. Tabs instead of spaces
3. Missing quotes

**Solutions:**
```yaml
# Use spaces, not tabs
# Correct indentation (2 spaces)
experiment:
  name: "My Study"

# Quote strings with special characters
session:
  start: "2025-02-01T10:00:00-06:00"
```

### Variable not substituted

```
Crawler name contains {factor_name}
```

**Causes:**
1. Variable not defined in design
2. Wrong variable syntax
3. Missing `maps_to`

**Solutions:**
```yaml
design:
  factors:
    condition:
      levels: [a, b]
      maps_to:
        # Define mapping for variable
        some_param: {a: 1, b: 2}

crawlers:
  - from_design: true
    name: "bot_{condition}"  # Will be substituted
```

### Template not found

```
Template 'my_template' not found
```

**Causes:**
1. Template name typo
2. Template not defined
3. Scope issue

**Solutions:**
```yaml
# Define template before use
templates:
  my_template:
    - visit: https://example.com
    - wait: 5s

crawlers:
  - session:
      workflow:
        - template: my_template  # Must match exactly
```

### Upload fails

```
Upload failed: 422 Unprocessable Entity
```

**Causes:**
1. Invalid JSON structure
2. Missing required fields
3. Validation error on API side

**Solutions:**
```bash
# Generate JSON and inspect
orchestrator experiment generate spec.yaml -o output.json
cat output.json | jq .

# Try dry run
orchestrator experiment upload spec.yaml --dry-run

# Check API logs
docker-compose logs -f php
```

## Grafana Dashboard Issues

### Dashboard not loading

**Causes:**
1. Datasource not configured
2. Wrong datasource UID
3. Grafana not running

**Solutions:**
```bash
# Check Grafana status
curl http://localhost:3000/api/health

# Verify datasources
curl http://localhost:3000/api/datasources \
  -H "Authorization: Basic YWRtaW46YWRtaW4="

# Check dashboard JSON has correct datasource UIDs
cat grafana/dashboards/*.json | grep '"uid"'
```

### No data in panels

**Causes:**
1. No logs/metrics in time range
2. Wrong query
3. Datasource connection issue

**Solutions:**
1. Expand time range in Grafana
2. Check datasource connection in Grafana settings
3. Test query directly in Explore view

## Performance Issues

### CLI is slow to start

**Causes:**
1. Importing heavy modules at startup

**Solutions:**
```python
# Lazy import (already done in CLI)
def my_command():
    from orchestrator.heavy_module import HeavyClass
    # ...
```

### Log queries are slow

**Causes:**
1. Large time range
2. No filters
3. High limit

**Solutions:**
```bash
# Use shorter time range
orchestrator logs query -t 15m

# Add filters
orchestrator logs query -c api -l error

# Reduce limit
orchestrator logs query -n 50
```

### Memory issues with large traces

**Causes:**
1. Too many logs included
2. Deep trace with all jobs

**Solutions:**
```bash
# Exclude logs
orchestrator trace experiment 123 --no-jobs

# Use summary format
orchestrator trace experiment 123 -f summary
```

## Getting Help

1. **Check logs**: Look at orchestrator output with `-v` flag
2. **Verify configuration**: Run `orchestrator status -v`
3. **Test components individually**: API, Loki, CloudWatch
4. **Check related repos**: primemover_api, primemover_runner

## Environment Variable Reference

```bash
# Core
ENV=development

# Monitoring Stack
GRAFANA_URL=http://localhost:3000
LOKI_URL=http://localhost:3100
PROMETHEUS_URL=http://localhost:9090
TEMPO_URL=http://localhost:3200

# PrimeMover API
PM_API_BASE_URL=http://localhost:8090/api/v1
PM_API_EMAIL=admin@primemover.example
PM_API_PASSWORD=password

# Log Paths
PM_API_LOG_PATH=../primemover_api/storage/logs
PM_RUNNER_LOG_PATH=../primemover_runner/primemover_runner/log
PM_RUNNER_HAR_PATH=../primemover_runner/primemover_runner/output

# AWS
AWS_DEFAULT_REGION=eu-central-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
LAMBDA_LOG_GROUP=/aws/lambda/bot-decision-yt
LAMBDA_FUNCTION_NAME=bot-decision-yt
```
