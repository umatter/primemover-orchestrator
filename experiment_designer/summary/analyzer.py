"""
Statistical analysis for experiment summaries.

Computes aggregated statistics from crawler and job data.
"""

import statistics
from typing import Any

from .models import (
    CrawlerSummary,
    ExperimentSummary,
    NumericStats,
    StatisticalSummary,
)


class ExperimentAnalyzer:
    """Analyze experiment data and compute statistics."""

    def __init__(self, summary: ExperimentSummary):
        self.summary = summary

    def analyze(self) -> StatisticalSummary:
        """Compute all statistics for the experiment."""
        stats = StatisticalSummary()

        if not self.summary.crawlers:
            return stats

        crawlers = self.summary.crawlers

        # Basic counts
        stats.crawler_count = len(crawlers)
        stats.active_crawler_count = sum(1 for c in crawlers if c.active)
        stats.testing_crawler_count = sum(1 for c in crawlers if c.testing)

        # Parameter distributions
        stats.pi_stats = self._compute_param_stats(crawlers, "pi")
        stats.alpha_stats = self._compute_param_stats(crawlers, "alpha")
        stats.tau_stats = self._compute_param_stats(crawlers, "tau")
        stats.kappa_distribution = self._compute_discrete_distribution(crawlers, "kappa")

        # Infrastructure distributions
        stats.location_distribution = self._compute_string_distribution(
            crawlers, lambda c: c.location
        )
        stats.browser_distribution = self._compute_string_distribution(
            crawlers, lambda c: c.browser
        )

        # Factor level distributions
        stats.factor_level_counts = self._compute_factor_distributions(crawlers)

        # Job/queue averages
        total_jobs = sum(c.total_jobs for c in crawlers)
        total_queues = sum(c.queue_count for c in crawlers)

        if stats.crawler_count > 0:
            stats.jobs_per_crawler_mean = total_jobs / stats.crawler_count
            stats.queues_per_crawler_mean = total_queues / stats.crawler_count

        return stats

    def _compute_param_stats(
        self, crawlers: list[CrawlerSummary], param_name: str
    ) -> NumericStats | None:
        """Compute statistics for a numeric parameter."""
        values = []

        for crawler in crawlers:
            # Try sampled_parameters first, then params
            value = crawler.sampled_parameters.get(param_name)
            if value is None:
                value = crawler.params.get(param_name)

            if value is not None:
                try:
                    values.append(float(value))
                except (ValueError, TypeError):
                    pass

        if not values:
            return None

        return NumericStats(
            count=len(values),
            min_value=min(values),
            max_value=max(values),
            mean=statistics.mean(values),
            std=statistics.stdev(values) if len(values) > 1 else 0.0,
            median=statistics.median(values),
            values=values,
        )

    def _compute_discrete_distribution(
        self, crawlers: list[CrawlerSummary], param_name: str
    ) -> dict[Any, int]:
        """Compute distribution for a discrete parameter."""
        distribution: dict[Any, int] = {}

        for crawler in crawlers:
            value = crawler.sampled_parameters.get(param_name)
            if value is None:
                value = crawler.params.get(param_name)

            if value is not None:
                # Convert to string for consistent keys
                key = str(value)
                distribution[key] = distribution.get(key, 0) + 1

        return distribution

    def _compute_string_distribution(
        self, crawlers: list[CrawlerSummary], getter
    ) -> dict[str, int]:
        """Compute distribution for a string field."""
        distribution: dict[str, int] = {}

        for crawler in crawlers:
            value = getter(crawler)
            if value:
                distribution[value] = distribution.get(value, 0) + 1

        return distribution

    def _compute_factor_distributions(
        self, crawlers: list[CrawlerSummary]
    ) -> dict[str, dict[str, int]]:
        """Compute distributions for factor levels."""
        distributions: dict[str, dict[str, int]] = {}

        for crawler in crawlers:
            for factor_name, level in crawler.factor_values.items():
                if factor_name not in distributions:
                    distributions[factor_name] = {}
                distributions[factor_name][level] = distributions[factor_name].get(level, 0) + 1

        return distributions


def compute_job_type_distribution(jobs_data: list[dict]) -> dict[str, int]:
    """Compute job type distribution from job data."""
    distribution: dict[str, int] = {}

    for job in jobs_data:
        job_type = job.get("type", "unknown")
        distribution[job_type] = distribution.get(job_type, 0) + 1

    return distribution


def estimate_duration_from_jobs(jobs_data: list[dict]) -> int:
    """Estimate total duration in seconds from jobs."""
    total_seconds = 0

    for job in jobs_data:
        job_type = job.get("type", "")

        # Extract duration from waitjob and scrollbydurationjob
        if job_type in ("waitjob", "scrollbydurationjob"):
            for behavior in job.get("behaviors", []):
                if behavior.get("name") in ("seconds", "duration"):
                    try:
                        total_seconds += int(behavior.get("value", 0))
                    except (ValueError, TypeError):
                        pass

    return total_seconds
