"""
Job Debugger - Combines all data sources for comprehensive job diagnostics.

Aggregates:
- Job metadata from PrimeMover API
- Logs from Loki/files (API, Runner, infrastructure)
- Lambda decision output from job reports
- HAR file analysis for browser activity
- CloudWatch Lambda execution logs
"""

import io
import json
import logging
import os
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from orchestrator import status as status_contract

from .har_parser import HARParser, HARSummary

logger = logging.getLogger(__name__)


@dataclass
class LambdaDecision:
    """Lambda decision output from job report.

    ``success`` used to mean "the output.json parsed", so a payload carrying a
    hard ``{"error": ...}`` was reported as a successful decision and
    ``_generate_error_summary`` never flagged it. The questions are kept apart:

    ``output_parsed``
        the report contained an output.json and it deserialised.
    ``success``
        the decision function actually did what was asked -- it parsed, carried
        no error, and every *requested* filter applied.
    ``degraded``
        at least one requested filter failed, or selection failed outright.
        "A human should look at this decision."
    ``treatment_lost``
        the bot was configured to be treated and produced control-arm data
        anyway. "This session's data is scientifically void." This is the only
        alarm-worthy state, and the one to surface first.
    ``selection_mode``
        a *neutral* description of how the video was picked. ``"random"`` means
        no LLM rating informed it -- which is the correct, healthy outcome for a
        control crawler, and 82% of this box's traffic. Never a finding on its
        own; see :mod:`orchestrator.status`.
    ``report_present``
        whether the payload carried a degradation report at all. ``False`` is
        *unknown* (a payload from the currently deployed decision function), not
        "clean" and not "degraded".
    """

    success: bool = False
    output_parsed: bool = False
    degraded: bool = False
    treatment_lost: bool = False
    report_present: bool = False
    selection_mode: str = ""
    filters: dict[str, str] = field(default_factory=dict)
    llm: dict = field(default_factory=dict)
    selected_title: str = ""
    selected_id: str = ""
    xpath: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    raw_output: dict | None = None

    @property
    def random_selection(self) -> bool:
        """True when no LLM rating informed the pick.

        Descriptive only. It is **not** evidence that anything went wrong: a
        control crawler requests no LLM filter and therefore always selects at
        random. Use :attr:`treatment_lost` for "this arm produced control-arm
        data when it was supposed to be treated".
        """
        return self.selection_mode == "random"

    @property
    def failed_filters(self) -> dict[str, str]:
        """Only the filters that were requested and did not apply.

        ``"skipped"`` means *not requested* and must never be counted here: it
        is the state of five of the six filters on every healthy control
        invocation, and counting it made ``debug job`` report filter casualties
        on clean jobs.
        """
        return status_contract.lambda_failed_filters({"filters": self.filters})

    @property
    def skipped_filters(self) -> list[str]:
        """Filters that were never requested. Informational, never a finding."""
        return status_contract.lambda_skipped_filters({"filters": self.filters})

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "output_parsed": self.output_parsed,
            "degraded": self.degraded,
            "treatment_lost": self.treatment_lost,
            "report_present": self.report_present,
            "selection_mode": self.selection_mode,
            "filters": self.filters,
            "failed_filters": self.failed_filters,
            "skipped_filters": self.skipped_filters,
            "llm": self.llm,
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
    """Job metadata from PrimeMover API.

    ``status`` holds a *canonical* status code (see :mod:`orchestrator.status`),
    not the raw field: the API sends ``status_code`` and this class used to read
    ``status``, so it was always "" and every status-based branch downstream was
    dead.
    """

    job_id: int
    experiment_id: int | None = None
    crawler_id: int | None = None
    queue_id: int | None = None
    experiment_name: str = ""
    crawler_name: str = ""
    status: str = ""
    status_message: str = ""
    job_type: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    error_message: str | None = None
    raw_data: dict | None = None

    @property
    def is_failure(self) -> bool:
        return status_contract.is_failure(self.status)

    @property
    def is_warning(self) -> bool:
        return status_contract.is_warning(self.status)

    @property
    def needs_attention(self) -> bool:
        """A failure *or* a warning -- what "should a human look at this?" means."""
        return status_contract.is_bad(self.status)

    @property
    def diagnostic(self) -> str:
        """The best available human-readable account of what happened."""
        return self.error_message or self.status_message or ""

    @property
    def diagnostic_label(self) -> str:
        """What a renderer should *call* that diagnostic.

        Not always "Error": a ``warning`` job ran to completion and its
        diagnostic explains a degradation, so labelling it "Error" both
        overstates it and contradicts the Status row two lines above.
        """
        if self.is_failure:
            return "Error"
        if self.is_warning:
            return "Degradation"
        return "Detail"

    @property
    def decision(self) -> status_contract.LambdaVerdict:
        """The decision verdict the API now persists on the job row.

        Reads the four ``decision_*`` columns out of ``raw_data``. Before they
        existed, the only witness to a lost treatment was the job's MinIO object
        -- and when the store is unreachable (which it silently was for a month)
        that meant no witness at all. ``verdict.unknown`` is ``True`` for a
        legacy job, a non-lambda job, or a job written by a runner that predates
        the columns; unknown is never rendered as clean.
        """
        return status_contract.job_decision_verdict(self.raw_data)

    @property
    def diagnostic_is_duplicate(self) -> bool:
        """True when :attr:`diagnostic` is verbatim the ``status_message``.

        The usual case: the API sends no ``error_message`` of its own, so the
        runner's ``status_message`` is promoted into it. A panel that already
        shows a Status Message row must skip the second one rather than print
        the same sentence twice under two different labels.
        """
        error_message = self.error_message
        if not error_message:
            return False
        return error_message.strip() == self.status_message.strip()

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "job_id": self.job_id,
            "experiment_id": self.experiment_id,
            "crawler_id": self.crawler_id,
            "queue_id": self.queue_id,
            "experiment_name": self.experiment_name,
            "crawler_name": self.crawler_name,
            "status": self.status,
            "status_message": self.status_message,
            "job_type": self.job_type,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_seconds": self.duration_seconds,
            "error_message": self.error_message,
            "diagnostic": self.diagnostic,
            "diagnostic_label": self.diagnostic_label,
            # Omitted rather than serialised as all-false when no verdict was
            # recorded: null is "we cannot tell", and a false verdict is a claim.
            "decision": self.decision.to_dict() if self.decision.reported else None,
        }


