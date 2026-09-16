"""
Summary Generator - Generate experiment summaries from various sources.

Supports:
- YAML specification files (ParsedSpec)
- JSON specification files (ConversionResult or raw JSON)
- API experiment data
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

if TYPE_CHECKING:
    from ..lock.model import Lock

from .analyzer import (
    ExperimentAnalyzer,
    compute_job_type_distribution,
    estimate_duration_from_jobs,
)
from .models import (
    CrawlerSummary,
    DataSource,
    ExperimentSummary,
    FactorSummary,
    ParameterSummary,
)


class SummaryGenerator:
    """Generate experiment summaries from various sources."""

    def __init__(self, seed: int | None = None):
        """
        Initialize the summary generator.

        Args:
            seed: Random seed for reproducible operations
        """
        self.seed = seed

    def from_yaml_file(self, file_path: str | Path, seed: int | None = None) -> ExperimentSummary:
        """Generate summary from a YAML specification file."""
        file_path = Path(file_path)

        with open(file_path) as f:
            yaml_content = f.read()

        summary = self.from_yaml_string(yaml_content, base_dir=file_path.parent, seed=seed)
        summary.source_file = str(file_path)
        return summary

    def from_yaml_string(
        self,
        yaml_content: str,
        base_dir: Path | None = None,
        seed: int | None = None,
    ) -> ExperimentSummary:
        """Generate summary from YAML content string."""
        from ..lock import build

        seed = seed if seed is not None else self.seed

        # A `runs_for:` spec is prepared exactly as compile_spec prepares it --
        # base lock loaded, `from_base:` expanded -- or a summary of one would
        # be an unnamed experiment with no crawlers, which is a report about
        # this function rather than about the spec.
        raw, raw_errors = build.load_spec_raw(
            yaml_content, base_dir=base_dir or Path.cwd(), seed=seed
        )
        base = None
        runs_errors: list[str] = list(raw_errors)
        if not raw_errors:
            raw, base, prep_errors = build.prepare_runs_spec(raw, base_dir=base_dir or Path.cwd())
            runs_errors.extend(prep_errors)
        is_runs = isinstance(raw, dict) and "runs_for" in raw
        if is_runs and base is None:
            is_runs = False  # nothing to prepare against; the errors say why

        # Parse and convert through the one seed helper compile_spec uses: an
        # explicit seed wins over the spec's own seed, the parser's pool stream
        # is seed + 1 and the converter's design stream is seed, so the
        # round-trip (spec -> lock -> summary == spec -> summary) draws the
        # same rows.
        parsed, result = build.parse_and_convert(
            yaml_content,
            base_dir=base_dir,
            seed=build.effective_seed(raw, is_runs, seed),
            # Summarising is not generating: times stay unresolved, exactly as
            # compile_spec leaves them, so a seeded `start_at: {from, jitter}`
            # is summarised rather than refused.
            resolve_times=False,
            raw=raw if is_runs else None,
            runs=is_runs,
        )

        # Build summary from parsed spec and conversion result
        experiment = base.lock.experiment if base is not None else parsed.experiment
        summary = ExperimentSummary(
            source=DataSource.YAML_SPEC,
            source_timestamp=datetime.now(),
            name=experiment.get("name") or "Unnamed Experiment",
            description=experiment.get("description", ""),
            version=parsed.version,
            defaults=parsed.defaults,
            template_names=list(parsed.templates.keys()),
            data_pool_names=list(parsed.data_pools.keys()),
            errors=runs_errors + list(parsed.errors) + list(result.errors),
            warnings=list(parsed.warnings) + list(result.warnings),
            is_valid=not runs_errors and not parsed.errors and not result.errors,
        )

        # Extract design information
        if parsed.design:
            summary.has_design = True
            summary.design_type = parsed.design.get("type", "factorial")
            summary.design_seed = parsed.design.get("seed")
            summary.design_replications = parsed.design.get("replications", 1)

            # Extract factors
            factors_data = parsed.design.get("factors", {})
            for factor_name, factor_def in factors_data.items():
                levels = factor_def.get("levels", [])
                if isinstance(levels, list):
                    if levels and isinstance(levels[0], dict):
                        # Proportioned levels
                        level_ids = [lvl.get("id", str(lvl)) for lvl in levels]
                        proportions = {
                            lvl.get("id", str(lvl)): lvl.get("proportion", 1.0 / len(levels))
                            for lvl in levels
                        }
                        has_proportions = any(lvl.get("proportion") for lvl in levels)
                    else:
                        # Simple levels
                        level_ids = [str(lvl) for lvl in levels]
                        proportions = {}
                        has_proportions = False
                else:
                    level_ids = [str(levels)]
                    proportions = {}
                    has_proportions = False

                maps_to = list(factor_def.get("maps_to", {}).keys())

                summary.design_factors.append(
                    FactorSummary(
                        name=factor_name,
                        levels=level_ids,
                        level_count=len(level_ids),
                        has_proportions=has_proportions,
                        proportions=proportions,
                        maps_to=maps_to,
                    )
                )

            # Extract parameter distributions
            params_data = parsed.design.get("parameters", {})
            for param_name, param_def in params_data.items():
                if isinstance(param_def, dict):
                    dist_type = param_def.get("distribution", "fixed")
                    if "value" in param_def:
                        dist_type = "fixed"
                    config = {k: v for k, v in param_def.items() if k != "distribution"}
                else:
                    dist_type = "fixed"
                    config = {"value": param_def}

                summary.design_parameters[param_name] = ParameterSummary(
                    name=param_name,
                    distribution_type=dist_type,
                    config=config,
                )

            # Build design formula
            formula_parts = []
            for factor in summary.design_factors:
                formula_parts.append(f"{factor.name}({factor.level_count})")
            if summary.design_replications > 1:
                formula_parts.append(f"×{summary.design_replications}")
            summary.design_formula = " × ".join(formula_parts)

        # Process crawlers from conversion result
        summary.crawler_count = result.crawler_count
        summary.total_jobs = result.total_jobs

        all_jobs = []
        for crawler_data in result.json_data:
            crawler_summary = self._extract_crawler_summary_from_json(crawler_data)
            summary.crawlers.append(crawler_summary)
            summary.total_queues += crawler_summary.queue_count

            # Collect jobs for analysis
            for queue in crawler_data.get("queues", []):
                all_jobs.extend(queue.get("jobs", []))

        # Compute job statistics
        summary.jobs_by_type = compute_job_type_distribution(all_jobs)
        summary.estimated_duration_seconds = estimate_duration_from_jobs(all_jobs)

        # Compute statistics
        analyzer = ExperimentAnalyzer(summary)
        summary.statistics = analyzer.analyze()

        return summary

    def from_json_file(self, file_path: str | Path) -> ExperimentSummary:
        """Generate summary from a JSON specification file."""
        file_path = Path(file_path)

        with open(file_path) as f:
            json_content = f.read()

        summary = self.from_json_string(json_content)
        summary.source_file = str(file_path)
        return summary

    def from_json_string(self, json_content: str) -> ExperimentSummary:
        """Generate summary from JSON content string."""
        data = json.loads(json_content)

        # Handle both array and single-object formats
        if isinstance(data, dict):
            crawlers_data = [data]
        else:
            crawlers_data = data

        summary = ExperimentSummary(
            source=DataSource.JSON_SPEC,
            source_timestamp=datetime.now(),
            name=crawlers_data[0].get("name", "JSON Experiment") if crawlers_data else "Empty",
            description=crawlers_data[0].get("description", "") if crawlers_data else "",
            is_valid=True,
        )

        summary.crawler_count = len(crawlers_data)

        all_jobs = []
        for crawler_data in crawlers_data:
            crawler_summary = self._extract_crawler_summary_from_json(crawler_data)
            summary.crawlers.append(crawler_summary)
            summary.total_queues += crawler_summary.queue_count
            summary.total_jobs += crawler_summary.total_jobs

            # Collect jobs
            for queue in crawler_data.get("queues", []):
                all_jobs.extend(queue.get("jobs", []))

        # Compute job statistics
        summary.jobs_by_type = compute_job_type_distribution(all_jobs)
        summary.estimated_duration_seconds = estimate_duration_from_jobs(all_jobs)

        # Compute statistics
        analyzer = ExperimentAnalyzer(summary)
        summary.statistics = analyzer.analyze()

        return summary

    def from_lock(self, lock: "Lock") -> ExperimentSummary:
        """Generate summary from an already-compiled lock.

        A lock is post-expansion: every crawler, queue and job already exists,
        every random draw already made. There is no design to report -- only
        its result -- so `has_design` is always False here and no design
        formula/factors/parameters are populated.
        """
        summary = ExperimentSummary(
            source=DataSource.LOCK,
            source_timestamp=datetime.now(),
            name=lock.experiment.get("name", "Unnamed Experiment"),
            description=lock.experiment.get("description", ""),
            has_design=False,
            design_seed=lock.provenance.seed,
            source_commit=lock.provenance.git_commit,
            is_valid=True,
        )

        summary.crawler_count = len(lock.crawlers)

        all_jobs = []
        for crawler_data in lock.crawlers:
            # Lock queues carry `start_offset` instead of `start_at`; the
            # extractor never reads either field, so this is harmless.
            crawler_summary = self._extract_crawler_summary_from_json(crawler_data)
            summary.crawlers.append(crawler_summary)
            summary.total_queues += crawler_summary.queue_count
            summary.total_jobs += crawler_summary.total_jobs

            for queue in crawler_data.get("queues", []):
                all_jobs.extend(queue.get("jobs", []))

        # Compute job statistics
        summary.jobs_by_type = compute_job_type_distribution(all_jobs)
        summary.estimated_duration_seconds = estimate_duration_from_jobs(all_jobs)

        # Compute statistics
        analyzer = ExperimentAnalyzer(summary)
        summary.statistics = analyzer.analyze()

        return summary

    def from_lock_text(self, text: str) -> ExperimentSummary:
        """Generate summary from a lock document's YAML text."""
        from ..lock.io import load_lock

        return self.from_lock(load_lock(text))

    def from_api(
        self,
        experiment_id: int,
        api_url: str,
        email: str,
        password: str,
    ) -> ExperimentSummary:
        """Generate summary from API experiment data."""
        # Authenticate
        auth_response = requests.post(
            f"{api_url}/login",
            json={"email": email, "password": password},
            headers={"Accept": "application/json"},
            timeout=30,
        )
        auth_response.raise_for_status()
        token = auth_response.json().get("access_token")

        # Fetch experiment
        exp_response = requests.get(
            f"{api_url}/experiments/{experiment_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=60,
        )
        exp_response.raise_for_status()
        experiment_data = exp_response.json()

        return self.from_api_data(experiment_data.get("data", experiment_data))

    def from_api_data(self, experiment_data: dict[str, Any]) -> ExperimentSummary:
        """Generate summary from API experiment response data."""
        from .models import ExecutionStats

        crawlers_data = experiment_data.get("crawlers", [])

        summary = ExperimentSummary(
            source=DataSource.API_RESPONSE,
            source_timestamp=datetime.now(),
            name=experiment_data.get("name", "API Experiment"),
            description=experiment_data.get("description", ""),
            experiment_id=experiment_data.get("id"),
            user_id=experiment_data.get("user_id"),
            is_valid=True,
        )

        # Parse timestamps
        if experiment_data.get("created_at"):
            try:
                summary.created_at = datetime.fromisoformat(
                    experiment_data["created_at"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        if experiment_data.get("updated_at"):
            try:
                summary.updated_at = datetime.fromisoformat(
                    experiment_data["updated_at"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        summary.crawler_count = len(crawlers_data)

        # Execution stats
        exec_stats = ExecutionStats()

        all_jobs = []
        for crawler_data in crawlers_data:
            crawler_summary = self._extract_crawler_summary_from_api(crawler_data)
            summary.crawlers.append(crawler_summary)
            summary.total_queues += crawler_summary.queue_count
            summary.total_jobs += crawler_summary.total_jobs

            # Collect jobs and execution stats
            for queue in crawler_data.get("queues", []):
                all_jobs.extend(queue.get("jobs", []))
                exec_stats.total_queues += 1
                if queue.get("processed"):
                    exec_stats.total_queues_processed += 1
                if queue.get("processing"):
                    exec_stats.total_queues_processing += 1
                if queue.get("reviewed"):
                    exec_stats.total_queues_reviewed += 1

        exec_stats.total_queues_pending = (
            exec_stats.total_queues
            - exec_stats.total_queues_processed
            - exec_stats.total_queues_processing
        )

        if exec_stats.total_queues > 0:
            exec_stats.completion_rate = exec_stats.total_queues_processed / exec_stats.total_queues

        summary.execution_stats = exec_stats

        # Compute job statistics
        summary.jobs_by_type = compute_job_type_distribution(all_jobs)
        summary.estimated_duration_seconds = estimate_duration_from_jobs(all_jobs)

        # Compute statistics
        analyzer = ExperimentAnalyzer(summary)
        summary.statistics = analyzer.analyze()

        return summary

    def _extract_crawler_summary_from_json(self, crawler_data: dict[str, Any]) -> CrawlerSummary:
        """Extract crawler summary from JSON crawler data."""
        summary = CrawlerSummary(
            name=crawler_data.get("name", ""),
            description=crawler_data.get("description", ""),
            active=crawler_data.get("active", True),
            testing=crawler_data.get("testing", False),
        )

        # Configuration params
        config = crawler_data.get("configuration", {})
        params = config.get("params", {})

        # Extract standard parameters
        for param_name in ["pi", "psi", "alpha", "tau", "kappa", "beta"]:
            if param_name in params:
                summary.params[param_name] = params[param_name]

        # Agent info
        agent = crawler_data.get("agent", {})
        summary.location = agent.get("location", "")
        summary.browser = agent.get("identification", "")

        # Queue/job counts
        queues = crawler_data.get("queues", [])
        summary.queue_count = len(queues)
        summary.total_jobs = sum(len(q.get("jobs", [])) for q in queues)

        return summary

    def _extract_crawler_summary_from_api(self, crawler_data: dict[str, Any]) -> CrawlerSummary:
        """Extract crawler summary from API crawler data."""
        summary = self._extract_crawler_summary_from_json(crawler_data)

        # Add API-specific fields
        summary.crawler_id = crawler_data.get("id")

        return summary
