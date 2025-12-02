"""
Unified log aggregator.

Aggregates logs from multiple sources:
- Loki (API and Runner logs)
- HAR files (Lambda outputs)
- CloudWatch (Lambda execution logs)
"""

import json
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from orchestrator.clients.loki import LogEntry, LokiClient, QueryResult
from orchestrator.config import get_settings


@dataclass
class AggregatedResult:
    """Result of an aggregated log query."""

    entries: list[LogEntry]
    sources: dict[str, int]  # source -> count
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
        loki_url: Optional[str] = None,
        har_path: Optional[str] = None,
        api_log_path: Optional[str] = None,
        runner_log_path: Optional[str] = None,
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

        self._loki_client: Optional[LokiClient] = None

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

    def query(
        self,
        component: Optional[str] = None,
        level: Optional[str] = None,
        search: Optional[str] = None,
        experiment_id: Optional[int] = None,
        crawler_id: Optional[int] = None,
        job_id: Optional[int] = None,
        time_range: str = "1h",
        limit: int = 100,
        include_lambda: bool = True,
    ) -> AggregatedResult:
        """
        Query logs from all sources.

        Args:
            component: Filter by component (api, runner, lambda, or None for all)
            level: Minimum log level
            search: Text search pattern
            experiment_id: Filter by experiment ID
            crawler_id: Filter by crawler ID
            job_id: Filter by job ID
            time_range: Time range (15m, 1h, 24h, 7d)
            limit: Maximum results per source
            include_lambda: Whether to include Lambda HAR extraction

        Returns:
            AggregatedResult with entries from all sources
        """
        entries: list[LogEntry] = []
        sources: dict[str, int] = {}

        query_params = {
            "component": component,
            "level": level,
            "search": search,
            "experiment_id": experiment_id,
            "crawler_id": crawler_id,
            "job_id": job_id,
            "time_range": time_range,
            "limit": limit,
        }

        # Query Loki for API and Runner logs
        if component in (None, "all", "api", "runner"):
            if self.is_loki_available():
                try:
                    loki_result = self.loki_client.query_logs(
                        component=component if component not in (None, "all") else None,
                        level=level,
                        search=search,
                        experiment_id=experiment_id,
                        crawler_id=crawler_id,
                        job_id=job_id,
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

        # Query Lambda outputs from HAR files
        if include_lambda and component in (None, "all", "lambda"):
            lambda_entries = self._query_lambda_har(
                experiment_id=experiment_id,
                crawler_id=crawler_id,
                job_id=job_id,
                search=search,
                limit=limit,
            )
            entries.extend(lambda_entries)
            sources["lambda_har"] = len(lambda_entries)

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
        component: Optional[str] = None,
        level: Optional[str] = None,
        search: Optional[str] = None,
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
                    content = log_file.read_text(errors="replace")
                    # Only read last portion for efficiency
                    if len(content) > 100000:
                        content = content[-100000:]
                    file_entries = parse_laravel_log(content)
                    entries.extend(file_entries)
                except Exception:
                    pass

        # Read Runner logs
        if component in (None, "all", "runner"):
            for log_file in self.runner_log_path.glob("*.log"):
                try:
                    content = log_file.read_text(errors="replace")
                    if len(content) > 100000:
                        content = content[-100000:]
                    file_entries = parse_runner_log(content)
                    entries.extend(file_entries)
                except Exception:
                    pass

        # Filter by level
        if level and level != "all":
            level_order = {"debug": 0, "info": 1, "warning": 2, "warn": 2, "error": 3}
            min_level = level_order.get(level.lower(), 0)
            entries = [
                e for e in entries
                if level_order.get(e.level.lower(), 0) >= min_level
            ]

        # Filter by search
        if search:
            search_lower = search.lower()
            entries = [e for e in entries if search_lower in e.message.lower()]

        # Sort and limit
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries[:limit]

    def _query_lambda_har(
        self,
        experiment_id: Optional[int] = None,
        crawler_id: Optional[int] = None,
        job_id: Optional[int] = None,
        search: Optional[str] = None,
        limit: int = 100,
    ) -> list[LogEntry]:
        """Extract Lambda outputs from HAR files."""
        entries: list[LogEntry] = []

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
            )[:50]  # Last 50 jobs

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

    def _extract_lambda_from_zip(
        self,
        zip_path: Path,
        search: Optional[str] = None,
    ) -> list[LogEntry]:
        """Extract Lambda output from a HAR zip file."""
        entries: list[LogEntry] = []

        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                if "output" in name.lower() and name.endswith(".json"):
                    try:
                        with zf.open(name) as f:
                            data = json.load(f)

                        # Extract job ID from filename
                        job_id = None
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
                                    if first_ts and not (isinstance(first_ts, float) and first_ts != first_ts):
                                        try:
                                            timestamp = datetime.fromtimestamp(float(first_ts), tz=timezone.utc)
                                        except (ValueError, TypeError):
                                            pass

                        # Build message summary
                        selected = data.get("selected", {})
                        message = f"Lambda decision: selected video '{selected.get('title', 'unknown')[:50]}'"

                        if search and search.lower() not in message.lower():
                            if search.lower() not in json.dumps(data).lower():
                                continue

                        entry = LogEntry(
                            timestamp=timestamp,
                            component="lambda",
                            level="info",
                            message=message,
                            labels={
                                "xpath": data.get("xpath", "")[:100],
                                "video_id": selected.get("id", ""),
                            },
                            raw=json.dumps(data)[:1000],
                            job_id=job_id,
                        )
                        entries.append(entry)

                        # Add warnings as separate entries
                        for warning in data.get("warnings", [])[:5]:
                            warn_entry = LogEntry(
                                timestamp=timestamp,
                                component="lambda",
                                level="warning",
                                message=str(warning)[:500],
                                labels={},
                                raw=str(warning),
                                job_id=job_id,
                            )
                            entries.append(warn_entry)

                    except (json.JSONDecodeError, KeyError):
                        pass

        return entries

    def _query_lambda_cloudwatch(
        self,
        job_id: Optional[int] = None,
        search: Optional[str] = None,
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
                if any(x in cw_entry.message for x in ["START RequestId", "END RequestId", "REPORT RequestId"]):
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

    def get_lambda_output(self, job_id: int) -> Optional[dict]:
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
                                return json.load(f)
            except Exception:
                pass

        return None

    def tail(
        self,
        component: Optional[str] = None,
        level: Optional[str] = None,
        search: Optional[str] = None,
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