@dataclass
class CloudWatchInfo:
    """CloudWatch Lambda execution info."""

    available: bool = False
    request_id: str = ""
    duration_ms: float = 0
    billed_duration_ms: float = 0
    init_duration_ms: float = 0
    memory_used_mb: int = 0
    memory_limit_mb: int = 0
    logs: list[LogEntry] = field(default_factory=list)
    error: str | None = None

    @property
    def error_entries(self) -> list[LogEntry]:
        """The error-level lines among ``logs``.

        There is no separate ``errors`` list to populate: CloudWatch gives one
        stream, and the errors in it are simply its error-level entries.
        """
        return [log for log in self.logs if log.level.lower() in ("error", "critical", "fatal")]

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "available": self.available,
            "request_id": self.request_id,
            "duration_ms": self.duration_ms,
            "billed_duration_ms": self.billed_duration_ms,
            "init_duration_ms": self.init_duration_ms,
            "memory_used_mb": self.memory_used_mb,
            "memory_limit_mb": self.memory_limit_mb,
            "logs": [log.to_dict() for log in self.logs],
            "errors": [log.message for log in self.error_entries],
            "error": self.error,
        }


@dataclass
class Artifacts:
    """Paths to job artifacts."""

    job_dir: Path | None = None
    har_file: Path | None = None
    screenshot_files: list[Path] = field(default_factory=list)
    lambda_zip: Path | None = None

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
    metadata: JobMetadata | None = None
    lambda_decision: LambdaDecision | None = None
    har_summary: HARSummary | None = None
    cloudwatch: CloudWatchInfo | None = None
    artifacts: Artifacts | None = None

    # Logs by component
    logs: list[LogEntry] = field(default_factory=list)
    error_logs: list[LogEntry] = field(default_factory=list)

    # Summary
    has_errors: bool = False
    error_summary: str = ""
    # "warning" findings are kept apart from errors on purpose: a degraded
    # decision is not a failure, and folding it into error_summary would either
    # over-report the job as failed or (as before) not be reported at all.
    has_warnings: bool = False
    warning_summary: str = ""

    @property
    def treatment_lost(self) -> bool:
        """The one finding that voids a session's data.

        Promoted to the report level so a caller does not have to reach into
        ``lambda_decision`` (and does not have to know that ``selection_mode``
        is not the field to look at).

        Either witness is enough. The MinIO payload is the richer one, but it is
        also the one that goes missing; the job row's persisted verdict answers
        even when no artifact could be fetched.
        """
        if self.metadata is not None and self.metadata.decision.treatment_lost:
            return True
        return bool(self.lambda_decision and self.lambda_decision.treatment_lost)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "job_id": self.job_id,
            "generated_at": self.generated_at.isoformat(),
            "treatment_lost": self.treatment_lost,
            "metadata": self.metadata.to_dict() if self.metadata else None,
            "lambda_decision": self.lambda_decision.to_dict() if self.lambda_decision else None,
            "har_summary": self.har_summary.to_dict() if self.har_summary else None,
            "cloudwatch": self.cloudwatch.to_dict() if self.cloudwatch else None,
            "artifacts": self.artifacts.to_dict() if self.artifacts else None,
            "logs": [log.to_dict() for log in self.logs],
            "error_logs": [log.to_dict() for log in self.error_logs],
            "has_errors": self.has_errors,
            "error_summary": self.error_summary,
            "has_warnings": self.has_warnings,
            "warning_summary": self.warning_summary,
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
        api_base_url: str | None = None,
        api_email: str | None = None,
        api_password: str | None = None,
        har_path: str | None = None,
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

        # Job data has gone only to MinIO since primemover_runner cadb15e; the
        # local directory this used to read is never created any more. See
        # orchestrator/logs/jobdata_s3.py.
        from orchestrator.logs.jobdata_s3 import store_from_settings

        self.jobdata_store = store_from_settings(settings)
        self.jobdata_cache = (
            Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
            / "primemover-orchestrator"
            / "jobdata"
        )

        self._token: str | None = None
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
            report.lambda_decision = self._extract_lambda_decision(report.artifacts.lambda_zip)

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

    def _get_job_metadata(self, job_id: int) -> JobMetadata | None:
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
                return JobMetadata(
                    job_id=job_id, error_message=f"API error: {response.status_code}"
                )

            data = response.json().get("data", response.json())
            return self._metadata_from_payload(job_id, data)

        except Exception as e:
            return JobMetadata(job_id=job_id, error_message=str(e))

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        """Parse one API timestamp, or None. The API mixes offset spellings."""
        if not isinstance(value, str) or not value:
            return None
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        # +0200 -> +02:00; fromisoformat only learned the colon-less form in 3.11.
        if len(text) >= 5 and text[-5] in "+-" and text[-4:].isdigit():
            text = text[:-2] + ":" + text[-2:]
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    def _metadata_from_payload(self, job_id: int, data: dict) -> JobMetadata:
        """Build JobMetadata from a ``GET /jobs/{id}`` payload.

        Split out from the HTTP call so the field-name handling -- the part that
        was wrong -- is testable without a live API.
        """
        # ``status_code``/``finished_at`` are what the API sends. This used to
        # read ``status`` and ``completed_at``, neither of which exists on the
        # wire, so the status was always "" and the duration always None.
        started_at = self._parse_timestamp(data.get("started_at"))
        completed_at = self._parse_timestamp(status_contract.finished_at_of(data))

        duration = None
        if started_at and completed_at:
            seconds = (completed_at - started_at).total_seconds()
            duration = seconds if seconds >= 0 else None

        status_code = status_contract.status_of(data)
        status_message = str(data.get("status_message") or "")

        def _nested(key: str) -> dict:
            value = data.get(key)
            return value if isinstance(value, dict) else {}

        queue = _nested("queue")
        crawler = _nested("crawler")
        experiment = _nested("experiment")

        crawler_id = data.get("crawler_id") or queue.get("crawler_id") or crawler.get("id")
        experiment_id = (
            data.get("experiment_id") or crawler.get("experiment_id") or experiment.get("id")
        )
        queue_id = data.get("queue_id") or data.get("joblist_id") or queue.get("id")

        return JobMetadata(
            job_id=job_id,
            experiment_id=experiment_id,
            crawler_id=crawler_id,
            queue_id=queue_id,
            experiment_name=str(data.get("experiment_name") or experiment.get("name") or ""),
            crawler_name=str(data.get("crawler_name") or crawler.get("name") or ""),
            status=status_code,
            status_message=status_message,
            job_type=str(data.get("type") or data.get("job_type") or ""),
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration,
            # status_message is the field the runner now puts a real diagnostic
            # in, and it was dropped on the floor everywhere in this codebase.
            # Only promote it to error_message when the job actually went wrong:
            # on a green job it reads "successfully finished job", which must
            # not render as an error.
            error_message=(
                data.get("error_message")
                or (status_message if status_contract.is_bad(status_code) else None)
                or None
            ),
            raw_data=data,
        )

    def _find_artifacts(self, job_id: int) -> Artifacts:
        """Find job artifacts in the output directory."""
        artifacts = Artifacts()

        job_dir: Path | None = self.har_path / str(job_id)
        if job_dir is None or not job_dir.exists():
            # Nothing on local disk is the normal case now, not an error.
            job_dir = self._materialise_from_store(job_id)
        if job_dir is None:
            return artifacts

        artifacts.job_dir = job_dir

        # Find HAR file
        har_files = list(job_dir.glob("*.har"))
        if har_files:
            artifacts.har_file = har_files[0]

        # Find screenshots
        artifacts.screenshot_files = list(job_dir.glob("*.png")) + list(job_dir.glob("*.jpg"))

        # Find Lambda zip
        lambda_zips = list(job_dir.glob("*lambda*.zip"))
        if lambda_zips:
            artifacts.lambda_zip = lambda_zips[0]

        return artifacts

    def _materialise_from_store(self, job_id: int) -> Path | None:
        """Lay a job's stored objects out on disk the way the parsers expect.

        The object store is flat and its objects are zips; every reader below
        wants files -- ``*lambda*.zip``, ``*.har``, ``*.png``. Rather than
        rewrite three parsers around byte streams, one job's objects are written
        into a cache directory in that shape. Cheap (a few hundred KB per job),
        reusable across runs, and it keeps the exported report's paths valid
        after the command exits, which a temporary directory would not.

        Returns None when the store is unconfigured or holds nothing for the job
        -- the caller then reports no artifacts, exactly as before.
        """
        if not self.jobdata_store.configured:
            return None

        try:
            objects = self.jobdata_store.objects_for_job(job_id)
        except Exception as e:
            # An unreachable store is missing evidence about a job, never
            # evidence that the job was fine -- so say so rather than returning
            # a confident empty result.
            logger.warning("job data store unreachable for job %s: %s", job_id, e)
            return None

        if not objects:
            return None

        job_dir = self.jobdata_cache / str(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)

        for obj in objects:
            try:
                payload = self.jobdata_store.read(obj.key)
            except Exception as e:
                logger.debug("could not read %s: %s", obj.key, e)
                continue

            if not payload:
                continue  # static-captcha is routinely zero bytes

            if obj.kind == "lambda-jobdata":
                # Kept zipped: _extract_lambda_decision opens it as a zip, and
                # the *lambda*.zip glob is what finds it.
                (job_dir / f"{obj.key.rsplit('/', 1)[-1]}.zip").write_bytes(payload)
                continue

            if obj.kind == "static-jobdata":
                # Unpacked: the HAR and the screenshot are inside, and the
                # parsers want them as standalone files.
                try:
                    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                        for name in zf.namelist():
                            lowered = name.lower()
                            if "har" in lowered:
                                # Stored as .json; the glob looks for .har.
                                target = job_dir / f"{Path(name).stem}.har"
                            elif lowered.endswith((".png", ".jpg")):
                                target = job_dir / Path(name).name
                            else:
                                target = job_dir / Path(name).name
                            target.write_bytes(zf.read(name))
                except zipfile.BadZipFile:
                    logger.debug("%s is not a zip; skipped", obj.key)
                continue

            if obj.kind == "page-finalsource":
                try:
                    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                        for name in zf.namelist():
                            (job_dir / Path(name).name).write_bytes(zf.read(name))
                except zipfile.BadZipFile:
                    logger.debug("%s is not a zip; skipped", obj.key)

        return job_dir

    @staticmethod
    def unwrap_invocation(data: dict) -> dict:
        """Return the decision body from the runner's invocation envelope.

        The stored object is what ``LambdaCaller`` recorded, not what the
        decision function returned::

            {"success": true, "statusCode": 200, "functionError": null,
             "payload": "{\\"xpath\\": \\"\\", \\"degraded\\": true, ...}"}

        ``success`` and ``statusCode`` describe the HTTP call, which is 200 even
        when every requested filter failed. Reading them as the decision's own
        verdict reported ``degraded: true, xpath: ""`` as "ok" -- precisely the
        silent success the degradation report exists to prevent, in the tool
        meant to diagnose it.

        A body that is already bare is returned unchanged, so older payloads and
        anything written by a different producer still parse.
        """
        if "payload" not in data:
            return data

        inner = data["payload"]
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except (TypeError, ValueError):
                return data
        return inner if isinstance(inner, dict) else data

    def _extract_lambda_decision(self, zip_path: Path) -> LambdaDecision:
        """Extract Lambda decision from zip file."""
        decision = LambdaDecision()

        try:
            with zipfile.ZipFile(zip_path) as zf:
                for name in zf.namelist():
                    if "output" in name.lower() and name.endswith(".json"):
                        with zf.open(name) as f:
                            data = json.load(f)
                        return self.decision_from_payload(self.unwrap_invocation(data))

        except Exception as e:
            decision.output_parsed = False
            decision.success = False
            decision.error = str(e)

        return decision

    @staticmethod
    def decision_from_payload(data: dict) -> LambdaDecision:
        """Interpret one decision-function response.

        The response is HTTP 200 even when every LLM-driven filter failed, so
        "did this decision succeed?" can only be answered from the body. See the
        ``lambda_*`` helpers in :mod:`orchestrator.status`.
        """
        decision = LambdaDecision(raw_output=data, output_parsed=True)

        raw_selected = data.get("selected")
        selected: dict = raw_selected if isinstance(raw_selected, dict) else {}
        decision.selected_title = str(selected.get("title") or "")
        decision.selected_id = str(selected.get("id") or "")
        decision.xpath = str(data.get("xpath") or "")

        verdict = status_contract.lambda_verdict(data)
        decision.warnings = verdict.warnings
        decision.error = verdict.error
        decision.degraded = verdict.degraded
        decision.treatment_lost = verdict.treatment_lost
        decision.report_present = verdict.reported
        decision.selection_mode = verdict.selection_mode
        decision.filters = verdict.filters

        llm = data.get("llm")
        decision.llm = dict(llm) if isinstance(llm, dict) else {}

        # "The output parsed" is decision.output_parsed. THIS is "the decision
        # function did what was asked": no hard error, and every *requested*
        # filter applied. A control crawler that skipped five filters and picked
        # at random is a success -- that is what it was configured to do.
        decision.success = not decision.error and not decision.degraded
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
                    # Present only on a cold start; the renderer showed it long
                    # before anything parsed it.
                    init_match = re.search(r"Init Duration:\s*([\d.]+)\s*ms", msg)
                    memory_match = re.search(r"Memory Size:\s*(\d+)\s*MB", msg)
                    used_match = re.search(r"Max Memory Used:\s*(\d+)\s*MB", msg)

                    if duration_match:
                        info.duration_ms = float(duration_match.group(1))
                    if billed_match:
                        info.billed_duration_ms = float(billed_match.group(1))
                    if init_match:
                        info.init_duration_ms = float(init_match.group(1))
                    if memory_match:
                        info.memory_limit_mb = int(memory_match.group(1))
                    if used_match:
                        info.memory_used_mb = int(used_match.group(1))

                # Add non-system logs
                if not any(
                    x in msg for x in ["START RequestId", "END RequestId", "REPORT RequestId"]
                ):
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
        metadata: JobMetadata | None,
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
        """Split the findings into hard errors and degradation warnings.

        The status check used to compare against ``("failed", "error")`` while
        ``metadata.status`` was always "" (it read a field the API does not
        send), so this branch had never fired once.
        """
        errors: list[str] = []
        warnings: list[str] = []

        # Check job status. is_bad, not is_failure: a "warning" job ran to
        # completion with something the experiment cares about degraded, which
        # is the whole reason the state exists.
        meta = report.metadata
        if meta:
            if meta.is_failure:
                errors.append(f"Job status: {meta.status}")
                if meta.error_message:
                    errors.append(f"Job error: {meta.error_message}")
            elif meta.is_warning:
                warnings.append(f"Job status: {meta.status}")
                if meta.status_message:
                    warnings.append(f"Job detail: {meta.status_message}")

        # Check Lambda decision. A hard error is a failure; a degraded decision
        # is a warning -- and TREATMENT LOST is the loudest of them, because a
        # bot that was configured to be treated produced control-arm data.
        #
        # What is deliberately absent: any finding derived from
        # ``selection_mode`` alone. Random selection is the correct mode for a
        # control crawler (82% of real payloads on this box), and a filter
        # reported as "skipped" was never requested. Both used to be counted as
        # casualties here, which put warnings on clean jobs.
        ld = report.lambda_decision
        if ld:
            if not ld.output_parsed:
                errors.append(f"Lambda output could not be read: {ld.error}")
            elif ld.error:
                errors.append(f"Lambda failed: {ld.error}")
            else:
                if ld.treatment_lost:
                    # insert, not append: this is the most consequential thing
                    # this report can say, and a reader who stops after the
                    # first line must not miss it behind "Job status: warning".
                    warnings.insert(
                        0,
                        "TREATMENT LOST: this bot was configured to be treated but "
                        "produced control-arm data - no requested treatment applied, "
                        "so this session is scientifically void",
                    )
                elif ld.degraded:
                    warnings.append("Lambda decision degraded: a requested filter did not apply")
                for name, state in list(ld.failed_filters.items())[:6]:
                    warnings.append(f"Lambda filter {name}: {state}")
                for warning in ld.warnings[:3]:
                    warnings.append(f"Lambda warning: {warning[:100]}")

        # The same verdict, read off the job row, when the MinIO object is the
        # witness that is missing. That is not a hypothetical: the HAR readers
        # answered "nothing" for a month after the writer moved to MinIO, and an
        # unreachable store is missing evidence about a job, never evidence that
        # the job was fine. Only consulted when the payload could not be parsed,
        # so a readable artifact never produces the finding twice.
        row_verdict = meta.decision if meta else None
        if row_verdict and row_verdict.reported and not (ld and ld.output_parsed):
            if row_verdict.treatment_lost:
                warnings.insert(
                    0,
                    "TREATMENT LOST (recorded on the job, no payload read): this bot "
                    "was configured to be treated but produced control-arm data, so "
                    "this session is scientifically void",
                )
            elif row_verdict.degraded:
                warnings.append(
                    "Lambda decision degraded (recorded on the job, no payload read): "
                    "a requested filter did not apply"
                )
            for name, state in list(row_verdict.failed_filters.items())[:6]:
                warnings.append(f"Lambda filter {name}: {state}")

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
        report.has_warnings = len(warnings) > 0
        report.warning_summary = "\n".join(warnings) if warnings else "No warnings found"
