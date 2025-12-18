"""
PrimeMover Orchestrator MCP Server

Provides Claude Code with tools for monitoring, debugging, and managing experiments.
"""

import json
import os
import sys
from typing import Optional

from orchestrator.config import get_settings


def _get_mcp_config():
    """Parse command-line args and env vars to get MCP configuration."""
    transport = "stdio"
    port = 8100
    # SECURITY: Default to localhost - require explicit --host flag for external access
    host = "127.0.0.1"

    if "--http" in sys.argv or os.environ.get("MCP_TRANSPORT") == "http":
        transport = "streamable-http"

    if "--port" in sys.argv:
        try:
            port_idx = sys.argv.index("--port") + 1
            port = int(sys.argv[port_idx])
        except (IndexError, ValueError):
            pass

    if os.environ.get("MCP_PORT"):
        try:
            port = int(os.environ.get("MCP_PORT"))
        except ValueError:
            pass

    # Allow explicit host binding via --host flag or MCP_HOST env var
    if "--host" in sys.argv:
        try:
            host_idx = sys.argv.index("--host") + 1
            host = sys.argv[host_idx]
        except (IndexError, ValueError):
            pass

    if os.environ.get("MCP_HOST"):
        host = os.environ.get("MCP_HOST")

    return transport, host, port


# Parse config before creating FastMCP instance
_transport, _host, _port = _get_mcp_config()

# Import FastMCP after parsing args
from mcp.server.fastmcp import FastMCP

# Initialize MCP server with correct settings for HTTP mode
if _transport == "streamable-http":
    mcp = FastMCP("primemover-orchestrator", host=_host, port=_port)
else:
    mcp = FastMCP("primemover-orchestrator")


# =============================================================================
# INPUT VALIDATION HELPERS
# =============================================================================

import re as _re

# Valid time range pattern (e.g., "15m", "1h", "24h", "7d")
_TIME_RANGE_PATTERN = _re.compile(r"^(\d{1,3})([mhd])$")

# Valid component names
_VALID_COMPONENTS = {"all", "api", "runner", "lambda", "apache", "mysql", "redis", "php", "memcached"}

# Valid log levels
_VALID_LEVELS = {"all", "debug", "info", "warning", "warn", "error", "critical"}

# Maximum limits for various parameters
_MAX_LIMIT = 10000
_MAX_SEARCH_LENGTH = 500
_MAX_ID = 2147483647  # Max 32-bit signed integer


def _validate_time_range(time_range: str) -> str:
    """Validate time range format."""
    if not isinstance(time_range, str):
        raise ValueError("time_range must be a string")
    if not _TIME_RANGE_PATTERN.match(time_range.lower()):
        raise ValueError(f"Invalid time_range format: {time_range}. Use format like '15m', '1h', '24h', '7d'")
    return time_range.lower()


def _validate_component(component: str) -> str:
    """Validate component name."""
    if not isinstance(component, str):
        raise ValueError("component must be a string")
    component = component.lower()
    if component not in _VALID_COMPONENTS:
        raise ValueError(f"Invalid component: {component}. Valid: {', '.join(sorted(_VALID_COMPONENTS))}")
    return component


def _validate_level(level: str) -> str:
    """Validate log level."""
    if not isinstance(level, str):
        raise ValueError("level must be a string")
    level = level.lower()
    if level not in _VALID_LEVELS:
        raise ValueError(f"Invalid level: {level}. Valid: {', '.join(sorted(_VALID_LEVELS))}")
    return level


def _validate_limit(limit: int) -> int:
    """Validate result limit."""
    if not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if limit < 1:
        return 1
    if limit > _MAX_LIMIT:
        return _MAX_LIMIT
    return limit


def _validate_id(value: Optional[int], name: str) -> Optional[int]:
    """Validate an ID parameter."""
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < 1 or value > _MAX_ID:
        raise ValueError(f"{name} must be between 1 and {_MAX_ID}")
    return value


def _validate_search(search: str) -> str:
    """Validate and sanitize search string."""
    if not isinstance(search, str):
        raise ValueError("search must be a string")
    if len(search) > _MAX_SEARCH_LENGTH:
        raise ValueError(f"search string too long (max {_MAX_SEARCH_LENGTH} characters)")
    # Remove null bytes
    search = search.replace("\x00", "")
    return search


# =============================================================================
# LOG TOOLS
# =============================================================================


