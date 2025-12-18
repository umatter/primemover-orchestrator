"""
CloudWatch Logs client for querying Lambda execution logs.

Provides access to Lambda logs from the /aws/lambda/bot-decision-yt log group.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from orchestrator.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class LambdaLogEntry:
    """A single Lambda log entry from CloudWatch."""

    timestamp: datetime
    message: str
    log_stream: str

    # Parsed fields
    request_id: Optional[str] = None
    level: str = "info"

    # Execution metrics (from REPORT line)
    duration_ms: Optional[float] = None
    billed_duration_ms: Optional[float] = None
    memory_used_mb: Optional[float] = None
    memory_size_mb: Optional[float] = None
    init_duration_ms: Optional[float] = None

    # Error info
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    # Correlation
    job_id: Optional[int] = None
    crawler_id: Optional[int] = None
    experiment_id: Optional[int] = None

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "message": self.message,
            "log_stream": self.log_stream,
            "request_id": self.request_id,
            "level": self.level,
            "duration_ms": self.duration_ms,
            "billed_duration_ms": self.billed_duration_ms,
            "memory_used_mb": self.memory_used_mb,
            "memory_size_mb": self.memory_size_mb,
            "init_duration_ms": self.init_duration_ms,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "job_id": self.job_id,
            "crawler_id": self.crawler_id,
            "experiment_id": self.experiment_id,
        }


@dataclass
class LambdaInvocation:
    """A complete Lambda invocation with all associated logs."""

    request_id: str
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None

    # Metrics
    duration_ms: Optional[float] = None
    billed_duration_ms: Optional[float] = None
    memory_used_mb: Optional[float] = None
    memory_size_mb: Optional[float] = None
    init_duration_ms: Optional[float] = None

    # Status
    status: str = "unknown"  # success, error, timeout
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    # Correlation
    job_id: Optional[int] = None
    crawler_id: Optional[int] = None
    experiment_id: Optional[int] = None

    # All log entries
    logs: list[LambdaLogEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "request_id": self.request_id,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_ms": self.duration_ms,
            "billed_duration_ms": self.billed_duration_ms,
            "memory_used_mb": self.memory_used_mb,
            "memory_size_mb": self.memory_size_mb,
            "init_duration_ms": self.init_duration_ms,
            "status": self.status,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "job_id": self.job_id,
            "crawler_id": self.crawler_id,
            "experiment_id": self.experiment_id,
            "log_count": len(self.logs),
        }


@dataclass
class CloudWatchQueryResult:
    """Result of a CloudWatch logs query."""

    entries: list[LambdaLogEntry]
    invocations: list[LambdaInvocation]
    total_count: int
    query_time_ms: float = 0


class CloudWatchClient:
    """
    Client for querying Lambda logs from CloudWatch.

    Example usage:
        client = CloudWatchClient()
        result = client.query_logs(time_range="1h", limit=100)

        for entry in result.entries:
            print(f"{entry.timestamp}: {entry.message}")
    """

    # Patterns for parsing Lambda logs
    START_PATTERN = re.compile(r"START RequestId: ([a-f0-9-]+)")
    END_PATTERN = re.compile(r"END RequestId: ([a-f0-9-]+)")
    REPORT_PATTERN = re.compile(
        r"REPORT RequestId: ([a-f0-9-]+)\s+"
        r"Duration: ([\d.]+) ms\s+"
        r"Billed Duration: ([\d.]+) ms\s+"
        r"Memory Size: (\d+) MB\s+"
        r"Max Memory Used: (\d+) MB"
        r"(?:\s+Init Duration: ([\d.]+) ms)?"
    )
    ERROR_PATTERN = re.compile(r"\[ERROR\]|Error:|Exception:|Traceback", re.IGNORECASE)
    TIMEOUT_PATTERN = re.compile(r"Task timed out after", re.IGNORECASE)

    # Patterns for extracting correlation IDs
    JOB_ID_PATTERN = re.compile(r"job[_\s]?[iI]d[=:\s]*(\d+)", re.IGNORECASE)
    CRAWLER_ID_PATTERN = re.compile(r"crawler[_\s]?[iI]d[=:\s]*(\d+)", re.IGNORECASE)
    EXPERIMENT_ID_PATTERN = re.compile(r"experiment[_\s]?[iI]d[=:\s]*(\d+)", re.IGNORECASE)

    def __init__(
        self,
        log_group: Optional[str] = None,
        region: Optional[str] = None,
    ):
        """
        Initialize CloudWatch client.

        Args:
            log_group: CloudWatch log group name (default from settings)
            region: AWS region (default from settings)
        """
        settings = get_settings()

        self.log_group = log_group or settings.lambda_log_group
        self.region = region or settings.aws_region

        self._client = None

    @property
    def client(self):
        """Get or create boto3 CloudWatch Logs client with retry configuration."""
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config

                # Configure retry with exponential backoff
                retry_config = Config(
                    retries={
                        "max_attempts": 3,
                        "mode": "adaptive",  # Adapts to throttling
                    },
                    connect_timeout=10,
                    read_timeout=30,
                )

                self._client = boto3.client(
                    "logs",
                    region_name=self.region,
                    config=retry_config,
                )
            except ImportError:
                raise ImportError("boto3 is required for CloudWatch integration")

        return self._client

    def is_available(self) -> bool:
        """Check if CloudWatch is available and configured."""
        if not self.log_group:
            return False

        try:
            # Try to describe the log group
            response = self.client.describe_log_groups(
                logGroupNamePrefix=self.log_group,
                limit=1,
            )
            return len(response.get("logGroups", [])) > 0
        except Exception as e:
            logger.debug(f"CloudWatch availability check failed: {e}")
            return False

    def query_logs(
        self,
        time_range: str = "1h",
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        filter_pattern: Optional[str] = None,
        request_id: Optional[str] = None,
        job_id: Optional[int] = None,
        limit: int = 100,
    ) -> CloudWatchQueryResult:
        """
        Query Lambda logs from CloudWatch.

        Args:
            time_range: Time range (15m, 1h, 24h, 7d)
            start_time: Explicit start time
            end_time: Explicit end time
            filter_pattern: CloudWatch filter pattern
            request_id: Filter by Lambda request ID
            job_id: Filter by job ID (searches log content)
            limit: Maximum number of log entries

        Returns:
            CloudWatchQueryResult with entries and invocations
        """
        import time

        query_start = time.time()

        # Calculate time range
        if end_time is None:
            end_time = datetime.now(timezone.utc)
        if start_time is None:
            start_time = end_time - self._parse_time_range(time_range)

        # Convert to milliseconds for CloudWatch
        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)

        # Build filter pattern
        patterns = []
        if filter_pattern:
            patterns.append(filter_pattern)
        if request_id:
            patterns.append(f'"{request_id}"')
        if job_id:
            patterns.append(f'"job" "{job_id}"')

        combined_pattern = " ".join(patterns) if patterns else None

        # Query CloudWatch
        try:
            entries = list(self._fetch_logs(
                start_ms=start_ms,
                end_ms=end_ms,
                filter_pattern=combined_pattern,
                limit=limit,
            ))
        except Exception as e:
            # Return empty result on error
            return CloudWatchQueryResult(
                entries=[],
                invocations=[],
                total_count=0,
                query_time_ms=(time.time() - query_start) * 1000,
            )

        # Group into invocations
        invocations = self._group_into_invocations(entries)

        return CloudWatchQueryResult(
            entries=entries,
            invocations=invocations,
            total_count=len(entries),
            query_time_ms=(time.time() - query_start) * 1000,
        )

    def get_invocation(self, request_id: str) -> Optional[LambdaInvocation]:
        """
        Get a specific Lambda invocation by request ID.

        Args:
            request_id: Lambda request ID

        Returns:
            LambdaInvocation or None if not found
        """
        result = self.query_logs(
            time_range="7d",
            request_id=request_id,
            limit=100,
        )

        for invocation in result.invocations:
            if invocation.request_id == request_id:
                return invocation

        return None

    def get_recent_errors(
        self,
        time_range: str = "24h",
        limit: int = 50,
    ) -> list[LambdaInvocation]:
        """
        Get recent Lambda errors.

        Args:
            time_range: Time range to search
            limit: Maximum number of errors

        Returns:
            List of failed invocations
        """
        result = self.query_logs(
            time_range=time_range,
            filter_pattern="?ERROR ?Exception ?Traceback ?\"Task timed out\"",
            limit=limit * 10,  # Fetch more to find related logs
        )

        # Filter to error invocations
        errors = [
            inv for inv in result.invocations
            if inv.status in ("error", "timeout")
        ]

        return errors[:limit]

    def get_metrics_summary(
        self,
        time_range: str = "24h",
    ) -> dict:
        """
        Get summary metrics for Lambda executions.

        Args:
            time_range: Time range for metrics

        Returns:
            Dictionary with aggregated metrics
        """
        result = self.query_logs(
            time_range=time_range,
            filter_pattern="REPORT",
            limit=1000,
        )

        if not result.invocations:
            return {
                "total_invocations": 0,
                "successful": 0,
                "errors": 0,
                "timeouts": 0,
            }

        durations = [
            inv.duration_ms for inv in result.invocations
            if inv.duration_ms is not None
        ]
        memory_used = [
            inv.memory_used_mb for inv in result.invocations
            if inv.memory_used_mb is not None
        ]

        successful = sum(1 for inv in result.invocations if inv.status == "success")
        errors = sum(1 for inv in result.invocations if inv.status == "error")
        timeouts = sum(1 for inv in result.invocations if inv.status == "timeout")

        return {
            "total_invocations": len(result.invocations),
            "successful": successful,
            "errors": errors,
            "timeouts": timeouts,
            "success_rate": successful / len(result.invocations) if result.invocations else 0,
            "duration_avg_ms": sum(durations) / len(durations) if durations else None,
            "duration_max_ms": max(durations) if durations else None,
            "duration_min_ms": min(durations) if durations else None,
            "memory_avg_mb": sum(memory_used) / len(memory_used) if memory_used else None,
            "memory_max_mb": max(memory_used) if memory_used else None,
        }

    def _fetch_logs(
        self,
        start_ms: int,
        end_ms: int,
        filter_pattern: Optional[str] = None,
        limit: int = 100,
    ) -> Iterator[LambdaLogEntry]:
        """Fetch logs from CloudWatch with pagination."""
        params = {
            "logGroupName": self.log_group,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": min(limit, 10000),
        }

        if filter_pattern:
            params["filterPattern"] = filter_pattern

        count = 0
        next_token = None

        while count < limit:
            if next_token:
                params["nextToken"] = next_token

            response = self.client.filter_log_events(**params)

            for event in response.get("events", []):
                entry = self._parse_log_event(event)
                yield entry
                count += 1

                if count >= limit:
                    break

            next_token = response.get("nextToken")
            if not next_token:
                break

    def _parse_log_event(self, event: dict) -> LambdaLogEntry:
        """Parse a CloudWatch log event into LambdaLogEntry."""
        timestamp = datetime.fromtimestamp(
            event["timestamp"] / 1000,
            tz=timezone.utc,
        )
        message = event.get("message", "").strip()
        log_stream = event.get("logStreamName", "")

        entry = LambdaLogEntry(
            timestamp=timestamp,
            message=message,
            log_stream=log_stream,
        )

        # Parse START line
        match = self.START_PATTERN.search(message)
        if match:
            entry.request_id = match.group(1)
            entry.level = "info"
            return entry

        # Parse END line
        match = self.END_PATTERN.search(message)
        if match:
            entry.request_id = match.group(1)
            entry.level = "info"
            return entry

        # Parse REPORT line
        match = self.REPORT_PATTERN.search(message)
        if match:
            entry.request_id = match.group(1)
            entry.duration_ms = float(match.group(2))
            entry.billed_duration_ms = float(match.group(3))
            entry.memory_size_mb = float(match.group(4))
            entry.memory_used_mb = float(match.group(5))
            if match.group(6):
                entry.init_duration_ms = float(match.group(6))
            entry.level = "info"
            return entry

        # Check for errors
        if self.ERROR_PATTERN.search(message):
            entry.level = "error"
            # Try to extract error type
            error_match = re.search(r"(\w+Error|\w+Exception):", message)
            if error_match:
                entry.error_type = error_match.group(1)
            entry.error_message = message[:500]

        # Check for timeout
        if self.TIMEOUT_PATTERN.search(message):
            entry.level = "error"
            entry.error_type = "Timeout"
            entry.error_message = message[:500]

        # Extract correlation IDs
        match = self.JOB_ID_PATTERN.search(message)
        if match:
            entry.job_id = int(match.group(1))

        match = self.CRAWLER_ID_PATTERN.search(message)
        if match:
            entry.crawler_id = int(match.group(1))

        match = self.EXPERIMENT_ID_PATTERN.search(message)
        if match:
            entry.experiment_id = int(match.group(1))

        return entry

    def _group_into_invocations(
        self,
        entries: list[LambdaLogEntry],
    ) -> list[LambdaInvocation]:
        """Group log entries into Lambda invocations."""
        invocations: dict[str, LambdaInvocation] = {}

        for entry in entries:
            # Try to find request ID
            request_id = entry.request_id

            if not request_id:
                # Try to extract from message
                for pattern in [self.START_PATTERN, self.END_PATTERN, self.REPORT_PATTERN]:
                    match = pattern.search(entry.message)
                    if match:
                        request_id = match.group(1)
                        break

            if not request_id:
                continue

            # Get or create invocation
            if request_id not in invocations:
                invocations[request_id] = LambdaInvocation(request_id=request_id)

            inv = invocations[request_id]
            inv.logs.append(entry)

            # Update invocation from entry
            if entry.duration_ms is not None:
                inv.duration_ms = entry.duration_ms
            if entry.billed_duration_ms is not None:
                inv.billed_duration_ms = entry.billed_duration_ms
            if entry.memory_used_mb is not None:
                inv.memory_used_mb = entry.memory_used_mb
            if entry.memory_size_mb is not None:
                inv.memory_size_mb = entry.memory_size_mb
            if entry.init_duration_ms is not None:
                inv.init_duration_ms = entry.init_duration_ms

            # Update correlation
            if entry.job_id:
                inv.job_id = entry.job_id
            if entry.crawler_id:
                inv.crawler_id = entry.crawler_id
            if entry.experiment_id:
                inv.experiment_id = entry.experiment_id

            # Update timeline
            if inv.start_time is None or entry.timestamp < inv.start_time:
                inv.start_time = entry.timestamp
            if inv.end_time is None or entry.timestamp > inv.end_time:
                inv.end_time = entry.timestamp

            # Update status
            if entry.error_type == "Timeout":
                inv.status = "timeout"
                inv.error_type = entry.error_type
                inv.error_message = entry.error_message
            elif entry.level == "error" and inv.status != "timeout":
                inv.status = "error"
                inv.error_type = entry.error_type
                inv.error_message = entry.error_message
            elif "END RequestId" in entry.message and inv.status == "unknown":
                inv.status = "success"

        return list(invocations.values())

    def _parse_time_range(self, time_range: str) -> timedelta:
        """Parse time range string into timedelta."""
        match = re.match(r"^(\d+)([mhd])$", time_range.lower())
        if not match:
            return timedelta(hours=1)

        value, unit = int(match.group(1)), match.group(2)

        if unit == "m":
            return timedelta(minutes=value)
        elif unit == "h":
            return timedelta(hours=value)
        elif unit == "d":
            return timedelta(days=value)

        return timedelta(hours=1)

    def close(self):
        """Close the client."""
        self._client = None
