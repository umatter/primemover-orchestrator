"""
Data models for experiment summaries.

These dataclasses provide a unified structure for summarizing experiments
from any source: YAML specs, JSON specs, or API experiments.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class DataSource(Enum):
    """Origin of experiment data."""

    YAML_SPEC = "yaml_spec"
    JSON_SPEC = "json_spec"
    API_RESPONSE = "api_response"
    LOCK = "lock"


@dataclass
class NumericStats:
    """Statistics for a numeric field."""

    count: int = 0
    min_value: float = 0.0
    max_value: float = 0.0
    mean: float = 0.0
    std: float = 0.0
    median: float = 0.0
    values: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary, excluding raw values for JSON output."""
        return {
            "count": self.count,
            "min": self.min_value,
            "max": self.max_value,
            "mean": round(self.mean, 4),
            "std": round(self.std, 4),
            "median": round(self.median, 4),
        }


@dataclass
class FactorSummary:
    """Summary of a design factor."""

    name: str
    levels: list[str] = field(default_factory=list)
    level_count: int = 0
    has_proportions: bool = False
    proportions: dict[str, float] = field(default_factory=dict)
    maps_to: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "levels": self.levels,
            "level_count": self.level_count,
            "has_proportions": self.has_proportions,
            "proportions": self.proportions,
            "maps_to": self.maps_to,
        }


@dataclass
class ParameterSummary:
    """Summary of a parameter distribution."""

    name: str
    distribution_type: str  # uniform, discrete, normal, gumbel, fixed
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "distribution_type": self.distribution_type,
            "config": self.config,
        }


@dataclass
class ExecutionStats:
    """Runtime execution statistics (only available from API)."""

    total_queues: int = 0
    total_queues_processed: int = 0
    total_queues_pending: int = 0
    total_queues_processing: int = 0
    total_queues_reviewed: int = 0
    completion_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_queues": self.total_queues,
            "processed": self.total_queues_processed,
            "pending": self.total_queues_pending,
            "processing": self.total_queues_processing,
            "reviewed": self.total_queues_reviewed,
            "completion_rate": round(self.completion_rate, 4),
        }


@dataclass
class StatisticalSummary:
    """Aggregated statistics computed from experiment data."""

    crawler_count: int = 0
    active_crawler_count: int = 0
    testing_crawler_count: int = 0

    # Parameter distributions
    pi_stats: NumericStats | None = None
    alpha_stats: NumericStats | None = None
    tau_stats: NumericStats | None = None
    kappa_distribution: dict[Any, int] = field(default_factory=dict)

    # Infrastructure distribution
    location_distribution: dict[str, int] = field(default_factory=dict)
    browser_distribution: dict[str, int] = field(default_factory=dict)

    # Factor level distribution
    factor_level_counts: dict[str, dict[str, int]] = field(default_factory=dict)

    # Job statistics
    jobs_per_crawler_mean: float = 0.0
    queues_per_crawler_mean: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        result = {
            "crawler_count": self.crawler_count,
            "active_crawler_count": self.active_crawler_count,
            "testing_crawler_count": self.testing_crawler_count,
            "location_distribution": self.location_distribution,
            "browser_distribution": self.browser_distribution,
            "factor_level_counts": self.factor_level_counts,
            "jobs_per_crawler_mean": round(self.jobs_per_crawler_mean, 2),
            "queues_per_crawler_mean": round(self.queues_per_crawler_mean, 2),
        }

        if self.pi_stats:
            result["pi_stats"] = self.pi_stats.to_dict()
        if self.alpha_stats:
            result["alpha_stats"] = self.alpha_stats.to_dict()
        if self.tau_stats:
            result["tau_stats"] = self.tau_stats.to_dict()
        if self.kappa_distribution:
            result["kappa_distribution"] = self.kappa_distribution

        return result


@dataclass
class CrawlerSummary:
    """Summary of a single crawler."""

    name: str = ""
    description: str = ""

    # Runtime IDs (API only)
    crawler_id: int | None = None

    # Design context
    from_design: bool = False
    cell_index: int | None = None
    replica: int | None = None
    factor_values: dict[str, str] = field(default_factory=dict)
    sampled_parameters: dict[str, Any] = field(default_factory=dict)

    # Configuration
    params: dict[str, Any] = field(default_factory=dict)
    location: str = ""
    browser: str = ""

    # Flags
    active: bool = True
    testing: bool = False

    # Job counts
    queue_count: int = 0
    total_jobs: int = 0

    def to_dict(self) -> dict[str, Any]:
        result = {
            "name": self.name,
            "description": self.description,
            "from_design": self.from_design,
            "params": self.params,
            "location": self.location,
            "browser": self.browser,
            "active": self.active,
            "testing": self.testing,
            "queue_count": self.queue_count,
            "total_jobs": self.total_jobs,
        }

        if self.crawler_id is not None:
            result["crawler_id"] = self.crawler_id
        if self.factor_values:
            result["factor_values"] = self.factor_values
        if self.sampled_parameters:
            result["sampled_parameters"] = self.sampled_parameters
        if self.cell_index is not None:
            result["cell_index"] = self.cell_index
        if self.replica is not None:
            result["replica"] = self.replica

        return result


