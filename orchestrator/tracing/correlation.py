"""
Correlation ID utilities for cross-component tracing.

Correlation ID format: exp-{experiment_id}|cr-{crawler_id}|q-{queue_id}|j-{job_id}

Examples:
    exp-15|cr-42|q-123|j-456
    cr-42|q-123|j-456  (no experiment context)
    j-456  (job-only context)
"""

import re
from dataclasses import dataclass


@dataclass
class CorrelationId:
    """Parsed correlation ID with all context levels."""

    experiment_id: int | None = None
    crawler_id: int | None = None
    queue_id: int | None = None
    job_id: int | None = None

    def __str__(self) -> str:
        """Convert back to string format."""
        parts = []
        if self.experiment_id is not None:
            parts.append(f"exp-{self.experiment_id}")
        if self.crawler_id is not None:
            parts.append(f"cr-{self.crawler_id}")
        if self.queue_id is not None:
            parts.append(f"q-{self.queue_id}")
        if self.job_id is not None:
            parts.append(f"j-{self.job_id}")
        return "|".join(parts)

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "experiment_id": self.experiment_id,
            "crawler_id": self.crawler_id,
            "queue_id": self.queue_id,
            "job_id": self.job_id,
        }

    @property
    def is_empty(self) -> bool:
        """Check if correlation ID has no context."""
        return all(
            v is None for v in [self.experiment_id, self.crawler_id, self.queue_id, self.job_id]
        )

    def matches(self, other: "CorrelationId") -> bool:
        """Check if this correlation ID matches another (non-None fields must match)."""
        if other.experiment_id is not None and self.experiment_id != other.experiment_id:
            return False
        if other.crawler_id is not None and self.crawler_id != other.crawler_id:
            return False
        if other.queue_id is not None and self.queue_id != other.queue_id:
            return False
        if other.job_id is not None and self.job_id != other.job_id:
            return False
        return True


# Patterns for parsing correlation IDs
PATTERNS = {
    "experiment": re.compile(r"exp-(\d+)"),
    "crawler": re.compile(r"cr-(\d+)"),
    "queue": re.compile(r"(?:q|jl)-(\d+)"),  # q- or jl- (joblist)
    "job": re.compile(r"j-(\d+)"),
}

# Patterns for extracting IDs from log messages
MESSAGE_PATTERNS = {
    "experiment_id": re.compile(r"experiment[_\s]?[iI]d[=:\s]*(\d+)", re.IGNORECASE),
    "crawler_id": re.compile(r"crawler[_\s]?[iI]d[=:\s]*(\d+)", re.IGNORECASE),
    "queue_id": re.compile(r"(?:queue|joblist)[_\s]?[iI]d[=:\s]*(\d+)", re.IGNORECASE),
    "job_id": re.compile(r"job[_\s]?[iI]d[=:\s]*(\d+)", re.IGNORECASE),
}


def parse_correlation_id(value: str) -> CorrelationId:
    """
    Parse a correlation ID string into its components.

    Args:
        value: Correlation ID string (e.g., "exp-15|cr-42|q-123|j-456")

    Returns:
        CorrelationId with parsed components
    """
    result = CorrelationId()

    if not value:
        return result

    # Parse experiment ID
    match = PATTERNS["experiment"].search(value)
    if match:
        result.experiment_id = int(match.group(1))

    # Parse crawler ID
    match = PATTERNS["crawler"].search(value)
    if match:
        result.crawler_id = int(match.group(1))

    # Parse queue ID
    match = PATTERNS["queue"].search(value)
    if match:
        result.queue_id = int(match.group(1))

    # Parse job ID
    match = PATTERNS["job"].search(value)
    if match:
        result.job_id = int(match.group(1))

    return result


def build_correlation_id(
    experiment_id: int | None = None,
    crawler_id: int | None = None,
    queue_id: int | None = None,
    job_id: int | None = None,
) -> str:
    """
    Build a correlation ID string from components.

    Args:
        experiment_id: Experiment ID
        crawler_id: Crawler ID
        queue_id: Queue ID
        job_id: Job ID

    Returns:
        Correlation ID string
    """
    return str(
        CorrelationId(
            experiment_id=experiment_id,
            crawler_id=crawler_id,
            queue_id=queue_id,
            job_id=job_id,
        )
    )


def extract_correlation_from_message(message: str) -> CorrelationId:
    """
    Extract correlation IDs from a log message.

    Looks for patterns like:
        - experimentId=15, crawlerId=42
        - experiment_id: 15, crawler_id: 42
        - exp-15|cr-42|q-123|j-456

    Args:
        message: Log message text

    Returns:
        CorrelationId with extracted components
    """
    result = CorrelationId()

    # First try to find a full correlation ID string
    full_pattern = re.search(r"((?:exp-\d+\|)?(?:cr-\d+\|)?(?:q-\d+\|)?(?:j-\d+))", message)
    if full_pattern:
        return parse_correlation_id(full_pattern.group(1))

    # Otherwise extract individual IDs from message
    for field, pattern in MESSAGE_PATTERNS.items():
        match = pattern.search(message)
        if match:
            setattr(result, field, int(match.group(1)))

    return result


def extract_correlation_from_labels(labels: dict) -> CorrelationId:
    """
    Extract correlation IDs from log labels/metadata.

    Args:
        labels: Dictionary of labels

    Returns:
        CorrelationId with extracted components
    """
    result = CorrelationId()

    # Check for correlation_id label
    if "correlation_id" in labels:
        result = parse_correlation_id(labels["correlation_id"])
    elif "correlationId" in labels:
        result = parse_correlation_id(labels["correlationId"])

    # Override with explicit ID labels
    for field in ["experiment_id", "crawler_id", "queue_id", "job_id"]:
        if field in labels:
            try:
                setattr(result, field, int(labels[field]))
            except (ValueError, TypeError):
                pass

        # Also check camelCase versions
        camel = field.replace("_id", "Id").replace("_", "")
        if camel in labels:
            try:
                setattr(result, field, int(labels[camel]))
            except (ValueError, TypeError):
                pass

    return result
