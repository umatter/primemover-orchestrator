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


#: Runner lines carry the correlation ids as Logback MDC keys in the line prefix
#: (``crawler.id=250, joblist.id=844, job.id=525``), not as ``crawler_id=250``.
#: ``job\.id`` cannot match inside ``joblist.id``: the character after ``job``
#: there is ``l``, not ``.``.
MDC_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "crawler_id": (r"crawler\.id",),
    "queue_id": (r"joblist\.id",),
    "job_id": (r"job\.id",),
}


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
        """Extract correlation IDs from message or labels.

        Ids passed explicitly by the caller win. They used to be overwritten
        unconditionally, so every entry the CloudWatch and HAR paths built with a
        known ``job_id`` lost it again before it was ever read.
        """
        # Try to extract from labels first
        if self.experiment_id is None:
            self.experiment_id = self._extract_id("experiment_id")
        if self.crawler_id is None:
            self.crawler_id = self._extract_id("crawler_id")
        if self.queue_id is None:
            self.queue_id = self._extract_id("queue_id") or self._extract_id("joblist_id")
        if self.job_id is None:
            self.job_id = self._extract_id("job_id")

        # Try to extract from correlation_id pattern. Run unconditionally: it only
        # fills fields still unset, and gating it on ``experiment_id`` meant a line
        # that carried an experiment id but no crawler/queue/job id lost all three.
        self._extract_from_correlation_id()

    def _extract_id(self, key: str) -> int | None:
        """Extract an ID from labels or message."""
        # From labels
        if key in self.labels:
            try:
                return int(self.labels[key])
            except (ValueError, TypeError):
                pass

        # From message using pattern key=value (or the runner's dotted MDC key).
        # The separator class also swallows the ``":`` of a Laravel JSON context,
        # so ``"job_id":525`` resolves as well as ``job_id=525``.
        for name in (key, *MDC_KEY_ALIASES.get(key, ())):
            match = re.search(rf'{name}[=:"\s]*(\d+)', self.message, re.IGNORECASE)
            if match:
                return int(match.group(1))

        return None

    def _extract_from_correlation_id(self):
        """Extract IDs from correlation_id format: exp-15|cr-42|q-123|j-456"""
        corr_id = self.labels.get("correlation_id", "") or self.labels.get("correlationId", "")
        if not corr_id:
            # Try to find in message: the runner prints the MDC form
            # ``correlationId=cr-250|jl-844``, Laravel the JSON context form
            # ``"correlation_id":"cr-250"``.
            for pattern in (r"correlationId=([^\s,]+)", r'"correlation_id"\s*:\s*"([^"]+)"'):
                match = re.search(pattern, self.message)
                if match:
                    corr_id = match.group(1)
                    break

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


#: Log-level synonyms. Promtail labels runner lines with Logback's ``WARN`` while
#: the apache/mysql/php scrapes lowercase theirs, and historical data keeps the
#: uppercase form regardless of what promtail does today -- so every level match
#: has to be case-insensitive AND has to treat ``warn``/``warning`` as one level.
#: Before this, ``level="warning"`` never matched a single runner line.
#:
#: ``error`` additionally covers the PSR-3 levels *above* it. Laravel writes
#: ``production.ALERT:`` / ``.CRITICAL:`` / ``.EMERGENCY:`` and promtail labels
#: them verbatim -- ``ALERT`` is in the live index right now -- so a plain
#: ``level=~"(?i)error"`` hid the three most severe things the API can say.
#: Anything a query at ``error`` should see has to be spelled out here; the label
#: matcher is an equality test against one label value, not a severity ordering.
LEVEL_SYNONYMS: dict[str, str] = {
    "warn": "warn(ing)?",
    "warning": "warn(ing)?",
    "error": "(?:error|err|critical|crit|alert|emergency|emerg|fatal)",
    "critical": "(?:critical|crit|alert|emergency|emerg|fatal)",
    "alert": "(?:alert|emergency|emerg)",
    "fatal": "(?:fatal|critical|crit|emergency|emerg)",
}


#: Loki rejects a stream selector in which every matcher can match the empty
#: string, so "all components" cannot be spelled ``{}`` -- that is a 400, which
#: ``LogAggregator`` swallowed into ``sources["loki_error"]`` and the CLI showed
#: as "no results". Every PrimeMover scrape sets ``component``.
ANY_COMPONENT_MATCHER = 'component=~".+"'