@dataclass
class ExperimentSummary:
    """Unified summary from any data source."""

    # Source info
    source: DataSource = DataSource.YAML_SPEC
    source_file: str | None = None
    source_timestamp: datetime | None = None
    source_commit: str | None = None  # lock provenance.git_commit; unused by other sources

    # Experiment identity (DESIGN_TIME)
    name: str = ""
    description: str = ""
    version: str | None = None

    # Runtime identity (API only)
    experiment_id: int | None = None
    user_id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # Design info (DESIGN_TIME)
    has_design: bool = False
    design_type: str | None = None  # factorial, stratified
    design_seed: int | None = None
    design_replications: int = 1
    design_factors: list[FactorSummary] = field(default_factory=list)
    design_parameters: dict[str, ParameterSummary] = field(default_factory=dict)
    design_formula: str = ""

    # Defaults
    defaults: dict[str, Any] = field(default_factory=dict)

    # Templates & Pools (DESIGN_TIME)
    template_names: list[str] = field(default_factory=list)
    data_pool_names: list[str] = field(default_factory=list)

    # Crawler summary
    crawler_count: int = 0
    crawlers: list[CrawlerSummary] = field(default_factory=list)

    # Job statistics (COMPUTED)
    total_jobs: int = 0
    total_queues: int = 0
    jobs_by_type: dict[str, int] = field(default_factory=dict)
    estimated_duration_seconds: int = 0

    # Statistical summary (COMPUTED)
    statistics: StatisticalSummary | None = None

    # Execution stats (API only)
    execution_stats: ExecutionStats | None = None

    # Validation
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    is_valid: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: dict[str, Any] = {
            "metadata": {
                "name": self.name,
                "description": self.description,
                "version": self.version,
                "source_type": self.source.value,
                "source_file": self.source_file,
                "source_timestamp": (
                    self.source_timestamp.isoformat() if self.source_timestamp else None
                ),
            },
            "design": {
                "has_design": self.has_design,
                "type": self.design_type,
                "seed": self.design_seed,
                "replications": self.design_replications,
                "formula": self.design_formula,
                "factors": [f.to_dict() for f in self.design_factors],
                "parameters": {k: v.to_dict() for k, v in self.design_parameters.items()},
            },
            "configuration": {
                "defaults": self.defaults,
                "template_names": self.template_names,
                "data_pool_names": self.data_pool_names,
            },
            "summary": {
                "crawler_count": self.crawler_count,
                "total_jobs": self.total_jobs,
                "total_queues": self.total_queues,
                "jobs_by_type": self.jobs_by_type,
                "estimated_duration_seconds": self.estimated_duration_seconds,
                "estimated_duration_human": self._format_duration(self.estimated_duration_seconds),
            },
            "validation": {
                "is_valid": self.is_valid,
                "errors": self.errors,
                "warnings": self.warnings,
            },
        }

        # Add runtime identity for API sources
        if self.experiment_id is not None:
            result["metadata"]["experiment_id"] = self.experiment_id
            result["metadata"]["user_id"] = self.user_id
            result["metadata"]["created_at"] = (
                self.created_at.isoformat() if self.created_at else None
            )
            result["metadata"]["updated_at"] = (
                self.updated_at.isoformat() if self.updated_at else None
            )

        # Add statistics if computed
        if self.statistics:
            result["statistics"] = self.statistics.to_dict()

        # Add execution stats for API sources
        if self.execution_stats:
            result["execution_stats"] = self.execution_stats.to_dict()

        # Add crawler details (limited to first 20 for brevity)
        result["crawlers"] = [c.to_dict() for c in self.crawlers[:20]]
        if len(self.crawlers) > 20:
            result["crawlers_truncated"] = True
            result["total_crawlers_in_list"] = len(self.crawlers)

        return result

    def to_json(self, pretty: bool = True) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=2 if pretty else None)

    def to_text(self) -> str:
        """Generate human-readable text summary."""
        lines = [
            f"{'=' * 60}",
            f"Experiment Summary: {self.name}",
            f"{'=' * 60}",
            f"Description: {self.description or '(none)'}",
        ]

        if self.source == DataSource.LOCK:
            lines.append(
                f"Source: lock (seed {self.design_seed}, commit {self.source_commit or 'unknown'})"
            )
        else:
            lines.append(
                f"Source: {self.source.value}"
                + (f" (ID: {self.experiment_id})" if self.experiment_id else "")
            )

        if self.source_file:
            lines.append(f"File: {self.source_file}")

        lines.extend(
            [
                "",
                "DESIGN OVERVIEW",
                "-" * 40,
            ]
        )

        if self.has_design:
            lines.append(f"  Type: {self.design_type}")
            if self.design_formula:
                lines.append(f"  Formula: {self.design_formula}")
            lines.append(f"  Replications: {self.design_replications}")
            if self.design_seed is not None:
                lines.append(f"  Seed: {self.design_seed}")
            lines.append(f"  Total Crawlers: {self.crawler_count}")

            if self.design_factors:
                lines.append("")
                lines.append("  Factors:")
                for factor in self.design_factors:
                    levels_str = ", ".join(factor.levels)
                    lines.append(f"    - {factor.name}: {factor.level_count} levels [{levels_str}]")
                    if factor.has_proportions:
                        props = ", ".join(f"{k}={v:.0%}" for k, v in factor.proportions.items())
                        lines.append(f"      Proportions: {props}")

            if self.design_parameters:
                lines.append("")
                lines.append("  Parameters:")
                for name, param in self.design_parameters.items():
                    if param.distribution_type == "fixed":
                        lines.append(f"    - {name}: {param.config.get('value', 'N/A')} (fixed)")
                    elif param.distribution_type == "uniform":
                        lines.append(
                            f"    - {name}: uniform({param.config.get('min')}, {param.config.get('max')})"
                        )
                    elif param.distribution_type == "discrete":
                        vals = ", ".join(
                            f"{k}={v:.0%}" for k, v in param.config.get("values", {}).items()
                        )
                        lines.append(f"    - {name}: discrete({vals})")
                    elif param.distribution_type == "normal":
                        lines.append(
                            f"    - {name}: normal(μ={param.config.get('mean')}, σ={param.config.get('std')})"
                        )
                    elif param.distribution_type == "gumbel":
                        lines.append(
                            f"    - {name}: gumbel(μ={param.config.get('mu')}, β={param.config.get('beta')})"
                        )
                    else:
                        lines.append(f"    - {name}: {param.distribution_type}")
        else:
            lines.append(f"  No factorial design (static crawlers: {self.crawler_count})")

        lines.extend(
            [
                "",
                "WORKFLOW SUMMARY",
                "-" * 40,
                f"  Total Jobs: {self.total_jobs}",
                f"  Total Queues: {self.total_queues}",
                f"  Estimated Duration: {self._format_duration(self.estimated_duration_seconds)}",
                "",
                "  Job Types:",
            ]
        )

        for job_type, count in sorted(self.jobs_by_type.items(), key=lambda x: -x[1]):
            pct = (count / self.total_jobs * 100) if self.total_jobs > 0 else 0
            lines.append(f"    {job_type}: {count} ({pct:.1f}%)")

        if self.template_names:
            lines.extend(
                [
                    "",
                    f"  Templates Used: {', '.join(self.template_names)}",
                ]
            )

        if self.data_pool_names:
            lines.append(f"  Data Pools: {', '.join(self.data_pool_names)}")

        # Statistics
        if self.statistics:
            lines.extend(
                [
                    "",
                    "STATISTICS",
                    "-" * 40,
                    f"  Crawlers: {self.statistics.crawler_count} total, {self.statistics.active_crawler_count} active, {self.statistics.testing_crawler_count} testing",
                ]
            )

            if self.statistics.pi_stats and self.statistics.pi_stats.count > 0:
                pi = self.statistics.pi_stats
                lines.append(
                    f"  Pi (π): mean={pi.mean:.3f}, std={pi.std:.3f}, range=[{pi.min_value:.3f}, {pi.max_value:.3f}]"
                )

            if self.statistics.alpha_stats and self.statistics.alpha_stats.count > 0:
                alpha = self.statistics.alpha_stats
                lines.append(
                    f"  Alpha (α): mean={alpha.mean:.3f}, std={alpha.std:.3f}, range=[{alpha.min_value:.3f}, {alpha.max_value:.3f}]"
                )

            if self.statistics.browser_distribution:
                browsers = ", ".join(
                    f"{k}={v}" for k, v in self.statistics.browser_distribution.items()
                )
                lines.append(f"  Browsers: {browsers}")

            if self.statistics.location_distribution:
                locs = list(self.statistics.location_distribution.items())
                if len(locs) <= 3:
                    locs_str = ", ".join(f"{k}={v}" for k, v in locs)
                else:
                    locs_str = f"{len(locs)} unique locations"
                lines.append(f"  Locations: {locs_str}")

        # Execution stats (API only)
        if self.execution_stats:
            lines.extend(
                [
                    "",
                    "EXECUTION STATUS",
                    "-" * 40,
                    f"  Queues: {self.execution_stats.total_queues_processed}/{self.execution_stats.total_queues} processed ({self.execution_stats.completion_rate:.1%})",
                    f"  Pending: {self.execution_stats.total_queues_pending}, Processing: {self.execution_stats.total_queues_processing}",
                ]
            )

        # Validation
        if not self.is_valid or self.errors or self.warnings:
            lines.extend(
                [
                    "",
                    "VALIDATION",
                    "-" * 40,
                ]
            )
            if self.errors:
                for error in self.errors:
                    lines.append(f"  ERROR: {error}")
            if self.warnings:
                for warning in self.warnings:
                    lines.append(f"  WARNING: {warning}")

        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _format_duration(seconds: int) -> str:
        """Format seconds into human-readable duration."""
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            mins = seconds // 60
            secs = seconds % 60
            return f"{mins}m {secs}s"
        else:
            hours = seconds // 3600
            mins = (seconds % 3600) // 60
            return f"{hours}h {mins}m"


