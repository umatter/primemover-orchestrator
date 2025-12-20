# PrimeMover Stack Access Guide

**Server:** `77.42.78.108`

## Quick Access

| Service | URL | Credentials |
|---------|-----|-------------|
| **API Docs** | http://77.42.78.108:8090/api/documentation | - |
| **Telescope** (API debugging) | http://77.42.78.108:8090/telescope | - |
| **Grafana** (monitoring) | http://77.42.78.108:3000 | `admin` / `admin` |

**API Credentials:** `admin@primemover.example` / `password`

## Uploading Experiments

### Option 1: Use the upload script

From your local machine, use the script in `primemover_api/examples/`:

```bash
# Set credentials
export PM_API_USERNAME="admin@primemover.example"
export PM_API_PASSWORD="password"

# Edit upload_experiment.sh line 50 to use remote API:
# API_BASE_URL="http://77.42.78.108:8090/api/v1"

# Run upload
./upload_experiment.sh your_experiment.json
```

### Option 2: Direct API calls

```bash
# Get auth token
TOKEN=$(curl -s -X POST "http://77.42.78.108:8090/api/v1/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@primemover.example","password":"password"}' | jq -r '.access_token')

# Create experiment
curl -X POST "http://77.42.78.108:8090/api/v1/experiments" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"My Experiment","description":"Test","active":1}'

# Upload config to experiment (replace EXPERIMENT_ID)
curl -X POST "http://77.42.78.108:8090/api/v1/experiments/EXPERIMENT_ID/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "json=@your_config.json;type=application/json"
```

## Monitoring

### Telescope (API Request Debugging)
- **URL:** http://77.42.78.108:8090/telescope
- Shows: HTTP requests, database queries, exceptions, logs
- Useful for debugging API issues

### Grafana (Logs & Metrics)
- **URL:** http://77.42.78.108:3000
- **Login:** `admin` / `admin`
- **Dashboards:** Check "Dashboards" menu for pre-built views
- **Explore:** Query logs directly via Loki

### Loki Log Queries (in Grafana Explore)
```
# Runner logs
{component="runner"}

# API logs
{component="api"}

# Errors only
{component="runner"} |= "ERROR"
```

## Checking Experiment Status

### Via API
```bash
# Get queue status
curl -s "http://77.42.78.108:8090/api/v1/queues/QUEUE_ID" \
  -H "Authorization: Bearer $TOKEN" | jq '.data.status_code'

# List unprocessed queues (what runner will pick up)
curl -s "http://77.42.78.108:8090/api/v1/queues-unprocessed" \
  -H "Authorization: Bearer $TOKEN" | jq '.data | length'
```

## Notes

- **Runner polls every 1 minute** for new jobs
- Jobs are scheduled based on `start_at` timestamp in the experiment
- The API runs in **CET timezone** (UTC+1)
- S3 reports are uploaded to `s3://primemoverrunner/reports/`

## SSH Access (if needed)

```bash
ssh primemover@77.42.78.108
cd ~/primemover-orchestrator
source venv/bin/activate
orchestrator status -v
```