def build_label_selector(matchers: list[str]) -> str:
    """Join label matchers into a valid LogQL stream selector."""
    if not matchers:
        matchers = [ANY_COMPONENT_MATCHER]
    return "{" + ",".join(matchers) + "}"


def level_label_matcher(level: str | None) -> str | None:
    """Return a LogQL ``level=~...`` label matcher for *level*, or ``None``.

    The level lives in a promtail-set **label**, not in the log line: Laravel
    lines read ``production.ERROR:`` and runner lines read ``... ERROR 3546872
    ---``, so the old line filter ``|~ "(?i)(level|lvl)[=:]*\\s*error"`` matched
    exactly zero rows in either component.
    """
    if not level or level == "all":
        return None
    normalized = level.strip().lower()
    if not re.match(r"^[a-z]+$", normalized):
        return None
    pattern = LEVEL_SYNONYMS.get(normalized, normalized)
    return f'level=~"(?i){pattern}"'


def logql_regex_filter(pattern: str) -> str:
    """Wrap a regex in a LogQL line filter, escaped for the string literal.

    LogQL string literals use Go escapes, so a bare ``\\b`` in the query text is
    read as a backspace character rather than a word boundary -- it has to be
    doubled. Quotes have to be escaped or the literal closes early.
    """
    return f'|~ "{_escape_logql_literal(pattern)}"'


def logql_regex_exclusion(pattern: str) -> str:
    """Wrap a regex in a *negative* LogQL line filter (``!~``)."""
    return f'!~ "{_escape_logql_literal(pattern)}"'


def _escape_logql_literal(pattern: str) -> str:
    """Escape a regex for embedding in a LogQL double-quoted string literal."""
    return pattern.replace("\\", "\\\\").replace('"', '\\"')


#: Messages the API logs on *every* HTTP request it serves, regardless of what the
#: request was for.
#:
#: ``AddCorrelationContext`` stamps the ids it can read off the route onto every
#: line logged during that request, so ``GET /api/v1/jobs/3943`` makes
#: ``ThrottleRequestsWithIp`` emit
#:
#:     production.DEBUG: 1649254282: Throttling request with IP
#:     {"...","job_id":3943,"correlation_id":"j-3943",...}
#:
#: which the job-3943 correlation filter then matches. The result was that
#: ``trace job`` and ``debug job`` reported the debugging tool's own HTTP traffic
#: as the job's related logs -- and, because every observation logged two more
#: such lines, the "related log" count and the log-derived time span *grew every
#: time the job was looked at*. Measured over one week on this box: 4,994 of the
#: 5,000 ``component="api"`` lines carrying a ``job_id`` were these two messages;
#: the six real ones were ``runner reported job failure`` / ``... degraded job``.
#:
#: Excluding the messages (rather than dropping ``component="api"`` wholesale)
#: keeps those six, which are exactly the lines a job trace wants.
#:
#: **Criterion for adding to this list**, because a deny-list rots otherwise: the
#: message must be emitted on *every* request regardless of what the request was,
#: and must say nothing about the entity. That is what makes its correlation to a
#: job purely an artefact of the route. A message that is merely *usually*
#: irrelevant does not qualify -- ``The resource owner or authorization server
#: denied the request`` is spurious under a job filter when the orchestrator
#: causes it, but is the real answer when the runner's own
#: ``PUT /runner/jobs/{id}`` is what got the 401, so it stays visible.
API_REQUEST_PLUMBING_MESSAGES: tuple[str, ...] = (
    "Throttling request with IP",
    "IP is whitelisted",
)


#: RE2 metacharacters. ``re.escape`` cannot be used to build a filter for Loki:
#: it also escapes spaces (``Throttling\\ request``), and ``\\ `` is a hard
#: "invalid escape sequence" parse error in Go's RE2 -- the whole query 400s.
_RE2_METACHARACTERS = r"\.+*?()|[]{}^$"


def _escape_re2(text: str) -> str:
    """Escape *text* for literal matching by Go's RE2, and only where RE2 needs it."""
    return "".join("\\" + char if char in _RE2_METACHARACTERS else char for char in text)


def api_request_plumbing_exclusion() -> str:
    """A ``!~`` line filter dropping the API's own per-request plumbing lines."""
    alternatives = "|".join(_escape_re2(message) for message in API_REQUEST_PLUMBING_MESSAGES)
    return logql_regex_exclusion(f"(?i)(?:{alternatives})")


