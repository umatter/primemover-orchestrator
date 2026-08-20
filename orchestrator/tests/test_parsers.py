"""
Tests for log parsers.

Run with: python -m pytest orchestrator/tests/test_parsers.py -v
"""

import unittest
from datetime import datetime

from orchestrator.logs.parsers.laravel import (
    LaravelLogEntry,
    LaravelLogParser,
    parse_laravel_log,
)
from orchestrator.logs.parsers.runner import (
    RunnerLogEntry,
    RunnerLogParser,
    parse_runner_log,
)


class TestLaravelLogParser(unittest.TestCase):
    """Tests for Laravel log parser."""

    def setUp(self):
        self.parser = LaravelLogParser()

    def test_parse_simple_line(self):
        """Test parsing a simple log line."""
        line = "[2025-01-15 10:30:45] local.INFO: Application started"

        # First call returns None (buffering)
        result = self.parser.parse_line(line)
        self.assertIsNone(result)

        # Trigger flush with another line
        result = self.parser.parse_line("[2025-01-15 10:30:46] local.DEBUG: Test")

        self.assertIsNotNone(result)
        self.assertEqual(result.level, "INFO")
        self.assertEqual(result.environment, "local")
        self.assertEqual(result.message, "Application started")

    def test_parse_line_with_code(self):
        """Test parsing a log line with numeric code."""
        line = "[2025-01-15 10:30:45] production.ERROR: 1649254282: Database connection failed"

        self.parser.parse_line(line)
        result = self.parser._flush_buffer()

        self.assertIsNotNone(result)
        self.assertEqual(result.level, "ERROR")
        self.assertEqual(result.code, "1649254282")
        self.assertIn("Database connection failed", result.message)

    def test_parse_line_with_context(self):
        """Test parsing a log line with JSON context."""
        line = (
            '[2025-01-15 10:30:45] local.INFO: Job processed {"experiment_id": 15, "job_id": 456}'
        )

        self.parser.parse_line(line)
        result = self.parser._flush_buffer()

        self.assertIsNotNone(result)
        self.assertEqual(result.context.get("experiment_id"), 15)
        self.assertEqual(result.context.get("job_id"), 456)

    def test_parse_text_multiple_lines(self):
        """Test parsing multiple log lines."""
        text = """[2025-01-15 10:30:45] local.INFO: First message
[2025-01-15 10:30:46] local.DEBUG: Second message
[2025-01-15 10:30:47] local.ERROR: Third message"""

        entries = self.parser.parse_text(text)

        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0].level, "INFO")
        self.assertEqual(entries[1].level, "DEBUG")
        self.assertEqual(entries[2].level, "ERROR")

    def test_parse_multiline_entry(self):
        """Test parsing a multiline log entry (e.g., stack trace)."""
        text = """[2025-01-15 10:30:45] local.ERROR: Exception occurred
#0 /app/src/Controller.php(42): handleRequest()
#1 /app/src/Router.php(15): dispatch()
[2025-01-15 10:30:46] local.INFO: Next entry"""

        entries = self.parser.parse_text(text)

        self.assertEqual(len(entries), 2)
        self.assertIn("#0 /app/src/Controller.php", entries[0].message)
        self.assertIn("#1 /app/src/Router.php", entries[0].message)

    def test_convert_to_log_entry(self):
        """Test converting LaravelLogEntry to generic LogEntry."""
        entry = LaravelLogEntry(
            timestamp=datetime(2025, 1, 15, 10, 30, 45),
            environment="production",
            level="ERROR",
            code="123",
            message="Test error experiment_id=15 job_id=456",  # IDs in message for extraction
            context={"experiment_id": 15, "job_id": 456},
        )

        log_entry = entry.to_log_entry()

        self.assertEqual(log_entry.component, "api")
        self.assertEqual(log_entry.level, "error")
        self.assertIn("Test error", log_entry.message)
        # Note: LogEntry extracts IDs from message/labels in __post_init__
        self.assertEqual(log_entry.experiment_id, 15)
        self.assertEqual(log_entry.job_id, 456)


class TestParseLaravelLog(unittest.TestCase):
    """Tests for parse_laravel_log helper function."""

    def test_parse_and_convert(self):
        """Test parsing and converting to LogEntry."""
        content = """[2025-01-15 10:30:45] local.INFO: Processing job_id=789 started {"job_id": 789}
[2025-01-15 10:30:46] local.ERROR: Job failed"""

        entries = parse_laravel_log(content)

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].component, "api")
        self.assertEqual(entries[0].level, "info")
        # LogEntry extracts IDs from message
        self.assertEqual(entries[0].job_id, 789)
        self.assertEqual(entries[1].level, "error")


