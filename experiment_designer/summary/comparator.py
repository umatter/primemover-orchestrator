"""
Experiment comparison logic.

Compares two ExperimentSummary instances and identifies differences.
"""

from typing import Any

from .models import ExperimentComparison, ExperimentSummary


def compare_summaries(
    summary_a: ExperimentSummary, summary_b: ExperimentSummary
) -> ExperimentComparison:
    """Compare two experiment summaries and return detailed differences."""
    comparison = ExperimentComparison(summary_a=summary_a, summary_b=summary_b)

    # High-level differences
    comparison.crawler_count_diff = summary_b.crawler_count - summary_a.crawler_count
    comparison.job_count_diff = summary_b.total_jobs - summary_a.total_jobs
    comparison.queue_count_diff = summary_b.total_queues - summary_a.total_queues

    differences: list[dict[str, Any]] = []

    # Compare metadata
    if summary_a.name != summary_b.name:
        differences.append(
            {
                "category": "metadata",
                "field": "name",
                "value_a": summary_a.name,
                "value_b": summary_b.name,
            }
        )

    if summary_a.description != summary_b.description:
        differences.append(
            {
                "category": "metadata",
                "field": "description",
                "value_a": summary_a.description[:100] if summary_a.description else None,
                "value_b": summary_b.description[:100] if summary_b.description else None,
            }
        )

    # Compare counts
    if summary_a.crawler_count != summary_b.crawler_count:
        differences.append(
            {
                "category": "summary",
                "field": "crawler_count",
                "value_a": summary_a.crawler_count,
                "value_b": summary_b.crawler_count,
            }
        )

    if summary_a.total_jobs != summary_b.total_jobs:
        differences.append(
            {
                "category": "summary",
                "field": "total_jobs",
                "value_a": summary_a.total_jobs,
                "value_b": summary_b.total_jobs,
            }
        )

    if summary_a.total_queues != summary_b.total_queues:
        differences.append(
            {
                "category": "summary",
                "field": "total_queues",
                "value_a": summary_a.total_queues,
                "value_b": summary_b.total_queues,
            }
        )

    # Compare design
    if summary_a.has_design != summary_b.has_design:
        differences.append(
            {
                "category": "design",
                "field": "has_design",
                "value_a": summary_a.has_design,
                "value_b": summary_b.has_design,
            }
        )

    if summary_a.design_type != summary_b.design_type:
        differences.append(
            {
                "category": "design",
                "field": "design_type",
                "value_a": summary_a.design_type,
                "value_b": summary_b.design_type,
            }
        )

    if summary_a.design_replications != summary_b.design_replications:
        differences.append(
            {
                "category": "design",
                "field": "replications",
                "value_a": summary_a.design_replications,
                "value_b": summary_b.design_replications,
            }
        )

    # Compare factors
    factors_a = {f.name for f in summary_a.design_factors}
    factors_b = {f.name for f in summary_b.design_factors}

    for factor in factors_a - factors_b:
        differences.append(
            {
                "category": "design",
                "field": f"factor.{factor}",
                "value_a": "present",
                "value_b": "missing",
            }
        )

    for factor in factors_b - factors_a:
        differences.append(
            {
                "category": "design",
                "field": f"factor.{factor}",
                "value_a": "missing",
                "value_b": "present",
            }
        )

    # Compare common factors
    for factor_name in factors_a & factors_b:
        factor_a = next(f for f in summary_a.design_factors if f.name == factor_name)
        factor_b = next(f for f in summary_b.design_factors if f.name == factor_name)

        if set(factor_a.levels) != set(factor_b.levels):
            differences.append(
                {
                    "category": "design",
                    "field": f"factor.{factor_name}.levels",
                    "value_a": factor_a.levels,
                    "value_b": factor_b.levels,
                }
            )

    # Compare job type distribution
    all_job_types = set(summary_a.jobs_by_type.keys()) | set(summary_b.jobs_by_type.keys())
    for job_type in all_job_types:
        count_a = summary_a.jobs_by_type.get(job_type, 0)
        count_b = summary_b.jobs_by_type.get(job_type, 0)
        if count_a != count_b:
            differences.append(
                {
                    "category": "workflow",
                    "field": f"jobs.{job_type}",
                    "value_a": count_a,
                    "value_b": count_b,
                }
            )

    # Compare defaults
    all_default_keys = set(summary_a.defaults.keys()) | set(summary_b.defaults.keys())
    for key in all_default_keys:
        val_a = summary_a.defaults.get(key)
        val_b = summary_b.defaults.get(key)
        if val_a != val_b:
            differences.append(
                {
                    "category": "configuration",
                    "field": f"defaults.{key}",
                    "value_a": val_a,
                    "value_b": val_b,
                }
            )

    # Compare crawlers by name
    crawler_names_a = {c.name for c in summary_a.crawlers}
    crawler_names_b = {c.name for c in summary_b.crawlers}

    comparison.crawlers_only_in_a = sorted(crawler_names_a - crawler_names_b)
    comparison.crawlers_only_in_b = sorted(crawler_names_b - crawler_names_a)
    comparison.crawlers_in_both = sorted(crawler_names_a & crawler_names_b)

    # Note differences in crawler sets
    if comparison.crawlers_only_in_a:
        differences.append(
            {
                "category": "crawlers",
                "field": "crawlers_only_in_a",
                "value_a": len(comparison.crawlers_only_in_a),
                "value_b": 0,
            }
        )

    if comparison.crawlers_only_in_b:
        differences.append(
            {
                "category": "crawlers",
                "field": "crawlers_only_in_b",
                "value_a": 0,
                "value_b": len(comparison.crawlers_only_in_b),
            }
        )

    # Compare statistics if both have them
    if summary_a.statistics and summary_b.statistics:
        stats_a = summary_a.statistics
        stats_b = summary_b.statistics

        # Compare browser distributions
        all_browsers = set(stats_a.browser_distribution.keys()) | set(
            stats_b.browser_distribution.keys()
        )
        for browser in all_browsers:
            count_a = stats_a.browser_distribution.get(browser, 0)
            count_b = stats_b.browser_distribution.get(browser, 0)
            if count_a != count_b:
                differences.append(
                    {
                        "category": "statistics",
                        "field": f"browser.{browser}",
                        "value_a": count_a,
                        "value_b": count_b,
                    }
                )

        # Compare location distributions (summarized)
        if stats_a.location_distribution != stats_b.location_distribution:
            differences.append(
                {
                    "category": "statistics",
                    "field": "location_distribution",
                    "value_a": len(stats_a.location_distribution),
                    "value_b": len(stats_b.location_distribution),
                }
            )

        # Compare pi distributions
        if stats_a.pi_stats and stats_b.pi_stats:
            if abs(stats_a.pi_stats.mean - stats_b.pi_stats.mean) > 0.01:
                differences.append(
                    {
                        "category": "statistics",
                        "field": "pi.mean",
                        "value_a": round(stats_a.pi_stats.mean, 4),
                        "value_b": round(stats_b.pi_stats.mean, 4),
                    }
                )

    comparison.differences = differences
    return comparison
