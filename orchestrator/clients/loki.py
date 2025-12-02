"""
Loki client for querying logs.

Provides a high-level interface for LogQL queries against Loki.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterator, Optional

import httpx


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
    experiment_id: Optional[int] = None
    crawler_id: Optional[int] = None
    queue_id: Optional[int] = None
    job_id: Optional[int] = None

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

    def _extract_id(self, key: str) -> Optional[int]:
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
        corr_id = self.labels.get("correlation_id", "") or self.labels.get(
            "correlationId", ""
        )
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


class LokiClient:
    """Client for querying Loki logs."""

    def __init__(
        self,
        base_url: str = "http://localhost:3100",
        timeout: float = 30.0,
        org_id: Optional[str] = None,
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

    def is_healthy(self) -> bool:
        """Check if Loki is healthy."""
        try:
            response = self._client.get("/ready")
            return response.status_code == 200
        except Exception:
            return False

    def query(
        self,
        query: str,
        time_range: str = "1h",
        limit: int = 100,
        direction: str = "backward",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
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

        response = self._client.get("/loki/api/v1/query_range", params=params)
        response.raise_for_status()

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
        component: Optional[str] = None,
        level: Optional[str] = None,
        search: Optional[str] = None,
        experiment_id: Optional[int] = None,
        crawler_id: Optional[int] = None,
        job_id: Optional[int] = None,
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
        component: Optional[str] = None,
        level: Optional[str] = None,
        search: Optional[str] = None,
        experiment_id: Optional[int] = None,
        crawler_id: Optional[int] = None,
        job_id: Optional[int] = None,
    ) -> str:
        """Build a LogQL query from filters."""
        # Label selectors
        labels = []
        if component and component != "all":
            labels.append(f'component="{component}"')

        label_selector = "{" + ",".join(labels) + "}" if labels else "{}"

        # Line filters and JSON parsing
        filters = []

        if search:
            # Escape special regex characters
            escaped = re.escape(search)
            filters.append(f'|~ "(?i){escaped}"')

        if level and level != "all":
            # Match level in various formats
            filters.append(f'|~ "(?i)(level|lvl)[=:]*\\s*{level}"')

        # Parse JSON for correlation IDs
        if experiment_id or crawler_id or job_id:
            filters.append("| json")

            if experiment_id:
                filters.append(f'| experiment_id="{experiment_id}"')
            if crawler_id:
                filters.append(f'| crawler_id="{crawler_id}"')
            if job_id:
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
                    timestamp = datetime.fromtimestamp(
                        int(timestamp_ns) / 1e9, tz=timezone.utc
                    )
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

            except Exception:
                time.sleep(5)  # Retry delay on error

    def get_label_values(self, label: str) -> list[str]:
        """Get all values for a label."""
        response = self._client.get(f"/loki/api/v1/label/{label}/values")
        response.raise_for_status()
        return response.json().get("data", [])

    def get_labels(self) -> list[str]:
        """Get all available labels."""
        response = self._client.get("/loki/api/v1/labels")
        response.raise_for_status()
        return response.json().get("data", [])
