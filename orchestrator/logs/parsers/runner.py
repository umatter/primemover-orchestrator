"""
Java/Logback runner log parser.

Parses Spring Boot Logback log format:
2025-12-02T10:25:21.079+01:00  DEBUG 514253 --- [primemover-runner] [scheduling-1] o.s.web.client.RestTemplate  : Message
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from orchestrator.clients.loki import LogEntry


@dataclass
class RunnerLogEntry:
    """Parsed runner log entry."""

    timestamp: datetime
    level: str
    pid: int
    application: str
    thread: str
    logger: str
    message: str

    # MDC context (if present)
    crawler_id: Optional[int] = None
    queue_id: Optional[int] = None
    job_id: Optional[int] = None
    correlation_id: Optional[str] = None

    def to_log_entry(self) -> LogEntry:
        """Convert to generic LogEntry."""
        return LogEntry(
            timestamp=self.timestamp,
            component="runner",
            level=self.level.lower(),
            message=self.message,
            labels={
                "thread": self.thread,
                "logger": self.logger,
                "pid": str(self.pid),
            },
            raw=str(self),
            crawler_id=self.crawler_id,
            queue_id=self.queue_id,
            job_id=self.job_id,
        )


class RunnerLogParser:
    """Parser for Java/Logback Spring Boot log format."""

    # Main pattern for Spring Boot logs
    # 2025-12-02T10:25:21.079+01:00  DEBUG 514253 --- [app] [thread] logger  : Message
    PATTERN = re.compile(
        r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3})[^\s]*\s+"
        r"(?P<level>\w+)\s+"
        r"(?P<pid>\d+)\s+---\s+"
        r"\[(?P<app>[^\]]+)\]\s+"
        r"\[(?P<thread>[^\]]+)\]\s+"
        r"(?P<logger>[^\s]+)\s+:\s+"
        r"(?P<message>.*)$"
    )

    # Pattern for MDC context in message
    MDC_PATTERN = re.compile(
        r"\{(?P<mdc>[^}]+)\}"
    )

    # Patterns for extracting IDs from MDC or message
    CRAWLER_ID_PATTERN = re.compile(r"crawler\.?[iI]d[=:]?\s*(\d+)")
    QUEUE_ID_PATTERN = re.compile(r"(?:queue|joblist)\.?[iI]d[=:]?\s*(\d+)")
    JOB_ID_PATTERN = re.compile(r"job\.?[iI]d[=:]?\s*(\d+)")
    CORRELATION_PATTERN = re.compile(r"correlationId[=:]?\s*([^\s,]+)")

    def __init__(self):
        self._buffer: list[str] = []
        self._current_entry: Optional[RunnerLogEntry] = None

    def parse_line(self, line: str) -> Optional[RunnerLogEntry]:
        """
        Parse a single log line.

        Returns parsed entry when complete, or None if buffering multiline.
        """
        line = line.rstrip()

        # Try to match main log pattern
        match = self.PATTERN.match(line)

        if match:
            # If we have a buffered entry, return it first
            result = self._flush_buffer()

            # Start new entry
            groups = match.groupdict()

            # Parse timestamp
            try:
                # Parse ISO format with milliseconds
                ts_str = groups["timestamp"]
                timestamp = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S.%f")
            except ValueError:
                timestamp = datetime.now()

            message = groups["message"]

            # Extract MDC context from message
            crawler_id = self._extract_id(self.CRAWLER_ID_PATTERN, message)
            queue_id = self._extract_id(self.QUEUE_ID_PATTERN, message)
            job_id = self._extract_id(self.JOB_ID_PATTERN, message)

            # Extract correlation ID
            corr_match = self.CORRELATION_PATTERN.search(message)
            correlation_id = corr_match.group(1) if corr_match else None

            self._current_entry = RunnerLogEntry(
                timestamp=timestamp,
                level=groups["level"],
                pid=int(groups["pid"]),
                application=groups["app"],
                thread=groups["thread"],
                logger=groups["logger"],
                message=message,
                crawler_id=crawler_id,
                queue_id=queue_id,
                job_id=job_id,
                correlation_id=correlation_id,
            )

            return result

        elif self._current_entry:
            # Continuation of multiline entry (e.g., stack trace)
            self._buffer.append(line)

        return None

    def _extract_id(self, pattern: re.Pattern, text: str) -> Optional[int]:
        """Extract an ID using a regex pattern."""
        match = pattern.search(text)
        if match:
            try:
                return int(match.group(1))
            except (ValueError, IndexError):
                pass
        return None

    def _flush_buffer(self) -> Optional[RunnerLogEntry]:
        """Flush buffered multiline content to current entry."""
        if not self._current_entry:
            self._buffer.clear()
            return None

        # Append buffered lines to message
        if self._buffer:
            self._current_entry.message += "\n" + "\n".join(self._buffer)
            self._buffer.clear()

        entry = self._current_entry
        self._current_entry = None
        return entry

    def parse_file(self, filepath: str) -> list[RunnerLogEntry]:
        """Parse an entire log file."""
        entries = []

        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                entry = self.parse_line(line)
                if entry:
                    entries.append(entry)

        # Don't forget the last entry
        final = self._flush_buffer()
        if final:
            entries.append(final)

        return entries

    def parse_text(self, text: str) -> list[RunnerLogEntry]:
        """Parse log text content."""
        entries = []

        for line in text.split("\n"):
            entry = self.parse_line(line)
            if entry:
                entries.append(entry)

        final = self._flush_buffer()
        if final:
            entries.append(final)

        return entries


def parse_runner_log(content: str) -> list[LogEntry]:
    """
    Parse runner log content and return generic LogEntry objects.

    Args:
        content: Raw log file content

    Returns:
        List of LogEntry objects
    """
    parser = RunnerLogParser()
    entries = parser.parse_text(content)
    return [e.to_log_entry() for e in entries]
