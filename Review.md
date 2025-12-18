# Code Review Report: PrimeMover Orchestrator

**Review Date:** December 17, 2025
**Codebase Version:** master branch (commit e533ebf)
**Reviewer:** Claude Code

---

## Executive Summary

The PrimeMover Orchestrator is a well-structured Python CLI application for monitoring, debugging, and managing the PrimeMover ecosystem. The codebase demonstrates good architectural patterns with clear separation of concerns. However, several issues should be addressed before production deployment, particularly around security, error handling, and robustness.

**Overall Assessment:** The code is functional and well-organized but requires security hardening and error handling improvements before production deployment.

---

## FIXES APPLIED

### Critical Security Issues (All 8 Fixed)

| Issue | Status | Files Changed |
|-------|--------|---------------|
| Hardcoded credentials in settings.py | **FIXED** | `orchestrator/config/settings.py`, `.env.example` |
| Hardcoded MySQL/Redis passwords | **FIXED** | `orchestrator/components/api.py` |
| MCP server binds to 0.0.0.0 | **FIXED** | `orchestrator/mcp_server/server.py`, `scripts/mcp-server.sh` |
| Grafana anonymous auth enabled | **FIXED** | `docker-compose.yml` |
| LogQL injection risk | **FIXED** | `orchestrator/clients/loki.py` |
| Missing MCP input validation | **FIXED** | `orchestrator/mcp_server/server.py` |
| Runner PID race conditions | **FIXED** | `orchestrator/components/runner.py` |
| Docker socket security undocumented | **FIXED** | `docker-compose.yml`, `docs/log-aggregation.md` |

### Important Issues (All 6 Fixed)

