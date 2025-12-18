"""
Tests for orchestrator clients (Loki, CloudWatch).

Run with: pytest orchestrator/tests/test_clients.py -v
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.clients.loki import LogEntry, LogLevel


class TestLogLevel:
    """Tests for LogLevel enum."""

    def test_debug_level(self):
        """Test DEBUG level value."""
        assert LogLevel.DEBUG.value == "debug"

    def test_info_level(self):
        """Test INFO level value."""
        assert LogLevel.INFO.value == "info"

    def test_warning_level(self):
        """Test WARNING level value."""
        assert LogLevel.WARNING.value == "warning"

    def test_error_level(self):
        """Test ERROR level value."""
        assert LogLevel.ERROR.value == "error"

    def test_from_string_lowercase(self):
        """Test parsing lowercase level."""
        assert LogLevel.from_string("debug") == LogLevel.DEBUG
        assert LogLevel.from_string("info") == LogLevel.INFO
        assert LogLevel.from_string("error") == LogLevel.ERROR

    def test_from_string_uppercase(self):
        """Test parsing uppercase level."""
        assert LogLevel.from_string("DEBUG") == LogLevel.DEBUG
        assert LogLevel.from_string("INFO") == LogLevel.INFO
        assert LogLevel.from_string("ERROR") == LogLevel.ERROR

    def test_from_string_warn_variants(self):
        """Test parsing warn/warning variants."""
        assert LogLevel.from_string("warn") == LogLevel.WARNING
        assert LogLevel.from_string("warning") == LogLevel.WARNING
        assert LogLevel.from_string("WARN") == LogLevel.WARNING
        assert LogLevel.from_string("WARNING") == LogLevel.WARNING

    def test_from_string_unknown_defaults_to_info(self):
        """Test unknown level defaults to INFO."""
        assert LogLevel.from_string("unknown") == LogLevel.INFO
        assert LogLevel.from_string("") == LogLevel.INFO

    def test_from_string_with_whitespace(self):
        """Test parsing level with whitespace."""
        assert LogLevel.from_string("  debug  ") == LogLevel.DEBUG
        assert LogLevel.from_string("\terror\n") == LogLevel.ERROR


class TestLogEntry:
    """Tests for LogEntry dataclass."""

    def test_basic_entry(self):
        """Test creating a basic log entry."""
        entry = LogEntry(
            timestamp=datetime(2025, 1, 15, 10, 30, 45, tzinfo=timezone.utc),
            component="api",
            level="info",
            message="Application started",
        )

        assert entry.timestamp.year == 2025
        assert entry.component == "api"
        assert entry.level == "info"
        assert entry.message == "Application started"

    def test_entry_with_labels(self):
        """Test entry with labels."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            component="runner",
            level="error",
            message="Job failed",
            labels={"environment": "production", "host": "server1"},
        )

        assert entry.labels["environment"] == "production"
        assert entry.labels["host"] == "server1"

    def test_extract_experiment_id_from_labels(self):
        """Test extracting experiment_id from labels."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            component="api",
            level="info",
            message="Processing",
            labels={"experiment_id": "42"},
        )

        assert entry.experiment_id == 42

    def test_extract_crawler_id_from_labels(self):
        """Test extracting crawler_id from labels."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            component="runner",
            level="info",
            message="Running",
            labels={"crawler_id": "123"},
        )

        assert entry.crawler_id == 123

    def test_extract_job_id_from_labels(self):
        """Test extracting job_id from labels."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            component="runner",
            level="info",
            message="Executing",
            labels={"job_id": "456"},
        )

        assert entry.job_id == 456

    def test_extract_ids_from_message_equals_format(self):
        """Test extracting IDs from message with key=value format."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            component="api",
            level="info",
            message="Processing experiment_id=15 crawler_id=42 job_id=789",
        )

        assert entry.experiment_id == 15
        assert entry.crawler_id == 42
        assert entry.job_id == 789

    def test_extract_ids_from_message_colon_format(self):
        """Test extracting IDs from message with key: value format."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            component="api",
            level="info",
            message="Processing experiment_id: 15 crawler_id: 42",
        )

        assert entry.experiment_id == 15
        assert entry.crawler_id == 42

    def test_extract_from_correlation_id_label(self):
        """Test extracting IDs from correlation_id label."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            component="runner",
            level="info",
            message="Processing",
            labels={"correlation_id": "exp-15|cr-42|q-123|j-456"},
        )

        assert entry.experiment_id == 15
        assert entry.crawler_id == 42
        assert entry.queue_id == 123
        assert entry.job_id == 456

    def test_extract_from_correlation_id_in_message(self):
        """Test extracting IDs from correlation_id in message."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            component="runner",
            level="info",
            message="Processing correlationId=exp-5|cr-10|j-20",
        )

        assert entry.experiment_id == 5
        assert entry.crawler_id == 10
        assert entry.job_id == 20

    def test_extract_queue_id_jl_format(self):
        """Test extracting queue_id with jl- prefix."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            component="runner",
            level="info",
            message="Processing",
            labels={"correlation_id": "exp-5|cr-10|jl-20"},
        )

        assert entry.queue_id == 20

    def test_labels_override_message_extraction(self):
        """Test that explicit labels take precedence."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            component="api",
            level="info",
            message="experiment_id=999",  # Different value in message
            labels={"experiment_id": "42"},  # This should be used
        )

        assert entry.experiment_id == 42

    def test_invalid_id_in_labels(self):
        """Test handling invalid ID values in labels."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            component="api",
            level="info",
            message="Processing",
            labels={"experiment_id": "not_a_number"},
        )

        assert entry.experiment_id is None

    def test_default_values(self):
        """Test default field values."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            component="api",
            level="info",
            message="Test",
        )

        assert entry.labels == {}
        assert entry.raw == ""
        assert entry.experiment_id is None
        assert entry.crawler_id is None
        assert entry.queue_id is None
        assert entry.job_id is None


class TestLogEntryEdgeCases:
    """Edge case tests for LogEntry."""

    def test_empty_message(self):
        """Test entry with empty message."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            component="api",
            level="info",
            message="",
        )

        assert entry.message == ""
        assert entry.experiment_id is None

    def test_special_characters_in_message(self):
        """Test entry with special characters."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            component="api",
            level="error",
            message="Error: <script>alert('xss')</script> job_id=123",
        )

        assert entry.job_id == 123

    def test_multiple_ids_same_type(self):
        """Test entry with multiple IDs of same type (first wins)."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            component="api",
            level="info",
            message="experiment_id=1 experiment_id=2",
        )

        # First occurrence should be extracted
        assert entry.experiment_id == 1

    def test_partial_correlation_id(self):
        """Test entry with partial correlation ID."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            component="runner",
            level="info",
            message="Processing",
            labels={"correlation_id": "cr-42|j-456"},  # No experiment
        )

        assert entry.experiment_id is None
        assert entry.crawler_id == 42
        assert entry.job_id == 456

    def test_case_insensitive_id_extraction(self):
        """Test that ID extraction is case insensitive."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            component="api",
            level="info",
            message="EXPERIMENT_ID=42 JOB_ID=123",
        )

        assert entry.experiment_id == 42
        assert entry.job_id == 123
