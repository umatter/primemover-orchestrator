"""
PrimeMover Orchestrator MCP Server

Provides Claude Code with tools for monitoring, debugging, and managing experiments.
"""

import json
import os
import re as _re
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from experiment_designer.lock.build import compile_spec
from experiment_designer.lock.diff import diff_locks
from experiment_designer.lock.io import LockError, dump_lock, is_lock_document, load_lock
from experiment_designer.lock.secrets import PLACEHOLDER_RE, SecretStore, find_placeholders
from experiment_designer.lock.status import experiment_status
from experiment_designer.lock.upload import MODES, UploadOptions, UploadRefused, upload_lock
from experiment_designer.lock.verify import Difference, verify_lock
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

    def check_health(
        name: str,
        url: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        healthy_codes: tuple[int, ...] | None = None,
    ) -> dict:
        try:
            response = httpx.get(f"{url.rstrip('/')}{path}", timeout=5.0, headers=headers)
            healthy = (
                response.status_code in healthy_codes
                if healthy_codes is not None
                else response.status_code < 400
            )
            return {
                "name": name,
                "status": "healthy" if healthy else "unhealthy",
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
    ]

    # The Laravel API has no unauthenticated health route: every /api/v1 path
    # sits behind `auth:api`. So a **401 is the healthy answer** -- it proves the
    # app booted, routing resolved and the auth middleware ran.
    #
    # Two separate traps made the old probe (`GET /api/health`, healthy iff
    # <400) report a perfectly healthy API as unhealthy:
    #   * `api/health` is not a defined route. It 404s with or without the
    #     Accept header below -- adding the header alone does not fix it.
    #   * On a route that *does* exist, Laravel answers an unauthenticated
    #     non-JSON request by redirecting to a `login` route this API never
    #     defines, so the probe sees a 404 instead of the 401. `Accept:
    #     application/json` is what turns that into `{"message":
    #     "Unauthenticated."}` with a 401.
    #
    # This is liveness only: a 401 says the PHP app is serving, not that MySQL
    # behind it is reachable.
    api_check = check_health(
        "primemover_api",
        settings.pm_api_base_url,
        "/crawlers",
        headers={"Accept": "application/json"},
        healthy_codes=(200, 401),
    )
    if api_check.get("code") == 401:
        api_check["note"] = "401 is expected and healthy: the API requires auth on every route"
    components.append(api_check)

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


def _materialise_files(files: dict[str, str] | None) -> tempfile.TemporaryDirectory[str]:
    """Pools passed as content become files in a temp dir used as the spec's base_dir.

    Callers of the MCP tools have no filesystem in common with this process, so a
    data pool's ``source:`` file has to arrive as text and be written out before
    ``SpecParser``/``compile_spec`` (which resolve pool sources relative to a
    ``base_dir`` path) can see it.
    """
    tmp = tempfile.TemporaryDirectory(prefix="pmspec-")
    for name, content in (files or {}).items():
        target = Path(tmp.name) / Path(name).name  # no directory traversal
        target.write_text(content)
    return tmp


