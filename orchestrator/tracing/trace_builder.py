"""
Trace Builder - Reconstruct execution traces from logs and API data.

Provides a unified view of experiment/crawler/job execution by combining:
- API entity data (experiment, crawler, queue, job metadata)
- Loki logs (API, Runner logs)
- Lambda outputs (from HAR files)
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

from orchestrator.config import get_settings
from orchestrator.logs.aggregator import LogAggregator
from orchestrator.clients.loki import LogEntry

logger = logging.getLogger(__name__)

from .correlation import (
    CorrelationId,
    extract_correlation_from_labels,
    extract_correlation_from_message,
)


class SpanStatus(str, Enum):
    """Status of a trace span."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass
class TraceSpan:
    """A single span in an execution trace."""

    name: str
    component: str  # api, runner, lambda
    status: SpanStatus = SpanStatus.UNKNOWN

    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None

    # Correlation context
    experiment_id: Optional[int] = None
    crawler_id: Optional[int] = None
    queue_id: Optional[int] = None
    job_id: Optional[int] = None

    # Additional data
    metadata: dict = field(default_factory=dict)
    logs: list[LogEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    children: list["TraceSpan"] = field(default_factory=list)

    @property
    def duration_ms(self) -> Optional[float]:
        """Calculate span duration in milliseconds."""
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds() * 1000
        return None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "component": self.component,
            "status": self.status.value,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_ms": self.duration_ms,
            "experiment_id": self.experiment_id,
            "crawler_id": self.crawler_id,
            "queue_id": self.queue_id,
            "job_id": self.job_id,
            "metadata": self.metadata,
            "errors": self.errors,
            "log_count": len(self.logs),
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class ExecutionTrace:
    """Complete execution trace for an entity."""

    entity_type: str  # experiment, crawler, queue, job
    entity_id: int

    # Root span
    root_span: Optional[TraceSpan] = None

    # Entity metadata from API
    entity_data: dict = field(default_factory=dict)

    # Summary statistics
    total_spans: int = 0
    total_logs: int = 0
    error_count: int = 0

    # Timeline
    first_event: Optional[datetime] = None
    last_event: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "entity_data": self.entity_data,
            "root_span": self.root_span.to_dict() if self.root_span else None,
            "summary": {
                "total_spans": self.total_spans,
                "total_logs": self.total_logs,
                "error_count": self.error_count,
                "first_event": self.first_event.isoformat() if self.first_event else None,
                "last_event": self.last_event.isoformat() if self.last_event else None,
                "duration_ms": (
                    (self.last_event - self.first_event).total_seconds() * 1000
                    if self.first_event and self.last_event else None
                ),
            },
        }

    def to_json(self, pretty: bool = True) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=2 if pretty else None)


