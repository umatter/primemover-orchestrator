"""
Loki client for querying logs.

Provides a high-level interface for LogQL queries against Loki.
"""

import logging
import re
import time as time_module
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import wraps
from typing import TypeVar

import httpx

logger = logging.getLogger(__name__)

# Retry configuration
DEFAULT_MAX_RETRIES = 3
DEFAULT_INITIAL_DELAY = 0.5  # seconds
DEFAULT_MAX_DELAY = 10.0  # seconds
DEFAULT_BACKOFF_FACTOR = 2.0

# Type variable for generic return type
T = TypeVar("T")


def with_retry(
    max_retries: int = DEFAULT_MAX_RETRIES,
    initial_delay: float = DEFAULT_INITIAL_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    retryable_exceptions: tuple = (httpx.TransportError, httpx.TimeoutException),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator that adds retry logic with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay between retries (seconds)
        max_delay: Maximum delay between retries (seconds)
        backoff_factor: Multiplier for delay after each retry
        retryable_exceptions: Tuple of exception types to retry on
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            delay = initial_delay
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        logger.debug(
                            f"Retry {attempt + 1}/{max_retries} for {func.__name__} "
                            f"after {delay:.1f}s due to: {e}"
                        )
                        time_module.sleep(delay)
                        delay = min(delay * backoff_factor, max_delay)
                    else:
                        logger.warning(
                            f"All {max_retries} retries exhausted for {func.__name__}: {e}"
                        )

            raise last_exception  # type: ignore

        return wrapper

    return decorator


class LogLevel(str, Enum):
    """Log level enumeration."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"

    @classmethod
    def from_string(cls, value: str) -> "LogLevel":
        """Parse log level from string."""
        normalized = value.lower().strip()
        if normalized in ("warn", "warning"):
            return cls.WARNING
        try:
            return cls(normalized)
        except ValueError:
            return cls.INFO


@dataclass
class LogEntry:
    """A single log entry from Loki."""

    timestamp: datetime
    component: str
    level: str
    message: str
    labels: dict = field(default_factory=dict)
    raw: str = ""

    # Extracted correlation IDs
    experiment_id: int | None = None
    crawler_id: int | None = None
    queue_id: int | None = None
    job_id: int | None = None

    def __post_init__(self):
        """Extract correlation IDs from message or labels."""
        # Try to extract from labels first
        self.experiment_id = self._extract_id("experiment_id")
        self.crawler_id = self._extract_id("crawler_id")
        self.queue_id = self._extract_id("queue_id") or self._extract_id("joblist_id")
        self.job_id = self._extract_id("job_id")

        # Try to extract from correlation_id pattern
        if not self.experiment_id:
            self._extract_from_correlation_id()

    def _extract_id(self, key: str) -> int | None:
        """Extract an ID from labels or message."""
        # From labels
        if key in self.labels:
            try:
                return int(self.labels[key])
            except (ValueError, TypeError):
                pass

        # From message using pattern key=value
        pattern = rf"{key}[=:]?\s*(\d+)"
        match = re.search(pattern, self.message, re.IGNORECASE)
        if match:
            return int(match.group(1))

        return None

    def _extract_from_correlation_id(self):
        """Extract IDs from correlation_id format: exp-15|cr-42|q-123|j-456"""
        corr_id = self.labels.get("correlation_id", "") or self.labels.get("correlationId", "")
        if not corr_id:
            # Try to find in message
            match = re.search(r"correlationId=([^\s,]+)", self.message)
            if match:
                corr_id = match.group(1)

        if corr_id:
            # Parse exp-N|cr-N|q-N|j-N format
            patterns = [
                (r"exp-(\d+)", "experiment_id"),
                (r"cr-(\d+)", "crawler_id"),
                (r"(?:q|jl)-(\d+)", "queue_id"),
                (r"j-(\d+)", "job_id"),
            ]
            for pattern, attr in patterns:
                match = re.search(pattern, corr_id)
                if match and getattr(self, attr) is None:
                    setattr(self, attr, int(match.group(1)))


@dataclass
class QueryResult:
    """Result of a Loki query."""

    entries: list[LogEntry]
    total_count: int
    query: str
    time_range: tuple[datetime, datetime]
    execution_time_ms: float = 0


def parse_time_range(time_range: str) -> timedelta:
    """
    Parse a time range string into a timedelta.

    Supports: 15m, 1h, 24h, 7d, 30d
    """
    match = re.match(r"^(\d+)([mhd])$", time_range.lower())
    if not match:
        raise ValueError(f"Invalid time range format: {time_range}")

    value, unit = int(match.group(1)), match.group(2)

    if unit == "m":
        return timedelta(minutes=value)
    elif unit == "h":
        return timedelta(hours=value)
    elif unit == "d":
        return timedelta(days=value)
    else:
        raise ValueError(f"Unknown time unit: {unit}")


def escape_logql_string(value: str) -> str:
    """
    Escape a string for safe use in LogQL queries.

    Escapes characters that have special meaning in LogQL:
    - Double quotes (") -> \\"
    - Backslashes (\\) -> \\\\
    - Newlines -> \\n
    - Carriage returns -> \\r
    - Tabs -> \\t

    Also validates the string doesn't contain characters that could
    break out of the LogQL context.

    Args:
        value: The string to escape

    Returns:
        Escaped string safe for LogQL queries

    Raises:
        ValueError: If the string contains invalid characters
    """
    if not isinstance(value, str):
        raise ValueError(f"Expected string, got {type(value)}")

    # Check for null bytes which could be used for injection
    if "\x00" in value:
        raise ValueError("String contains null bytes")

    # Escape backslashes first (order matters)
    escaped = value.replace("\\", "\\\\")
    # Escape double quotes
    escaped = escaped.replace('"', '\\"')
    # Escape control characters
    escaped = escaped.replace("\n", "\\n")
    escaped = escaped.replace("\r", "\\r")
    escaped = escaped.replace("\t", "\\t")

    return escaped


def validate_logql_label(value: str) -> str:
    """
    Validate and sanitize a label value for LogQL.

    Args:
        value: The label value to validate

    Returns:
        Sanitized label value

    Raises:
        ValueError: If the value contains invalid characters
    """
    if not isinstance(value, str):
        raise ValueError(f"Expected string, got {type(value)}")

    # Labels should be alphanumeric with underscores/hyphens
    if not re.match(r"^[a-zA-Z0-9_-]+$", value):
        raise ValueError(f"Invalid label value: {value}")

    return value


class LokiClient:
    """Client for querying Loki logs."""

    def __init__(
        self,
        base_url: str = "http://localhost:3100",
        timeout: float = 30.0,
        org_id: str | None = None,
    ):
        """
        Initialize Loki client.

        Args:
            base_url: Loki server URL
            timeout: Request timeout in seconds
            org_id: Optional organization ID for multi-tenant Loki
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.org_id = org_id

        # HTTP client
        headers = {}
        if org_id:
            headers["X-Scope-OrgID"] = org_id

        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers=headers,
        )

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @with_retry()
    def _get_with_retry(self, endpoint: str, **kwargs) -> httpx.Response:
        """Execute GET request with retry logic."""
        response = self._client.get(endpoint, **kwargs)
        response.raise_for_status()
        return response

    def is_healthy(self) -> bool:
        """Check if Loki is healthy."""
        try:
            response = self._client.get("/ready")
            return response.status_code == 200
        except Exception as e:
            logger.debug(f"Loki health check failed: {e}")
            return False

    def query(
        self,
        query: str,
        time_range: str = "1h",
        limit: int = 100,
        direction: str = "backward",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> QueryResult:
        """
        Execute a LogQL query.

        Args:
            query: LogQL query string
            time_range: Time range (15m, 1h, 24h, 7d)
            limit: Maximum number of results
            direction: 'forward' or 'backward'
            start: Optional explicit start time
            end: Optional explicit end time

        Returns:
            QueryResult with log entries
        """
        import time

        start_time = time.time()

        # Calculate time range
        if end is None:
            end = datetime.now(timezone.utc)
        if start is None:
            delta = parse_time_range(time_range)
            start = end - delta

        # Convert to nanoseconds for Loki
        start_ns = int(start.timestamp() * 1e9)
        end_ns = int(end.timestamp() * 1e9)

        params = {
            "query": query,
            "limit": limit,
            "direction": direction,
            "start": start_ns,
            "end": end_ns,
        }

        response = self._get_with_retry("/loki/api/v1/query_range", params=params)
        data = response.json()
        execution_time_ms = (time.time() - start_time) * 1000

        entries = self._parse_response(data)

        return QueryResult(
            entries=entries,
            total_count=len(entries),
            query=query,
            time_range=(start, end),
            execution_time_ms=execution_time_ms,
        )

    def query_logs(
        self,
        component: str | None = None,
        level: str | None = None,
        search: str | None = None,
        experiment_id: int | None = None,
        crawler_id: int | None = None,
        job_id: int | None = None,
        time_range: str = "1h",
        limit: int = 100,
    ) -> QueryResult:
        """
        Query logs with high-level filters.

        Args:
            component: Filter by component (api, runner, lambda)
            level: Minimum log level
            search: Text search pattern
            experiment_id: Filter by experiment ID
            crawler_id: Filter by crawler ID
            job_id: Filter by job ID
            time_range: Time range
            limit: Maximum results

        Returns:
            QueryResult with filtered log entries
        """
        query = self._build_query(
            component=component,
            level=level,
            search=search,
            experiment_id=experiment_id,
            crawler_id=crawler_id,
            job_id=job_id,
        )

        return self.query(query, time_range=time_range, limit=limit)

    def _build_query(
        self,
        component: str | None = None,
        level: str | None = None,
        search: str | None = None,
        experiment_id: int | None = None,
        crawler_id: int | None = None,
        job_id: int | None = None,
    ) -> str:
        """Build a LogQL query from filters with proper escaping."""
        # Label selectors - validate component names
        labels = []
        if component and component != "all":
            try:
                validated_component = validate_logql_label(component)
                labels.append(f'component="{validated_component}"')
            except ValueError:
                # Skip invalid component names
                pass

        label_selector = "{" + ",".join(labels) + "}" if labels else "{}"

        # Line filters and JSON parsing
        filters = []

        if search:
            # Escape for LogQL string context, then also escape regex metacharacters
            escaped = escape_logql_string(search)
            # Also escape regex metacharacters for the regex match
            escaped = re.escape(escaped)
            filters.append(f'|~ "(?i){escaped}"')

        if level and level != "all":
            # Validate level is alphanumeric only
            if re.match(r"^[a-zA-Z]+$", level):
                filters.append(f'|~ "(?i)(level|lvl)[=:]*\\s*{level}"')

        # Parse JSON for correlation IDs - IDs are validated as integers
        if experiment_id or crawler_id or job_id:
            filters.append("| json")

            # Integer IDs are safe - no injection possible
            if experiment_id is not None and isinstance(experiment_id, int):
                filters.append(f'| experiment_id="{experiment_id}"')
            if crawler_id is not None and isinstance(crawler_id, int):
                filters.append(f'| crawler_id="{crawler_id}"')
            if job_id is not None and isinstance(job_id, int):
                filters.append(f'| job_id="{job_id}"')

        query = label_selector
        if filters:
            query += " " + " ".join(filters)

        return query

    def _parse_response(self, data: dict) -> list[LogEntry]:
        """Parse Loki response into LogEntry objects."""
        entries = []

        result = data.get("data", {}).get("result", [])

        for stream in result:
            stream_labels = stream.get("stream", {})
            component = stream_labels.get("component", "unknown")
            level = stream_labels.get("level", "info")

            for value in stream.get("values", []):
                timestamp_ns, line = value

                # Parse timestamp (nanoseconds to datetime)
                try:
                    timestamp = datetime.fromtimestamp(int(timestamp_ns) / 1e9, tz=timezone.utc)
                except (ValueError, TypeError):
                    timestamp = datetime.now(timezone.utc)

                entry = LogEntry(
                    timestamp=timestamp,
                    component=component,
                    level=level,
                    message=line,
                    labels=stream_labels,
                    raw=line,
                )
                entries.append(entry)

        # Sort by timestamp (newest first for backward direction)
        entries.sort(key=lambda e: e.timestamp, reverse=True)

        return entries

    def tail(
        self,
        query: str = "{}",
        delay_for: int = 0,
    ) -> Iterator[LogEntry]:
        """
        Tail logs in real-time using WebSocket.

        Args:
            query: LogQL query
            delay_for: Delay in seconds before streaming starts

        Yields:
            LogEntry objects as they arrive
        """
        # Note: This is a simplified implementation
        # Full WebSocket streaming requires additional dependencies
        import time

        last_timestamp = datetime.now(timezone.utc)

        while True:
            try:
                result = self.query(
                    query,
                    time_range="1m",
                    limit=50,
                    direction="forward",
                    start=last_timestamp,
                )

                for entry in result.entries:
                    if entry.timestamp > last_timestamp:
                        yield entry
                        last_timestamp = entry.timestamp

                time.sleep(1)  # Poll interval

            except Exception as e:
                logger.debug(f"Loki tail error, retrying: {e}")
                time.sleep(5)  # Retry delay on error

    def get_label_values(self, label: str) -> list[str]:
        """Get all values for a label."""
        response = self._client.get(f"/loki/api/v1/label/{label}/values")
        response.raise_for_status()
        values: list[str] = response.json().get("data", [])
        return values

    def get_labels(self) -> list[str]:
        """Get all available labels."""
        response = self._client.get("/loki/api/v1/labels")
        response.raise_for_status()
        labels: list[str] = response.json().get("data", [])
        return labels