| Issue | Status | Files Changed |
|-------|--------|---------------|
| Bare exception handlers | **FIXED** | `orchestrator/tracing/trace_builder.py`, `orchestrator/clients/loki.py`, `orchestrator/clients/cloudwatch.py` |
| API token expiration handling | **FIXED** | `orchestrator/tracing/trace_builder.py` |
| Subprocess commands without timeout | **FIXED** | `orchestrator/components/api.py`, `orchestrator/components/monitoring.py` |
| HTTP client connections not closed | **FIXED** | `orchestrator/mcp_server/server.py` |
| Insecure default Grafana credentials | **FIXED** | (Same as critical #1) |
| External network dependency | **FIXED** | `docker-compose.yml`, `orchestrator/components/monitoring.py` |

### Minor Issues (6 of 9 Fixed)

| Issue | Status | Files Changed |
|-------|--------|---------------|
| Modern Python type hints | **FIXED** | `orchestrator/config/settings.py` |
| Log path configurable | **FIXED** | `orchestrator/components/runner.py`, `manager.py`, `cli.py` |
| HTTP retry logic | **FIXED** | `orchestrator/clients/loki.py`, `cloudwatch.py` |
| YAML loader security docs | **FIXED** | `orchestrator/mcp_server/server.py` |
| Large file memory issue | **FIXED** | `orchestrator/logs/aggregator.py` |
| Tempo healthcheck | **FIXED** | `docker-compose.yml` |
| Error message formatting | Not addressed | Low priority |
| Test docstrings | Not addressed | Per project guidelines |
| Test coverage gaps | Not addressed | Out of scope |

### Configuration Required After Fix

You must now set the following environment variables (no more defaults):

```bash
# Required API credentials
PM_API_EMAIL=your-email@example.com
PM_API_PASSWORD=your-password

# Required Grafana password
GRAFANA_ADMIN_PASSWORD=your-strong-password

# Optional: For health checks (if using passwords)
PM_MYSQL_PASSWORD=your-mysql-password
PM_REDIS_PASSWORD=your-redis-password
```

---

## Critical Issues (Must Fix Before Deployment)

### 1. ~~Hardcoded Credentials in Source Code~~ **[FIXED]**

**Location:** `orchestrator/config/settings.py:119-120`

**Status:** FIXED - Default credentials removed, now requires explicit configuration via environment variables.

---

### 2. ~~Hardcoded Database/Service Passwords in API Component~~ **[FIXED]**

**Location:** `orchestrator/components/api.py:360-376, 398-412`

**Status:** FIXED - MySQL and Redis passwords now read from `PM_MYSQL_PASSWORD` and `PM_REDIS_PASSWORD` settings.

---

### 3. ~~MCP Server Binds to All Interfaces by Default~~ **[FIXED]**

**Location:** `orchestrator/mcp_server/server.py:19`

**Status:** FIXED - Now defaults to `127.0.0.1`. Use `--host 0.0.0.0` explicitly for external access (with security warning).

---

### 4. ~~Docker Socket Access in Promtail~~ **[MITIGATED]**

**Location:** `docker-compose.yml:72-73`

**Status:** MITIGATED - Security implications now documented. Added container hardening: `cap_drop: ALL`, `cap_add: DAC_READ_SEARCH`, `no-new-privileges: true`. See `docs/log-aggregation.md` for security guidance.

---

### 5. ~~Grafana Anonymous Authentication Enabled~~ **[FIXED]**

**Location:** `docker-compose.yml:17`

**Status:** FIXED - Anonymous access now disabled by default. Set `GRAFANA_ANON_ENABLED=true` to enable. Grafana password now required via `GRAFANA_ADMIN_PASSWORD`.

---

### 6. ~~SQL Injection Risk in LogQL Query Building~~ **[FIXED]**

**Location:** `orchestrator/clients/loki.py:313-315`

**Status:** FIXED - Added `escape_logql_string()` and `validate_logql_label()` functions for proper LogQL escaping. Input validation added for component names, log levels, and search strings.

---

### 7. ~~Missing Input Validation in MCP Tools~~ **[FIXED]**

**Location:** `orchestrator/mcp_server/server.py` (multiple functions)

**Status:** FIXED - Added validation helpers: `_validate_time_range()`, `_validate_component()`, `_validate_level()`, `_validate_limit()`, `_validate_id()`, `_validate_search()`. Applied to all key MCP tools.

---

### 8. ~~Race Conditions in Runner PID Management~~ **[FIXED]**

**Location:** `orchestrator/components/runner.py:367-374`

**Status:** FIXED - Added `_pid_file_lock()` context manager using `fcntl.flock()` for atomic locking. Added `_write_pid()` for atomic write-and-rename pattern. Both `start()` and `stop()` now use file locking.

---

## Important Issues (Should Fix)

### 9. ~~Bare Exception Handlers Swallowing Errors~~ **[FIXED]**

**Locations:** Multiple files

**Status:** FIXED - Added logging to exception handlers in `orchestrator/tracing/trace_builder.py`, `orchestrator/clients/loki.py`, and `orchestrator/clients/cloudwatch.py`. Exceptions are now logged at debug level before returning fallback values.

---

### 10. ~~Missing API Token Expiration Handling~~ **[FIXED]**

**Location:** `orchestrator/tracing/trace_builder.py`

**Status:** FIXED - Added `_token_expiry` tracking with 55-minute validity. `_is_token_valid()` checks expiration before use. `_api_get()` now handles 401 responses by clearing the token and re-authenticating automatically.

---

### 11. ~~Subprocess Commands Without Timeout~~ **[FIXED]**

**Location:** `orchestrator/components/api.py`, `orchestrator/components/monitoring.py`

**Status:** FIXED - Added timeout constants (COMPOSE_TIMEOUT=300s, HEALTH_CHECK_TIMEOUT=10s, DOCKER_INSPECT_TIMEOUT=30s). All `_run_compose()` and subprocess calls now include timeout parameters.

---

### 12. ~~HTTP Client Connections Not Always Closed~~ **[FIXED]**

**Location:** `orchestrator/mcp_server/server.py`

**Status:** FIXED - `_api_request()` now uses `with httpx.Client() as client:` context manager to ensure connections are properly closed after use.

---

### 13. ~~Insecure Default Grafana Credentials~~ **[FIXED]**

**Location:** `orchestrator/config/settings.py`, `docker-compose.yml`

**Status:** FIXED - This was addressed as part of Critical Issue #1. Grafana password is now required via `GRAFANA_ADMIN_PASSWORD` environment variable with no default.

---

### 14. ~~External Network Dependency Without Fallback~~ **[FIXED]**

**Location:** `docker-compose.yml`, `orchestrator/components/monitoring.py`

**Status:** FIXED - Removed hard dependency on external `primemover-network`. The monitoring stack now starts independently. Promtail is connected to the API network dynamically via `connect_to_api_network()` after startup (best-effort, won't fail if API stack isn't running).

---

## Minor Issues (Nice to Fix)

### 15. Inconsistent Error Message Formatting

**Location:** Various CLI commands

**Issue:** Error messages use inconsistent formatting (some use Rich markup, others plain text). This affects user experience.

**Status:** Not addressed - low priority, cosmetic improvement.

---

### 16. Missing Docstrings in Test Files

**Location:** `orchestrator/tests/*.py`

**Issue:** Test classes and methods lack docstrings describing what they test.

**Status:** Not addressed - per project guidelines, don't add docstrings to unchanged code.

---

### 17. ~~Unused Import in Settings~~ **[FIXED]**

**Location:** `orchestrator/config/settings.py`

**Status:** FIXED - Updated to modern Python 3.10+ syntax using `X | None` instead of `Optional[X]`. Added `from __future__ import annotations` for forward compatibility.

---

### 18. ~~Log File Path Not Configurable at Runtime~~ **[FIXED]**

**Location:** `orchestrator/components/runner.py`, `orchestrator/components/manager.py`, `orchestrator/cli.py`

**Status:** FIXED - Added `log_path` parameter to `RunnerComponent` constructor. `ComponentManager` now accepts `runner_log_path` and passes it through. CLI uses `PM_RUNNER_LOG_PATH` from settings.

---

### 19. ~~Missing Request Retry Logic~~ **[FIXED]**

**Location:** `orchestrator/clients/loki.py`, `orchestrator/clients/cloudwatch.py`

**Status:** FIXED - Added `with_retry()` decorator with exponential backoff (3 retries, 0.5s initial delay, 2x backoff). CloudWatch client uses boto3's adaptive retry mode with 3 max attempts.

---

### 20. Test Coverage Gaps

**Location:** `orchestrator/tests/`

**Issue:** Several modules lack comprehensive test coverage.

**Status:** Not addressed - requires dedicated testing effort beyond scope of this review.

---

### 21. ~~YAML Parser Without Safe Loading Flag~~ **[FIXED]**

**Location:** `orchestrator/mcp_server/server.py`

**Status:** FIXED - Added security comment documenting that `FileTagLoader` inherits from `yaml.SafeLoader`, which prevents arbitrary code execution. The only custom tag is `!file` for loading external data files safely.

---

### 22. ~~Potential Memory Issues with Large Log Queries~~ **[FIXED]**

**Location:** `orchestrator/logs/aggregator.py`

**Status:** FIXED - Added `read_file_tail()` helper function that uses file seeking to read only the last N bytes of large files. Avoids loading entire files into memory.

---

### 23. ~~Missing Health Check for Tempo~~ **[FIXED]**

**Location:** `docker-compose.yml`

**Status:** FIXED - Added healthcheck for Tempo service using `/ready` endpoint, matching the pattern used by other services (30s interval, 10s timeout, 3 retries).

---

## Architecture Recommendations

### 24. Consider Implementing Circuit Breaker Pattern

For external service calls (API, Loki, CloudWatch), implement circuit breaker pattern to handle service outages gracefully and prevent cascading failures.

### 25. Add Structured Logging

Current logging is inconsistent. Consider implementing structured logging (JSON format) for better observability and log aggregation.

### 26. Implement Configuration Validation on Startup

Add a validation step on CLI startup that checks:
- Required environment variables are set
- Service URLs are reachable
- Credentials are valid

---

## Deployment Checklist

Before deploying to production:

- [ ] Remove or secure all default credentials
- [ ] Disable anonymous Grafana access
- [ ] Configure MCP server to bind to localhost only (or add authentication)
- [ ] Set up proper network isolation for Docker services
- [ ] Rotate all credentials from defaults
- [ ] Enable HTTPS for all web interfaces
- [ ] Set up proper log rotation
- [ ] Configure resource limits for containers
- [ ] Implement monitoring alerts
- [ ] Document security configuration

---

## Summary

| Category | Count | Status |
|----------|-------|--------|
| Critical Security | 5 | **ALL FIXED** |
| Critical Robustness | 3 | **ALL FIXED** |
| Important | 6 | Should fix |
| Minor | 9 | Nice to fix |

**All 8 critical security and robustness issues have been fixed.**

The codebase is now significantly more secure with:
- No hardcoded credentials (all require explicit configuration)
- Proper input validation and escaping for LogQL queries
- MCP server defaults to localhost-only binding
- Grafana anonymous access disabled by default
- Docker socket access properly documented and mitigated
- Race conditions in PID management resolved with file locking

**Remaining work:** The important and minor issues listed above are still present but are lower priority. They should be addressed as time permits but are not blockers for deployment.