@mcp.tool()
def query_logs(
    component: str = "all",
    time_range: str = "1h",
    level: str = "all",
    search: str = "",
    experiment_id: Optional[int] = None,
    crawler_id: Optional[int] = None,
    limit: int = 100,
) -> str:
    """
    Query logs from all PrimeMover components.

    Args:
        component: Filter by component - 'all', 'api', 'runner', or 'lambda'
        time_range: Time range to search - '15m', '1h', '24h', '7d'
        level: Minimum log level - 'all', 'debug', 'info', 'warning', 'error'
        search: Text pattern to search for in log messages
        experiment_id: Filter by experiment ID
        crawler_id: Filter by crawler ID
        limit: Maximum number of results to return

    Returns:
        JSON array of log entries with timestamp, component, level, and message
    """
    import httpx
    from orchestrator.clients.loki import escape_logql_string

    # Validate inputs
    try:
        component = _validate_component(component)
        time_range = _validate_time_range(time_range)
        level = _validate_level(level)
        search = _validate_search(search)
        experiment_id = _validate_id(experiment_id, "experiment_id")
        crawler_id = _validate_id(crawler_id, "crawler_id")
        limit = _validate_limit(limit)
    except ValueError as e:
        return json.dumps({"error": f"Validation error: {e}"})

    settings = get_settings()

    # Build LogQL query with proper escaping
    label_selectors = []
    if component != "all":
        label_selectors.append(f'component="{component}"')
    if level != "all":
        label_selectors.append(f'level=~"(?i){level}"')

    labels = "{" + ",".join(label_selectors) + "}" if label_selectors else "{}"
    query = labels

    # Add filters with proper escaping
    filters = []
    if search:
        escaped_search = escape_logql_string(search)
        filters.append(f'|~ "{escaped_search}"')
    if experiment_id:
        filters.append(f'| json | experiment_id="{experiment_id}"')
    if crawler_id:
        filters.append(f'| json | crawler_id="{crawler_id}"')

    if filters:
        query += " " + " ".join(filters)

    try:
        params = {"query": query, "limit": limit}
        response = httpx.get(
            f"{settings.loki_url}/loki/api/v1/query_range",
            params=params,
            timeout=30.0,
        )

        if response.status_code == 200:
            data = response.json()
            results = data.get("data", {}).get("result", [])

            entries = []
            for stream in results:
                stream_labels = stream.get("stream", {})
                for value in stream.get("values", []):
                    timestamp, line = value
                    entries.append(
                        {
                            "timestamp": timestamp,
                            "component": stream_labels.get("component", "unknown"),
                            "level": stream_labels.get("level", "unknown"),
                            "message": line[:500],
                        }
                    )

            return json.dumps({"count": len(entries), "entries": entries[:limit]})
        else:
            return json.dumps({"error": f"Loki query failed: {response.status_code}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_error_summary(time_range: str = "24h", component: str = "all") -> str:
    """
    Get aggregated error counts and patterns.

    Args:
        time_range: Time range to analyze - '1h', '24h', '7d'
        component: Filter by component or 'all'

    Returns:
        JSON with error counts by component and most frequent error messages
    """
    # Query for errors
    result = query_logs(
        component=component, time_range=time_range, level="error", limit=500
    )

    try:
        data = json.loads(result)
        if "error" in data:
            return result

        entries = data.get("entries", [])

        # Aggregate by component
        by_component: dict[str, int] = {}
        messages: dict[str, int] = {}

        for entry in entries:
            comp = entry.get("component", "unknown")
            by_component[comp] = by_component.get(comp, 0) + 1

            msg = entry.get("message", "")[:100]
            messages[msg] = messages.get(msg, 0) + 1

        # Sort messages by frequency
        top_messages = sorted(messages.items(), key=lambda x: x[1], reverse=True)[:10]

        return json.dumps(
            {
                "total_errors": len(entries),
                "by_component": by_component,
                "top_messages": [{"message": m, "count": c} for m, c in top_messages],
            }
        )
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def tail_logs(component: str = "all", filter_text: str = "", lines: int = 50) -> str:
    """
    Get the most recent log lines.

    Args:
        component: Filter by component or 'all'
        filter_text: Optional text pattern to filter
        lines: Number of lines to return

    Returns:
        JSON array of recent log entries
    """
    return query_logs(
        component=component, time_range="15m", search=filter_text, limit=lines
    )


# =============================================================================
# STATUS TOOLS
# =============================================================================


@mcp.tool()
def system_status() -> str:
    """
    Get health status of all PrimeMover components.

    Checks connectivity and health of:
    - Grafana (visualization)
    - Loki (log aggregation)
    - Prometheus (metrics)
    - Tempo (tracing)
    - PrimeMover API

    Returns:
        JSON with status for each component
    """
    import httpx

    settings = get_settings()

    def check_health(name: str, url: str, path: str) -> dict:
        try:
            response = httpx.get(f"{url.rstrip('/')}{path}", timeout=5.0)
            return {
                "name": name,
                "status": "healthy" if response.status_code < 400 else "unhealthy",
                "code": response.status_code,
            }
        except httpx.ConnectError:
            return {"name": name, "status": "down", "error": "Connection refused"}
        except httpx.TimeoutException:
            return {"name": name, "status": "down", "error": "Timeout"}
        except Exception as e:
            return {"name": name, "status": "error", "error": str(e)}

    components = [
        check_health("grafana", settings.grafana_url, "/api/health"),
        check_health("loki", settings.loki_url, "/ready"),
        check_health("prometheus", settings.prometheus_url, "/-/healthy"),
        check_health("tempo", settings.tempo_url, "/ready"),
        check_health(
            "primemover_api",
            settings.pm_api_base_url.replace("/api/v1", ""),
            "/api/health",
        ),
    ]

    healthy_count = sum(1 for c in components if c["status"] == "healthy")
    overall = "healthy" if healthy_count == len(components) else "degraded"
    if healthy_count == 0:
        overall = "down"

    return json.dumps(
        {"overall_status": overall, "healthy_count": healthy_count, "components": components}
    )


# =============================================================================
# API TOOLS - Experiments, Crawlers, Queues, Jobs
# =============================================================================


def _api_request(method: str, endpoint: str, data: dict = None, params: dict = None) -> dict:
    """
    Make an authenticated API request.

    Uses httpx.Client context manager to ensure connections are properly closed.
    """
    import httpx

    settings = get_settings()

    # Use context manager to ensure connections are closed
    with httpx.Client(timeout=30.0) as client:
        # Authenticate
        auth_response = client.post(
            f"{settings.pm_api_base_url}/auth/login",
            json={
                "email": settings.pm_api_email,
                "password": settings.pm_api_password,
            },
        )

        if auth_response.status_code != 200:
            raise Exception("API authentication failed")

        token = auth_response.json().get("access_token")
        headers = {"Authorization": f"Bearer {token}"}

        # Make request
        url = f"{settings.pm_api_base_url}/{endpoint}"

        if method == "GET":
            response = client.get(url, headers=headers, params=params)
        elif method == "POST":
            response = client.post(url, headers=headers, json=data)
        else:
            raise Exception(f"Unsupported method: {method}")

        if response.status_code >= 400:
            raise Exception(f"API request failed: {response.status_code}")

        return response.json()


@mcp.tool()
def list_experiments(page: int = 1, per_page: int = 15) -> str:
    """
    List experiments (paginated).

    Args:
        page: Page number (default: 1)
        per_page: Items per page (default: 15)

    Returns:
        JSON with paginated experiments list including id, name, description, crawler_count
    """
    try:
        result = _api_request("GET", "experiments", params={"page": page, "per_page": per_page})
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_all_experiments() -> str:
    """
    Get all experiments without pagination.

    Returns:
        JSON with complete experiments list
    """
    try:
        result = _api_request("GET", "experiments/experiments-list")
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_crawlers(status: str = "") -> str:
    """
    List crawlers by status.

    Args:
        status: Filter by status - 'processed', 'unprocessed', 'reviewed',
               'inactive', 'testing', 'protected', or empty for all

    Returns:
        JSON with crawlers list including id, name, experiment_id, status
    """
    try:
        endpoint = "crawlers"
        if status:
            valid_statuses = ["processed", "unprocessed", "reviewed", "inactive", "testing", "protected"]
            if status not in valid_statuses:
                return json.dumps({
                    "error": f"Invalid status. Must be one of: {', '.join(valid_statuses)}"
                })
            endpoint = f"crawlers-{status}"

        result = _api_request("GET", endpoint)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_queues(status: str = "") -> str:
    """
    List queues by status.

    Args:
        status: Filter by status - 'processed', 'unprocessed', 'reviewed',
               'unreviewed', or empty for all

    Returns:
        JSON with queues list including id, crawler_id, status, job_count
    """
    try:
        endpoint = "queues"
        if status:
            valid_statuses = ["processed", "unprocessed", "reviewed", "unreviewed"]
            if status not in valid_statuses:
                return json.dumps({
                    "error": f"Invalid status. Must be one of: {', '.join(valid_statuses)}"
                })
            endpoint = f"queues-{status}"

        result = _api_request("GET", endpoint)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_experiment(experiment_id: int) -> str:
    """
    Get detailed information about a specific experiment.

    Args:
        experiment_id: Experiment ID

    Returns:
        JSON with experiment details including all crawlers and their configurations
    """
    try:
        experiment_id = _validate_id(experiment_id, "experiment_id")
        if experiment_id is None:
            return json.dumps({"error": "experiment_id is required"})
        result = _api_request("GET", f"experiments/{experiment_id}")
        return json.dumps(result)
    except ValueError as e:
        return json.dumps({"error": f"Validation error: {e}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_crawler(crawler_id: int) -> str:
    """
    Get detailed information about a specific crawler.

    Args:
        crawler_id: Crawler ID

    Returns:
        JSON with crawler details including configuration, agent, proxy, queues
    """
    try:
        crawler_id = _validate_id(crawler_id, "crawler_id")
        if crawler_id is None:
            return json.dumps({"error": "crawler_id is required"})
        result = _api_request("GET", f"crawlers/{crawler_id}")
        return json.dumps(result)
    except ValueError as e:
        return json.dumps({"error": f"Validation error: {e}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_queue(queue_id: int) -> str:
    """
    Get detailed information about a specific queue.

    Args:
        queue_id: Queue ID

    Returns:
        JSON with queue details including all jobs and their behaviors
    """
    try:
        queue_id = _validate_id(queue_id, "queue_id")
        if queue_id is None:
            return json.dumps({"error": "queue_id is required"})
        result = _api_request("GET", f"queues/{queue_id}")
        return json.dumps(result)
    except ValueError as e:
        return json.dumps({"error": f"Validation error: {e}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_job(job_id: int) -> str:
    """
    Get detailed information about a specific job.

    Args:
        job_id: Job ID

    Returns:
        JSON with job details including type, behaviors, and execution status
    """
    try:
        job_id = _validate_id(job_id, "job_id")
        if job_id is None:
            return json.dumps({"error": "job_id is required"})
        result = _api_request("GET", f"jobs/{job_id}")
        return json.dumps(result)
    except ValueError as e:
        return json.dumps({"error": f"Validation error: {e}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def download_experiment(experiment_id: int) -> str:
    """
    Download experiment data as ZIP archive.

    Note: This verifies the download is possible and returns metadata.
    The actual file is not returned through MCP.

    Args:
        experiment_id: Experiment ID

    Returns:
        JSON with success status and file size information
    """
    import httpx

    settings = get_settings()

    try:
        # Authenticate
        auth_response = httpx.post(
            f"{settings.pm_api_base_url}/auth/login",
            json={
                "email": settings.pm_api_email,
                "password": settings.pm_api_password,
            },
            timeout=30.0,
        )

        if auth_response.status_code != 200:
            return json.dumps({"error": "API authentication failed"})

        token = auth_response.json().get("access_token")
        headers = {"Authorization": f"Bearer {token}"}

        # Download
        response = httpx.get(
            f"{settings.pm_api_base_url}/experiments/{experiment_id}/download",
            headers=headers,
            timeout=60.0,
        )

        if response.status_code != 200:
            return json.dumps({"error": f"Download failed: {response.status_code}"})

        size_mb = len(response.content) / (1024 * 1024)
        return json.dumps({
            "success": True,
            "message": f"Experiment {experiment_id} download available",
            "size_mb": round(size_mb, 2),
            "note": "Use CLI 'orchestrator experiment download' to save the file"
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def download_report(job_id: int) -> str:
    """
    Download report for a specific job.

    Note: This verifies the download is possible and returns metadata.
    The actual file is not returned through MCP.

    Args:
        job_id: Job ID

    Returns:
        JSON with success status and file size information
    """
    import httpx

    settings = get_settings()

    try:
        # Authenticate
        auth_response = httpx.post(
            f"{settings.pm_api_base_url}/auth/login",
            json={
                "email": settings.pm_api_email,
                "password": settings.pm_api_password,
            },
            timeout=30.0,
        )

        if auth_response.status_code != 200:
            return json.dumps({"error": "API authentication failed"})

        token = auth_response.json().get("access_token")
        headers = {"Authorization": f"Bearer {token}"}

        # Download
        response = httpx.get(
            f"{settings.pm_api_base_url}/download/report/{job_id}",
            headers=headers,
            timeout=60.0,
        )

        if response.status_code != 200:
            return json.dumps({"error": f"Download failed: {response.status_code}"})

        size_kb = len(response.content) / 1024
        return json.dumps({
            "success": True,
            "message": f"Report for job {job_id} download available",
            "size_kb": round(size_kb, 2),
            "note": "Use CLI 'orchestrator job report' to save the file"
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# =============================================================================
# TRACE TOOLS
# =============================================================================


@mcp.tool()
def trace_experiment(
    experiment_id: int,
    include_crawlers: bool = True,
    include_jobs: bool = False,
    time_range: str = "7d",
) -> str:
    """
    Trace full execution flow for an experiment.

    Reconstructs the execution timeline by combining API data and logs.

    Args:
        experiment_id: Experiment ID to trace
        include_crawlers: Whether to include crawler-level spans
        include_jobs: Whether to include job-level details
        time_range: Time range for log queries (15m, 1h, 24h, 7d)

    Returns:
        JSON with execution trace including timeline, status, and any errors
    """
    try:
        # Validate inputs
        experiment_id = _validate_id(experiment_id, "experiment_id")
        if experiment_id is None:
            return json.dumps({"error": "experiment_id is required"})
        time_range = _validate_time_range(time_range)
    except ValueError as e:
        return json.dumps({"error": f"Validation error: {e}"})

    try:
        from orchestrator.tracing import TraceBuilder

        builder = TraceBuilder()

        try:
            trace = builder.trace_experiment(
                experiment_id,
                include_crawlers=include_crawlers,
                include_jobs=include_jobs,
                time_range=time_range,
            )
            return trace.to_json()
        finally:
            builder.close()

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def trace_crawler(
    crawler_id: int,
    include_jobs: bool = True,
    time_range: str = "7d",
) -> str:
    """
    Trace execution for a specific crawler.

    Reconstructs the execution timeline by combining API data and logs.

    Args:
        crawler_id: Crawler ID to trace
        include_jobs: Whether to include job-level details
        time_range: Time range for log queries

    Returns:
        JSON with crawler execution trace
    """
    try:
        # Validate inputs
        crawler_id = _validate_id(crawler_id, "crawler_id")
        if crawler_id is None:
            return json.dumps({"error": "crawler_id is required"})
        time_range = _validate_time_range(time_range)
    except ValueError as e:
        return json.dumps({"error": f"Validation error: {e}"})

    try:
        from orchestrator.tracing import TraceBuilder

        builder = TraceBuilder()

        try:
            trace = builder.trace_crawler(
                crawler_id,
                include_jobs=include_jobs,
                time_range=time_range,
            )
            return trace.to_json()
        finally:
            builder.close()

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def trace_job(job_id: int, time_range: str = "7d") -> str:
    """
    Trace execution for a specific job.

    Includes Lambda output if available.

    Args:
        job_id: Job ID to trace
        time_range: Time range for log queries

    Returns:
        JSON with job execution trace including Lambda output
    """
    try:
        # Validate inputs
        job_id = _validate_id(job_id, "job_id")
        if job_id is None:
            return json.dumps({"error": "job_id is required"})
        time_range = _validate_time_range(time_range)
    except ValueError as e:
        return json.dumps({"error": f"Validation error: {e}"})

    try:
        from orchestrator.tracing import TraceBuilder

        builder = TraceBuilder()

        try:
            trace = builder.trace_job(job_id, time_range=time_range)
            return trace.to_json()
        finally:
            builder.close()

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def find_failed_executions(
    time_range: str = "24h", experiment_id: Optional[int] = None
) -> str:
    """
    Find all failed job executions with error details.

    Args:
        time_range: Time range to search
        experiment_id: Optional experiment ID filter

    Returns:
        JSON with failed executions grouped by error type
    """
    # Query for error logs
    result = query_logs(
        level="error",
        time_range=time_range,
        experiment_id=experiment_id,
        limit=200,
    )

    try:
        data = json.loads(result)
        entries = data.get("entries", [])

        # Group by component
        failures: dict[str, list] = {}
        for entry in entries:
            comp = entry.get("component", "unknown")
            if comp not in failures:
                failures[comp] = []
            failures[comp].append(entry)

        return json.dumps(
            {
                "total_failures": len(entries),
                "by_component": {k: len(v) for k, v in failures.items()},
                "recent_failures": entries[:20],
            }
        )
    except Exception as e:
        return json.dumps({"error": str(e)})


# =============================================================================
# LAMBDA TOOLS
# =============================================================================


@mcp.tool()
def get_lambda_invocations(
    time_range: str = "1h",
    crawler_id: Optional[int] = None,
    experiment_id: Optional[int] = None,
    limit: int = 50,
) -> str:
    """
    List Lambda function invocations for bot-decision-yt.

    Args:
        time_range: Time range to search
        crawler_id: Filter by crawler ID
        experiment_id: Filter by experiment ID
        limit: Maximum results

    Returns:
        JSON with Lambda invocation details
    """
    # Query Lambda component logs
    result = query_logs(
        component="lambda",
        time_range=time_range,
        experiment_id=experiment_id,
        crawler_id=crawler_id,
        limit=limit,
    )

    return result


@mcp.tool()
def analyze_lambda_decisions(experiment_id: int, group_by: str = "crawler") -> str:
    """
    Analyze Lambda decision patterns for an experiment.

    Args:
        experiment_id: Experiment ID to analyze
        group_by: How to group results - 'crawler', 'hour', 'decision_type'

    Returns:
        JSON with decision statistics and patterns
    """
    # TODO: Implement Lambda output analysis from HAR files
    return json.dumps(
        {
            "message": "Not yet implemented - requires HAR file parsing",
            "experiment_id": experiment_id,
            "group_by": group_by,
        }
    )


@mcp.tool()
def get_lambda_cloudwatch_logs(
    time_range: str = "1h",
    request_id: Optional[str] = None,
    job_id: Optional[int] = None,
    search: str = "",
    limit: int = 100,
) -> str:
    """
    Query Lambda (bot-decision-yt) logs from CloudWatch.

    Args:
        time_range: Time range to search - '15m', '1h', '24h', '7d'
        request_id: Filter by specific Lambda request ID
        job_id: Filter by job ID
        search: Text pattern to search for in log messages
        limit: Maximum number of log entries to return

    Returns:
        JSON with Lambda log entries including timestamp, level, message, and request_id
    """
    try:
        from orchestrator.clients.cloudwatch import CloudWatchClient
    except ImportError as e:
        return json.dumps({"error": f"CloudWatch client not available: {e}"})

    try:
        client = CloudWatchClient()

        if not client.is_available():
            return json.dumps({
                "error": "CloudWatch is not available",
                "hint": "Ensure AWS credentials are configured"
            })

        result = client.query_logs(
            time_range=time_range,
            request_id=request_id,
            job_id=job_id,
            filter_pattern=f'"{search}"' if search else None,
            limit=limit,
        )

        entries = [
            {
                "timestamp": e.timestamp.isoformat(),
                "level": e.level,
                "message": e.message,
                "request_id": e.request_id,
                "log_stream": e.log_stream,
            }
            for e in result.entries
        ]

        client.close()

        return json.dumps({
            "count": len(entries),
            "entries": entries
        })

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_lambda_execution_metrics(time_range: str = "1h") -> str:
    """
    Get Lambda execution metrics from CloudWatch.

    Provides statistics on invocations, duration, memory usage, and errors.

    Args:
        time_range: Time range for metrics - '15m', '1h', '24h', '7d'

    Returns:
        JSON with execution metrics including invocation counts, duration stats, and memory usage
    """
    try:
        from orchestrator.clients.cloudwatch import CloudWatchClient
    except ImportError as e:
        return json.dumps({"error": f"CloudWatch client not available: {e}"})

    try:
        client = CloudWatchClient()

        if not client.is_available():
            return json.dumps({
                "error": "CloudWatch is not available",
                "hint": "Ensure AWS credentials are configured"
            })

        metrics = client.get_metrics_summary(time_range=time_range)
        client.close()

        return json.dumps(metrics)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_lambda_invocation_details(request_id: str) -> str:
    """
    Get detailed information about a specific Lambda invocation.

    Includes timing, memory usage, cold start detection, error details, and full logs.

    Args:
        request_id: Lambda request ID (UUID from CloudWatch logs)

    Returns:
        JSON with complete invocation details including all log entries
    """
    try:
        from orchestrator.clients.cloudwatch import CloudWatchClient
    except ImportError as e:
        return json.dumps({"error": f"CloudWatch client not available: {e}"})

    try:
        client = CloudWatchClient()

        if not client.is_available():
            return json.dumps({
                "error": "CloudWatch is not available",
                "hint": "Ensure AWS credentials are configured"
            })

        invocation = client.get_invocation(request_id=request_id)

        if not invocation:
            return json.dumps({
                "error": f"Invocation not found: {request_id}",
                "hint": "CloudWatch logs may take a few seconds to appear"
            })

        result = {
            "request_id": invocation.request_id,
            "start_time": invocation.start_time.isoformat() if invocation.start_time else None,
            "end_time": invocation.end_time.isoformat() if invocation.end_time else None,
            "duration_ms": invocation.duration_ms,
            "billed_duration_ms": invocation.billed_duration_ms,
            "memory_size_mb": invocation.memory_size_mb,
            "max_memory_used_mb": invocation.max_memory_used_mb,
            "memory_utilization_pct": (
                (invocation.max_memory_used_mb / invocation.memory_size_mb * 100)
                if invocation.memory_size_mb and invocation.max_memory_used_mb else None
            ),
            "init_duration_ms": invocation.init_duration_ms,
            "is_cold_start": invocation.is_cold_start,
            "is_error": invocation.is_error,
            "error": invocation.error,
            "log_count": len(invocation.logs),
            "logs": [
                {
                    "timestamp": log.timestamp.isoformat(),
                    "level": log.level,
                    "message": log.message,
                }
                for log in invocation.logs
            ],
        }

        client.close()
        return json.dumps(result)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_lambda_recent_errors(time_range: str = "24h", limit: int = 20) -> str:
    """
    Get recent Lambda errors from CloudWatch.

    Finds invocations that resulted in errors and returns details about each.

    Args:
        time_range: Time range to search - '1h', '24h', '7d'
        limit: Maximum number of errors to return

    Returns:
        JSON with error invocations including error message and context
    """
    try:
        from orchestrator.clients.cloudwatch import CloudWatchClient
    except ImportError as e:
        return json.dumps({"error": f"CloudWatch client not available: {e}"})

    try:
        client = CloudWatchClient()

        if not client.is_available():
            return json.dumps({
                "error": "CloudWatch is not available",
                "hint": "Ensure AWS credentials are configured"
            })

        errors = client.get_recent_errors(time_range=time_range, limit=limit)

        if not errors:
            return json.dumps({
                "count": 0,
                "message": "No Lambda errors found",
                "errors": []
            })

        error_list = []
        for inv in errors:
            error_list.append({
                "request_id": inv.request_id,
                "start_time": inv.start_time.isoformat() if inv.start_time else None,
                "duration_ms": inv.duration_ms,
                "is_cold_start": inv.is_cold_start,
                "error": inv.error,
                "log_count": len(inv.logs),
            })

        client.close()

        return json.dumps({
            "count": len(error_list),
            "errors": error_list
        })

    except Exception as e:
        return json.dumps({"error": str(e)})


# =============================================================================
# EXPERIMENT TOOLS
# =============================================================================


@mcp.tool()
def validate_experiment_spec(yaml_content: str, strict: bool = False) -> str:
    """
    Validate a YAML experiment specification.

    Performs structural, semantic, and design validation.

    Args:
        yaml_content: YAML content to validate
        strict: If True, treat warnings as errors

    Returns:
        JSON with validation results including errors, warnings, and summary
    """
    try:
        from experiment_designer.spec import SpecParser, SpecConverter, SpecValidator
        from experiment_designer.spec.file_loader import create_file_tag_loader, resolve_file_references
        import yaml as yaml_lib
    except ImportError as e:
        return json.dumps({"error": f"experiment_designer not available: {e}"})

    # Parse YAML with custom loader
    # SECURITY: FileTagLoader inherits from yaml.SafeLoader, which prevents
    # arbitrary code execution. The only custom tag is !file for loading
    # external data files (CSV/JSON), which are parsed safely.
    FileTagLoader, file_loader = create_file_tag_loader()

    try:
        raw_spec = yaml_lib.load(yaml_content, Loader=FileTagLoader)
        raw_spec = resolve_file_references(raw_spec, file_loader)
    except yaml_lib.YAMLError as e:
        return json.dumps({"valid": False, "errors": [f"YAML parse error: {e}"], "warnings": []})

    # Run validator
    validator = SpecValidator()
    validation_result = validator.validate(raw_spec)

    # Also parse and convert to catch additional errors
    parser = SpecParser()
    parsed = parser.parse(yaml_content)

    converter = SpecConverter()
    result = converter.convert(parsed)

    # Combine all errors and warnings
    all_errors = list(set(
        validation_result.errors + parsed.errors + result.errors
    ))
    all_warnings = list(set(
        validation_result.warnings + parsed.warnings + result.warnings
    ))

    if strict:
        all_errors.extend(all_warnings)
        all_warnings = []

    return json.dumps({
        "valid": len(all_errors) == 0,
        "errors": all_errors,
        "warnings": all_warnings,
        "summary": {
            "experiment_name": parsed.experiment.get("name", ""),
            "crawler_count": result.crawler_count,
            "total_jobs": result.total_jobs,
            "has_design": parsed.design is not None,
        },
        "design_info": validation_result.info.get("design_info")
    })


@mcp.tool()
def preview_factorial_design(yaml_content: str) -> str:
    """
    Preview factorial design expansion without full conversion.

    Shows how many crawlers will be created and the factor combinations.

    Args:
        yaml_content: YAML content with design section

    Returns:
        JSON with design preview including factor combinations
    """
    try:
        from experiment_designer.spec import SpecParser
        from experiment_designer.spec.design_expander import DesignExpander
    except ImportError as e:
        return json.dumps({"error": f"experiment_designer not available: {e}"})

    parser = SpecParser()
    parsed = parser.parse(yaml_content)

    if parsed.errors:
        return json.dumps({"errors": parsed.errors})

    if not parsed.design:
        return json.dumps({
            "experiment_name": parsed.experiment.get("name", ""),
            "has_design": False,
            "crawler_count": len(parsed.crawlers),
            "message": "No factorial design specified"
        })

    expander = DesignExpander()
    preview = expander.preview(parsed.design)

    return json.dumps({
        "experiment_name": parsed.experiment.get("name", ""),
        "has_design": True,
        "design_type": preview.get("design_type", "factorial"),
        "factors": preview.get("factors", []),
        "replications": preview.get("replications", 1),
        "formula": preview.get("formula", ""),
        "total_cells": preview.get("total_cells", 0),
        "total_crawlers": preview.get("total_crawlers", 0)
    })


@mcp.tool()
def convert_spec_to_json(yaml_content: str) -> str:
    """
    Convert YAML experiment spec to API-compatible JSON.

    Args:
        yaml_content: YAML content to convert

    Returns:
        JSON string ready for API upload, or error details
    """
    try:
        from experiment_designer.spec import SpecParser, SpecConverter
    except ImportError as e:
        return json.dumps({"error": f"experiment_designer not available: {e}"})

    parser = SpecParser()
    parsed = parser.parse(yaml_content)

    if parsed.errors:
        return json.dumps({"errors": parsed.errors, "warnings": parsed.warnings})

    converter = SpecConverter()
    result = converter.convert(parsed)

    if result.errors:
        return json.dumps({"errors": result.errors, "warnings": result.warnings})

    return json.dumps({
        "experiment_name": result.experiment_name,
        "experiment_description": result.experiment_description,
        "crawler_count": result.crawler_count,
        "total_jobs": result.total_jobs,
        "crawlers": result.json_data,
        "warnings": result.warnings
    })


@mcp.tool()
def upload_experiment(
    yaml_content: str,
    experiment_id: Optional[int] = None,
    dry_run: bool = False,
) -> str:
    """
    Convert YAML spec and upload to PrimeMover API.

    This tool validates the YAML specification, converts it to API-compatible
    JSON format, and uploads it to create a new experiment or add crawlers
    to an existing one.

    Args:
        yaml_content: YAML experiment specification
        experiment_id: Optional experiment ID to add crawlers to (creates new if None)
        dry_run: If True, validate and convert but don't actually upload

    Returns:
        JSON with upload result including experiment ID and crawler count,
        or validation errors if the spec is invalid
    """
    try:
        from experiment_designer.spec import SpecParser, SpecConverter
        import requests
        from datetime import datetime
    except ImportError as e:
        return json.dumps({"error": f"Required modules not available: {e}"})

    settings = get_settings()

    # Parse and validate
    parser = SpecParser()
    parsed = parser.parse(yaml_content)

    if parsed.errors:
        return json.dumps({
            "success": False,
            "errors": parsed.errors,
            "warnings": parsed.warnings
        })

    # Convert
    converter = SpecConverter()
    result = converter.convert(parsed)

    if result.errors:
        return json.dumps({
            "success": False,
            "errors": result.errors,
            "warnings": result.warnings
        })

    if dry_run:
        return json.dumps({
            "success": True,
            "dry_run": True,
            "experiment_name": result.experiment_name,
            "crawler_count": result.crawler_count,
            "total_jobs": result.total_jobs,
            "warnings": result.warnings
        })

    # Get credentials
    api_url = settings.pm_api_base_url
    email = settings.pm_api_email
    password = settings.pm_api_password

    if not all([api_url, email, password]):
        return json.dumps({
            "success": False,
            "error": "API credentials not configured (PM_API_BASE_URL, PM_API_EMAIL, PM_API_PASSWORD)"
        })

    try:
        # Authenticate
        auth_response = requests.post(
            f"{api_url}/login",
            json={"email": email, "password": password},
            headers={"Accept": "application/json"}
        )
        auth_response.raise_for_status()
        token = auth_response.json().get("access_token")

        if not token:
            return json.dumps({"success": False, "error": "Authentication failed: no token received"})

        # Create experiment if needed
        if not experiment_id:
            exp_name = result.experiment_name or f"Experiment {datetime.now().strftime('%Y%m%d_%H%M%S')}"

            create_response = requests.post(
                f"{api_url}/experiments",
                json={
                    "name": exp_name,
                    "description": result.experiment_description or "Created via MCP",
                    "contact": "PrimeMover Orchestrator MCP",
                    "active": 1
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json"
                }
            )
            create_response.raise_for_status()
            create_result = create_response.json()

            experiment_id = (
                create_result.get("data", {}).get("id") or
                create_result.get("id")
            )

            if not experiment_id:
                return json.dumps({"success": False, "error": "Failed to create experiment: no ID returned"})

        # Upload crawlers
        upload_response = requests.post(
            f"{api_url}/experiments/{experiment_id}/upload",
            files={"json": ("crawlers.json", json.dumps(result.json_data, indent=2), "application/json")},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json"
            }
        )
        upload_response.raise_for_status()

        return json.dumps({
            "success": True,
            "experiment_id": experiment_id,
            "experiment_name": result.experiment_name,
            "crawler_count": result.crawler_count,
            "total_jobs": result.total_jobs,
            "warnings": result.warnings
        })

    except requests.exceptions.HTTPError as e:
        error_detail = ""
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_detail = json.dumps(e.response.json())
            except Exception:
                error_detail = e.response.text[:500]
        return json.dumps({"success": False, "error": f"Upload failed: {e}", "detail": error_detail})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def list_job_types() -> str:
    """
    List all available job types for workflow definitions.

    Returns:
        JSON with all job types and their required/optional behaviors
    """
    try:
        from experiment_designer.spec.job_registry import JobRegistry
    except ImportError as e:
        return json.dumps({"error": f"experiment_designer not available: {e}"})

    job_types = []
    for job in JobRegistry.list_all():
        required = [b.name for b in job.behaviors if b.required]
        optional = [b.name for b in job.behaviors if not b.required]

        job_types.append({
            "yaml_name": job.yaml_name,
            "api_type": job.api_type,
            "description": job.description,
            "required_behaviors": required,
            "optional_behaviors": optional
        })

    return json.dumps({"job_types": job_types})


@mcp.tool()
def get_job_type_schema(job_type: str) -> str:
    """
    Get detailed schema for a specific job type.

    Args:
        job_type: Job type name (e.g., 'visit', 'click', 'type', 'scroll')

    Returns:
        JSON with detailed job schema including behavior descriptions
    """
    try:
        from experiment_designer.spec.job_registry import JobRegistry
    except ImportError as e:
        return json.dumps({"error": f"experiment_designer not available: {e}"})

    schema = JobRegistry.get_schema(job_type)

    if not schema:
        available = [j.yaml_name for j in JobRegistry.list_all()]
        return json.dumps({
            "error": f"Unknown job type: {job_type}",
            "available_types": available
        })

    behaviors = []
    for b in schema.behaviors:
        behaviors.append({
            "name": b.name,
            "required": b.required,
            "default": b.default,
            "description": b.description
        })

    return json.dumps({
        "yaml_name": schema.yaml_name,
        "api_type": schema.api_type,
        "display_name": schema.display_name,
        "description": schema.description,
        "behaviors": behaviors
    })


@mcp.tool()
def get_spec_template(template_type: str = "default") -> str:
    """
    Get a YAML experiment spec template to start from.

    Args:
        template_type: Template type - 'default', 'youtube', or 'google'

    Returns:
        YAML template string
    """
    templates = {
        'default': '''# Experiment Specification
version: "1.0"

experiment:
  name: "My Experiment"
  description: "Experiment description"

defaults:
  location: US-IL-CHICAGO
  browser: mlx
  timezone: America/Chicago

crawlers:
  - name: "Crawler 1"
    description: "First crawler"

    configuration:
      params:
        pi: 0
        alpha: 1

    proxy:
      hostname: "127.0.0.1"
      port: 8080
      type: http

    agent:
      identification: mlx
      location: US-IL-CHICAGO

    session:
      name: "Session 1"
      start: "now"

      workflow:
        - visit: https://example.com
        - wait: 5s
''',
        'youtube': '''# YouTube Research Experiment
version: "1.0"

experiment:
  name: "YouTube Recommendation Study"
  description: "Studying recommendation algorithm pathways"

defaults:
  location: US-IL-CHICAGO
  browser: mlx
  timezone: America/Chicago
  captcha_mode: always

templates:
  cookie_consent:
    - try_click: "//button[contains(text(),'Accept')]"
    - wait: 2s

  watch_and_recommend:
    - wait: 10s
    - lambda_select:
        task: youtube_video_select
        function: bot-decision-yt

design:
  type: factorial
  factors:
    political_orientation:
      levels: [left, center, right]
      maps_to:
        params.pi:
          left: -0.5
          center: 0
          right: 0.5
  replications: 3

crawlers:
  - from_design: true
    name: "yt_{political_orientation}_{replica:02d}"
    description: "YouTube bot"

    configuration:
      params:
        alpha: 1

    proxy:
      hostname: "proxy.example.com"
      port: 8080
      type: http

    agent:
      identification: mlx

    session:
      name: "Daily Session"
      start: "now"

      workflow:
        - visit: https://youtube.com
        - wait: 5s
        - template: cookie_consent
        - repeat: 5
            template: watch_and_recommend
''',
        'google': '''# Google Search Experiment
version: "1.0"

experiment:
  name: "Google Search Study"
  description: "Studying search result personalization"

defaults:
  location: US-IL-CHICAGO
  browser: mlx
  timezone: America/Chicago

templates:
  google_search:
    - visit: https://google.com
    - wait: 3s
    - type:
        text: ${query}
        selector: "//input[@name='q']"
        submit: true
    - wait: 5s

crawlers:
  - name: "Search Bot"
    description: "Google search crawler"

    configuration:
      params:
        pi: 0
        alpha: 1

    proxy:
      hostname: "127.0.0.1"
      port: 8080
      type: http

    agent:
      identification: mlx
      location: US-IL-CHICAGO

    session:
      name: "Search Session"
      start: "now"

      workflow:
        - template: google_search
          with:
            query: "climate change"
        - scroll: 30s
        - wait: 5s
'''
    }

    template = templates.get(template_type, templates['default'])
    return json.dumps({"template": template, "type": template_type})


@mcp.tool()
def summarize_yaml_spec(yaml_content: str) -> str:
    """
    Generate a structured summary from a YAML experiment specification.

    Provides an overview of the experiment design including crawler counts,
    parameter distributions, workflow structure, and more.

    Args:
        yaml_content: YAML experiment specification content

    Returns:
        JSON with structured summary including:
        - metadata: name, description, version
        - design: type, factors, replications, total_crawlers
        - configuration: params ranges, preferences
        - workflow: job counts by type, estimated duration
        - statistics: parameter distributions, location/browser breakdown
    """
    try:
        from experiment_designer.summary import SummaryGenerator
    except ImportError as e:
        return json.dumps({"error": f"experiment_designer.summary not available: {e}"})

    try:
        generator = SummaryGenerator()
        summary = generator.from_yaml_string(yaml_content)

        return json.dumps({
            "success": True,
            "summary": summary.to_dict(),
            "text": summary.to_text(),
        })
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        })


@mcp.tool()
def summarize_experiment(experiment_id: int) -> str:
    """
    Generate a summary from an existing API experiment.

    Fetches experiment data from the PrimeMover API and generates
    a structured summary of its configuration and status.

    Args:
        experiment_id: Experiment ID in the PrimeMover API

    Returns:
        JSON with structured summary including:
        - metadata: id, name, description, created_at, updated_at
        - statistics: crawler_count, total_jobs, active_crawlers
        - configuration_summary: param distributions across crawlers
        - workflow_summary: common job patterns
        - status: processed/unprocessed queues, completion rate
    """
    try:
        from experiment_designer.summary import SummaryGenerator
    except ImportError as e:
        return json.dumps({"error": f"experiment_designer.summary not available: {e}"})

    import httpx

    settings = get_settings()

    try:
        # Authenticate and fetch experiment
        auth_response = httpx.post(
            f"{settings.pm_api_base_url}/auth/login",
            json={
                "email": settings.pm_api_email,
                "password": settings.pm_api_password,
            },
            timeout=30.0,
        )

        if auth_response.status_code != 200:
            return json.dumps({
                "success": False,
                "error": "API authentication failed",
            })

        token = auth_response.json().get("access_token")
        headers = {"Authorization": f"Bearer {token}"}

        # Fetch experiment with crawlers
        exp_response = httpx.get(
            f"{settings.pm_api_base_url}/experiments/{experiment_id}",
            headers=headers,
            timeout=30.0,
        )

        if exp_response.status_code != 200:
            return json.dumps({
                "success": False,
                "error": f"Failed to fetch experiment: {exp_response.status_code}",
            })

        experiment_data = exp_response.json().get("data", exp_response.json())

        generator = SummaryGenerator()
        summary = generator.from_api_data(experiment_data)

        return json.dumps({
            "success": True,
            "summary": summary.to_dict(),
            "text": summary.to_text(),
        })
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        })


@mcp.tool()
def compare_experiments(
    source1: str,
    source2: str,
    type1: str = "yaml",
    type2: str = "yaml"
) -> str:
    """
    Compare two experiments and return differences.

    Can compare YAML specs, JSON data, or existing API experiments.
    Useful for verifying that a YAML spec produces expected output,
    or comparing different versions of an experiment.

    Args:
        source1: First source - YAML content, JSON content, or experiment ID (as string)
        source2: Second source - YAML content, JSON content, or experiment ID (as string)
        type1: Type of source1 - 'yaml', 'json', or 'api'
        type2: Type of source2 - 'yaml', 'json', or 'api'

    Returns:
        JSON with comparison results:
        - identical: boolean indicating if experiments are identical
        - differences: list of differences by category
        - crawler_count_diff, job_count_diff: numeric differences
        - experiment1, experiment2: full summaries of each
    """
    try:
        from experiment_designer.summary import SummaryGenerator, compare_summaries
    except ImportError as e:
        return json.dumps({"error": f"experiment_designer.summary not available: {e}"})

    import httpx

    settings = get_settings()
    generator = SummaryGenerator()

    def get_api_data(experiment_id: str):
        """Fetch experiment data from API."""
        auth_response = httpx.post(
            f"{settings.pm_api_base_url}/auth/login",
            json={
                "email": settings.pm_api_email,
                "password": settings.pm_api_password,
            },
            timeout=30.0,
        )
        if auth_response.status_code != 200:
            raise Exception("API authentication failed")

        token = auth_response.json().get("access_token")
        headers = {"Authorization": f"Bearer {token}"}

        exp_response = httpx.get(
            f"{settings.pm_api_base_url}/experiments/{experiment_id}",
            headers=headers,
            timeout=30.0,
        )
        if exp_response.status_code != 200:
            raise Exception(f"Failed to fetch experiment {experiment_id}")

        return exp_response.json().get("data", exp_response.json())

    try:
        # Parse first source
        if type1 == "yaml":
            summary1 = generator.from_yaml_string(source1)
        elif type1 == "json":
            summary1 = generator.from_json_string(source1)
        elif type1 == "api":
            exp_data = get_api_data(source1)
            summary1 = generator.from_api_data(exp_data)
        else:
            return json.dumps({
                "success": False,
                "error": f"Unknown source type: {type1}. Use 'yaml', 'json', or 'api'",
            })

        # Parse second source
        if type2 == "yaml":
            summary2 = generator.from_yaml_string(source2)
        elif type2 == "json":
            summary2 = generator.from_json_string(source2)
        elif type2 == "api":
            exp_data = get_api_data(source2)
            summary2 = generator.from_api_data(exp_data)
        else:
            return json.dumps({
                "success": False,
                "error": f"Unknown source type: {type2}. Use 'yaml', 'json', or 'api'",
            })

        # Compare
        comparison = compare_summaries(summary1, summary2)

        return json.dumps({
            "success": True,
            "identical": comparison.is_identical(),
            "crawler_count_diff": comparison.crawler_count_diff,
            "job_count_diff": comparison.job_count_diff,
            "differences": comparison.differences,
            "crawlers_only_in_a": comparison.crawlers_only_in_a[:10],
            "crawlers_only_in_b": comparison.crawlers_only_in_b[:10],
            "experiment1": summary1.to_dict(),
            "experiment2": summary2.to_dict(),
        })
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        })


# =============================================================================
# RESOURCES
# =============================================================================


@mcp.resource("config://settings")
def get_current_settings() -> str:
    """Get current orchestrator configuration."""
    settings = get_settings()
    return json.dumps(
        {
            "env": settings.env,
            "grafana_url": settings.grafana_url,
            "loki_url": settings.loki_url,
            "prometheus_url": settings.prometheus_url,
            "tempo_url": settings.tempo_url,
            "pm_api_base_url": settings.pm_api_base_url,
            "lambda_function_name": settings.lambda_function_name,
        }
    )


def main():
    """
    Run the MCP server.

    Supports two transport modes:
    - stdio (default): For local Claude Code/Desktop integration
    - streamable-http: For remote access from Claude Code on another machine

    Configure via:
    - Environment variable: MCP_TRANSPORT=http
    - Command line: python -m orchestrator.mcp_server.server --http
    - Command line with port: python -m orchestrator.mcp_server.server --http --port 8100
    - Command line with host: python -m orchestrator.mcp_server.server --http --host 0.0.0.0

    SECURITY NOTE: By default, HTTP mode binds to localhost (127.0.0.1) only.
    Use --host 0.0.0.0 to allow external connections (ensure proper network security).
    """
    if _transport == "streamable-http":
        print(f"Starting MCP server in HTTP mode on {_host}:{_port}...", file=sys.stderr)

        # Security warning for external binding
        if _host == "0.0.0.0":
            print("WARNING: Server bound to all interfaces (0.0.0.0).", file=sys.stderr)
            print("         Ensure proper network security is in place.", file=sys.stderr)

        print(f"Connect with: claude mcp add --transport http primemover http://your-server:{_port}/mcp", file=sys.stderr)
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