def correlation_line_filters(
    experiment_id: int | None = None,
    crawler_id: int | None = None,
    queue_id: int | None = None,
    job_id: int | None = None,
) -> list[str]:
    """Build line filters that actually match the correlation text in the logs.

    The previous implementation used ``| json | crawler_id="250"``. The log
    *lines* are plain text -- a Logback line with an MDC prefix, or a Laravel
    line with a JSON context appended -- so LogQL's ``json`` stage dropped every
    entry and every id filter returned nothing, which reads as "no errors found".

    Two textual forms have to match, because both exist in the same index:

    * the runner's correlation id, historically ``cr-250|jl-844`` and, since the
      2026-08-27 change, the canonical ``exp-42|cr-250|q-844|j-525``;
    * the Laravel context object, ``{"crawler_id":250,"queue_id":844,...}``.

    Word boundaries keep ``cr-25`` from matching ``cr-250``. Ids are integers, so
    no injection is possible through this path.

    Whenever at least one id is filtered on, a negative filter for
    :data:`API_REQUEST_PLUMBING_MESSAGES` is appended: the API stamps route-derived
    ids onto every line of the request that carries them, so without it a job
    trace is dominated by the observer's own HTTP traffic. See that constant.
    """
    filters: list[str] = []
    specs: list[tuple[int | None, str, str]] = [
        (experiment_id, r"exp-", "experiment_id"),
        (crawler_id, r"cr-", "crawler_id"),
        (queue_id, r"(?:q|jl)-", "queue_id"),
        (job_id, r"j-", "job_id"),
    ]
    for value, token, json_key in specs:
        if value is None or not isinstance(value, int) or isinstance(value, bool):
            continue
        pattern = rf'(?:\b{token}{value}\b|"{json_key}": ?{value}\b)'
        filters.append(logql_regex_filter(pattern))
    if filters:
        filters.append(api_request_plumbing_exclusion())
    return filters


def time_range_bounds(
    time_range: str = "1h",
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Resolve a ``time_range`` string (plus optional overrides) to (start, end)."""
    if end is None:
        end = datetime.now(timezone.utc)
    if start is None:
        start = end - parse_time_range(time_range)
    return start, end


def range_query_params(
    time_range: str = "1h",
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, int]:
    """Loki ``query_range`` ``start``/``end`` parameters, in nanoseconds.

    Omitting these makes Loki default to the last hour, which is how
    ``time_range="7d"`` used to be silently ignored by every caller that built
    its own params dict.
    """
    resolved_start, resolved_end = time_range_bounds(time_range, start, end)
    return {
        "start": int(resolved_start.timestamp() * 1e9),
        "end": int(resolved_end.timestamp() * 1e9),
    }


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
        start, end = time_range_bounds(time_range, start, end)

        params: dict[str, str | int] = {
            "query": query,
            "limit": limit,
            "direction": direction,
            **range_query_params(start=start, end=end),
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
        queue_id: int | None = None,
        time_range: str = "1h",
        limit: int = 100,
    ) -> QueryResult:
        """
        Query logs with high-level filters.

        Args:
            component: Filter by component (api, runner, lambda)
            level: Log level (matched on the promtail-set label, case-insensitively;
                   ``warning`` and ``warn`` are the same level)
            search: Text search pattern
            experiment_id: Filter by experiment ID
            crawler_id: Filter by crawler ID
            job_id: Filter by job ID
            queue_id: Filter by queue (joblist) ID
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
            queue_id=queue_id,
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
        queue_id: int | None = None,
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

        # The level is a promtail-set label, not text in the line. Matching it as
        # a line filter is what made `orchestrator logs errors` return nothing.
        level_matcher = level_label_matcher(level)
        if level_matcher:
            labels.append(level_matcher)

        label_selector = build_label_selector(labels)

        # Line filters
        filters = []

        if search:
            # Escape for LogQL string context, then also escape regex metacharacters
            escaped = escape_logql_string(search)
            # Also escape regex metacharacters for the regex match
            escaped = re.escape(escaped)
            filters.append(f'|~ "(?i){escaped}"')

        # Correlation ids are matched against the text the components actually
        # print. `| json` never worked here: the lines are not JSON documents.
        filters.extend(
            correlation_line_filters(
                experiment_id=experiment_id,
                crawler_id=crawler_id,
                queue_id=queue_id,
                job_id=job_id,
            )
        )

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
