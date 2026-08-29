"""
Unified log aggregator.

Aggregates logs from multiple sources:
- Loki (API and Runner logs)
- HAR files (Lambda outputs)
- CloudWatch (Lambda execution logs)
"""

import json
import logging
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from orchestrator import status
from orchestrator.clients.loki import LogEntry, LokiClient
from orchestrator.config import get_settings

logger = logging.getLogger(__name__)

# Maximum bytes to read from log files (100KB)
MAX_LOG_READ_BYTES = 100_000

# How many of a single decision's ``warnings`` become their own log entries. The
# rest are counted, never silently dropped: a truncated warning list that does not
# say it was truncated is how a degraded arm stays invisible.
MAX_LAMBDA_WARNING_ENTRIES = 5

#: Severity ladder for the file-parsing fallback, ordered least to most severe.
#:
#: It has to cover the whole PSR-3 vocabulary because Laravel uses it: ALERT is
#: in the live index right now, and CRITICAL and EMERGENCY are the two levels
#: above it. The previous table held only debug/info/warning/warn/error, so an
#: unlisted level fell to the ``.get(..., 0)`` default and ranked *below debug* --
#: which meant ``logs errors`` and every ``--level`` filter silently discarded
#: the three most severe things the API can say. The most severe levels were the
#: only ones that could not be seen.
#:
#: Synonyms share a rank rather than getting their own: ``warn``/``warning`` and
#: ``err``/``error`` are one level each, spelled differently by Logback and by
#: syslog-derived sources.
LOG_LEVEL_SEVERITY: dict[str, int] = {
    "trace": 0,
    "debug": 1,
    "info": 2,
    "notice": 3,
    "warn": 4,
    "warning": 4,
    "err": 5,
    "error": 5,
    "crit": 6,
    "critical": 6,
    "fatal": 6,
    "alert": 7,
    "emerg": 8,
    "emergency": 8,
}

#: Rank given to a level this table does not know. Below everything, so an
#: unparsed level is not mistaken for a severe one -- but see
#: :func:`level_at_least`, which never *raises* an unknown level either.
UNKNOWN_LEVEL_SEVERITY = -1


def level_at_least(level: str, minimum: str) -> bool:
    """True when *level* is at least as severe as *minimum*.

    An unrecognised *minimum* admits everything (the caller asked for something
    this ladder cannot rank, so filtering on it would be guesswork); an
    unrecognised *level* is admitted only when no minimum was requested.
    """
    floor = LOG_LEVEL_SEVERITY.get(minimum.strip().lower(), UNKNOWN_LEVEL_SEVERITY)
    if floor == UNKNOWN_LEVEL_SEVERITY:
        return True
    return LOG_LEVEL_SEVERITY.get(level.strip().lower(), UNKNOWN_LEVEL_SEVERITY) >= floor


def read_file_tail(file_path: Path, max_bytes: int = MAX_LOG_READ_BYTES) -> str:
    """
    Read the tail of a file efficiently without loading the entire file.

    Uses file seeking to read only the last `max_bytes` of large files,
    avoiding memory issues with very large log files.

    Args:
        file_path: Path to the file to read
        max_bytes: Maximum bytes to read from the end

    Returns:
        File content (or tail for large files)
    """
    try:
        file_size = file_path.stat().st_size

        if file_size <= max_bytes:
            # Small file - read entire content
            return file_path.read_text(errors="replace")

        # Large file - seek to position and read tail
        with open(file_path, "rb") as f:
            f.seek(file_size - max_bytes)
            # Skip partial line at the start
            f.readline()
            content = f.read().decode("utf-8", errors="replace")

        return content

    except OSError as e:
        logger.debug(f"Error reading file {file_path}: {e}")
        return ""


@dataclass
class AggregatedResult:
    """Result of an aggregated log query."""

    entries: list[LogEntry]
    # source -> count, or "<source>_error" -> error message for sources that failed
    sources: dict[str, int | str]
    total_count: int
    query_params: dict


