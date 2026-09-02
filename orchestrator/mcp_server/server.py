"""
PrimeMover Orchestrator MCP Server

Provides Claude Code with tools for monitoring, debugging, and managing experiments.
"""

import json
import os
import re as _re
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from orchestrator import status
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

# Import FastMCP after parsing args: creating the instance reads the host/port
# resolved above, so this import cannot move to the top of the module.
from mcp.server.fastmcp import FastMCP  # noqa: E402

# Initialize MCP server with correct settings for HTTP mode
if _transport == "streamable-http":
    mcp = FastMCP("primemover-orchestrator", host=_host, port=_port)
else:
    mcp = FastMCP("primemover-orchestrator")


# =============================================================================
# INPUT VALIDATION HELPERS
# =============================================================================

# Valid time range pattern (e.g., "15m", "1h", "24h", "7d")
_TIME_RANGE_PATTERN = _re.compile(r"^(\d{1,3})([mhd])$")

# Valid component names
_VALID_COMPONENTS = {
    "all",
    "api",
    "runner",
    "lambda",
    "apache",
    "mysql",
    "redis",
    "php",
    "memcached",
}

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
        raise ValueError(
            f"Invalid time_range format: {time_range}. Use format like '15m', '1h', '24h', '7d'"
        )
    return time_range.lower()


def _validate_component(component: str) -> str:
    """Validate component name."""
    if not isinstance(component, str):
        raise ValueError("component must be a string")
    component = component.lower()
    if component not in _VALID_COMPONENTS:
        raise ValueError(
            f"Invalid component: {component}. Valid: {', '.join(sorted(_VALID_COMPONENTS))}"
        )
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


def _validate_id(value: int | None, name: str) -> int | None:
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
    experiment_id: int | None = None,
    crawler_id: int | None = None,
    queue_id: int | None = None,
    job_id: int | None = None,
    limit: int = 100,
) -> str:
    """
    Query logs from all PrimeMover components.

    Args:
        component: Filter by component - 'all', 'api', 'runner', or 'lambda'
        time_range: Time range to search - '15m', '1h', '24h', '7d'
        level: Log level - 'all', 'debug', 'info', 'warning', 'error'. Matched on
               the promtail-set label, case-insensitively; 'warning' also matches
               the runner's Logback 'WARN'.
        search: Text pattern to search for in log messages
        experiment_id: Filter by experiment ID
        crawler_id: Filter by crawler ID
        queue_id: Filter by queue (joblist / run) ID
        job_id: Filter by job ID
        limit: Maximum number of results to return

    Returns:
        JSON array of log entries with timestamp, component, level, and message
    """
    import httpx

    from orchestrator.clients.loki import (
        build_label_selector,
        correlation_line_filters,
        escape_logql_string,
        level_label_matcher,
        range_query_params,
    )

    # Validate inputs
    try:
        component = _validate_component(component)
        time_range = _validate_time_range(time_range)
        level = _validate_level(level)
        search = _validate_search(search)
        experiment_id = _validate_id(experiment_id, "experiment_id")
        crawler_id = _validate_id(crawler_id, "crawler_id")
        queue_id = _validate_id(queue_id, "queue_id")
        job_id = _validate_id(job_id, "job_id")
        limit = _validate_limit(limit)
    except ValueError as e:
        return json.dumps({"error": f"Validation error: {e}"})

    settings = get_settings()

    # Build LogQL query with proper escaping. The query-building primitives live in
    # orchestrator.clients.loki so this tool and LokiClient cannot drift apart --
    # they already had, and only one of them matched any rows.
    label_selectors = []
    if component != "all":
        label_selectors.append(f'component="{component}"')
    level_matcher = level_label_matcher(level)
    if level_matcher:
        label_selectors.append(level_matcher)

    query = build_label_selector(label_selectors)

    # Add filters with proper escaping
    filters = []
    if search:
        escaped_search = escape_logql_string(search)
        filters.append(f'|~ "{escaped_search}"')
    # Correlation ids are line filters over the text the components print. The old
    # `| json | crawler_id="N"` stage dropped every entry, because the log lines are
    # plain text -- so every id-filtered query returned nothing, which reads as
    # "no errors found".
    filters.extend(
        correlation_line_filters(
            experiment_id=experiment_id,
            crawler_id=crawler_id,
            queue_id=queue_id,
            job_id=job_id,
        )
    )

    if filters:
        query += " " + " ".join(filters)

    try:
        # Without explicit start/end Loki defaults to the last hour, which silently
        # ignored every time_range this tool was given (find_failed_executions("24h")
        # and get_error_summary("7d") both really searched 1h).
        params: dict[str, str | int] = {
            "query": query,
            "limit": limit,
            **range_query_params(time_range),
        }
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
    # Query for errors. `query_logs` is wrapped by @mcp.tool(), whose decorator is
    # typed as Callable[..., Any], so annotate the result to keep it a str.
    result: str = query_logs(component=component, time_range=time_range, level="error", limit=500)

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
    result: str = query_logs(component=component, time_range="15m", search=filter_text, limit=lines)
    return result


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


#: How long a cached access token is reused before a fresh login. The API issues
#: JWTs that outlive this comfortably; the point of the ceiling is that a token
#: revoked server-side stops being replayed within the hour, not that we know the
#: exact expiry.
_TOKEN_TTL_SECONDS = 45 * 60

#: The cached access token: ``(settings, token, monotonic deadline)``, or ``None``.
#:
#: Deliberately a single slot keyed on the *identity* of the ``Settings`` instance
#: rather than on its values. A token is derived from the credentials in Settings,
#: so its lifetime is exactly that object's: ``get_settings`` is ``lru_cache``d, so
#: in a live server there is one instance and one token; anything that clears that
#: cache (a config reload, and the autouse fixture in the test suite) hands out a
#: new instance, and the next call re-authenticates instead of replaying a token
#: minted from credentials that may no longer be the ones in force.
_TOKEN_CACHE: tuple[Any, str, float] | None = None
_TOKEN_LOCK = threading.Lock()


def _clear_token_cache() -> None:
    """Forget the cached token. Used by the 401 retry path and by tests."""
    global _TOKEN_CACHE
    with _TOKEN_LOCK:
        _TOKEN_CACHE = None


