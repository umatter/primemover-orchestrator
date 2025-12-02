"""
PrimeMover Orchestrator MCP Server

Provides Claude Code with tools for monitoring, debugging, and managing experiments.
"""

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from orchestrator.config import get_settings

# Initialize MCP server
mcp = FastMCP("primemover-orchestrator")


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

    settings = get_settings()

    # Build LogQL query
    label_selectors = []
    if component != "all":
        label_selectors.append(f'component="{component}"')
    if level != "all":
        label_selectors.append(f'level=~"(?i){level}"')

    labels = "{" + ",".join(label_selectors) + "}" if label_selectors else "{}"
    query = labels

    # Add filters
    filters = []
    if search:
        filters.append(f'|~ "{search}"')
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


@mcp.tool()
def active_experiments() -> str:
    """
    List currently active/running experiments.

    Returns:
        JSON with active experiments, their crawler counts, and status
    """
    # TODO: Implement via API client
    return json.dumps(
        {
            "message": "Not yet implemented - requires API client",
            "hint": "Will query /api/v1/experiments with active filter",
        }
    )


@mcp.tool()
def queue_status(status: str = "unprocessed", experiment_id: Optional[int] = None) -> str:
    """
    Get queue execution status.

    Args:
        status: Filter by status - 'all', 'processed', 'unprocessed', 'running'
        experiment_id: Optional experiment ID filter

    Returns:
        JSON with queue counts by status
    """
    # TODO: Implement via API client
    return json.dumps(
        {
            "message": "Not yet implemented - requires API client",
            "hint": f"Will query /api/v1/queues-{status}",
        }
    )


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
    """Run the MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