class TraceBuilder:
    """
    Build execution traces by combining API data and logs.

    Example usage:
        builder = TraceBuilder()
        trace = builder.trace_experiment(15)
        print(trace.to_json())
    """

    def __init__(
        self,
        api_base_url: Optional[str] = None,
        api_email: Optional[str] = None,
        api_password: Optional[str] = None,
    ):
        """
        Initialize trace builder.

        Args:
            api_base_url: PrimeMover API URL
            api_email: API email for authentication
            api_password: API password
        """
        settings = get_settings()

        self.api_base_url = api_base_url or settings.pm_api_base_url
        self.api_email = api_email or settings.pm_api_email
        self.api_password = api_password or settings.pm_api_password

        self._token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None
        self._log_aggregator: Optional[LogAggregator] = None

        # Token validity duration (default: 55 minutes, gives 5 min buffer before 1h expiry)
        self._token_validity_minutes = 55

    @property
    def log_aggregator(self) -> LogAggregator:
        """Get or create log aggregator."""
        if self._log_aggregator is None:
            self._log_aggregator = LogAggregator()
        return self._log_aggregator

    def _is_token_valid(self) -> bool:
        """Check if the current token is still valid."""
        if not self._token or not self._token_expiry:
            return False
        return datetime.now(timezone.utc) < self._token_expiry

    def _clear_token(self) -> None:
        """Clear the cached token."""
        self._token = None
        self._token_expiry = None

    def _authenticate(self, force_refresh: bool = False) -> Optional[str]:
        """
        Authenticate with the API and get a token.

        Args:
            force_refresh: Force re-authentication even if token exists

        Returns:
            Access token or None if authentication fails
        """
        # Return cached token if still valid
        if not force_refresh and self._is_token_valid():
            return self._token

        if not all([self.api_base_url, self.api_email, self.api_password]):
            logger.debug("API credentials not configured")
            return None

        try:
            import requests

            response = requests.post(
                f"{self.api_base_url}/login",
                json={"email": self.api_email, "password": self.api_password},
                headers={"Accept": "application/json"},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            self._token = data.get("access_token")

            # Set expiry time (use expires_in from response if available, else default)
            expires_in = data.get("expires_in", self._token_validity_minutes * 60)
            self._token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)

            return self._token
        except Exception as e:
            logger.debug(f"API authentication failed: {e}")
            self._clear_token()
            return None

    def _api_get(self, endpoint: str, retry_on_401: bool = True) -> Optional[dict]:
        """
        Make an authenticated GET request to the API.

        Args:
            endpoint: API endpoint to call
            retry_on_401: Whether to retry with fresh token on 401 response

        Returns:
            Response JSON or None if request fails
        """
        token = self._authenticate()
        if not token:
            return None

        try:
            import requests

            response = requests.get(
                f"{self.api_base_url}/{endpoint.lstrip('/')}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=30,
            )

            # Handle token expiry - retry with fresh token
            if response.status_code == 401 and retry_on_401:
                logger.debug(f"API returned 401 for {endpoint}, refreshing token")
                self._clear_token()
                return self._api_get(endpoint, retry_on_401=False)

            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.debug(f"API GET {endpoint} failed: {e}")
            return None

    def trace_experiment(
        self,
        experiment_id: int,
        include_crawlers: bool = True,
        include_jobs: bool = False,
        time_range: str = "7d",
    ) -> ExecutionTrace:
        """
        Build a trace for an experiment.

        Args:
            experiment_id: Experiment ID
            include_crawlers: Include crawler-level spans
            include_jobs: Include job-level spans
            time_range: Log query time range

        Returns:
            ExecutionTrace for the experiment
        """
        trace = ExecutionTrace(
            entity_type="experiment",
            entity_id=experiment_id,
        )

        # Fetch experiment data from API
        exp_data = self._api_get(f"experiments/{experiment_id}")
        if exp_data:
            trace.entity_data = exp_data.get("data", exp_data)

        # Create root span
        trace.root_span = TraceSpan(
            name=trace.entity_data.get("name", f"Experiment {experiment_id}"),
            component="api",
            experiment_id=experiment_id,
            metadata={
                "description": trace.entity_data.get("description", ""),
                "active": trace.entity_data.get("active", False),
            },
        )

        # Query logs for this experiment
        log_result = self.log_aggregator.query(
            experiment_id=experiment_id,
            time_range=time_range,
            limit=500,
        )

        trace.total_logs = log_result.total_count
        trace.root_span.logs = log_result.entries

        # Extract timeline
        if log_result.entries:
            timestamps = [e.timestamp for e in log_result.entries if e.timestamp]
            if timestamps:
                trace.first_event = min(timestamps)
                trace.last_event = max(timestamps)
                trace.root_span.start_time = trace.first_event
                trace.root_span.end_time = trace.last_event

        # Count errors
        error_entries = [e for e in log_result.entries if e.level.lower() == "error"]
        trace.error_count = len(error_entries)
        trace.root_span.errors = [e.message[:200] for e in error_entries[:10]]

        # Determine status
        if trace.error_count > 0:
            trace.root_span.status = SpanStatus.FAILED
        elif trace.total_logs > 0:
            trace.root_span.status = SpanStatus.SUCCESS
        else:
            trace.root_span.status = SpanStatus.UNKNOWN

        # Add crawler spans if requested
        if include_crawlers:
            crawlers = trace.entity_data.get("crawlers", [])
            for crawler in crawlers:
                crawler_span = self._build_crawler_span(
                    crawler,
                    experiment_id=experiment_id,
                    include_jobs=include_jobs,
                    time_range=time_range,
                )
                trace.root_span.children.append(crawler_span)
                trace.total_spans += 1

        trace.total_spans += 1  # Count root span
        return trace

    def trace_crawler(
        self,
        crawler_id: int,
        include_jobs: bool = True,
        time_range: str = "7d",
    ) -> ExecutionTrace:
        """
        Build a trace for a crawler.

        Args:
            crawler_id: Crawler ID
            include_jobs: Include job-level spans
            time_range: Log query time range

        Returns:
            ExecutionTrace for the crawler
        """
        trace = ExecutionTrace(
            entity_type="crawler",
            entity_id=crawler_id,
        )

        # Fetch crawler data from API
        crawler_data = self._api_get(f"crawlers/{crawler_id}")
        if crawler_data:
            trace.entity_data = crawler_data.get("data", crawler_data)

        # Build root span
        trace.root_span = self._build_crawler_span(
            trace.entity_data,
            crawler_id=crawler_id,
            include_jobs=include_jobs,
            time_range=time_range,
        )

        # Calculate summary
        trace.total_spans = 1 + len(trace.root_span.children)
        trace.total_logs = len(trace.root_span.logs)
        trace.error_count = len(trace.root_span.errors)

        if trace.root_span.logs:
            timestamps = [e.timestamp for e in trace.root_span.logs if e.timestamp]
            if timestamps:
                trace.first_event = min(timestamps)
                trace.last_event = max(timestamps)

        return trace

    def _build_crawler_span(
        self,
        crawler_data: dict,
        experiment_id: Optional[int] = None,
        crawler_id: Optional[int] = None,
        include_jobs: bool = False,
        time_range: str = "7d",
    ) -> TraceSpan:
        """Build a span for a crawler."""
        crawler_id = crawler_id or crawler_data.get("id")

        span = TraceSpan(
            name=crawler_data.get("name", f"Crawler {crawler_id}"),
            component="runner",
            experiment_id=experiment_id or crawler_data.get("experiment_id"),
            crawler_id=crawler_id,
            metadata={
                "active": crawler_data.get("active", False),
                "testing": crawler_data.get("testing", False),
            },
        )

        # Query logs for this crawler
        log_result = self.log_aggregator.query(
            crawler_id=crawler_id,
            time_range=time_range,
            limit=200,
        )

        span.logs = log_result.entries

        # Extract timeline
        if log_result.entries:
            timestamps = [e.timestamp for e in log_result.entries if e.timestamp]
            if timestamps:
                span.start_time = min(timestamps)
                span.end_time = max(timestamps)

        # Count errors
        error_entries = [e for e in log_result.entries if e.level.lower() == "error"]
        span.errors = [e.message[:200] for e in error_entries[:5]]

        # Determine status
        if error_entries:
            span.status = SpanStatus.FAILED
        elif log_result.entries:
            span.status = SpanStatus.SUCCESS
        else:
            span.status = SpanStatus.UNKNOWN

        # Add job spans if requested
        if include_jobs:
            queues = crawler_data.get("queues", [])
            for queue in queues:
                for job in queue.get("jobs", []):
                    job_span = self._build_job_span(
                        job,
                        experiment_id=span.experiment_id,
                        crawler_id=crawler_id,
                        queue_id=queue.get("id"),
                        time_range=time_range,
                    )
                    span.children.append(job_span)

        return span

    def trace_job(
        self,
        job_id: int,
        time_range: str = "7d",
    ) -> ExecutionTrace:
        """
        Build a trace for a job.

        Args:
            job_id: Job ID
            time_range: Log query time range

        Returns:
            ExecutionTrace for the job
        """
        trace = ExecutionTrace(
            entity_type="job",
            entity_id=job_id,
        )

        # Fetch job data from API
        job_data = self._api_get(f"jobs/{job_id}")
        if job_data:
            trace.entity_data = job_data.get("data", job_data)

        # Build root span
        trace.root_span = self._build_job_span(
            trace.entity_data,
            job_id=job_id,
            time_range=time_range,
        )

        # Get Lambda output if available
        lambda_output = self.log_aggregator.get_lambda_output(job_id)
        if lambda_output:
            trace.root_span.metadata["lambda_output"] = lambda_output
            trace.root_span.children.append(self._build_lambda_span(job_id, lambda_output))

        # Calculate summary
        trace.total_spans = 1 + len(trace.root_span.children)
        trace.total_logs = len(trace.root_span.logs)
        trace.error_count = len(trace.root_span.errors)

        if trace.root_span.start_time:
            trace.first_event = trace.root_span.start_time
        if trace.root_span.end_time:
            trace.last_event = trace.root_span.end_time

        return trace

    def _build_job_span(
        self,
        job_data: dict,
        experiment_id: Optional[int] = None,
        crawler_id: Optional[int] = None,
        queue_id: Optional[int] = None,
        job_id: Optional[int] = None,
        time_range: str = "7d",
    ) -> TraceSpan:
        """Build a span for a job."""
        job_id = job_id or job_data.get("id")

        span = TraceSpan(
            name=job_data.get("name", f"Job {job_id}"),
            component="runner",
            experiment_id=experiment_id,
            crawler_id=crawler_id,
            queue_id=queue_id,
            job_id=job_id,
            metadata={
                "type": job_data.get("type", "unknown"),
                "active": job_data.get("active", False),
                "behaviors": [
                    {"name": b.get("name"), "value": b.get("value")}
                    for b in job_data.get("behaviors", [])[:5]
                ],
            },
        )

        # Query logs for this job
        log_result = self.log_aggregator.query(
            job_id=job_id,
            time_range=time_range,
            limit=100,
        )

        span.logs = log_result.entries

        # Extract timeline
        if log_result.entries:
            timestamps = [e.timestamp for e in log_result.entries if e.timestamp]
            if timestamps:
                span.start_time = min(timestamps)
                span.end_time = max(timestamps)

        # Count errors
        error_entries = [e for e in log_result.entries if e.level.lower() == "error"]
        span.errors = [e.message[:200] for e in error_entries[:3]]

        # Determine status from job data or logs
        status = job_data.get("status", "").lower()
        if status == "completed" or status == "success":
            span.status = SpanStatus.SUCCESS
        elif status == "failed" or status == "error":
            span.status = SpanStatus.FAILED
        elif status == "running" or status == "processing":
            span.status = SpanStatus.RUNNING
        elif status == "pending":
            span.status = SpanStatus.PENDING
        elif error_entries:
            span.status = SpanStatus.FAILED
        elif log_result.entries:
            span.status = SpanStatus.SUCCESS
        else:
            span.status = SpanStatus.UNKNOWN

        return span

    def _build_lambda_span(self, job_id: int, lambda_output: dict) -> TraceSpan:
        """Build a span for Lambda execution."""
        span = TraceSpan(
            name="Lambda Decision",
            component="lambda",
            job_id=job_id,
            status=SpanStatus.SUCCESS,
        )

        # Extract selected video info
        selected = lambda_output.get("selected", {})
        span.metadata = {
            "selected_video": selected.get("title", ""),
            "selected_id": selected.get("id", ""),
            "xpath": lambda_output.get("xpath", ""),
        }

        # Extract warnings
        warnings = lambda_output.get("warnings", [])
        if warnings:
            span.errors = [str(w)[:200] for w in warnings[:5]]
            if any("error" in str(w).lower() for w in warnings):
                span.status = SpanStatus.FAILED

        # Extract timestamp
        timestamps = lambda_output.get("timestamps", {})
        if isinstance(timestamps, dict) and "timestamp" in timestamps:
            ts_values = timestamps["timestamp"]
            if isinstance(ts_values, dict) and ts_values:
                first_ts = next(iter(ts_values.values()), None)
                if first_ts:
                    try:
                        span.start_time = datetime.fromtimestamp(float(first_ts), tz=timezone.utc)
                        span.end_time = span.start_time
                    except (ValueError, TypeError):
                        pass

        return span

    def close(self):
        """Close any open connections."""
        if self._log_aggregator:
            self._log_aggregator.close()
            self._log_aggregator = None
