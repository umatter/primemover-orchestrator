"""
Job Debugger - Combines all data sources for comprehensive job diagnostics.

Aggregates:
- Job metadata from PrimeMover API
- Logs from Loki/files (API, Runner, infrastructure)
- Lambda decision output from job reports
- HAR file analysis for browser activity
- CloudWatch Lambda execution logs
"""

import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .har_parser import HARParser, HARSummary, format_bytes, format_ms


@dataclass
class LambdaDecision:
    """Lambda decision output from job report."""

    success: bool = False
    selected_title: str = ""
    selected_id: str = ""
    xpath: str = ""
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None
    raw_output: Optional[dict] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "selected_title": self.selected_title,
            "selected_id": self.selected_id,
            "xpath": self.xpath,
            "warnings": self.warnings,
            "error": self.error,
        }


@dataclass
class LogEntry:
    """A log entry from any source."""

    timestamp: datetime
    component: str
    level: str
    message: str
    source: str = ""  # loki, file, cloudwatch

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "component": self.component,
            "level": self.level,
            "message": self.message,
            "source": self.source,
        }


@dataclass
class JobMetadata:
    """Job metadata from PrimeMover API."""

    job_id: int
    experiment_id: Optional[int] = None
    crawler_id: Optional[int] = None
    queue_id: Optional[int] = None
    status: str = ""
    job_type: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    error_message: Optional[str] = None
    raw_data: Optional[dict] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "job_id": self.job_id,
            "experiment_id": self.experiment_id,
            "crawler_id": self.crawler_id,
            "queue_id": self.queue_id,
            "status": self.status,
            "job_type": self.job_type,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_seconds": self.duration_seconds,
            "error_message": self.error_message,
        }


@dataclass
class CloudWatchInfo:
    """CloudWatch Lambda execution info."""

    available: bool = False
    request_id: str = ""
    duration_ms: float = 0
    billed_duration_ms: float = 0
    memory_used_mb: int = 0
    memory_limit_mb: int = 0
    logs: list[LogEntry] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "available": self.available,
            "request_id": self.request_id,
            "duration_ms": self.duration_ms,
            "billed_duration_ms": self.billed_duration_ms,
            "memory_used_mb": self.memory_used_mb,
            "memory_limit_mb": self.memory_limit_mb,
            "logs": [log.to_dict() for log in self.logs],
            "error": self.error,
        }


@dataclass
class Artifacts:
    """Paths to job artifacts."""

    job_dir: Optional[Path] = None
    har_file: Optional[Path] = None
    screenshot_files: list[Path] = field(default_factory=list)
    lambda_zip: Optional[Path] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "job_dir": str(self.job_dir) if self.job_dir else None,
            "har_file": str(self.har_file) if self.har_file else None,
            "screenshot_files": [str(p) for p in self.screenshot_files],
            "lambda_zip": str(self.lambda_zip) if self.lambda_zip else None,
        }


@dataclass
class JobDebugReport:
    """Complete debug report for a job."""

    job_id: int
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Components
    metadata: Optional[JobMetadata] = None
    lambda_decision: Optional[LambdaDecision] = None
    har_summary: Optional[HARSummary] = None
    cloudwatch: Optional[CloudWatchInfo] = None
    artifacts: Optional[Artifacts] = None

    # Logs by component
    logs: list[LogEntry] = field(default_factory=list)
    error_logs: list[LogEntry] = field(default_factory=list)

    # Summary
    has_errors: bool = False
    error_summary: str = ""

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "job_id": self.job_id,
            "generated_at": self.generated_at.isoformat(),
            "metadata": self.metadata.to_dict() if self.metadata else None,
            "lambda_decision": self.lambda_decision.to_dict() if self.lambda_decision else None,
            "har_summary": self.har_summary.to_dict() if self.har_summary else None,
            "cloudwatch": self.cloudwatch.to_dict() if self.cloudwatch else None,
            "artifacts": self.artifacts.to_dict() if self.artifacts else None,
            "logs": [log.to_dict() for log in self.logs],
            "error_logs": [log.to_dict() for log in self.error_logs],
            "has_errors": self.has_errors,
            "error_summary": self.error_summary,
        }

    def to_json(self, indent: int = 2) -> str:
        """Export report as JSON string."""
        return json.dumps(self.to_dict(), indent=indent, default=str)


