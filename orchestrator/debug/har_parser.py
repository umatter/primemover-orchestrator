"""
HAR (HTTP Archive) Parser for selective extraction.

Parses HAR files efficiently, extracting only relevant information for debugging:
- Request/response metadata (URL, status, timing)
- Failed requests (4xx/5xx) with response bodies
- Slow requests above threshold
- Summary statistics by domain
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


@dataclass
class HARRequest:
    """A single HTTP request from a HAR file."""

    url: str
    method: str
    status: int
    status_text: str
    time_ms: float
    started: datetime | None = None

    # Request details
    request_headers: dict = field(default_factory=dict)
    request_body: str | None = None

    # Response details
    response_headers: dict = field(default_factory=dict)
    response_body: str | None = None
    response_size: int = 0

    # Parsed URL components
    domain: str = ""
    path: str = ""

    def __post_init__(self):
        """Parse URL components."""
        try:
            parsed = urlparse(self.url)
            self.domain = parsed.netloc
            self.path = parsed.path
        except Exception:
            pass

    @property
    def is_failed(self) -> bool:
        """Check if request failed (4xx or 5xx)."""
        return self.status >= 400

    @property
    def is_slow(self) -> bool:
        """Check if request is slow (default: >5000ms)."""
        return self.time_ms > 5000

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "url": self.url,
            "method": self.method,
            "status": self.status,
            "status_text": self.status_text,
            "time_ms": self.time_ms,
            "domain": self.domain,
            "path": self.path,
            "response_size": self.response_size,
            "is_failed": self.is_failed,
            "request_headers": self.request_headers,
            "response_headers": self.response_headers,
            "response_body": self.response_body,
        }


@dataclass
class DomainStats:
    """Statistics for a single domain."""

    domain: str
    request_count: int = 0
    failed_count: int = 0
    total_time_ms: float = 0
    total_size: int = 0

    @property
    def avg_time_ms(self) -> float:
        """Average request time in milliseconds."""
        if self.request_count == 0:
            return 0
        return self.total_time_ms / self.request_count


@dataclass
class HARSummary:
    """Summary of a HAR file for debugging."""

    # File info
    har_path: str | None = None
    har_size_bytes: int = 0

    # Overall statistics
    total_requests: int = 0
    total_time_ms: float = 0
    total_transfer_bytes: int = 0

    # Counts
    failed_count: int = 0
    slow_count: int = 0

    # Detailed lists
    failed_requests: list[HARRequest] = field(default_factory=list)
    slow_requests: list[HARRequest] = field(default_factory=list)

    # Domain breakdown
    domain_stats: dict[str, DomainStats] = field(default_factory=dict)

    # Browser/page info
    browser_name: str = ""
    browser_version: str = ""
    page_title: str = ""

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "har_path": self.har_path,
            "har_size_bytes": self.har_size_bytes,
            "total_requests": self.total_requests,
            "total_time_ms": self.total_time_ms,
            "total_transfer_bytes": self.total_transfer_bytes,
            "failed_count": self.failed_count,
            "slow_count": self.slow_count,
            "failed_requests": [r.to_dict() for r in self.failed_requests],
            "slow_requests": [r.to_dict() for r in self.slow_requests],
            "domain_stats": {
                domain: {
                    "request_count": stats.request_count,
                    "failed_count": stats.failed_count,
                    "avg_time_ms": stats.avg_time_ms,
                    "total_size": stats.total_size,
                }
                for domain, stats in self.domain_stats.items()
            },
            "browser_name": self.browser_name,
            "browser_version": self.browser_version,
            "page_title": self.page_title,
        }


class HARParser:
    """
    Parser for HAR (HTTP Archive) files.

    Extracts debugging-relevant information without loading entire response bodies
    into memory (except for failed requests).
    """

    def __init__(
        self,
        slow_threshold_ms: float = 5000,
        max_body_size: int = 2048,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ):
        """
        Initialize the HAR parser.

        Args:
            slow_threshold_ms: Threshold for slow requests (default: 5000ms)
            max_body_size: Max response body size to include (default: 2048 bytes)
            include_domains: Only include requests to these domains (None = all)
            exclude_domains: Exclude requests to these domains
        """
        self.slow_threshold_ms = slow_threshold_ms
        self.max_body_size = max_body_size
        self.include_domains = include_domains
        self.exclude_domains = exclude_domains or []

    def parse_file(self, har_path: Path) -> HARSummary:
        """
        Parse a HAR file and return a summary.

        Args:
            har_path: Path to the HAR file

        Returns:
            HARSummary with extracted information
        """
        summary = HARSummary(
            har_path=str(har_path),
            har_size_bytes=har_path.stat().st_size if har_path.exists() else 0,
        )

        if not har_path.exists():
            return summary

        try:
            with open(har_path, encoding="utf-8", errors="replace") as f:
                har_data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            summary.page_title = f"Error parsing HAR: {e}"
            return summary

        self._parse_har_data(har_data, summary)
        return summary

    def parse_string(self, har_content: str) -> HARSummary:
        """
        Parse HAR content from a string.

        Args:
            har_content: HAR JSON content as string

        Returns:
            HARSummary with extracted information
        """
        summary = HARSummary()

        try:
            har_data = json.loads(har_content)
        except json.JSONDecodeError as e:
            summary.page_title = f"Error parsing HAR: {e}"
            return summary

        self._parse_har_data(har_data, summary)
        return summary

    def _parse_har_data(self, har_data: dict, summary: HARSummary) -> None:
        """Parse HAR data structure into summary."""
        log = har_data.get("log", {})

        # Browser info
        browser = log.get("browser", {})
        summary.browser_name = browser.get("name", "")
        summary.browser_version = browser.get("version", "")

        # Page info
        pages = log.get("pages", [])
        if pages:
            summary.page_title = pages[0].get("title", "")

        # Process entries
        entries = log.get("entries", [])
        for entry in entries:
            request = self._parse_entry(entry)
            if request is None:
                continue

            # Check domain filters
            if self.include_domains and request.domain not in self.include_domains:
                continue
            if request.domain in self.exclude_domains:
                continue

            # Update summary
            summary.total_requests += 1
            summary.total_time_ms += request.time_ms
            summary.total_transfer_bytes += request.response_size

            # Track domain stats
            if request.domain not in summary.domain_stats:
                summary.domain_stats[request.domain] = DomainStats(domain=request.domain)

            domain_stat = summary.domain_stats[request.domain]
            domain_stat.request_count += 1
            domain_stat.total_time_ms += request.time_ms
            domain_stat.total_size += request.response_size

            # Track failed requests
            if request.is_failed:
                summary.failed_count += 1
                domain_stat.failed_count += 1
                summary.failed_requests.append(request)

            # Track slow requests (but not already failed ones)
            elif request.time_ms > self.slow_threshold_ms:
                summary.slow_count += 1
                summary.slow_requests.append(request)

        # Sort failed and slow by time descending
        summary.failed_requests.sort(key=lambda r: r.time_ms, reverse=True)
        summary.slow_requests.sort(key=lambda r: r.time_ms, reverse=True)

        # Sort domain stats by request count
        summary.domain_stats = dict(
            sorted(
                summary.domain_stats.items(),
                key=lambda x: x[1].request_count,
                reverse=True,
            )
        )

    def _parse_entry(self, entry: dict) -> HARRequest | None:
        """Parse a single HAR entry into a HARRequest."""
        try:
            request_data = entry.get("request", {})
            response_data = entry.get("response", {})

            # Basic info
            url = request_data.get("url", "")
            method = request_data.get("method", "GET")
            status = response_data.get("status", 0)
            status_text = response_data.get("statusText", "")
            time_ms = entry.get("time", 0)

            # Timestamp
            started = None
            started_str = entry.get("startedDateTime")
            if started_str:
                try:
                    # Handle various ISO formats
                    started_str = started_str.replace("Z", "+00:00")
                    started = datetime.fromisoformat(started_str)
                except ValueError:
                    pass

            # Request headers
            request_headers = {}
            for header in request_data.get("headers", []):
                name = header.get("name", "").lower()
                if name in ("content-type", "authorization", "user-agent", "accept"):
                    request_headers[name] = header.get("value", "")

            # Response headers
            response_headers = {}
            for header in response_data.get("headers", []):
                name = header.get("name", "").lower()
                if name in ("content-type", "content-length", "server", "x-request-id"):
                    response_headers[name] = header.get("value", "")

            # Response size
            content = response_data.get("content", {})
            response_size = content.get("size", 0)

            # Response body (only for failed requests, truncated)
            response_body = None
            if status >= 400:
                body_text = content.get("text", "")
                if body_text:
                    if len(body_text) > self.max_body_size:
                        response_body = body_text[: self.max_body_size] + "... [truncated]"
                    else:
                        response_body = body_text

            return HARRequest(
                url=url,
                method=method,
                status=status,
                status_text=status_text,
                time_ms=time_ms,
                started=started,
                request_headers=request_headers,
                response_headers=response_headers,
                response_body=response_body,
                response_size=response_size,
            )

        except Exception:
            return None


def format_bytes(size: float) -> str:
    """Format byte size to human-readable string."""
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_ms(ms: float) -> str:
    """Format milliseconds to human-readable string."""
    if ms < 1000:
        return f"{ms:.0f}ms"
    elif ms < 60000:
        return f"{ms / 1000:.1f}s"
    else:
        return f"{ms / 60000:.1f}m"