@dataclass
class ExperimentComparison:
    """Result of comparing two experiments."""

    summary_a: ExperimentSummary
    summary_b: ExperimentSummary

    # High-level differences
    crawler_count_diff: int = 0
    job_count_diff: int = 0
    queue_count_diff: int = 0

    # Detailed differences
    differences: list[dict[str, Any]] = field(default_factory=list)

    # Crawlers only in one or the other
    crawlers_only_in_a: list[str] = field(default_factory=list)
    crawlers_only_in_b: list[str] = field(default_factory=list)
    crawlers_in_both: list[str] = field(default_factory=list)

    def is_identical(self) -> bool:
        """Check if experiments are identical."""
        return len(self.differences) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "identical": self.is_identical(),
            "summary": {
                "crawler_count_diff": self.crawler_count_diff,
                "job_count_diff": self.job_count_diff,
                "queue_count_diff": self.queue_count_diff,
            },
            "differences": self.differences,
            "crawlers": {
                "only_in_a": self.crawlers_only_in_a,
                "only_in_b": self.crawlers_only_in_b,
                "in_both": len(self.crawlers_in_both),
            },
            "experiment_a": self.summary_a.to_dict(),
            "experiment_b": self.summary_b.to_dict(),
        }

    def to_json(self, pretty: bool = True) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=2 if pretty else None)

    def to_text(self) -> str:
        """Generate human-readable comparison text."""
        lines = [
            f"{'=' * 60}",
            "Experiment Comparison",
            f"{'=' * 60}",
            "",
            f"Experiment A: {self.summary_a.name}",
            f"Experiment B: {self.summary_b.name}",
            "",
            "SUMMARY",
            "-" * 40,
        ]

        if self.is_identical():
            lines.append("  Experiments are IDENTICAL")
        else:
            lines.append(f"  Crawler count difference: {self.crawler_count_diff:+d}")
            lines.append(f"  Job count difference: {self.job_count_diff:+d}")
            lines.append(f"  Queue count difference: {self.queue_count_diff:+d}")

            if self.crawlers_only_in_a:
                lines.append(f"  Crawlers only in A: {len(self.crawlers_only_in_a)}")
            if self.crawlers_only_in_b:
                lines.append(f"  Crawlers only in B: {len(self.crawlers_only_in_b)}")

        if self.differences:
            lines.extend(
                [
                    "",
                    "DIFFERENCES",
                    "-" * 40,
                ]
            )
            for diff in self.differences[:20]:  # Limit to first 20
                lines.append(f"  [{diff.get('category', 'unknown')}] {diff.get('field', '')}")
                lines.append(f"    A: {diff.get('value_a', 'N/A')}")
                lines.append(f"    B: {diff.get('value_b', 'N/A')}")

            if len(self.differences) > 20:
                lines.append(f"  ... and {len(self.differences) - 20} more differences")

        lines.append("")
        return "\n".join(lines)
