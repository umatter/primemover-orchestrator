"""
Tests for the correlation ID module.

Run with: python -m pytest orchestrator/tests/test_correlation.py -v
"""

import unittest

from orchestrator.tracing.correlation import (
    CorrelationId,
    build_correlation_id,
    extract_correlation_from_labels,
    extract_correlation_from_message,
    parse_correlation_id,
)


class TestCorrelationId(unittest.TestCase):
    """Tests for CorrelationId dataclass."""

    def test_str_full_id(self):
        """Test string representation with all components."""
        cid = CorrelationId(
            experiment_id=15,
            crawler_id=42,
            queue_id=123,
            job_id=456,
        )
        self.assertEqual(str(cid), "exp-15|cr-42|q-123|j-456")

    def test_str_partial_id(self):
        """Test string representation with partial components."""
        cid = CorrelationId(experiment_id=15, job_id=456)
        self.assertEqual(str(cid), "exp-15|j-456")

    def test_str_empty_id(self):
        """Test string representation with no components."""
        cid = CorrelationId()
        self.assertEqual(str(cid), "")

    def test_to_dict(self):
        """Test conversion to dictionary."""
        cid = CorrelationId(experiment_id=15, crawler_id=42)
        result = cid.to_dict()
        self.assertEqual(result["experiment_id"], 15)
        self.assertEqual(result["crawler_id"], 42)
        self.assertIsNone(result["queue_id"])
        self.assertIsNone(result["job_id"])

    def test_is_empty_true(self):
        """Test is_empty returns True for empty ID."""
        cid = CorrelationId()
        self.assertTrue(cid.is_empty)

    def test_is_empty_false(self):
        """Test is_empty returns False when ID has components."""
        cid = CorrelationId(job_id=1)
        self.assertFalse(cid.is_empty)

    def test_matches_exact(self):
        """Test matches with exact match."""
        cid1 = CorrelationId(experiment_id=15, crawler_id=42)
        cid2 = CorrelationId(experiment_id=15, crawler_id=42)
        self.assertTrue(cid1.matches(cid2))

    def test_matches_subset(self):
        """Test matches with subset."""
        cid1 = CorrelationId(experiment_id=15, crawler_id=42, job_id=456)
        cid2 = CorrelationId(experiment_id=15)  # Only experiment specified
        self.assertTrue(cid1.matches(cid2))

    def test_matches_fails(self):
        """Test matches returns False for non-matching IDs."""
        cid1 = CorrelationId(experiment_id=15, crawler_id=42)
        cid2 = CorrelationId(experiment_id=15, crawler_id=99)
        self.assertFalse(cid1.matches(cid2))


class TestParseCorrelationId(unittest.TestCase):
    """Tests for parse_correlation_id function."""

    def test_parse_full_id(self):
        """Test parsing a full correlation ID."""
        cid = parse_correlation_id("exp-15|cr-42|q-123|j-456")
        self.assertEqual(cid.experiment_id, 15)
        self.assertEqual(cid.crawler_id, 42)
        self.assertEqual(cid.queue_id, 123)
        self.assertEqual(cid.job_id, 456)

    def test_parse_partial_id(self):
        """Test parsing a partial correlation ID."""
        cid = parse_correlation_id("cr-42|j-456")
        self.assertIsNone(cid.experiment_id)
        self.assertEqual(cid.crawler_id, 42)
        self.assertIsNone(cid.queue_id)
        self.assertEqual(cid.job_id, 456)

    def test_parse_single_component(self):
        """Test parsing a single component."""
        cid = parse_correlation_id("j-789")
        self.assertIsNone(cid.experiment_id)
        self.assertIsNone(cid.crawler_id)
        self.assertIsNone(cid.queue_id)
        self.assertEqual(cid.job_id, 789)

    def test_parse_empty_string(self):
        """Test parsing an empty string."""
        cid = parse_correlation_id("")
        self.assertTrue(cid.is_empty)

    def test_parse_joblist_format(self):
        """Test parsing queue ID with jl- prefix (joblist)."""
        cid = parse_correlation_id("exp-5|cr-10|jl-20")
        self.assertEqual(cid.experiment_id, 5)
        self.assertEqual(cid.crawler_id, 10)
        self.assertEqual(cid.queue_id, 20)

    def test_parse_embedded_in_text(self):
        """Test parsing correlation ID embedded in text."""
        cid = parse_correlation_id("Processing exp-5|cr-10|j-20 started")
        self.assertEqual(cid.experiment_id, 5)
        self.assertEqual(cid.crawler_id, 10)
        self.assertEqual(cid.job_id, 20)