def _api_token(client: Any, settings: Any, force: bool = False) -> str:
    """Return an access token, logging in only when there is no usable one.

    Every ``_api_request`` used to POST ``/login`` first. One ``trace experiment``
    or ``pm_compose_run`` is dozens of API calls, so a single MCP tool call meant
    dozens of logins -- each one burning a bcrypt verification on the API and
    writing two more ``Throttling request with IP`` lines into the log stream that
    ``trace job`` then has to filter back out. The login storm was polluting the
    very evidence it was being used to fetch.
    """
    global _TOKEN_CACHE
    now = time.monotonic()

    if not force:
        with _TOKEN_LOCK:
            cached = _TOKEN_CACHE
            if cached is not None and cached[0] is settings and cached[2] > now:
                return cached[1]

    # NOTE: the auth route is `/login` (not `/auth/login`).
    auth_response = client.post(
        f"{settings.pm_api_base_url}/login",
        headers={"Accept": "application/json"},
        json={
            "email": settings.pm_api_email,
            "password": settings.pm_api_password,
        },
    )

    if auth_response.status_code != 200:
        _clear_token_cache()
        raise Exception(f"API authentication failed ({auth_response.status_code})")

    token = auth_response.json().get("access_token")
    if not token:
        _clear_token_cache()
        raise Exception("API authentication returned no access_token")

    with _TOKEN_LOCK:
        _TOKEN_CACHE = (settings, str(token), time.monotonic() + _TOKEN_TTL_SECONDS)
    return str(token)