class _McpSession:
    """ApiSession-shaped adapter over the MCP's own authenticated request path.

    ``upload_lock``/``verify_lock`` are written against a small session protocol
    (get/post/put/post_file/get_bytes/base_url) so they do not have to know how
    this process authenticates. ``get``/``post``/``put`` delegate to
    ``_api_request`` so they share its token cache and 401-retry; ``post_file``
    and ``get_bytes`` are the two things ``_api_request`` cannot do (multipart
    upload, raw bytes) and use ``httpx`` directly with the same token.
    """

    def __init__(self) -> None:
        self.base_url = get_settings().pm_api_base_url

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return _unwrap(_api_request("GET", path, params=params))

    def post(self, path: str, json: dict[str, Any]) -> Any:
        return _unwrap(_api_request("POST", path, data=json))

    def put(self, path: str, json: dict[str, Any]) -> Any:
        return _unwrap(_api_request("PUT", path, data=json))

    def post_file(
        self, path: str, field: str, filename: str, content: str, content_type: str
    ) -> Any:
        # Mutating and not expressible through _api_request (multipart). Not
        # guarded here: the only caller, upload_experiment, checks
        # _require_local_write_target() before it ever constructs a session --
        # see the AST-scan note in test_mcp_write_guard.py for why that still
        # counts as guarded.
        import httpx

        settings = get_settings()
        with httpx.Client(timeout=300.0) as client:
            token = _api_token(client, settings)
            r = client.post(
                f"{settings.pm_api_base_url}/{path}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                files={field: (filename, content, content_type)},
            )
            # Mirror _api_request's error construction: a crawler-upload 422's body
            # is the informative part (which job/behaviour failed validation), and
            # raise_for_status() alone would discard it.
            if r.status_code >= 400:
                raise Exception(f"API request failed: {r.status_code} {r.text[:400]}")
            return _unwrap(r.json()) if r.content else {}

    def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        import httpx

        settings = get_settings()
        with httpx.Client(timeout=300.0) as client:
            token = _api_token(client, settings)
            r = client.get(
                f"{settings.pm_api_base_url}/{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            return r.content


def _redact_differences(differences: list[Difference]) -> list[Difference]:
    """A verify Difference's ``actual`` side is a value read straight off the live
    experiment. When ``expected`` is a secret placeholder (a lock keeps
    ``${secret:...}`` for anything outside ``proxy.*`` too, e.g. a preference or
    behaviour value), ``actual`` is that placeholder's *resolved* value -- the
    literal secret. Nothing that serialises a Difference (a JSON tool result, a
    rendered report) may carry that, so every placeholder-valued difference is
    replaced before it is ever rendered or returned.
    """
    return [
        (
            Difference(d.path, d.expected, "<redacted>")
            if isinstance(d.expected, str) and PLACEHOLDER_RE.search(d.expected)
            else d
        )
        for d in differences
    ]


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
            None means "not supplied" -- there is no way to force the default
            runner from this argument; omit it and the API applies its own default.
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


# What compile_experiment_spec accepts as a caller-supplied commit: a short or
# full hex SHA, optionally "-dirty" as the pmspec CLI records it.
_CLIENT_COMMIT_RE = _re.compile(r"[0-9a-f]{7,40}(-dirty)?")


@mcp.tool()
def compile_experiment_spec(
    yaml_content: str,
    files: dict[str, str] | None = None,
    seed: int | None = None,
    anchor: str | None = None,
    generator: str | None = None,
    lax: bool = False,
    git_commit: str | None = None,
    spec_path: str | None = None,
) -> str:
    """Compile a YAML spec into a lock: the self-contained, committable artifact
    upload_experiment and verify_experiment read.

    Provenance is the caller's to supply. This compiles from content in a
    temporary directory, not from a checkout, so it cannot see the repo the
    spec lives in: without ``git_commit`` the lock records ``git_commit: null``
    and ``spec_path: <mcp>``, and a `real`-mode ``upload_experiment`` of it is
    refused. Pass the spec repo's HEAD (``git rev-parse --short HEAD``, with a
    ``-dirty`` suffix if ``git status --porcelain`` is non-empty) and the spec's
    path relative to that repo, and the lock records them verbatim -- the same
    facts the pmspec CLI would have gathered itself. A commit is a claim the
    caller makes; `verify` checks the lock hash, not the commit.

    Args:
        yaml_content: the spec
        files: data-pool files by the bare filename the spec's `source:` names,
            content as text -- this is how a pool reaches a content-only caller
        seed: the one seed; required unless the spec sets design.seed
        anchor: 'upload' or a Europe/Zurich wall-clock ISO time; default derived from the spec
        generator: free-text provenance ("code/design/build_spec.R")
        lax: downgrade strict-contract errors to warnings (recorded in the lock)
        git_commit: the spec repo's short HEAD, optionally suffixed "-dirty"
        spec_path: the spec's path relative to that repo's root

    Returns:
        JSON: {"lock": <text>, "crawler_count", "seed", "anchor", "git_commit",
        "spec_path", "warnings", "dropped"} or {"errors": [...], "warnings", "dropped"}
    """
    if git_commit is not None and not _CLIENT_COMMIT_RE.fullmatch(git_commit):
        return json.dumps(
            {
                "errors": [
                    f"git_commit {git_commit!r} is not a short or full SHA "
                    "(optionally suffixed '-dirty')"
                ],
                "warnings": [],
            }
        )
    with _materialise_files(files) as tmp:
        result = compile_spec(
            yaml_content,
            base_dir=Path(tmp),
            spec_path="<mcp>",
            seed=seed,
            anchor=anchor,
            generator=generator,
            lax=lax,
            git_root=None,
        )
    if result.lock is None:
        return json.dumps(
            {"errors": result.errors, "warnings": result.warnings, "dropped": result.dropped}
        )
    # Whatever the temp dir's parent happens to be, this lock came from no
    # checkout: only the caller's word is recorded, never the temp dir's git.
    result.lock.provenance.git_commit = git_commit
    result.lock.provenance.spec_path = spec_path or "<mcp>"
    return json.dumps(
        {
            "lock": dump_lock(result.lock),
            "crawler_count": len(result.lock.crawlers),
            "seed": result.lock.provenance.seed,
            "anchor": result.lock.schedule["anchor"],
            "git_commit": result.lock.provenance.git_commit,
            "spec_path": result.lock.provenance.spec_path,
            "warnings": result.warnings,
            "dropped": result.dropped,
        }
    )


@mcp.tool()
def validate_experiment_spec(
    yaml_content: str,
    strict: bool = False,
    lax: bool = False,
    files: dict[str, str] | None = None,
) -> str:
    """
    Validate a YAML experiment specification, or a lock.

    A lock (``pmspec_lock: ...``) is validated against the lock schema and
    reported as ``{"valid", "kind": "lock", ...}``. A spec is validated
    structurally, semantically, and against the design, reported as
    ``{"valid", "kind": "spec", ...}``.

    Args:
        yaml_content: YAML spec content, or a lock
        strict: If True, treat warnings as errors (spec only)
        lax: If True, downgrade strict-contract errors (drops) to warnings (spec only)
        files: data-pool files by the bare filename the spec's `source:` names,
            content as text -- materialised into a temp dir used as base_dir

    Returns:
        JSON with validation results including errors, warnings, and summary
    """
    if is_lock_document(yaml_content):
        try:
            lock = load_lock(yaml_content)
        except LockError as e:
            return json.dumps({"valid": False, "kind": "lock", "errors": e.errors, "warnings": []})
        return json.dumps(
            {
                "valid": True,
                "kind": "lock",
                "errors": [],
                "warnings": [],
                "summary": {
                    "experiment_name": lock.experiment["name"],
                    "crawler_count": len(lock.crawlers),
                    "anchor": lock.schedule["anchor"],
                    "seed": lock.provenance.seed,
                },
            }
        )

    try:
        import yaml as yaml_lib

        from experiment_designer.lock.build import merge_reports
        from experiment_designer.spec import SpecConverter, SpecParser, SpecValidator
        from experiment_designer.spec.file_loader import (
            create_file_tag_loader,
            resolve_file_references,
        )
    except ImportError as e:
        return json.dumps({"error": f"experiment_designer not available: {e}"})

    with _materialise_files(files) as tmp:
        base_dir = Path(tmp)

        # Parse YAML with custom loader
        # SECURITY: FileTagLoader inherits from yaml.SafeLoader, which prevents
        # arbitrary code execution. The only custom tag is !file for loading
        # external data files (CSV/JSON), which are parsed safely.
        # base_dir anchors relative !file paths and data-pool `source:` files to
        # the temp dir any `files` were materialised into.
        FileTagLoader, file_loader = create_file_tag_loader(base_dir=base_dir)

        try:
            raw_spec = yaml_lib.load(yaml_content, Loader=FileTagLoader)
            raw_spec = resolve_file_references(raw_spec, file_loader)
        except yaml_lib.YAMLError as e:
            return json.dumps(
                {
                    "valid": False,
                    "kind": "spec",
                    "errors": [f"YAML parse error: {e}"],
                    "warnings": [],
                }
            )

        # Run validator
        validator = SpecValidator(base_dir=base_dir, lax=lax)
        validation_result = validator.validate(raw_spec)

        # Also parse and convert to catch additional errors. All three
        # components get `lax`, or a parser/converter drop stays an error under
        # lax=True; the validator has already named every unknown workflow
        # item, so the parser does not report it again.
        parser = SpecParser(base_dir=base_dir, lax=lax, report_unknown_items=False)
        parsed = parser.parse(yaml_content)

        converter = SpecConverter(lax=lax)
        result = converter.convert(parsed)

    # Combine all errors and warnings: de-duplicated, one drop-count line
    all_errors, all_warnings = merge_reports(
        [validation_result.errors, parsed.errors, result.errors],
        [validation_result.warnings, parsed.warnings, result.warnings],
        validation_result.dropped + parsed.dropped + result.dropped,
        lax,
    )

    if strict:
        all_errors.extend(all_warnings)
        all_warnings = []

    return json.dumps(
        {
            "valid": len(all_errors) == 0,
            "kind": "spec",
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

    Prefer compile_experiment_spec: this output carries no provenance (no seed,
    no git commit, no lock hash to bind an upload to), so it cannot be verified
    later and should not be used as an experiment's system of record.

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
    lock_content: str,
    mode: str,
    experiment_id: int | None = None,
    dry_run: bool = True,
    deep_verify: bool = False,
) -> str:
    """Upload a LOCK (from compile_experiment_spec or `pmspec compile`) and verify it.

    Dry-run by default; local-only (the API target must be localhost:8090).

    Args:
        lock_content: a lock, not a spec -- call compile_experiment_spec first
        mode: 'test' -> VM runner, name becomes 'test-<name>-<date>', dirty commit
            allowed; 'real' -> as named in the lock, refused without a clean commit
        experiment_id: append to this experiment instead of creating one
        dry_run: preview only. Defaults to True; pass False to apply.
        deep_verify: also compare each queue's jobs/behaviours, not just schedule

    Secrets: ${secret:...} placeholders resolve from PMSPEC_SECRETS_FILE on this host.

    Returns:
        JSON with the upload result and a bound-and-identical verify, or an error
    """
    if mode not in MODES:
        return json.dumps({"success": False, "error": f"mode must be one of {MODES}"})
    if not is_lock_document(lock_content):
        return json.dumps(
            {"success": False, "error": "not a lock: call compile_experiment_spec first"}
        )
    try:
        lock = load_lock(lock_content)
    except LockError as e:
        return json.dumps({"success": False, "error": "invalid lock", "errors": e.errors})

    needed = sorted(find_placeholders(lock.crawlers))
    if dry_run:
        return json.dumps(
            {
                "success": True,
                "dry_run": True,
                "mode": mode,
                "experiment_name": lock.experiment["name"],
                "crawler_count": len(lock.crawlers),
                "anchor": lock.schedule["anchor"],
                "git_commit": lock.provenance.git_commit,
                "secrets_needed": needed,
                "to_apply": "call again with dry_run=false",
            }
        )

    # Local-only, like every other write on this surface. Checked here rather
    # than inside the dry-run branch above, which is pure local validation and
    # touches no API at all -- this is the first line that can actually change
    # something, and _McpSession's post_file relies on this having run first.
    guard = _require_local_write_target()
    if guard:
        return json.dumps({"success": False, "error": guard})

    store = None
    if needed:
        try:
            store = SecretStore.load()
        except FileNotFoundError:
            return json.dumps(
                {
                    "success": False,
                    "error": "lock needs secrets but no secrets file exists on this "
                    "host (PMSPEC_SECRETS_FILE)",
                }
            )
        except PermissionError as e:
            return json.dumps({"success": False, "error": str(e)})

    session = _McpSession()
    options = UploadOptions(
        mode=mode, experiment_id=experiment_id, contact="PrimeMover Orchestrator MCP"
    )
    try:
        result = upload_lock(lock_content, session=session, store=store, options=options)
    except UploadRefused as e:
        return json.dumps({"success": False, "error": str(e)})
    except Exception as e:
        return json.dumps({"success": False, "error": f"Upload failed: {e}"})

    response: dict[str, Any] = {
        "success": True,
        "experiment_id": result.experiment_id,
        "experiment_name": result.experiment_name,
        "crawler_count": result.crawler_count,
        "total_jobs": result.total_jobs,
        "mode": result.mode,
        "runner_user_id": result.runner_user_id,
        "lock_sha256": result.lock_sha256,
        "anchor": result.anchor,
        "record": result.record_line,
    }

    # Outside any earlier try: the upload itself already succeeded, and an
    # exception here (an httpx failure fetching the export under deep_verify,
    # an unexpected API shape) must not turn into a raised tool error that
    # loses the experiment_id -- an LLM seeing that error and no id is liable
    # to retry with dry_run=False and create a duplicate experiment.
    try:
        verdict = verify_lock(
            lock_content, session=session, experiment_id=result.experiment_id, deep=deep_verify
        )
    except Exception as e:
        response["verify"] = {"error": str(e)}
        return json.dumps(response)

    verdict.differences = _redact_differences(verdict.differences)
    response["verify"] = {
        "ok": verdict.ok,
        "bound": verdict.bound,
        "differences": [d.__dict__ for d in verdict.differences],
        "render": verdict.render(),
    }
    return json.dumps(response)


@mcp.tool()
def verify_experiment(experiment_id: int, lock_content: str, deep: bool = False) -> str:
    """Is the live experiment bound to this lock (hash) and structurally identical to it?

    Args:
        experiment_id: the live experiment to check
        lock_content: the lock it should match
        deep: also compare each queue's jobs/behaviours, not just schedule (slower:
            downloads the experiment export)

    Returns:
        JSON: {"ok", "bound", "local_sha256", "live_sha256s", "anchor", "differences",
        "extra_live_crawlers", "render"}. Only the lock's own crawlers are compared;
        crawlers the experiment holds from other uploads are listed, not diffed.
    """
    try:
        r = verify_lock(lock_content, session=_McpSession(), experiment_id=experiment_id, deep=deep)
    except LockError as e:
        return json.dumps({"error": "invalid lock", "errors": e.errors})
    except Exception as e:
        return json.dumps({"error": str(e)})
    r.differences = _redact_differences(r.differences)
    return json.dumps(
        {
            "experiment_id": r.experiment_id,
            "ok": r.ok,
            "bound": r.bound,
            "local_sha256": r.local_sha256,
            "live_sha256s": r.live_sha256s,
            "anchor": r.anchor,
            "deep": r.deep,
            "differences": [d.__dict__ for d in r.differences],
            "extra_live_crawlers": r.extra_live_crawlers,
            "render": r.render(),
        }
    )


@mcp.tool(name="experiment_status")
def experiment_status_tool(
    experiment_id: int,
    hours: int = 24,
    lock_content: str | None = None,
    deep: bool = False,
) -> str:
    """Provenance (what pmspec uploaded) plus health (how it is running) for a live
    experiment, and -- when a lock is given -- whether the two still agree.

    Args:
        experiment_id: the live experiment to inspect
        hours: health window in hours
        lock_content: optional lock text; when given, also runs verify against it
        deep: with lock_content, also compare each queue's jobs/behaviours

    Returns:
        JSON: {"experiment_id", "name", "runner_user_id", "uploads": [{"label", "values"}],
        "health", "ok", "bound_to_any_lock", "verify": {...}|None, "render"}.
    """
    try:
        st = experiment_status(
            _McpSession(), experiment_id, hours=hours, lock_text=lock_content, deep=deep
        )
    except LockError as e:
        return json.dumps({"error": "invalid lock", "errors": e.errors})
    except Exception as e:
        return json.dumps({"error": str(e)})

    verify_payload = None
    if st.verify is not None:
        st.verify.differences = _redact_differences(st.verify.differences)
        verify_payload = {
            "ok": st.verify.ok,
            "bound": st.verify.bound,
            "local_sha256": st.verify.local_sha256,
            "live_sha256s": st.verify.live_sha256s,
            "anchor": st.verify.anchor,
            "deep": st.verify.deep,
            "differences": [d.__dict__ for d in st.verify.differences],
            "extra_live_crawlers": st.verify.extra_live_crawlers,
            "render": st.verify.render(),
        }

    return json.dumps(
        {
            "experiment_id": st.experiment_id,
            "name": st.name,
            "runner_user_id": st.runner_user_id,
            "uploads": [{"label": u.label, "values": u.values} for u in st.uploads],
            "health": st.health,
            "ok": st.ok,
            "bound_to_any_lock": st.bound_to_any_lock,
            "verify": verify_payload,
            "render": st.render(),
        }
    )


@mcp.tool(name="diff_locks")
def diff_locks_tool(lock_a: str, lock_b: str) -> str:
    """Diff two locks: provenance side by side, anchor, and crawler-level structural
    differences (built at a fixed reference anchor, so a re-anchored-but-otherwise
    -identical lock differs only in the anchor line). Read-only, purely local -- no
    live experiment or API call involved.

    Args:
        lock_a: first lock's text
        lock_b: second lock's text

    Returns:
        JSON: {"identical", "differences", "only_in_a", "only_in_b", "anchor_a",
        "anchor_b", "render"} or {"error": ..., "errors": [...]}
    """
    try:
        d = diff_locks(lock_a, lock_b)
    except LockError as e:
        return json.dumps({"error": "invalid lock", "errors": e.errors})

    d.differences = _redact_differences(d.differences)
    return json.dumps(
        {
            "identical": d.identical,
            "differences": [x.__dict__ for x in d.differences],
            "only_in_a": d.only_in_a,
            "only_in_b": d.only_in_b,
            "anchor_a": d.anchor_a,
            "anchor_b": d.anchor_b,
            "render": d.render(),
        }
    )


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
        job_type: Either the DSL yaml_name ('visit', 'scroll') or the API type
            ('visitjob', 'scrollbydurationjob'). Both are accepted -- the DSL uses
            yaml_name, while the pm_add_job / pm_compose_run write tools take the
            api_type, so this looks up by either to avoid that namespace trap.

    Returns:
        JSON with detailed job schema including behavior descriptions
    """
    try:
        from experiment_designer.spec.job_registry import JobRegistry
    except ImportError as e:
        return json.dumps({"error": f"experiment_designer not available: {e}"})

    schema = JobRegistry.get_schema(job_type)
    if not schema:
        # Fall back to an api_type lookup so callers holding the write-tool name
        # (e.g. 'visitjob') still resolve.
        schema = next((j for j in JobRegistry.list_all() if j.api_type == job_type), None)

    if not schema:
        available = [f"{j.yaml_name} ({j.api_type})" for j in JobRegistry.list_all()]
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
    # Treatment params (pi/psi/kappa/tau) are 0/1 SWITCHES, read by the decision
    # function as int(round(x))==1 -- use ONLY 0 or 1. A fractional value such as
    # 0.5 rounds to 0 and silently disables the treatment arm.
    mainstream_media_aversion:
      levels: [control, treated]
      maps_to:
        params.pi:
          control: 0
          treated: 1
  replications: 3

crawlers:
  - from_design: true
    name: "yt_{mainstream_media_aversion}_{replica:02d}"
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


def _read_repo_doc(*relative_parts: str) -> str:
    """Read a doc file from the repo, for serving over MCP.

    The design knowledge an agent needs to build a *valid* experiment lives in the
    repo docs; an agent connected only to the MCP (no checkout) cannot see them.
    These resources close that gap. The server process has the repo, so it reads
    and returns the file content.
    """
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root.joinpath(*relative_parts)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        return f"(could not read {'/'.join(relative_parts)}: {e})"


@mcp.resource("doc://experiment-designer")
def doc_experiment_designer() -> str:
    """The experiment-designer YAML DSL reference (factors, maps_to, distributions,
    data pools, templates, job types, relative timestamps)."""
    return _read_repo_doc("docs", "experiment-designer.md")


@mcp.resource("doc://integration-contract")
def doc_integration_contract() -> str:
    """The cross-component integration contract, including the status vocabulary
    (§3.4) and the decision-function degradation contract (§7.3)."""
    return _read_repo_doc("docs", "integration-contract.md")


@mcp.resource("doc://treatment-params")
def doc_treatment_params() -> str:
    """How treatment is expressed today, and the traps to avoid.

    Distilled and stable (does not depend on grepping CLAUDE.md) so an MCP-only
    agent designing an experiment has the semantics, not just the tools.
    """
    return (
        "# Treatment parameters and decision-function semantics\n"
        "\n"
        "## pi / psi / kappa / tau are 0/1 SWITCHES, not intensities\n"
        "The decision function reads each as `int(round(x)) == 1`. Use ONLY 0 or 1.\n"
        "A fractional value such as 0.5 rounds to 0 (Python rounds half-to-even) and\n"
        "SILENTLY disables the arm it looks like it configures -- no error anywhere.\n"
        "A control crawler is `pi = psi = kappa = tau = 0`. A factor that toggles a\n"
        "switch must have 0/1 levels; a multi-level factor cannot map to one switch\n"
        "(map it to preferences or search terms instead). `validate_experiment_spec`\n"
        "now errors on non-0/1 switch values.\n"
        "\n"
        "## These four names are one study's design, not the framework\n"
        "They are specific to the YouTube personalization study (the `youtube_v1`\n"
        "strategy). The general model: the lambda (decision function) decides, driven\n"
        "by a per-experiment STRATEGY -- LLM-driven or parameters + a decision function.\n"
        "\n"
        "## Treatment via the behaviours channel (the generic path)\n"
        "A lambda_select job can carry a `strategy` (which decision logic runs) and a\n"
        "`treatment` block (its config); both become job behaviours the decision\n"
        "function reads. The channel activates ONLY when `strategy` is present -- a\n"
        "`treatment` without a `strategy` is ignored (validator warns), and legacy specs\n"
        "with no `strategy` keep reading the old selectionConfiguration params. Example:\n"
        "  - lambda_select:\n"
        "      task: youtube_video_select\n"
        "      function: bot-decision-yt\n"
        "      strategy: youtube_v1\n"
        "      treatment: {pi: 1, tau: 0}\n"
        "`treatment` is validated against strategies/<strategy>.schema.json in\n"
        "bot-decision-yt (youtube_v1 still enforces 0/1 on pi/psi/kappa/tau).\n"
        "\n"
        "## cookie_pref / consent is a treatment the runner acts on (runner PR #61)\n"
        "The runner answers cookie/GDPR dialogs per the bot's `cookie_pref`\n"
        "(`{accept_all: true}` -> 'Accept all', `false` -> 'Reject all'), overridable\n"
        "per job with the `consent` behaviour (`accept | reject | ignore`). 'Reject\n"
        "all' switches off the signed-out personalisation a study measures -- choose\n"
        "it deliberately. Appears only from a CH/EU exit IP.\n"
        "\n"
        "## The decision returns HTTP 200 even when degraded -- read the report\n"
        "Three fields (full contract: doc://integration-contract §7.3):\n"
        "- `degraded`: a requested filter failed to apply, or nothing could be\n"
        "  selected -> 'a human should look'.\n"
        "- `treatment_lost`: degraded AND nothing requested applied -> 'this session's\n"
        "  data is void' (the only alarm). Computed once, at the source.\n"
        "- `selection_mode`: description only; `random` = no LLM rating informed the\n"
        "  pick. NEVER alert on it -- a control crawler correctly draws at random.\n"
    )


@mcp.resource("doc://capacity")
def doc_capacity() -> str:
    """Parallel-bot capacity and scheduling constraints for designing runs."""
    return (
        "# Capacity & scheduling constraints (for run design)\n"
        "\n"
        "## Scheduling\n"
        "- `start_at` MUST be in the future or the runner will not pick up the queue.\n"
        "- Timestamps are interpreted as CET (+01:00).\n"
        "\n"
        "## Parallel-bot capacity (measured on ex63: 20 cores / 62 GB, HW decode)\n"
        "- Multilogin profile-create rate limit is the first ceiling: ~50 ops/min.\n"
        "  ~15 simultaneous launches is clean; beyond that, stagger `start_at` over\n"
        "  60-90s so profile-creates stay under the limit. This caps launch RATE, not\n"
        "  how many can run.\n"
        "- RAM is a distant second: ~0.85 GB/bot (PSS). Judge headroom from `free`/PSS,\n"
        "  never summed RSS. ~30-40 with staggered launch; then exit-IP supply and the\n"
        "  GB budget bind, not the box.\n"
        "- One stable exit IP per bot (a shared IP is a detection signal and a\n"
        "  confound). Multilogin residential is GB-metered (~450 MB per bot-hour).\n"
        "\n"
        "## These numbers assume ex63's hardware video decode\n"
        "A GPU-less VM falls back to software decode and is CPU-bound: expect far\n"
        "lower concurrency there and measure it, rather than reusing these figures.\n"
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
