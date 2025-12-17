# Integration Contract

This document defines the integration points between the PrimeMover Orchestrator and the other repositories in the ecosystem. **Changes to these contracts require coordinated updates across repositories.**

## Overview

The orchestrator integrates with:
- **primemover_api** (Laravel) - via Docker, logs, and REST API
- **primemover_runner** (Java/Spring Boot) - via process management and logs
- **bot-decision-yt** (AWS Lambda) - via CloudWatch logs and HAR files

---

## 1. Correlation ID Format

**Status:** Critical - used for cross-component tracing

All components must use this format for correlation IDs:

```
exp-{experiment_id}|cr-{crawler_id}|q-{queue_id}|j-{job_id}
```

**Examples:**
- Full: `exp-15|cr-42|q-123|j-456`
- Partial: `cr-42|j-456`
- Job only: `j-456`

**Orchestrator file:** `orchestrator/tracing/correlation.py`

**Validation regex:**
```python
r"exp-(\d+)"      # experiment_id
r"cr-(\d+)"       # crawler_id
r"(?:q|jl)-(\d+)" # queue_id (q- or jl- prefix)
r"j-(\d+)"        # job_id
```

---

## 2. Log Formats

### 2.1 Laravel (primemover_api)

**Status:** Critical - parsed for log aggregation and tracing

**Expected format:**
```
[YYYY-MM-DD HH:MM:SS] environment.LEVEL: message {"context": "json"}
```

**Example:**
```
[2025-01-15 10:30:45] production.ERROR: Job failed {"experiment_id": 15, "job_id": 456}
```

**Orchestrator file:** `orchestrator/logs/parsers/laravel.py`

**Required context fields** (when applicable):
- `experiment_id`
- `crawler_id`
- `queue_id` or `joblist_id`
- `job_id`

**Log location:** `storage/logs/` directory

### 2.2 Spring Boot (primemover_runner)

**Status:** Critical - parsed for log aggregation and tracing

**Expected format:**
```
YYYY-MM-DDTHH:MM:SS.mmm+ZZ:ZZ  LEVEL PID --- [application] [thread] logger  : message
```

**Example:**
```
2025-01-15T10:30:45.123+00:00  INFO 12345 --- [primemover-runner] [main] c.p.r.Runner  : Processing crawlerId=42
```

**Orchestrator file:** `orchestrator/logs/parsers/runner.py`

**ID extraction patterns in messages:**
- `crawlerId=N` or `crawler_id=N` or `crawler.id=N`
- `jobId=N` or `job_id=N` or `job.id=N`
- `correlationId=exp-N|cr-N|j-N`

**Log location:** `primemover-runner/log/` directory

---

## 3. API Endpoints

**Status:** Critical - used for data retrieval and experiment upload

### 3.1 Authentication

**Endpoint:** `POST /api/v1/login`

**Request:**
```json
{
  "email": "user@example.com",
  "password": "password"
}
```

**Response:**
```json
{
  "access_token": "eyJ..."
}
```

**Usage:** Bearer token in `Authorization` header

### 3.2 Data Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/experiments` | GET | List experiments |
| `/api/v1/experiments/{id}` | GET | Get experiment details |
| `/api/v1/crawlers/{id}` | GET | Get crawler details |
| `/api/v1/jobs/{id}` | GET | Get job details |
| `/api/v1/experiments` | POST | Create experiment |

**Orchestrator file:** `orchestrator/tracing/trace_builder.py`

### 3.3 Health Check

**Endpoint:** `GET /api/v1/health` or `GET /`

**Expected:** HTTP 200 response

---

## 4. Directory Structure

### 4.1 primemover_api

```
primemover_api/
├── docker-compose.yml        # Required - orchestrator runs this
├── .env                      # Configuration
├── storage/
│   └── logs/                 # Laravel logs (*.log)
│       └── laravel.log
└── public/
    └── gui/                  # GUI static files (optional)
```

### 4.2 primemover_runner

```
primemover_runner/
├── pom.xml                   # Maven parent POM
├── primemover-runner/
│   ├── pom.xml
│   ├── target/
│   │   └── primemover-runner-*.jar   # Built JAR (excludes -sources, -tests)
│   ├── config/
│   │   └── application.properties    # Spring config
│   ├── log/                          # Runner logs
│   └── out/
│       └── har/                      # HAR output files
│           └── {job_id}/
│               └── *lambda*.zip      # Lambda response archives
└── primemover-generated/
    └── pom.xml
```