class TestRunnerLogParser(unittest.TestCase):
    """Tests for Runner (Spring Boot) log parser."""

    def setUp(self):
        self.parser = RunnerLogParser()

    def test_parse_simple_line(self):
        """Test parsing a simple Spring Boot log line."""
        line = "2025-01-15T10:30:45.123+00:00  INFO 12345 --- [primemover-runner] [main] o.s.boot.SpringApplication  : Application started"

        # First call returns None (buffering)
        result = self.parser.parse_line(line)
        self.assertIsNone(result)

        # Trigger flush
        result = self.parser._flush_buffer()

        self.assertIsNotNone(result)
        self.assertEqual(result.level, "INFO")
        self.assertEqual(result.pid, 12345)
        self.assertEqual(result.application, "primemover-runner")
        self.assertEqual(result.thread, "main")
        self.assertIn("Application started", result.message)

    def test_parse_line_with_crawler_id(self):
        """Test parsing a log line containing crawler ID."""
        line = "2025-01-15T10:30:45.123+00:00  DEBUG 12345 --- [primemover-runner] [scheduling-1] c.p.r.Runner  : Processing crawlerId=42"

        self.parser.parse_line(line)
        result = self.parser._flush_buffer()

        self.assertIsNotNone(result)
        self.assertEqual(result.crawler_id, 42)

    def test_parse_line_with_job_id(self):
        """Test parsing a log line containing job ID."""
        line = "2025-01-15T10:30:45.123+00:00  INFO 12345 --- [primemover-runner] [pool-1] c.p.r.JobRunner  : Executing job.id=789"

        self.parser.parse_line(line)
        result = self.parser._flush_buffer()

        self.assertIsNotNone(result)
        self.assertEqual(result.job_id, 789)

    def test_parse_line_with_correlation_id(self):
        """Test parsing a log line containing correlation ID."""
        line = "2025-01-15T10:30:45.123+00:00  INFO 12345 --- [primemover-runner] [pool-1] c.p.r.Runner  : Processing correlationId=exp-5|cr-10|j-20"

        self.parser.parse_line(line)
        result = self.parser._flush_buffer()

        self.assertIsNotNone(result)
        self.assertEqual(result.correlation_id, "exp-5|cr-10|j-20")

    def test_parse_text_multiple_lines(self):
        """Test parsing multiple log lines."""
        text = """2025-01-15T10:30:45.123+00:00  INFO 12345 --- [primemover-runner] [main] o.s.boot.SpringApplication  : Starting application
2025-01-15T10:30:46.456+00:00  DEBUG 12345 --- [primemover-runner] [main] o.s.web.RestTemplate  : Connecting to API
2025-01-15T10:30:47.789+00:00  ERROR 12345 --- [primemover-runner] [pool-1] c.p.r.JobRunner  : Job execution failed"""

        entries = self.parser.parse_text(text)

        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0].level, "INFO")
        self.assertEqual(entries[1].level, "DEBUG")
        self.assertEqual(entries[2].level, "ERROR")

    def test_parse_multiline_stacktrace(self):
        """Test parsing a multiline stack trace."""
        text = """2025-01-15T10:30:45.123+00:00  ERROR 12345 --- [primemover-runner] [pool-1] c.p.r.JobRunner  : Exception occurred
java.lang.NullPointerException: Cannot invoke method
\tat com.primemover.runner.JobRunner.execute(JobRunner.java:42)
\tat com.primemover.runner.Runner.run(Runner.java:15)
2025-01-15T10:30:46.456+00:00  INFO 12345 --- [primemover-runner] [main] o.s.boot.SpringApplication  : Next entry"""

        entries = self.parser.parse_text(text)

        self.assertEqual(len(entries), 2)
        self.assertIn("java.lang.NullPointerException", entries[0].message)
        self.assertIn("JobRunner.execute", entries[0].message)

    def test_convert_to_log_entry(self):
        """Test converting RunnerLogEntry to generic LogEntry."""
        entry = RunnerLogEntry(
            timestamp=datetime(2025, 1, 15, 10, 30, 45),
            level="ERROR",
            pid=12345,
            application="primemover-runner",
            thread="pool-1",
            logger="c.p.r.JobRunner",
            message="Job failed crawler_id=42 job_id=789",  # IDs in message for extraction
            crawler_id=42,
            job_id=789,
        )

        log_entry = entry.to_log_entry()

        self.assertEqual(log_entry.component, "runner")
        self.assertEqual(log_entry.level, "error")
        self.assertIn("Job failed", log_entry.message)
        # Note: LogEntry extracts IDs from message/labels in __post_init__
        self.assertEqual(log_entry.crawler_id, 42)
        self.assertEqual(log_entry.job_id, 789)
        self.assertEqual(log_entry.labels["thread"], "pool-1")


class TestParseRunnerLog(unittest.TestCase):
    """Tests for parse_runner_log helper function."""

    def test_parse_and_convert(self):
        """Test parsing and converting to LogEntry."""
        content = """2025-01-15T10:30:45.123+00:00  INFO 12345 --- [primemover-runner] [main] c.p.r.Runner  : Processing crawler_id=42 job_id=789
2025-01-15T10:30:46.456+00:00  ERROR 12345 --- [primemover-runner] [pool-1] c.p.r.JobRunner  : Job failed"""

        entries = parse_runner_log(content)

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].component, "runner")
        # LogEntry extracts IDs from message using pattern key=value
        self.assertEqual(entries[0].crawler_id, 42)
        self.assertEqual(entries[0].job_id, 789)
        self.assertEqual(entries[1].level, "error")


class TestLogLevel(unittest.TestCase):
    """Tests for log level parsing."""

    def test_levels_are_normalized(self):
        """Test that log levels are normalized to lowercase."""
        laravel_content = "[2025-01-15 10:30:45] local.WARNING: Test warning"
        entries = parse_laravel_log(laravel_content)
        self.assertEqual(entries[0].level, "warning")

        runner_content = "2025-01-15T10:30:45.123+00:00  WARN 12345 --- [primemover-runner] [main] c.p.r.Runner  : Test warn"
        entries = parse_runner_log(runner_content)
        self.assertEqual(entries[0].level, "warn")


if __name__ == "__main__":
    unittest.main()