class JobDebugger:
    """
    Debugger for PrimeMover jobs.

    Combines data from multiple sources to create comprehensive debug reports.
    """

    def __init__(
        self,
        api_base_url: Optional[str] = None,
        api_email: Optional[str] = None,
        api_password: Optional[str] = None,
        har_path: Optional[str] = None,
        slow_threshold_ms: float = 5000,
    ):
        """
        Initialize the job debugger.

        Args:
            api_base_url: PrimeMover API base URL
            api_email: API login email
            api_password: API login password
            har_path: Path to job report directory
            slow_threshold_ms: Threshold for slow requests in HAR analysis
        """
        from orchestrator.config import get_settings

        settings = get_settings()

        self.api_base_url = api_base_url or settings.pm_api_base_url
        self.api_email = api_email or settings.pm_api_email
        self.api_password = api_password or settings.pm_api_password
        self.har_path = Path(har_path or settings.pm_runner_har_path)
        self.slow_threshold_ms = slow_threshold_ms

        self._token: Optional[str] = None
        self._har_parser = HARParser(slow_threshold_ms=slow_threshold_ms)

    def debug_job(
        self,
        job_id: int,
        include_har: bool = True,
        include_cloudwatch: bool = True,
        include_logs: bool = True,
        log_time_range: str = "24h",
        verbose: bool = False,
    ) -> JobDebugReport:
        """
        Generate a comprehensive debug report for a job.

        Args:
            job_id: Job ID to debug
            include_har: Include HAR file analysis
            include_cloudwatch: Include CloudWatch Lambda logs
            include_logs: Include component logs
            log_time_range: Time range for log queries
            verbose: Include more details

        Returns:
            JobDebugReport with all collected information
        """
        report = JobDebugReport(job_id=job_id)

        # 1. Get job metadata from API
        report.metadata = self._get_job_metadata(job_id)

        # 2. Find and analyze artifacts
        report.artifacts = self._find_artifacts(job_id)

        # 3. Extract Lambda decision output
        if report.artifacts and report.artifacts.lambda_zip:
            report.lambda_decision = self._extract_lambda_decision(
                report.artifacts.lambda_zip
            )

        # 4. Parse HAR file
        if include_har and report.artifacts and report.artifacts.har_file:
            report.har_summary = self._har_parser.parse_file(report.artifacts.har_file)

        # 5. Get CloudWatch Lambda logs
        if include_cloudwatch:
            report.cloudwatch = self._get_cloudwatch_info(job_id, log_time_range)

        # 6. Get component logs
        if include_logs:
            report.logs, report.error_logs = self._get_component_logs(
                job_id,
                report.metadata,
                log_time_range,
            )

        # 7. Generate error summary
        self._generate_error_summary(report)

        return report

    def _get_job_metadata(self, job_id: int) -> Optional[JobMetadata]:
        """Fetch job metadata from PrimeMover API."""
        try:
            import httpx

            # Authenticate
            if not self._token:
                auth_response = httpx.post(
                    f"{self.api_base_url}/login",
                    json={"email": self.api_email, "password": self.api_password},
                    timeout=30.0,
                )
                if auth_response.status_code == 200:
                    self._token = auth_response.json().get("access_token")

            if not self._token:
                return None

            # Fetch job
            response = httpx.get(
                f"{self.api_base_url}/jobs/{job_id}",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=30.0,
            )

            if response.status_code != 200:
                return JobMetadata(job_id=job_id, error_message=f"API error: {response.status_code}")

            data = response.json().get("data", response.json())

            # Parse timestamps
            started_at = None
            completed_at = None
            if data.get("started_at"):
                try:
                    started_at = datetime.fromisoformat(
                        data["started_at"].replace("Z", "+00:00")
                    )
                except ValueError:
                    pass
            if data.get("completed_at"):
                try:
                    completed_at = datetime.fromisoformat(
                        data["completed_at"].replace("Z", "+00:00")
                    )
                except ValueError:
                    pass

            # Calculate duration
            duration = None
            if started_at and completed_at:
                duration = (completed_at - started_at).total_seconds()

            return JobMetadata(
                job_id=job_id,
                experiment_id=data.get("experiment_id"),
                crawler_id=data.get("crawler_id"),
                queue_id=data.get("queue_id") or data.get("joblist_id"),
                status=data.get("status", ""),
                job_type=data.get("type", ""),
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=duration,
                error_message=data.get("error_message"),
                raw_data=data,
            )

        except Exception as e:
            return JobMetadata(job_id=job_id, error_message=str(e))

    def _find_artifacts(self, job_id: int) -> Artifacts:
        """Find job artifacts in the output directory."""
        artifacts = Artifacts()

        job_dir = self.har_path / str(job_id)
        if not job_dir.exists():
            return artifacts

        artifacts.job_dir = job_dir

        # Find HAR file
        har_files = list(job_dir.glob("*.har"))
        if har_files:
            artifacts.har_file = har_files[0]

        # Find screenshots
        artifacts.screenshot_files = list(job_dir.glob("*.png")) + list(
            job_dir.glob("*.jpg")
        )

        # Find Lambda zip
        lambda_zips = list(job_dir.glob("*lambda*.zip"))
        if lambda_zips:
            artifacts.lambda_zip = lambda_zips[0]

        return artifacts

    def _extract_lambda_decision(self, zip_path: Path) -> LambdaDecision:
        """Extract Lambda decision from zip file."""
        decision = LambdaDecision()

        try:
            with zipfile.ZipFile(zip_path) as zf:
                for name in zf.namelist():
                    if "output" in name.lower() and name.endswith(".json"):
                        with zf.open(name) as f:
                            data = json.load(f)
                            decision.raw_output = data
                            decision.success = True

                            # Extract selected video info
                            selected = data.get("selected", {})
                            decision.selected_title = selected.get("title", "")
                            decision.selected_id = selected.get("id", "")
                            decision.xpath = data.get("xpath", "")

                            # Extract warnings
                            decision.warnings = data.get("warnings", [])

                            break

        except Exception as e:
            decision.success = False
            decision.error = str(e)

        return decision

    def _get_cloudwatch_info(self, job_id: int, time_range: str) -> CloudWatchInfo:
        """Get CloudWatch Lambda execution info."""
        info = CloudWatchInfo()

        try:
            from orchestrator.clients.cloudwatch import CloudWatchClient

            client = CloudWatchClient()
            if not client.is_available():
                info.error = "CloudWatch not available"
                return info

            info.available = True

            # Query logs for this job
            result = client.query_logs(
                time_range=time_range,
                job_id=job_id,
                limit=100,
            )

            # Parse REPORT lines for metrics
            for entry in result.entries:
                msg = entry.message

                # Extract request ID
                if "RequestId:" in msg and not info.request_id:
                    import re
                    match = re.search(r"RequestId:\s*([a-f0-9-]+)", msg)
                    if match:
                        info.request_id = match.group(1)

                # Extract metrics from REPORT line
                if msg.startswith("REPORT"):
                    import re
                    duration_match = re.search(r"Duration:\s*([\d.]+)\s*ms", msg)
                    billed_match = re.search(r"Billed Duration:\s*([\d.]+)\s*ms", msg)
                    memory_match = re.search(r"Memory Size:\s*(\d+)\s*MB", msg)
                    used_match = re.search(r"Max Memory Used:\s*(\d+)\s*MB", msg)

                    if duration_match:
                        info.duration_ms = float(duration_match.group(1))
                    if billed_match:
                        info.billed_duration_ms = float(billed_match.group(1))
                    if memory_match:
                        info.memory_limit_mb = int(memory_match.group(1))
                    if used_match:
                        info.memory_used_mb = int(used_match.group(1))

                # Add non-system logs
                if not any(x in msg for x in ["START RequestId", "END RequestId", "REPORT RequestId"]):
                    info.logs.append(
                        LogEntry(
                            timestamp=entry.timestamp,
                            component="lambda",
                            level=entry.level,
                            message=msg[:500],
                            source="cloudwatch",
                        )
                    )

            client.close()

        except ImportError:
            info.error = "CloudWatch client not available (boto3 not installed)"
        except Exception as e:
            info.error = str(e)

        return info

    def _get_component_logs(
        self,
        job_id: int,
        metadata: Optional[JobMetadata],
        time_range: str,
    ) -> tuple[list[LogEntry], list[LogEntry]]:
        """Get logs from all components for this job."""
        all_logs: list[LogEntry] = []
        error_logs: list[LogEntry] = []

        try:
            from orchestrator.logs.aggregator import LogAggregator

            aggregator = LogAggregator()

            # Query logs for this job
            result = aggregator.query(
                job_id=job_id,
                experiment_id=metadata.experiment_id if metadata else None,
                crawler_id=metadata.crawler_id if metadata else None,
                time_range=time_range,
                limit=200,
                include_lambda=False,  # We handle Lambda separately
            )

            for entry in result.entries:
                log_entry = LogEntry(
                    timestamp=entry.timestamp,
                    component=entry.component,
                    level=entry.level,
                    message=entry.message[:500],
                    source="loki" if aggregator.is_loki_available() else "file",
                )
                all_logs.append(log_entry)

                if entry.level.lower() in ("error", "critical", "fatal"):
                    error_logs.append(log_entry)

            aggregator.close()

        except Exception:
            pass

        # Sort by timestamp
        all_logs.sort(key=lambda x: x.timestamp)
        error_logs.sort(key=lambda x: x.timestamp)

        return all_logs, error_logs

    def _generate_error_summary(self, report: JobDebugReport) -> None:
        """Generate a summary of errors found."""
        errors = []

        # Check job status
        if report.metadata:
            if report.metadata.status in ("failed", "error"):
                errors.append(f"Job status: {report.metadata.status}")
                if report.metadata.error_message:
                    errors.append(f"Job error: {report.metadata.error_message}")

        # Check Lambda decision
        if report.lambda_decision:
            if not report.lambda_decision.success:
                errors.append(f"Lambda failed: {report.lambda_decision.error}")
            elif report.lambda_decision.warnings:
                for warning in report.lambda_decision.warnings[:3]:
                    errors.append(f"Lambda warning: {warning[:100]}")

        # Check HAR for failed requests
        if report.har_summary and report.har_summary.failed_requests:
            for req in report.har_summary.failed_requests[:3]:
                errors.append(f"HTTP {req.status}: {req.method} {req.url[:80]}")

        # Check error logs
        if report.error_logs:
            for log in report.error_logs[:5]:
                errors.append(f"[{log.component}] {log.message[:100]}")

        # Check CloudWatch
        if report.cloudwatch and report.cloudwatch.error:
            errors.append(f"CloudWatch: {report.cloudwatch.error}")

        report.has_errors = len(errors) > 0
        report.error_summary = "\n".join(errors) if errors else "No errors found"