class LogAggregator:
    """
    Unified interface for querying logs from all sources.

    Sources:
    - Loki: API and Runner logs (when Loki is available)
    - Files: Direct log file access (fallback)
    - HAR: Lambda outputs from runner HAR captures
    - CloudWatch: Lambda execution logs (optional)
    """

    def __init__(
        self,
        loki_url: str | None = None,
        har_path: str | None = None,
        api_log_path: str | None = None,
        runner_log_path: str | None = None,
    ):
        """
        Initialize the log aggregator.

        Args:
            loki_url: Loki server URL (None to use settings)
            har_path: Path to HAR output directory
            api_log_path: Path to API log directory
            runner_log_path: Path to runner log directory
        """
        settings = get_settings()

        self.loki_url = loki_url or settings.loki_url
        self.har_path = Path(har_path or settings.pm_runner_har_path)
        self.api_log_path = Path(api_log_path or settings.pm_api_log_path)
        self.runner_log_path = Path(runner_log_path or settings.pm_runner_log_path)

        # Job data lives in MinIO since primemover_runner cadb15e stopped the
        # local disk copy; har_path stays as the fallback for hosts that still
        # write locally (storage.local.enabled=true).
        from orchestrator.logs.jobdata_s3 import store_from_settings

        self.jobdata_store = store_from_settings(settings)

        self._loki_client: LokiClient | None = None

    @property
    def loki_client(self) -> LokiClient:
        """Get or create Loki client."""
        if self._loki_client is None:
            self._loki_client = LokiClient(base_url=self.loki_url)
        return self._loki_client

    def is_loki_available(self) -> bool:
        """Check if Loki is available."""
        try:
            return self.loki_client.is_healthy()
        except Exception:
            return False

    # Valid component names for filtering
    COMPONENTS = {
        # Application components
        "api",  # Laravel API
        "runner",  # Java Runner
        "lambda",  # AWS Lambda (CloudWatch + HAR)
        # Infrastructure components
        "apache",  # Apache web server
        "mysql",  # MySQL database
        "redis",  # Redis cache
        "php",  # PHP-FPM
        "memcached",  # Memcached
        # Special values
        "all",  # All components
    }

    # Components that are queried via Loki
    LOKI_COMPONENTS = {"api", "runner", "apache", "mysql", "redis", "php", "memcached"}

    def query(
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
        include_lambda: bool = True,
    ) -> AggregatedResult:
        """
        Query logs from all sources.

        Args:
            component: Filter by component (api, runner, lambda, apache, mysql,
                      redis, php, memcached, or None for all)
            level: Minimum log level
            search: Text search pattern
            experiment_id: Filter by experiment ID
            crawler_id: Filter by crawler ID
            job_id: Filter by job ID
            queue_id: Filter by queue (joblist / run) ID
            time_range: Time range (15m, 1h, 24h, 7d)
            limit: Maximum results per source
            include_lambda: Whether to include Lambda HAR extraction

        Returns:
            AggregatedResult with entries from all sources
        """
        entries: list[LogEntry] = []
        sources: dict[str, int | str] = {}

        query_params = {
            "component": component,
            "level": level,
            "search": search,
            "experiment_id": experiment_id,
            "crawler_id": crawler_id,
            "job_id": job_id,
            "queue_id": queue_id,
            "time_range": time_range,
            "limit": limit,
        }

        # Determine which components to query via Loki
        query_loki = component is None or component == "all" or component in self.LOKI_COMPONENTS

        # Query Loki for application and infrastructure logs
        if query_loki:
            if self.is_loki_available():
                try:
                    loki_result = self.loki_client.query_logs(
                        component=component if component not in (None, "all") else None,
                        level=level,
                        search=search,
                        experiment_id=experiment_id,
                        crawler_id=crawler_id,
                        job_id=job_id,
                        queue_id=queue_id,
                        time_range=time_range,
                        limit=limit,
                    )
                    entries.extend(loki_result.entries)
                    sources["loki"] = len(loki_result.entries)
                except Exception as e:
                    sources["loki_error"] = str(e)
            else:
                # Fallback to direct file reading
                file_entries = self._query_log_files(
                    component=component,
                    level=level,
                    search=search,
                    limit=limit,
                )
                entries.extend(file_entries)
                sources["files"] = len(file_entries)

        # Query Lambda decision outputs from job reports
        if include_lambda and component in (None, "all", "lambda"):
            lambda_entries = self._query_lambda_output(
                experiment_id=experiment_id,
                crawler_id=crawler_id,
                job_id=job_id,
                search=search,
                limit=limit,
            )
            entries.extend(lambda_entries)
            sources["lambda_output"] = len(lambda_entries)

        # Query Lambda logs from CloudWatch
        if include_lambda and component in (None, "all", "lambda"):
            cloudwatch_entries = self._query_lambda_cloudwatch(
                job_id=job_id,
                search=search,
                time_range=time_range,
                limit=limit,
            )
            entries.extend(cloudwatch_entries)
            sources["cloudwatch"] = len(cloudwatch_entries)

        # Sort all entries by timestamp (newest first)
        entries.sort(key=lambda e: e.timestamp, reverse=True)

        # Apply global limit
        entries = entries[:limit]

        return AggregatedResult(
            entries=entries,
            sources=sources,
            total_count=len(entries),
            query_params=query_params,
        )

    def _query_log_files(
        self,
        component: str | None = None,
        level: str | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> list[LogEntry]:
        """Query log files directly (fallback when Loki unavailable)."""
        from orchestrator.logs.parsers.laravel import parse_laravel_log
        from orchestrator.logs.parsers.runner import parse_runner_log

        entries: list[LogEntry] = []

        # Read API logs
        if component in (None, "all", "api"):
            for log_file in self.api_log_path.glob("*.log"):
                try:
                    # Use efficient tail reading for large files
                    content = read_file_tail(log_file)
                    if content:
                        file_entries = parse_laravel_log(content)
                        entries.extend(file_entries)
                except Exception as e:
                    logger.debug(f"Error parsing {log_file}: {e}")

        # Read Runner logs
        if component in (None, "all", "runner"):
            for log_file in self.runner_log_path.glob("*.log"):
                try:
                    # Use efficient tail reading for large files
                    content = read_file_tail(log_file)
                    if content:
                        file_entries = parse_runner_log(content)
                        entries.extend(file_entries)
                except Exception as e:
                    logger.debug(f"Error parsing {log_file}: {e}")

        # Filter by level. The ladder is LOG_LEVEL_SEVERITY, which -- unlike the
        # five-entry table this replaced -- ranks Laravel's ALERT / CRITICAL /
        # EMERGENCY above ERROR instead of below DEBUG.
        if level and level != "all":
            entries = [e for e in entries if level_at_least(e.level, level)]

        # Filter by search
        if search:
            search_lower = search.lower()
            entries = [e for e in entries if search_lower in e.message.lower()]

        # Sort and limit
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries[:limit]

    def _query_lambda_output(
        self,
        experiment_id: int | None = None,
        crawler_id: int | None = None,
        job_id: int | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> list[LogEntry]:
        """Extract Lambda decision outputs from job report zip files."""
        entries: list[LogEntry] = []

        if self.jobdata_store.configured:
            return self._query_lambda_output_s3(job_id=job_id, search=search, limit=limit)

        if not self.har_path.exists():
            return entries

        # If specific job_id, look in that directory
        if job_id:
            job_dirs = [self.har_path / str(job_id)]
        else:
            # Get recent job directories
            job_dirs = sorted(
                [d for d in self.har_path.iterdir() if d.is_dir()],
                key=lambda d: d.stat().st_mtime,
                reverse=True,
            )[
                :50
            ]  # Last 50 jobs

        for job_dir in job_dirs:
            if not job_dir.exists():
                continue

            # Find Lambda output zips
            for zip_file in job_dir.glob("*lambda*.zip"):
                try:
                    lambda_entries = self._extract_lambda_from_zip(
                        zip_file,
                        search=search,
                    )
                    entries.extend(lambda_entries)
                except Exception:
                    pass

        # Sort and limit
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries[:limit]

    def _query_lambda_output_s3(
        self,
        job_id: int | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> list[LogEntry]:
        """Extract Lambda decision outputs from the MinIO job-data store.

        The store is flat, so a job's objects are found by matching the id in
        the key rather than by opening a directory. Only ``lambda-jobdata``
        objects carry the decision payload; the HAR and the page source live
        beside them under the same job id and are not parsed here.

        A store that cannot be reached is reported and yields nothing, rather
        than raising: an unreachable archive is missing evidence about a job,
        not evidence that the job was fine.
        """
        import io

        entries: list[LogEntry] = []
        try:
            job_ids = (
                [job_id] if job_id is not None else self.jobdata_store.recent_job_ids(limit=50)
            )
        except Exception as exc:
            logger.warning("job data store unavailable at %s: %s", self.jobdata_store.endpoint, exc)
            return entries

        for jid in job_ids:
            for obj in self.jobdata_store.objects_for_job(jid):
                if obj.kind != "lambda-jobdata":
                    continue
                try:
                    payload = self.jobdata_store.read(obj.key)
                except Exception as exc:
                    logger.debug("could not read %s: %s", obj.key, exc)
                    continue
                entries.extend(
                    self._extract_lambda_from_zip(io.BytesIO(payload), search=search, job_id=jid)
                )

        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries[:limit]

    def _extract_lambda_from_zip(
        self,
        zip_path: Path | IO[bytes],
        search: str | None = None,
        job_id: int | None = None,
    ) -> list[LogEntry]:
        """Extract Lambda decision output from a job report zip file.

        ``zip_path`` may be a local path or an open binary stream, so one parser
        serves both the on-disk layout and objects fetched from MinIO. ``job_id``
        is supplied by the object-store caller, whose keys are flat and carry the
        id in the object name rather than in a parent directory.
        """
        entries: list[LogEntry] = []

        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                if "output" in name.lower() and name.endswith(".json"):
                    try:
                        with zf.open(name) as f:
                            data = json.load(f)

                        # Job id comes from the caller for object-store reads;
                        # for a local zip it is still in the parent directory.
                        if job_id is None and isinstance(zip_path, Path):
                            import re

                            match = re.search(r"(\d+)", zip_path.parent.name)
                            if match:
                                job_id = int(match.group(1))

                        # Create entry for the Lambda execution
                        timestamp = datetime.now(timezone.utc)

                        # Try to get timestamp from data
                        if "timestamps" in data:
                            ts_data = data["timestamps"]
                            if isinstance(ts_data, dict) and "timestamp" in ts_data:
                                ts_values = ts_data["timestamp"]
                                if isinstance(ts_values, dict) and ts_values:
                                    first_ts = next(iter(ts_values.values()), None)
                                    if first_ts and not (
                                        isinstance(first_ts, float) and first_ts != first_ts
                                    ):
                                        try:
                                            timestamp = datetime.fromtimestamp(
                                                float(first_ts), tz=timezone.utc
                                            )
                                        except (ValueError, TypeError):
                                            pass

                        # Build message summary
                        selected = data.get("selected", {})
                        message = f"Lambda decision: selected video '{selected.get('title', 'unknown')[:50]}'"

                        if search and search.lower() not in message.lower():
                            if search.lower() not in json.dumps(data).lower():
                                continue

                        entries.extend(
                            self._lambda_entries(
                                data,
                                message=message,
                                timestamp=timestamp,
                                job_id=job_id,
                            )
                        )

                    except (json.JSONDecodeError, KeyError):
                        pass

        return entries

    @staticmethod
    def _lambda_entries(
        data: dict,
        message: str,
        timestamp: datetime,
        job_id: int | None,
    ) -> list[LogEntry]:
        """Turn one decision-function response into log entries.

        The decision function answers HTTP 200 even when a requested filter has
        failed, so the status code never reveals a degraded decision. The fields
        that do are read through ``orchestrator.status``:

        ``degraded``
            At least one *requested* filter failed, or selection failed
            outright. "A human should look at this decision."
        ``treatment_lost``
            The bot was configured to be treated and produced control-arm data
            anyway. This is the one alarm condition, and it gets its own
            ERROR-level entry rather than being one warning string among several.
        ``selection_mode``
            A neutral description of how the video was chosen. Nothing here
            branches on it. ``random`` is the correct answer for a control
            crawler -- 82% of the decision payloads on this box carry no rating
            column at all, and 236 of 243 live bot configurations are control-arm,
            so an ERROR per random selection would have fired on four of every
            five ``lambda_select`` jobs and buried the real ones.
        ``filters``
            Per-filter ``ok`` / ``skipped`` / ``failed: <ExcClass>: <msg>``.
            ``skipped`` means NOT REQUESTED and is never counted as a failure --
            on a control crawler five of the six are skipped on every healthy
            invocation.

        A payload carrying no report at all is a *legacy* payload from the
        currently deployed decision function. Absence reads as "unknown", never
        as degraded, so the ``degraded`` label says ``unknown`` rather than
        ``false``.
        """
        selected = data.get("selected", {}) or {}
        verdict = status.lambda_verdict(data)
        failed_filters = verdict.failed_filters

        summary = message
        if verdict.selection_mode:
            summary += f" [selection_mode={verdict.selection_mode}]"
        if verdict.treatment_lost:
            summary += " [TREATMENT LOST]"
        elif verdict.degraded:
            summary += " [DEGRADED]"
        elif not verdict.reported:
            summary += " [no degradation report]"

        labels = {
            "xpath": str(data.get("xpath", ""))[:100],
            "video_id": selected.get("id", ""),
        }
        if verdict.selection_mode:
            labels["selection_mode"] = verdict.selection_mode
        # Three states, not two: "unknown" is what a payload with no report means,
        # and collapsing it to "false" is how a blind spot reads as a clean bill
        # of health.
        if verdict.degraded:
            labels["degraded"] = "true"
        elif verdict.reported:
            labels["degraded"] = "false"
        else:
            labels["degraded"] = "unknown"
        labels["treatment_lost"] = "true" if verdict.treatment_lost else "false"
        if failed_filters:
            labels["failed_filters"] = ",".join(sorted(failed_filters))
        if verdict.skipped_filters:
            labels["skipped_filters"] = ",".join(verdict.skipped_filters)

        entries = [
            LogEntry(
                timestamp=timestamp,
                component="lambda",
                level="warning" if verdict.degraded else "info",
                message=summary,
                labels=labels,
                raw=json.dumps(data)[:1000],
                job_id=job_id,
            )
        ]

        hard_error = verdict.error
        if hard_error:
            entries.append(
                LogEntry(
                    timestamp=timestamp,
                    component="lambda",
                    level="error",
                    message=f"Lambda decision failed: {hard_error}"[:500],
                    labels={"degraded": "true"},
                    raw=str(hard_error),
                    job_id=job_id,
                )
            )

        if verdict.treatment_lost:
            entries.append(
                LogEntry(
                    timestamp=timestamp,
                    component="lambda",
                    level="error",
                    message=(
                        "Lambda decision lost the treatment: this crawler was "
                        "configured to be treated but no requested treatment applied, "
                        "so it produced control-arm data for this job"
                    ),
                    labels={
                        "selection_mode": verdict.selection_mode,
                        "degraded": "true",
                        "treatment_lost": "true",
                    },
                    raw=json.dumps({"filters": verdict.filters, "llm": data.get("llm")})[:1000],
                    job_id=job_id,
                )
            )

        # Only *requested* filters that failed. A "skipped" filter was never
        # asked for and must never produce an entry.
        for name, state in sorted(failed_filters.items()):
            entries.append(
                LogEntry(
                    timestamp=timestamp,
                    component="lambda",
                    level="warning",
                    message=f"Lambda filter '{name}' did not apply: {state}"[:500],
                    labels={"filter": name, "degraded": "true"},
                    raw=state,
                    job_id=job_id,
                )
            )

        warnings = verdict.warnings
        for warning in warnings[:MAX_LAMBDA_WARNING_ENTRIES]:
            entries.append(
                LogEntry(
                    timestamp=timestamp,
                    component="lambda",
                    level="warning",
                    message=str(warning)[:500],
                    labels={},
                    raw=str(warning),
                    job_id=job_id,
                )
            )

        # Never truncate silently: say how many were dropped and where to read them.
        dropped = len(warnings) - MAX_LAMBDA_WARNING_ENTRIES
        if dropped > 0:
            entries.append(
                LogEntry(
                    timestamp=timestamp,
                    component="lambda",
                    level="warning",
                    message=(
                        f"... and {dropped} further lambda warning(s) not shown "
                        f"({len(warnings)} in total); read them all with "
                        f"LogAggregator.get_lambda_output(job_id)"
                    ),
                    labels={"truncated": str(dropped)},
                    raw="",
                    job_id=job_id,
                )
            )

        return entries

    def _query_lambda_cloudwatch(
        self,
        job_id: int | None = None,
        search: str | None = None,
        time_range: str = "1h",
        limit: int = 100,
    ) -> list[LogEntry]:
        """Query Lambda logs from CloudWatch."""
        try:
            from orchestrator.clients.cloudwatch import CloudWatchClient
        except ImportError:
            return []

        try:
            client = CloudWatchClient()

            if not client.is_available():
                return []

            result = client.query_logs(
                time_range=time_range,
                job_id=job_id,
                filter_pattern=f'"{search}"' if search else None,
                limit=limit,
            )

            entries = []
            for cw_entry in result.entries:
                # Skip START/END/REPORT lines for cleaner output
                if any(
                    x in cw_entry.message
                    for x in ["START RequestId", "END RequestId", "REPORT RequestId"]
                ):
                    continue

                entry = LogEntry(
                    timestamp=cw_entry.timestamp,
                    component="lambda",
                    level=cw_entry.level,
                    message=cw_entry.message[:500],
                    labels={
                        "request_id": cw_entry.request_id or "",
                        "log_stream": cw_entry.log_stream,
                    },
                    raw=cw_entry.message,
                    job_id=cw_entry.job_id,
                    crawler_id=cw_entry.crawler_id,
                    experiment_id=cw_entry.experiment_id,
                )
                entries.append(entry)

            client.close()
            return entries

        except Exception:
            return []

    def get_lambda_output(self, job_id: int) -> dict | None:
        """
        Get full Lambda output for a specific job.

        Args:
            job_id: Job ID

        Returns:
            Lambda output dict or None if not found
        """
        job_dir = self.har_path / str(job_id)

        if not job_dir.exists():
            return None

        for zip_file in job_dir.glob("*lambda*.zip"):
            try:
                with zipfile.ZipFile(zip_file) as zf:
                    for name in zf.namelist():
                        if "output" in name.lower() and name.endswith(".json"):
                            with zf.open(name) as f:
                                output: dict = json.load(f)
                                return output
            except Exception:
                pass

        return None

    def tail(
        self,
        component: str | None = None,
        level: str | None = None,
        search: str | None = None,
    ) -> Iterator[LogEntry]:
        """
        Tail logs in real-time.

        Args:
            component: Filter by component
            level: Minimum log level
            search: Search pattern

        Yields:
            LogEntry objects as they arrive
        """
        if self.is_loki_available():
            # Build query
            labels = []
            if component and component not in ("all", None):
                labels.append(f'component="{component}"')

            query = "{" + ",".join(labels) + "}" if labels else "{}"

            if search:
                query += f' |~ "(?i){search}"'

            yield from self.loki_client.tail(query)
        else:
            # Fallback: poll log files
            import time

            seen_entries: set[str] = set()

            while True:
                result = self.query(
                    component=component,
                    level=level,
                    search=search,
                    time_range="1m",
                    limit=50,
                )

                for entry in reversed(result.entries):
                    entry_key = f"{entry.timestamp}:{entry.message[:50]}"
                    if entry_key not in seen_entries:
                        seen_entries.add(entry_key)
                        yield entry

                # Keep set size manageable
                if len(seen_entries) > 1000:
                    seen_entries = set(list(seen_entries)[-500:])

                time.sleep(1)

    def close(self):
        """Close any open connections."""
        if self._loki_client:
            self._loki_client.close()
            self._loki_client = None