class TestBuildCorrelationId(unittest.TestCase):
    """Tests for build_correlation_id function."""

    def test_build_full_id(self):
        """Test building a full correlation ID."""
        result = build_correlation_id(
            experiment_id=15,
            crawler_id=42,
            queue_id=123,
            job_id=456,
        )
        self.assertEqual(result, "exp-15|cr-42|q-123|j-456")

    def test_build_partial_id(self):
        """Test building a partial correlation ID."""
        result = build_correlation_id(experiment_id=15, job_id=456)
        self.assertEqual(result, "exp-15|j-456")

    def test_build_empty_id(self):
        """Test building with no components."""
        result = build_correlation_id()
        self.assertEqual(result, "")


class TestExtractCorrelationFromMessage(unittest.TestCase):
    """Tests for extract_correlation_from_message function."""

    def test_extract_full_correlation_id(self):
        """Test extracting a full correlation ID from message."""
        message = "Processing job exp-5|cr-10|q-15|j-20 completed"
        cid = extract_correlation_from_message(message)
        self.assertEqual(cid.experiment_id, 5)
        self.assertEqual(cid.crawler_id, 10)
        self.assertEqual(cid.queue_id, 15)
        self.assertEqual(cid.job_id, 20)

    def test_extract_individual_ids(self):
        """Test extracting individual IDs from message."""
        message = "experimentId=15, crawlerId=42, jobId=456"
        cid = extract_correlation_from_message(message)
        self.assertEqual(cid.experiment_id, 15)
        self.assertEqual(cid.crawler_id, 42)
        self.assertEqual(cid.job_id, 456)

    def test_extract_underscore_format(self):
        """Test extracting IDs with underscore format."""
        message = "experiment_id: 15, crawler_id: 42"
        cid = extract_correlation_from_message(message)
        self.assertEqual(cid.experiment_id, 15)
        self.assertEqual(cid.crawler_id, 42)

    def test_extract_mixed_formats(self):
        """Test extracting IDs with mixed formats."""
        message = "Job started: job_id=789 for queue id 123"
        cid = extract_correlation_from_message(message)
        self.assertEqual(cid.job_id, 789)
        self.assertEqual(cid.queue_id, 123)

    def test_extract_no_ids(self):
        """Test extracting from message with no IDs."""
        message = "Application started successfully"
        cid = extract_correlation_from_message(message)
        self.assertTrue(cid.is_empty)


class TestExtractCorrelationFromLabels(unittest.TestCase):
    """Tests for extract_correlation_from_labels function."""

    def test_extract_from_correlation_id_label(self):
        """Test extracting from correlation_id label."""
        labels = {"correlation_id": "exp-15|cr-42|j-456"}
        cid = extract_correlation_from_labels(labels)
        self.assertEqual(cid.experiment_id, 15)
        self.assertEqual(cid.crawler_id, 42)
        self.assertEqual(cid.job_id, 456)

    def test_extract_from_individual_labels(self):
        """Test extracting from individual ID labels."""
        labels = {
            "experiment_id": "15",
            "crawler_id": "42",
            "job_id": "456",
        }
        cid = extract_correlation_from_labels(labels)
        self.assertEqual(cid.experiment_id, 15)
        self.assertEqual(cid.crawler_id, 42)
        self.assertEqual(cid.job_id, 456)

    def test_extract_from_camel_case_labels(self):
        """Test extracting from camelCase labels."""
        labels = {
            "experimentId": "15",
            "crawlerId": "42",
        }
        cid = extract_correlation_from_labels(labels)
        self.assertEqual(cid.experiment_id, 15)
        self.assertEqual(cid.crawler_id, 42)

    def test_individual_labels_override_correlation_id(self):
        """Test that individual labels override correlation_id."""
        labels = {
            "correlation_id": "exp-15|cr-42",
            "crawler_id": "99",  # Should override
        }
        cid = extract_correlation_from_labels(labels)
        self.assertEqual(cid.experiment_id, 15)
        self.assertEqual(cid.crawler_id, 99)

    def test_extract_from_empty_labels(self):
        """Test extracting from empty labels."""
        cid = extract_correlation_from_labels({})
        self.assertTrue(cid.is_empty)


if __name__ == "__main__":
    unittest.main()
