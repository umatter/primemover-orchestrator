"""
Laravel log parser.

Parses Laravel/Monolog log format:
[2025-12-02 10:30:00] local.INFO: 1649254282: message {"context": "..."}
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from orchestrator.clients.loki import LogEntry


@dataclass
class LaravelLogEntry:
    """Parsed Laravel log entry."""

    timestamp: datetime
    environment: str
    level: str
    code: Optional[str]
    message: str
    context: dict

    def to_log_entry(self) -> LogEntry:
        """Convert to generic LogEntry."""
        return LogEntry(
            timestamp=self.timestamp,
            component="api",
            level=self.level.lower(),
            message=self.message,
            labels={
                "environment": self.environment,
                "code": self.code or "",
            },
            raw=str(self),
            experiment_id=self.context.get("experiment_id"),
            crawler_id=self.context.get("crawler_id"),
            queue_id=self.context.get("queue_id"),
            job_id=self.context.get("job_id"),
        )


class LaravelLogParser:
    """Parser for Laravel/Monolog log format."""

    # Main pattern for Laravel logs
    # [2025-12-02 10:30:00] local.INFO: 1649254282: message {"context": "..."}
    PATTERN = re.compile(
        r"^\[(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] "
        r"(?P<env>\w+)\.(?P<level>\w+): "
        r"(?:(?P<code>\d+): )?"
        r"(?P<message>.*?)(?:\s+(?P<context>\{.*\}))?$",
        re.DOTALL,
    )

    # Pattern for multiline stack traces
    STACK_TRACE_START = re.compile(r"^#\d+ ")
    EXCEPTION_PATTERN = re.compile(r"^\[object\] \((?P<class>[^)]+)\)")

    def __init__(self):
        self._buffer: list[str] = []
        self._current_entry: Optional[LaravelLogEntry] = None

    def parse_line(self, line: str) -> Optional[LaravelLogEntry]:
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
                timestamp = datetime.strptime(
                    groups["timestamp"], "%Y-%m-%d %H:%M:%S"
                )
            except ValueError:
                timestamp = datetime.now()

            # Parse context JSON
            context = {}
            if groups.get("context"):
                try:
                    context = json.loads(groups["context"])
                except json.JSONDecodeError:
                    pass

            self._current_entry = LaravelLogEntry(
                timestamp=timestamp,
                environment=groups["env"],
                level=groups["level"],
                code=groups.get("code"),
                message=groups["message"].strip(),
                context=context,
            )

            return result

        elif self._current_entry:
            # Continuation of multiline entry
            self._buffer.append(line)

        return None

    def _flush_buffer(self) -> Optional[LaravelLogEntry]:
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

    def parse_file(self, filepath: str) -> list[LaravelLogEntry]:
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

    def parse_text(self, text: str) -> list[LaravelLogEntry]:
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


def parse_laravel_log(content: str) -> list[LogEntry]:
    """
    Parse Laravel log content and return generic LogEntry objects.

    Args:
        content: Raw log file content

    Returns:
        List of LogEntry objects
    """
    parser = LaravelLogParser()
    entries = parser.parse_text(content)
    return [e.to_log_entry() for e in entries]