def _api_request(
    method: str,
    endpoint: str,
    data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Make an authenticated API request.

    Uses httpx.Client context manager to ensure connections are properly closed.
    The access token is cached across calls (see :func:`_api_token`) and refreshed
    once on a 401, so a token revoked or expired server-side self-heals rather
    than failing every tool until the process restarts.
    """
    import httpx

    settings = get_settings()

    url = f"{settings.pm_api_base_url}/{endpoint}"

    # Use context manager to ensure connections are closed
    with httpx.Client(timeout=30.0) as client:
        response = None
        for attempt in range(2):
            token = _api_token(client, settings, force=attempt > 0)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

            if method == "GET":
                response = client.get(url, headers=headers, params=params)
            elif method == "POST":
                response = client.post(url, headers=headers, json=data)
            elif method == "PUT":
                response = client.put(url, headers=headers, json=data)
            elif method == "DELETE":
                response = client.delete(url, headers=headers, params=params)
            else:
                raise Exception(f"Unsupported method: {method}")

            # A 401 means the cached token is no longer good. Drop it and log in
            # again -- once. Retrying further would turn a wrong password into the
            # login storm this cache exists to prevent.
            if response.status_code == 401 and attempt == 0:
                _clear_token_cache()
                continue
            break

        if response is None:  # unreachable: the loop assigns before it can break
            raise Exception("API request produced no response")

        if response.status_code >= 400:
            # Include the body so validation errors (422) are actionable.
            raise Exception(f"API request failed: {response.status_code} {response.text[:400]}")

        if response.status_code == 204 or not response.text:
            return {"status": response.status_code}
        body: dict[str, Any] = response.json()
        return body


# =============================================================================
# Write / control tools (dry-run by default, local-only) — see
# docs/specs/mcp-control-surface.md
# =============================================================================


def _require_local_write_target() -> str | None:
    """Return an error string if the API target is not the local dev server (:8090)."""
    from urllib.parse import urlparse

    parsed = urlparse(get_settings().pm_api_base_url)
    host, port = parsed.hostname, parsed.port
    if host not in ("localhost", "127.0.0.1") or (port is not None and port != 8090):
        return (
            f"MCP writes are restricted to the local dev API (:8090); "
            f"refusing to mutate {get_settings().pm_api_base_url}."
        )
    return None


def _unwrap(result: dict[str, Any]) -> Any:
    """API resources come back as {'data': {...}}; return the inner object when present."""
    return result.get("data", result) if isinstance(result, dict) else result


def _write(
    action: str,
    method: str,
    endpoint: str,
    payload: dict[str, Any] | None = None,
    *,
    confirm: bool = False,
    current: Any = None,
    warnings: list[str] | None = None,
) -> str:
    """Shared dry-run/confirm + local-guard path for every write tool."""
    guard = _require_local_write_target()
    if guard:
        return json.dumps({"error": guard})

    if not confirm:
        preview: dict[str, Any] = {
            "dry_run": True,
            "action": action,
            "method": method,
            "endpoint": endpoint,
            "payload": payload,
            "to_apply": "call again with confirm=true",
        }
        if current is not None:
            preview["current"] = current
        if warnings:
            preview["warnings"] = warnings
        return json.dumps(preview)

    try:
        result = _api_request(method, endpoint, data=payload)
        return json.dumps({"applied": True, "action": action, "result": _unwrap(result)})
    except Exception as e:
        return json.dumps({"applied": False, "action": action, "error": str(e)})


def _valid_job_api_types() -> list[str]:
    try:
        from experiment_designer.spec.job_registry import JobRegistry

        return [j.api_type for j in JobRegistry.list_all()]
    except Exception:
        return []


def _next_job_slot(queue_id: int) -> tuple[int, int | None]:
    """Return (order, previous_job_id) for appending a job to a queue's existing sequence."""
    try:
        result = _api_request("GET", "jobs", params={"filter[queue]": str(queue_id)})
        rows = result.get("data", []) if isinstance(result, dict) else (result or [])
        if not rows:
            return 1, None
        rows.sort(key=lambda j: j.get("order") or 0)
        return (rows[-1].get("order") or len(rows)) + 1, rows[-1].get("id")
    except Exception:
        return 1, None


@mcp.tool()
def list_library() -> str:
    """List the Library building blocks a Bot is composed from — agents (browser identity),
    proxies (connection) and configurations (profile) — with their ids and names. Use this to pick
    `agent_id`/`proxy_id`/`configuration_id` for pm_add_bot. Read-only (works on any configured API).
    """
    fields = ("id", "name", "description", "type", "active")
    # Per key: the rows, or an {"error": ...} object if that endpoint failed.
    out: dict[str, list[dict[str, Any]] | dict[str, str]] = {}
    for key, endpoint in (
        ("agents", "agents"),
        ("proxies", "proxies"),
        ("configurations", "configurations"),
    ):
        try:
            result = _api_request("GET", endpoint, params={"per_page": 100})
            rows = result.get("data", []) if isinstance(result, dict) else (result or [])
            out[key] = [{k: r.get(k) for k in fields if k in r} for r in rows]
        except Exception as e:
            out[key] = {"error": str(e)}
    return json.dumps(out)


@mcp.tool()
def pm_create_experiment(
    name: str,
    contact: str,
    description: str = "",
    testing: bool = False,
    runner_user_id: int | None = None,
    confirm: bool = False,
) -> str:
    """Create an experiment (study). Dry-run unless confirm=true. Local API only.

    Args:
        name: Study name.
        contact: Contact person (required by the API).
        description: Optional description.
        testing: Mark as a testing experiment (runner uses testing endpoints).
        runner_user_id: Which runner executes it: 1 = ex63 (the default when omitted),
            3 = the runner-only VM. Must be a configured runner account on the API.
        confirm: Apply the change; without it, returns a preview only.
    """
    payload: dict[str, Any] = {
        "name": name,
        "contact": contact,
        "description": description or name,
        "testing": testing,
    }
    if runner_user_id is not None:
        payload["runner_user_id"] = runner_user_id
    return _write("create experiment", "POST", "experiments", payload, confirm=confirm)


@mcp.tool()
def pm_add_bot(
    experiment_id: int,
    name: str,
    agent_id: int,
    proxy_id: int,
    configuration_id: int,
    description: str = "",
    confirm: bool = False,
) -> str:
    """Add a Bot (crawler) to an experiment from Library building blocks. Dry-run unless confirm=true.

    Args:
        experiment_id: Target experiment.
        name: Bot name.
        agent_id / proxy_id / configuration_id: Library block ids (browser identity / connection / profile).
        description: Optional description.
        confirm: Apply the change.
    """
    payload = {
        "experiment_id": experiment_id,
        "name": name,
        "description": description or name,
        "agent_id": agent_id,
        "proxy_id": proxy_id,
        "configuration_id": configuration_id,
    }
    return _write("add bot", "POST", "crawlers", payload, confirm=confirm)


@mcp.tool()
def pm_create_run(
    crawler_id: int,
    name: str,
    start_at: str,
    timezone: str = "Europe/Berlin",
    timezone_utc_offset: str = "+02:00",
    description: str = "",
    active: int = 0,
    confirm: bool = False,
) -> str:
    """Create/schedule a Run (queue) on a Bot. Dry-run unless confirm=true.

    Args:
        crawler_id: The bot this run belongs to.
        name: Run name.
        start_at: Local start datetime, e.g. '2026-08-01T09:00'.
        timezone / timezone_utc_offset: Scheduling timezone and its UTC offset (e.g. '+02:00').
        description: Optional (defaults to the run name; the API requires non-empty).
        active: 0 = scheduled/inactive, 1 = active. Compose jobs before activating.
        confirm: Apply the change.
    """
    payload = {
        "crawler_id": crawler_id,
        "name": name,
        "description": description or name,
        "start_at": start_at,
        "timezone": timezone,
        "timezone_utc_offset": timezone_utc_offset,
        "active": active,
    }
    return _write("create run", "POST", "queues", payload, confirm=confirm)


@mcp.tool()
def pm_activate_run(queue_id: int, active: int = 1, confirm: bool = False) -> str:
    """Activate (or deactivate) a Run so the runner will pick it up. Dry-run unless confirm=true."""
    return _write(
        f"set run {queue_id} active={active}",
        "PUT",
        f"queues/{queue_id}",
        {"active": active},
        confirm=confirm,
    )


@mcp.tool()
def pm_add_job(
    queue_id: int,
    type: str,
    name: str,
    order: int | None = None,
    description: str = "",
    active: int = 1,
    previous_job_id: int | None = None,
    confirm: bool = False,
) -> str:
    """Append a Job to a Run's sequence. `order`/`previous_job_id` auto-computed if omitted.
    Job parameters are set separately as behaviours (see pm_set_behaviour). Dry-run unless confirm=true.

    Args:
        queue_id: The run to add the job to.
        type: API job type, e.g. 'visitjob', 'waitjob', 'clickjob' (see list_job_types).
        name: Human label for the step.
        order: Position; if omitted, appended after the last job.
        description / active: optional.
        previous_job_id: Predecessor; if omitted, chained to the current last job (null for the first).
        confirm: Apply the change.
    """
    valid = _valid_job_api_types()
    if valid and type not in valid:
        return json.dumps({"error": f"Unknown job type '{type}'. Valid: {valid}"})
    if order is None or previous_job_id is None:
        auto_order, auto_prev = _next_job_slot(queue_id)
        order = auto_order if order is None else order
        previous_job_id = auto_prev if previous_job_id is None else previous_job_id
    payload = {
        "queue_id": queue_id,
        "type": type,
        "name": name,
        "description": description,
        "order": order,
        "active": active,
        "previous_job_id": previous_job_id,
    }
    return _write("add job", "POST", "jobs", payload, confirm=confirm)


@mcp.tool()
def pm_set_behaviour(
    job_id: int,
    name: str,
    value: str,
    description: str = "",
    active: bool = True,
    behaviour_id: int | None = None,
    confirm: bool = False,
) -> str:
    """Set a Job behaviour (its type-specific parameter, e.g. url/seconds/selector). Creates, or
    updates when behaviour_id is given. Dry-run unless confirm=true.

    Args:
        job_id: The job the behaviour belongs to.
        name: Parameter name (e.g. 'url').
        value: Parameter value (e.g. 'https://youtube.com').
        description / active: optional.
        behaviour_id: If set, updates that behaviour instead of creating a new one.
        confirm: Apply the change.
    """
    payload = {
        "job_id": job_id,
        "name": name,
        "value": value,
        "description": description,
        "active": active,
    }
    if behaviour_id:
        return _write(
            "update behaviour", "PUT", f"behaviors/{behaviour_id}", payload, confirm=confirm
        )
    return _write("set behaviour", "POST", "behaviors", payload, confirm=confirm)


@mcp.tool()
def pm_delete(entity: str, id: int, confirm: bool = False) -> str:
    """Delete an entity by id. entity in {experiment, bot, run, job, behaviour}.
    Dry-run (with a cascade warning) unless confirm=true. Local API only.
    """
    routes = {
        "experiment": (f"experiments/{id}", "its bots, runs, jobs and behaviours"),
        "bot": (f"crawlers/{id}/destroy", "its runs, jobs and behaviours"),
        "run": (f"queues/{id}", "its jobs and behaviours"),
        "job": (f"jobs/{id}", "its behaviours"),
        "behaviour": (f"behaviors/{id}", None),
    }
    if entity not in routes:
        return json.dumps({"error": f"Unknown entity '{entity}'. Use one of {list(routes)}."})
    endpoint, cascade = routes[entity]
    warnings = [f"Deleting this {entity} also removes {cascade}."] if cascade else None
    return _write(f"delete {entity} {id}", "DELETE", endpoint, confirm=confirm, warnings=warnings)


@mcp.tool()
def pm_compose_run(crawler_id: int, run: dict, jobs: list, confirm: bool = False) -> str:
    """Compose a whole Run in one call: create the run, then its ordered jobs (chained), each with
    its behaviours. Dry-run shows the full plan; confirm=true executes it. Local API only.

    Args:
        crawler_id: The bot to attach the run to.
        run: {name, start_at, timezone?, timezone_utc_offset?, description?} for the run.
        jobs: ordered list of {type, name, behaviours?: [{name, value}]} steps.
        confirm: Execute the plan.

    Example jobs: [
        {"type": "visitjob", "name": "Open YouTube", "behaviours": [{"name": "url", "value": "https://youtube.com"}]},
        {"type": "waitjob", "name": "Watch", "behaviours": [{"name": "seconds", "value": "30"}]}
    ]
    """
    guard = _require_local_write_target()
    if guard:
        return json.dumps({"error": guard})

    valid = _valid_job_api_types()
    bad = [j.get("type") for j in jobs if valid and j.get("type") not in valid]
    if bad:
        return json.dumps({"error": f"Unknown job type(s): {bad}. Valid: {valid}"})

    if not confirm:
        return json.dumps(
            {
                "dry_run": True,
                "action": "compose run",
                "plan": {
                    "create_run": {"crawler_id": crawler_id, **run},
                    "jobs": [
                        {
                            "type": j.get("type"),
                            "name": j.get("name"),
                            "behaviours": j.get("behaviours", []),
                        }
                        for j in jobs
                    ],
                },
                "to_apply": "call again with confirm=true",
            }
        )

    try:
        run_payload = {
            "crawler_id": crawler_id,
            "name": run["name"],
            "description": run.get("description") or run["name"],
            "start_at": run["start_at"],
            "timezone": run.get("timezone", "Europe/Berlin"),
            "timezone_utc_offset": run.get("timezone_utc_offset", "+02:00"),
            "active": run.get("active", 0),
        }
        queue = _unwrap(_api_request("POST", "queues", data=run_payload))
        queue_id = queue["id"]

        created_jobs = []
        prev = None
        for i, j in enumerate(jobs):
            job_payload = {
                "queue_id": queue_id,
                "type": j["type"],
                "name": j.get("name", j["type"]),
                "description": j.get("description", ""),
                "order": i + 1,
                "active": 1,
                "previous_job_id": prev,
            }
            job = _unwrap(_api_request("POST", "jobs", data=job_payload))
            prev = job["id"]
            behaviours = []
            for b in j.get("behaviours", []):
                bp = {
                    "job_id": job["id"],
                    "name": b["name"],
                    "value": str(b["value"]),
                    "description": b.get("description", ""),
                    "active": True,
                }
                behaviours.append(_unwrap(_api_request("POST", "behaviors", data=bp)).get("id"))
            created_jobs.append({"id": job["id"], "type": j["type"], "behaviour_ids": behaviours})

        return json.dumps(
            {"applied": True, "action": "compose run", "run_id": queue_id, "jobs": created_jobs}
        )
    except Exception as e:
        return json.dumps({"applied": False, "action": "compose run", "error": str(e)})


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
            valid_statuses = [
                "processed",
                "unprocessed",
                "reviewed",
                "inactive",
                "testing",
                "protected",
            ]
            if status not in valid_statuses:
                return json.dumps(
                    {"error": f"Invalid status. Must be one of: {', '.join(valid_statuses)}"}
                )
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
                return json.dumps(
                    {"error": f"Invalid status. Must be one of: {', '.join(valid_statuses)}"}
                )
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
        valid_experiment_id = _validate_id(experiment_id, "experiment_id")
        if valid_experiment_id is None:
            return json.dumps({"error": "experiment_id is required"})
        result = _api_request("GET", f"experiments/{valid_experiment_id}")
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
        valid_crawler_id = _validate_id(crawler_id, "crawler_id")
        if valid_crawler_id is None:
            return json.dumps({"error": "crawler_id is required"})
        result = _api_request("GET", f"crawlers/{valid_crawler_id}")
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
        valid_queue_id = _validate_id(queue_id, "queue_id")
        if valid_queue_id is None:
            return json.dumps({"error": "queue_id is required"})
        result = _api_request("GET", f"queues/{valid_queue_id}")
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
        valid_job_id = _validate_id(job_id, "job_id")
        if valid_job_id is None:
            return json.dumps({"error": "job_id is required"})
        result = _api_request("GET", f"jobs/{valid_job_id}")
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
            f"{settings.pm_api_base_url}/login",
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
        return json.dumps(
            {
                "success": True,
                "message": f"Experiment {experiment_id} download available",
                "size_mb": round(size_mb, 2),
                "note": "Use CLI 'orchestrator experiment download' to save the file",
            }
        )
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
            f"{settings.pm_api_base_url}/login",
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
        return json.dumps(
            {
                "success": True,
                "message": f"Report for job {job_id} download available",
                "size_kb": round(size_kb, 2),
                "note": "Use CLI 'orchestrator job report' to save the file",
            }
        )
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
        valid_experiment_id = _validate_id(experiment_id, "experiment_id")
        if valid_experiment_id is None:
            return json.dumps({"error": "experiment_id is required"})
        time_range = _validate_time_range(time_range)
    except ValueError as e:
        return json.dumps({"error": f"Validation error: {e}"})

    try:
        from orchestrator.tracing import TraceBuilder

        builder = TraceBuilder()

        try:
            trace = builder.trace_experiment(
                valid_experiment_id,
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
        valid_crawler_id = _validate_id(crawler_id, "crawler_id")
        if valid_crawler_id is None:
            return json.dumps({"error": "crawler_id is required"})
        time_range = _validate_time_range(time_range)
    except ValueError as e:
        return json.dumps({"error": f"Validation error: {e}"})

    try:
        from orchestrator.tracing import TraceBuilder

        builder = TraceBuilder()

        try:
            trace = builder.trace_crawler(
                valid_crawler_id,
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
        valid_job_id = _validate_id(job_id, "job_id")
        if valid_job_id is None:
            return json.dumps({"error": "job_id is required"})
        time_range = _validate_time_range(time_range)
    except ValueError as e:
        return json.dumps({"error": f"Validation error: {e}"})

    try:
        from orchestrator.tracing import TraceBuilder

        builder = TraceBuilder()

        try:
            trace = builder.trace_job(valid_job_id, time_range=time_range)
            return trace.to_json()
        finally:
            builder.close()

    except Exception as e:
        return json.dumps({"error": str(e)})


# How far the API scan walks back through the id-ordered jobs/queues listings
# before giving up. 20 x 100 covers roughly a fortnight of a busy run at the
# observed job rate; the scan also stops as soon as it walks past the window.
_MAX_STATUS_SCAN_PAGES = 20
_STATUS_SCAN_PAGE_SIZE = 100


def _parse_api_datetime(value: Any) -> datetime | None:
    """Parse the several timestamp spellings the API emits.

    ``updated_at`` is ``2026-08-27T07:54:03+02:00`` but ``finished_at`` is
    ``2026-08-27T07:54:03.008+0200`` -- no colon in the offset, which
    ``datetime.fromisoformat`` rejects before Python 3.11.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    text = _re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _experiment_crawler_ids(experiment_id: int) -> set[int]:
    """Crawler ids belonging to an experiment (``GET experiments/{id}`` embeds them)."""
    result = _api_request("GET", f"experiments/{experiment_id}")
    data = result.get("data", result) if isinstance(result, dict) else {}
    crawlers = data.get("crawlers") or [] if isinstance(data, dict) else []
    return {c["id"] for c in crawlers if isinstance(c, dict) and c.get("id") is not None}


def _scan_bad_executions(
    endpoint: str,
    cutoff: datetime,
    crawler_ids: set[int] | None,
    row_to_record: Callable[[dict[str, Any]], dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, bool]:
    """Walk the newest-first API listing for jobs/queues in a bad terminal state.

    Returns (records, rows_scanned, hit_page_cap). Rows are kept when
    ``status.is_bad`` holds -- i.e. failure *or* the new ``warning`` state, which
    is the whole point: a warning job is one that "succeeded" while silently
    degrading, and ``is_failure`` alone hides exactly those.

    The window is judged on when the row *ran* -- ``finished_at``, else
    ``started_at``, else (via ``status.started_at_of``) ``created_at``. Never on
    ``updated_at``: a bulk update bumps that on rows which last executed weeks
    ago, which both pulled a 2026-08-18 queue into a 24h window and stopped the
    scan from ever recognising that it had walked past the window. Scanning stops
    once a whole page falls outside the window -- per page, not per row, because
    creation order and execution order are not the same.
    """
    records: list[dict[str, Any]] = []
    scanned = 0
    page = 1
    hit_cap = False

    while page <= _MAX_STATUS_SCAN_PAGES:
        result = _api_request(
            "GET", endpoint, params={"page": page, "per_page": _STATUS_SCAN_PAGE_SIZE}
        )
        rows = result.get("data", []) if isinstance(result, dict) else []
        if not rows:
            break

        page_latest: datetime | None = None
        for row in rows:
            scanned += 1
            occurred = _parse_api_datetime(status.finished_at_of(row) or status.started_at_of(row))
            if occurred is not None and (page_latest is None or occurred > page_latest):
                page_latest = occurred
            if occurred is not None and occurred < cutoff:
                continue
            if not status.is_bad(status.status_of(row)):
                continue
            record = row_to_record(row)
            if crawler_ids is not None and record.get("crawler_id") not in crawler_ids:
                continue
            records.append(record)

        meta = result.get("meta", {}) if isinstance(result, dict) else {}
        last_page = meta.get("last_page")
        page_entirely_old = page_latest is not None and page_latest < cutoff
        if page_entirely_old or (isinstance(last_page, int) and page >= last_page):
            break
        page += 1
    else:
        hit_cap = True

    return records, scanned, hit_cap


def _job_record(row: dict[str, Any]) -> dict[str, Any]:
    queue = row.get("queue") or {}
    return {
        "kind": "job",
        "id": row.get("id"),
        "name": row.get("name"),
        "type": row.get("type"),
        "queue_id": row.get("queue_id"),
        "crawler_id": queue.get("crawler_id") if isinstance(queue, dict) else None,
        "status_code": status.status_of(row),
        "status_message": status.message_of(row) or None,
        "started_at": status.started_at_of(row),
        "finished_at": status.finished_at_of(row),
    }


def _queue_record(row: dict[str, Any]) -> dict[str, Any]:
    crawler = row.get("crawler") or {}
    return {
        "kind": "queue",
        "id": row.get("id"),
        "name": row.get("name"),
        "crawler_id": row.get("crawler_id"),
        "experiment_id": crawler.get("experiment_id") if isinstance(crawler, dict) else None,
        "status_code": status.status_of(row),
        "status_message": status.message_of(row) or None,
        "started_at": status.started_at_of(row),
        "finished_at": status.finished_at_of(row),
        "processing": row.get("processing"),
        "processed": row.get("processed"),
    }


@mcp.tool()
def find_failed_executions(
    time_range: str = "24h",
    experiment_id: int | None = None,
    limit: int = 50,
    include_logs: bool = True,
) -> str:
    """
    Find jobs and runs that ended badly -- status_code 'failure' OR 'warning'.

    This asks the API for the recorded outcome of every recent job and queue. It
    used to only grep the logs for ERROR lines, so a run that failed without
    logging an ERROR was invisible, and a job that completed while silently
    degrading (job data not written, or the decision function falling back to
    RANDOM video selection) was reported as a success.

    Args:
        time_range: How far back to look - '1h', '24h', '7d'
        experiment_id: Optional experiment ID filter
        limit: Maximum executions to return per kind (jobs, queues)
        include_logs: Also attach the secondary, log-derived ERROR signal

    Returns:
        JSON with the API-recorded bad executions (primary) and, separately
        labelled, the ERROR log lines in the same window (secondary).
    """
    from orchestrator.clients.loki import time_range_bounds

    try:
        time_range = _validate_time_range(time_range)
        experiment_id = _validate_id(experiment_id, "experiment_id")
        limit = _validate_limit(limit)
    except ValueError as e:
        return json.dumps({"error": f"Validation error: {e}"})

    cutoff, _ = time_range_bounds(time_range)

    out: dict[str, Any] = {
        "time_range": time_range,
        "experiment_id": experiment_id,
        "status_contract": (
            "status_code is one of running|success|warning|failure|skipped; "
            "'warning' means the job completed but something the experiment cares "
            "about degraded. See orchestrator/status.py."
        ),
    }

    executions: dict[str, Any] = {}
    try:
        crawler_ids = _experiment_crawler_ids(experiment_id) if experiment_id else None

        jobs, jobs_scanned, jobs_capped = _scan_bad_executions(
            "jobs", cutoff, crawler_ids, _job_record
        )
        queues, queues_scanned, queues_capped = _scan_bad_executions(
            "queues", cutoff, crawler_ids, _queue_record
        )

        def _worst_first(record: dict[str, Any]) -> tuple[int, Any]:
            severity = status.SEVERITY.get(record.get("status_code", ""), 0)
            return (-severity, -(record.get("id") or 0))

        jobs.sort(key=_worst_first)
        queues.sort(key=_worst_first)

        by_status: dict[str, int] = {}
        for record in jobs + queues:
            code = record.get("status_code", "unknown")
            by_status[code] = by_status.get(code, 0) + 1

        executions = {
            "total": len(jobs) + len(queues),
            "by_status": by_status,
            "jobs": jobs[:limit],
            "queues": queues[:limit],
            "truncated": len(jobs) > limit or len(queues) > limit,
            "scanned": {
                "jobs": jobs_scanned,
                "queues": queues_scanned,
                "page_cap_reached": jobs_capped or queues_capped,
                "note": (
                    "The API listings are ordered newest-id-first and the scan stops "
                    "at the first row older than the window, so a very old row touched "
                    "recently can be missed."
                ),
            },
        }
    except Exception as e:
        # An API outage must not look like a clean run.
        executions = {"error": f"API query failed: {e}", "total": None}

    out["executions"] = executions

    if include_logs:
        log_signal: dict[str, Any] = {
            "note": (
                "SECONDARY signal only. These are ERROR log lines in the window, not "
                "recorded outcomes: a run can fail with no ERROR line, and a degraded "
                "'warning' job usually logs none. Trust 'executions' first. WARN-level "
                "lines are not included here -- use query_logs(level='warning')."
            )
        }
        try:
            data = json.loads(
                query_logs(
                    level="error",
                    time_range=time_range,
                    experiment_id=experiment_id,
                    limit=200,
                )
            )
            if "error" in data:
                log_signal["error"] = data["error"]
            else:
                entries = data.get("entries", [])
                by_component: dict[str, int] = {}
                for entry in entries:
                    comp = entry.get("component", "unknown")
                    by_component[comp] = by_component.get(comp, 0) + 1
                log_signal["total_errors"] = len(entries)
                log_signal["by_component"] = by_component
                log_signal["recent_errors"] = entries[:20]
        except Exception as e:
            log_signal["error"] = str(e)

        out["log_signal"] = log_signal

    return json.dumps(out)


# =============================================================================
# LAMBDA TOOLS
# =============================================================================


@mcp.tool()
def get_lambda_invocations(
    time_range: str = "1h",
    crawler_id: int | None = None,
    experiment_id: int | None = None,
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
    result: str = query_logs(
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
    request_id: str | None = None,
    job_id: int | None = None,
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
            return json.dumps(
                {
                    "error": "CloudWatch is not available",
                    "hint": "Ensure AWS credentials are configured",
                }
            )

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

        return json.dumps({"count": len(entries), "entries": entries})

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
            return json.dumps(
                {
                    "error": "CloudWatch is not available",
                    "hint": "Ensure AWS credentials are configured",
                }
            )

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
            return json.dumps(
                {
                    "error": "CloudWatch is not available",
                    "hint": "Ensure AWS credentials are configured",
                }
            )

        invocation = client.get_invocation(request_id=request_id)

        if not invocation:
            return json.dumps(
                {
                    "error": f"Invocation not found: {request_id}",
                    "hint": "CloudWatch logs may take a few seconds to appear",
                }
            )

        result = {
            "request_id": invocation.request_id,
            "start_time": invocation.start_time.isoformat() if invocation.start_time else None,
            "end_time": invocation.end_time.isoformat() if invocation.end_time else None,
            "duration_ms": invocation.duration_ms,
            "billed_duration_ms": invocation.billed_duration_ms,
            "memory_size_mb": invocation.memory_size_mb,
            "max_memory_used_mb": invocation.memory_used_mb,
            "memory_utilization_pct": (
                (invocation.memory_used_mb / invocation.memory_size_mb * 100)
                if invocation.memory_size_mb and invocation.memory_used_mb
                else None
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
            return json.dumps(
                {
                    "error": "CloudWatch is not available",
                    "hint": "Ensure AWS credentials are configured",
                }
            )

        errors = client.get_recent_errors(time_range=time_range, limit=limit)

        if not errors:
            return json.dumps({"count": 0, "message": "No Lambda errors found", "errors": []})

        error_list = []
        for inv in errors:
            error_list.append(
                {
                    "request_id": inv.request_id,
                    "start_time": inv.start_time.isoformat() if inv.start_time else None,
                    "duration_ms": inv.duration_ms,
                    "is_cold_start": inv.is_cold_start,
                    "error": inv.error,
                    "log_count": len(inv.logs),
                }
            )

        client.close()

        return json.dumps({"count": len(error_list), "errors": error_list})

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
        from pathlib import Path

        import yaml as yaml_lib

        from experiment_designer.spec import SpecConverter, SpecParser, SpecValidator
        from experiment_designer.spec.file_loader import (
            create_file_tag_loader,
            resolve_file_references,
        )
    except ImportError as e:
        return json.dumps({"error": f"experiment_designer not available: {e}"})

    # Parse YAML with custom loader
    # SECURITY: FileTagLoader inherits from yaml.SafeLoader, which prevents
    # arbitrary code execution. The only custom tag is !file for loading
    # external data files (CSV/JSON), which are parsed safely.
    # base_dir anchors relative !file paths. The spec arrives here as a string
    # rather than a file, so there is no containing directory to inherit; the
    # working directory is the closest equivalent to how the CLI resolves them.
    FileTagLoader, file_loader = create_file_tag_loader(base_dir=Path.cwd())

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
    all_errors = list(set(validation_result.errors + parsed.errors + result.errors))
    all_warnings = list(set(validation_result.warnings + parsed.warnings + result.warnings))

    if strict:
        all_errors.extend(all_warnings)
        all_warnings = []

    return json.dumps(
        {
            "valid": len(all_errors) == 0,
            "errors": all_errors,
            "warnings": all_warnings,
            "summary": {
                "experiment_name": parsed.experiment.get("name", ""),
                "crawler_count": result.crawler_count,
                "total_jobs": result.total_jobs,
                "has_design": parsed.design is not None,
            },
            "design_info": validation_result.info.get("design_info"),
        }
    )


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
        return json.dumps(
            {
                "experiment_name": parsed.experiment.get("name", ""),
                "has_design": False,
                "crawler_count": len(parsed.crawlers),
                "message": "No factorial design specified",
            }
        )

    expander = DesignExpander()
    preview = expander.preview(parsed.design)

    return json.dumps(
        {
            "experiment_name": parsed.experiment.get("name", ""),
            "has_design": True,
            "design_type": preview.get("design_type", "factorial"),
            "factors": preview.get("factors", []),
            "replications": preview.get("replications", 1),
            "formula": preview.get("formula", ""),
            "total_cells": preview.get("total_cells", 0),
            "total_crawlers": preview.get("total_crawlers", 0),
        }
    )


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
        from experiment_designer.spec import SpecConverter, SpecParser
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

    return json.dumps(
        {
            "experiment_name": result.experiment_name,
            "experiment_description": result.experiment_description,
            "crawler_count": result.crawler_count,
            "total_jobs": result.total_jobs,
            "crawlers": result.json_data,
            "warnings": result.warnings,
        }
    )


@mcp.tool()
def upload_experiment(
    yaml_content: str,
    experiment_id: int | None = None,
    dry_run: bool = True,
    runner_user_id: int | None = None,
) -> str:
    """
    Convert YAML spec and upload to PrimeMover API.

    This tool validates the YAML specification, converts it to API-compatible
    JSON format, and uploads it to create a new experiment or add crawlers
    to an existing one.

    Previews by default. Pass dry_run=false to actually upload, and note that
    writes are refused unless the API target is the local dev server -- the same
    two rules every pm_ tool follows.

    Args:
        yaml_content: YAML experiment specification
        experiment_id: Optional experiment ID to add crawlers to (creates new if None)
        dry_run: Validate and convert without uploading. Defaults to True; pass
            False to apply.
        runner_user_id: Which runner executes it: 1 = ex63 (the default when omitted),
            3 = the runner-only VM. Must be a configured runner account on the API.

    Returns:
        JSON with upload result including experiment ID and crawler count,
        or validation errors if the spec is invalid
    """
    try:
        from datetime import datetime

        import requests

        from experiment_designer.spec import SpecConverter, SpecParser
    except ImportError as e:
        return json.dumps({"error": f"Required modules not available: {e}"})

    settings = get_settings()

    # Parse and validate
    parser = SpecParser()
    parsed = parser.parse(yaml_content)

    if parsed.errors:
        return json.dumps({"success": False, "errors": parsed.errors, "warnings": parsed.warnings})

    # Convert
    converter = SpecConverter()
    result = converter.convert(parsed)

    if result.errors:
        return json.dumps({"success": False, "errors": result.errors, "warnings": result.warnings})

    if dry_run:
        return json.dumps(
            {
                "success": True,
                "dry_run": True,
                "experiment_name": result.experiment_name,
                "crawler_count": result.crawler_count,
                "total_jobs": result.total_jobs,
                "warnings": result.warnings,
                "to_apply": "call again with dry_run=false",
            }
        )

    # Local-only, like every other write on this surface. Checked here rather
    # than at the top of the function because the dry-run branch above is pure
    # local YAML validation and conversion -- it touches no API at all, so it is
    # useful whatever the configured target is. This is the first line that can
    # actually change something.
    guard = _require_local_write_target()
    if guard:
        return json.dumps({"success": False, "error": guard})

    # Get credentials
    api_url = settings.pm_api_base_url
    email = settings.pm_api_email
    password = settings.pm_api_password

    if not all([api_url, email, password]):
        return json.dumps(
            {
                "success": False,
                "error": "API credentials not configured (PM_API_BASE_URL, PM_API_EMAIL, PM_API_PASSWORD)",
            }
        )

    try:
        # Authenticate
        auth_response = requests.post(
            f"{api_url}/login",
            json={"email": email, "password": password},
            headers={"Accept": "application/json"},
        )
        auth_response.raise_for_status()
        token = auth_response.json().get("access_token")

        if not token:
            return json.dumps(
                {"success": False, "error": "Authentication failed: no token received"}
            )

        # Create experiment if needed
        if not experiment_id:
            exp_name = (
                result.experiment_name or f"Experiment {datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )

            create_payload: dict[str, Any] = {
                "name": exp_name,
                "description": result.experiment_description or "Created via MCP",
                "contact": "PrimeMover Orchestrator MCP",
                "active": 1,
            }
            if runner_user_id is not None:
                create_payload["runner_user_id"] = runner_user_id
            elif (exp_runner_id := getattr(result, "experiment_runner_user_id", None)) is not None:
                create_payload["runner_user_id"] = exp_runner_id

            create_response = requests.post(
                f"{api_url}/experiments",
                json=create_payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
            create_response.raise_for_status()
            create_result = create_response.json()

            experiment_id = create_result.get("data", {}).get("id") or create_result.get("id")

            if not experiment_id:
                return json.dumps(
                    {"success": False, "error": "Failed to create experiment: no ID returned"}
                )

        # Upload crawlers
        upload_response = requests.post(
            f"{api_url}/experiments/{experiment_id}/upload",
            files={
                "json": (
                    "crawlers.json",
                    json.dumps(result.json_data, indent=2),
                    "application/json",
                )
            },
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        upload_response.raise_for_status()

        return json.dumps(
            {
                "success": True,
                "experiment_id": experiment_id,
                "experiment_name": result.experiment_name,
                "crawler_count": result.crawler_count,
                "total_jobs": result.total_jobs,
                "warnings": result.warnings,
            }
        )

    except requests.exceptions.HTTPError as e:
        error_detail = ""
        if hasattr(e, "response") and e.response is not None:
            try:
                error_detail = json.dumps(e.response.json())
            except Exception:
                error_detail = e.response.text[:500]
        return json.dumps(
            {"success": False, "error": f"Upload failed: {e}", "detail": error_detail}
        )
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

        job_types.append(
            {
                "yaml_name": job.yaml_name,
                "api_type": job.api_type,
                "description": job.description,
                "required_behaviors": required,
                "optional_behaviors": optional,
            }
        )

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
        return json.dumps({"error": f"Unknown job type: {job_type}", "available_types": available})

    behaviors = []
    for b in schema.behaviors:
        behaviors.append(
            {
                "name": b.name,
                "required": b.required,
                "default": b.default,
                "description": b.description,
            }
        )

    return json.dumps(
        {
            "yaml_name": schema.yaml_name,
            "api_type": schema.api_type,
            "display_name": schema.display_name,
            "description": schema.description,
            "behaviors": behaviors,
        }
    )


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
        "default": """# Experiment Specification
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
""",
        "youtube": """# YouTube Research Experiment
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
""",
        "google": """# Google Search Experiment
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
""",
    }

    template = templates.get(template_type, templates["default"])
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

        return json.dumps(
            {
                "success": True,
                "summary": summary.to_dict(),
                "text": summary.to_text(),
            }
        )
    except Exception as e:
        return json.dumps(
            {
                "success": False,
                "error": str(e),
            }
        )


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
            f"{settings.pm_api_base_url}/login",
            json={
                "email": settings.pm_api_email,
                "password": settings.pm_api_password,
            },
            timeout=30.0,
        )

        if auth_response.status_code != 200:
            return json.dumps(
                {
                    "success": False,
                    "error": "API authentication failed",
                }
            )

        token = auth_response.json().get("access_token")
        headers = {"Authorization": f"Bearer {token}"}

        # Fetch experiment with crawlers
        exp_response = httpx.get(
            f"{settings.pm_api_base_url}/experiments/{experiment_id}",
            headers=headers,
            timeout=30.0,
        )

        if exp_response.status_code != 200:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Failed to fetch experiment: {exp_response.status_code}",
                }
            )

        experiment_data = exp_response.json().get("data", exp_response.json())

        generator = SummaryGenerator()
        summary = generator.from_api_data(experiment_data)

        return json.dumps(
            {
                "success": True,
                "summary": summary.to_dict(),
                "text": summary.to_text(),
            }
        )
    except Exception as e:
        return json.dumps(
            {
                "success": False,
                "error": str(e),
            }
        )


@mcp.tool()
def compare_experiments(
    source1: str, source2: str, type1: str = "yaml", type2: str = "yaml"
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
            f"{settings.pm_api_base_url}/login",
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
            return json.dumps(
                {
                    "success": False,
                    "error": f"Unknown source type: {type1}. Use 'yaml', 'json', or 'api'",
                }
            )

        # Parse second source
        if type2 == "yaml":
            summary2 = generator.from_yaml_string(source2)
        elif type2 == "json":
            summary2 = generator.from_json_string(source2)
        elif type2 == "api":
            exp_data = get_api_data(source2)
            summary2 = generator.from_api_data(exp_data)
        else:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Unknown source type: {type2}. Use 'yaml', 'json', or 'api'",
                }
            )

        # Compare
        comparison = compare_summaries(summary1, summary2)

        return json.dumps(
            {
                "success": True,
                "identical": comparison.is_identical(),
                "crawler_count_diff": comparison.crawler_count_diff,
                "job_count_diff": comparison.job_count_diff,
                "differences": comparison.differences,
                "crawlers_only_in_a": comparison.crawlers_only_in_a[:10],
                "crawlers_only_in_b": comparison.crawlers_only_in_b[:10],
                "experiment1": summary1.to_dict(),
                "experiment2": summary2.to_dict(),
            }
        )
    except Exception as e:
        return json.dumps(
            {
                "success": False,
                "error": str(e),
            }
        )


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


def _auth_tokens_from_env() -> dict[str, str]:
    """Named bearer tokens, ``{name: token}``.

    ``MCP_AUTH_TOKENS`` is ``name:token,name:token`` -- one per person, so a
    request can be logged against a name and one person can be revoked without
    rotating everyone. The older single ``MCP_AUTH_TOKEN`` is still honoured as
    the name ``default``. Malformed entries raise rather than being skipped: a
    typo that silently drops a token looks, from the outside, like revocation.
    """
    tokens: dict[str, str] = {}
    legacy = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    if legacy:
        tokens["default"] = legacy

    for entry in os.environ.get("MCP_AUTH_TOKENS", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, token = entry.partition(":")
        name, token = name.strip(), token.strip()
        if not sep or not name or not token:
            raise ValueError(f"MCP_AUTH_TOKENS entry is not name:token: {entry[:20]!r}")
        if len(token) < 16:
            raise ValueError(f"MCP_AUTH_TOKENS token for {name!r} is too short (min 16 chars)")
        if name in tokens:
            raise ValueError(f"MCP_AUTH_TOKENS has duplicate name {name!r}")
        tokens[name] = token

    return tokens


def _auth_token_from_env() -> str | None:
    """Kept for callers of the single-token form; prefer _auth_tokens_from_env."""
    token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    return token or None


def build_http_app(tokens: dict[str, str] | str):
    """Wrap the streamable-HTTP ASGI app so every MCP request needs a bearer token.

    The server has no notion of users: whoever reaches the port gets every tool,
    including the ``pm_`` writes. Network-level controls (a tailnet allow-list, a
    tunnel) say who can reach it; the token says who may use it -- and which
    name that was is logged per request. ``GET /health`` stays open so the edge
    proxy and the boot check can probe liveness.
    """
    import hmac
    import logging

    from starlette.responses import JSONResponse, PlainTextResponse

    if isinstance(tokens, str):
        tokens = {"default": tokens}
    if not tokens:
        raise ValueError("build_http_app needs at least one token")

    log = logging.getLogger("orchestrator.mcp.auth")
    inner = mcp.streamable_http_app()

    def _identify(presented: str) -> str | None:
        # Check every token so timing does not depend on which name matched.
        matched = None
        for name, token in tokens.items():
            if hmac.compare_digest(presented, token):
                matched = name
        return matched

    async def app(scope, receive, send):
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return

        if scope["method"] == "GET" and scope["path"] == "/health":
            await PlainTextResponse("ok")(scope, receive, send)
            return

        auth = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                auth = value.decode("latin-1")
                break
        scheme, _, presented = auth.partition(" ")
        client = (scope.get("client") or ("?", 0))[0]
        user = _identify(presented.strip()) if scheme.lower() == "bearer" else None
        if user is None:
            log.warning("mcp auth rejected client=%s path=%s", client, scope["path"])
            await JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return

        log.info(
            "mcp request user=%s client=%s %s %s", user, client, scope["method"], scope["path"]
        )
        scope.setdefault("state", {})["mcp_user"] = user
        await inner(scope, receive, send)

    return app


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
    Use --host to bind elsewhere (ensure proper network security). HTTP mode
    always requires MCP_AUTH_TOKENS (named, one per person); requests without a matching bearer token
    get 401. ``GET /health`` is the only unauthenticated route.
    """
    if _transport == "streamable-http":
        print(f"Starting MCP server in HTTP mode on {_host}:{_port}...", file=sys.stderr)

        # Security warning for external binding
        if _host == "0.0.0.0":
            print("WARNING: Server bound to all interfaces (0.0.0.0).", file=sys.stderr)
            print("         Ensure proper network security is in place.", file=sys.stderr)

        try:
            tokens = _auth_tokens_from_env()
        except ValueError as exc:
            print(f"ERROR: {exc}. Refusing to start.", file=sys.stderr)
            sys.exit(2)
        if not tokens:
            # Fail closed: an unauthenticated HTTP MCP server hands out the
            # write tools to anyone who can reach the port.
            print(
                "ERROR: HTTP mode requires MCP_AUTH_TOKENS (name:token,... ; tokens from "
                "`openssl rand -hex 32`) in the environment or .env. Refusing to start.",
                file=sys.stderr,
            )
            sys.exit(2)
        print(f"MCP bearer tokens loaded for: {', '.join(sorted(tokens))}", file=sys.stderr)

        print(
            "Connect with: claude mcp add --transport http primemover "
            f"http://your-server:{_port}/mcp --header 'Authorization: Bearer <your token>'",
            file=sys.stderr,
        )

        import uvicorn

        uvicorn.run(build_http_app(tokens), host=_host, port=_port, log_level="info")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