**Critical paths:**
- JAR: `primemover-runner/target/primemover-runner-*.jar`
- Config: `primemover-runner/config/`
- Logs: `primemover-runner/log/`
- HAR: `primemover-runner/out/har/`

---

## 5. Docker Configuration

### 5.1 Container Names

The API's docker-compose.yml must use these container name prefixes:

| Service | Container Name |
|---------|---------------|
| Apache | `primemover-apache` |
| PHP | `primemover-php` |
| MySQL | `primemover-mysql` |
| Redis | `primemover-redis` |
| Memcached | `primemover-memcached` |

**Orchestrator file:** `orchestrator/components/api.py`

### 5.2 Default Ports

| Service | Port | Environment Variable |
|---------|------|---------------------|
| HTTP (Apache) | 8090 | `PM_API_HTTP_PORT` |
| MySQL | 3307 | `PM_API_MYSQL_PORT` |
| Redis | 8889 | `PM_API_REDIS_PORT` |

### 5.3 Artisan Commands

The orchestrator executes Laravel Artisan commands via:

```bash
docker-compose exec -T php php artisan {command}
```

**Required commands:**
- `migrate`
- `db:seed`
- `cache:clear`
- `config:cache`

---

## 6. Runner Process Management

### 6.1 JAR Discovery

**Pattern:** `primemover-runner/target/primemover-runner-*.jar`

**Exclusions:** Files containing `-sources` or `-tests`

**Orchestrator file:** `orchestrator/components/runner.py`

### 6.2 Startup Command

```bash
java {JAVA_OPTS} -jar {jar_path} \
  --spring.profiles.active={profile} \
  --spring.config.location={config_path}/
```

**Defaults:**
- `JAVA_OPTS`: `-Xmx2g -Xms512m`
- `profile`: `local`
- `config_path`: `primemover-runner/config/`

### 6.3 Startup Detection

The orchestrator waits for one of these log messages:
- `Started PrimemoverRunnerApplication`
- `Tomcat started on port`

**Timeout:** 30 seconds

### 6.4 Java Version

**Required:** Java 17 or higher

---

## 7. Lambda Integration

### 7.1 AWS Configuration

| Setting | Default | Environment Variable |
|---------|---------|---------------------|
| Region | `eu-central-1` | `AWS_DEFAULT_REGION` |
| Function | `bot-decision-yt` | `LAMBDA_FUNCTION_NAME` |
| Log Group | `/aws/lambda/bot-decision-yt` | `LAMBDA_LOG_GROUP` |

### 7.2 HAR Output Format

Lambda responses are stored in ZIP files within the HAR directory:

**Path:** `{PM_RUNNER_HAR_PATH}/{job_id}/*lambda*.zip`

**ZIP contents:** `output.json` (or similar)

**Expected JSON fields:**
```json
{
  "selected": {
    "title": "Video Title",
    "id": "video_id"
  },
  "xpath": "//xpath/query",
  "warnings": [],
  "timestamps": {
    "timestamp": {}
  }
}
```

---

## 8. Breaking Change Checklist

Before making changes to the API or Runner repositories, check if any of these are affected:

### primemover_api

- [ ] Log format in `config/logging.php`
- [ ] Docker container names in `docker-compose.yml`
- [ ] API endpoint routes in `routes/api.php`
- [ ] Authentication response format
- [ ] Context fields in log messages

### primemover_runner

- [ ] Logback pattern in `src/main/resources/logback.xml`
- [ ] JAR artifact name in `pom.xml`
- [ ] Directory structure (`config/`, `log/`, `out/har/`)
- [ ] Spring profiles configuration
- [ ] Startup log messages
- [ ] Correlation ID format in MDC

### bot-decision-yt

- [ ] Lambda function name
- [ ] CloudWatch log group name
- [ ] Output JSON structure
- [ ] ZIP file naming pattern

---

## 9. Testing Integration Changes

After making changes to integrated components:

```bash
# 1. Run orchestrator tests
make test

# 2. Check log parsing still works
orchestrator logs query -c api -t 1h
orchestrator logs query -c runner -t 1h

# 3. Verify tracing works
orchestrator trace experiment {id}

# 4. Check component health
orchestrator status -v
orchestrator doctor
```

---

## 10. Version Compatibility

| Orchestrator | API | Runner | Notes |
|--------------|-----|--------|-------|
| 0.1.x | Any | Any | Initial release |

*Update this table when breaking changes are made.*
