"""
Data Pools - Load and resolve data pools for experiment specifications.

Supports:
- Loading data pools from external files (CSV, JSON, YAML)
- Pool references in crawler configurations
- Selection strategies: all, random, first, top (with scoring)
- Dynamic data selection based on crawler parameters

Example YAML:
    data_pools:
      outlets:
        source: ./data/outlets_pool.csv
      search_terms:
        source: ./data/searchterms_pool.csv

    crawlers:
      - configuration:
          params:
            media_outlet_urls:
              from_pool: outlets
              select: top
              count: 10
              score: "{alpha} - {tau} * pow(abs({crawler.pi} - {item.pi}), 2)"
"""

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .expression import ExpressionError, SafeExpressionEvaluator
from .file_loader import FileLoader, FileReference


@dataclass
class PoolDefinition:
    """
    Definition of a data pool.

    Attributes:
        name: Pool name (for reference)
        source: File path (relative to spec)
        format: File format (csv, json, yaml) - auto-detected if not specified
        columns: Expected columns (for documentation/validation)
    """

    name: str
    source: str
    format: str | None = None
    columns: list[str] | None = None


@dataclass
class PoolReference:
    """
    Reference to a data pool with selection criteria.

    Attributes:
        from_pool: Pool name to reference
        select: Selection strategy (all, random, first, top)
        count: Number of records to select
        score: Scoring expression for 'top' selection
        filter: Filter criteria
        columns: Column mapping for output
    """

    from_pool: str
    select: str = "all"
    count: int | None = None
    score: str | None = None
    filter: dict[str, Any] | None = None
    columns: dict[str, str] | None = None


class DataPoolManager:
    """
    Manage data pools for an experiment specification.

    Example usage:
        manager = DataPoolManager(base_dir=Path("/path/to/spec"), seed=42)

        # Load pool definitions from spec
        manager.load_pool("outlets", {"source": "./data/outlets.csv"})
        manager.load_pool("search_terms", {"source": "./data/terms.csv"})

        # Resolve a pool reference
        crawler_context = {"pi": -0.5, "alpha": 0.35, "tau": 1}
        pool_ref = {
            "from_pool": "outlets",
            "select": "top",
            "count": 10,
            "score": "{alpha} - {tau} * pow(abs({crawler.pi} - {item.pi}), 2)"
        }
        outlets = manager.resolve(pool_ref, crawler_context)
    """

    def __init__(self, base_dir: Path | None = None, seed: int | None = None):
        """
        Initialize pool manager.

        Args:
            base_dir: Base directory for resolving file paths
            seed: Random seed for reproducible selections
        """
        self.base_dir = base_dir or Path.cwd()
        self.seed = seed
        self.rng = random.Random(seed)
        self.file_loader = FileLoader(base_dir=self.base_dir, seed=seed)
        self.expression_evaluator = SafeExpressionEvaluator()

        self.pools: dict[str, list[dict[str, Any]]] = {}
        self.pool_definitions: dict[str, PoolDefinition] = {}
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def load_pools_from_spec(self, data_pools: dict[str, Any]) -> None:
        """
        Load all pool definitions from a spec's data_pools section.

        Args:
            data_pools: Dict from spec's data_pools section
        """
        for name, config in data_pools.items():
            self.load_pool(name, config)

    def load_pool(self, name: str, config: str | dict[str, Any]) -> bool:
        """
        Load a data pool from configuration.

        Args:
            name: Pool name
            config: Pool configuration (path string or dict with source, etc.)

        Returns:
            True if loaded successfully
        """
        # Handle string shorthand (just a path)
        if isinstance(config, str):
            config = {"source": config}

        source = config.get("source", "")
        if not source:
            self.errors.append(f"Pool '{name}' missing 'source' field")
            return False

        # Create definition
        definition = PoolDefinition(
            name=name, source=source, format=config.get("format"), columns=config.get("columns")
        )
        self.pool_definitions[name] = definition

        # Load data using file loader
        ref = FileReference(
            path=source, format=definition.format, columns=config.get("column_mapping")
        )
        data = self.file_loader.load(ref)

        # Propagate errors
        self.errors.extend(self.file_loader.errors)
        self.warnings.extend(self.file_loader.warnings)
        self.file_loader.errors = []
        self.file_loader.warnings = []

        if data:
            self.pools[name] = data
            return True
        return False

    def resolve(
        self, pool_ref: dict[str, Any] | PoolReference, crawler_context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        Resolve a pool reference to actual data.

        Args:
            pool_ref: Pool reference dict or PoolReference object
            crawler_context: Crawler parameters for scoring/filtering

        Returns:
            List of selected records
        """
        # Convert dict to PoolReference if needed
        if isinstance(pool_ref, dict):
            pool_ref = PoolReference(
                from_pool=pool_ref.get("from_pool", ""),
                select=pool_ref.get("select", "all"),
                count=pool_ref.get("count"),
                score=pool_ref.get("score"),
                filter=pool_ref.get("filter"),
                columns=pool_ref.get("columns"),
            )

        # Get pool data
        pool_name = pool_ref.from_pool
        if pool_name not in self.pools:
            self.errors.append(f"Unknown pool: {pool_name}")
            return []

        data = self.pools[pool_name].copy()

        # Apply filter if specified
        if pool_ref.filter:
            data = self._apply_filter(data, pool_ref.filter, crawler_context)

        # Apply selection strategy
        if pool_ref.select == "top" and pool_ref.score:
            data = self._select_top(data, pool_ref.count, pool_ref.score, crawler_context)
        elif pool_ref.select == "random" and pool_ref.count:
            data = self._select_random(data, pool_ref.count)
        elif pool_ref.select == "first" and pool_ref.count:
            data = data[: pool_ref.count]

        # Apply column mapping for output
        if pool_ref.columns:
            data = self._apply_column_mapping(data, pool_ref.columns)

        return data

    def _apply_filter(
        self, data: list[dict[str, Any]], filter_spec: dict[str, Any], context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Filter records by field values."""
        result = []
        for item in data:
            matches = True
            for field, expected in filter_spec.items():
                actual = item.get(field)

                # Substitute variables in expected value
                if isinstance(expected, str) and "{" in expected:
                    try:
                        expected = self.expression_evaluator.evaluate(expected, context)
                    except ExpressionError:
                        # Keep as string if evaluation fails
                        pass

                if actual != expected:
                    matches = False
                    break

            if matches:
                result.append(item)

        return result

    def _select_top(
        self,
        data: list[dict[str, Any]],
        count: int | None,
        score_expr: str,
        crawler_context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """
        Select top N items by score expression.

        Args:
            data: List of records
            count: Number to select (None = all)
            score_expr: Scoring expression
            crawler_context: Crawler parameters

        Returns:
            Top N items sorted by score (descending)
        """
        scored = []

        for item in data:
            # Build context for expression evaluation
            eval_context = {
                "crawler": crawler_context,
                "item": item,
                **crawler_context,  # Allow direct access to crawler params
            }

            try:
                score = self.expression_evaluator.evaluate(score_expr, eval_context)
                scored.append((score, item))
            except ExpressionError as e:
                self.warnings.append(f"Scoring error for item: {e}")
                # Assign low score on error
                scored.append((float("-inf"), item))

        # Sort by score (descending)
        scored.sort(key=lambda x: x[0], reverse=True)

        # Select top N
        if count is not None:
            scored = scored[:count]

        return [item for _, item in scored]

    def _select_random(self, data: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
        """Select random N items."""
        if count >= len(data):
            return data
        return self.rng.sample(data, count)

    def _apply_column_mapping(
        self, data: list[dict[str, Any]], columns: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Rename columns in output."""
        result = []
        for row in data:
            new_row = {}
            for key, value in row.items():
                new_key = columns.get(key, key)
                new_row[new_key] = value
            result.append(new_row)
        return result

    def is_pool_reference(self, value: Any) -> bool:
        """Check if a value is a pool reference."""
        return isinstance(value, dict) and "from_pool" in value

    def get_pool_names(self) -> list[str]:
        """Get list of loaded pool names."""
        return list(self.pools.keys())

    def get_pool_info(self, name: str) -> dict[str, Any] | None:
        """Get information about a pool."""
        if name not in self.pools:
            return None

        data = self.pools[name]
        definition = self.pool_definitions.get(name)

        info = {
            "name": name,
            "record_count": len(data),
            "columns": list(data[0].keys()) if data else [],
        }

        if definition:
            info["source"] = definition.source
            info["format"] = definition.format

        return info


def resolve_pool_references(
    obj: Any, pool_manager: DataPoolManager, crawler_context: dict[str, Any]
) -> Any:
    """
    Recursively resolve pool references in a data structure.

    Args:
        obj: Data structure potentially containing pool references
        pool_manager: DataPoolManager instance
        crawler_context: Crawler parameters for scoring/filtering

    Returns:
        Data structure with pool references replaced by resolved data
    """
    if isinstance(obj, dict):
        if "from_pool" in obj:
            # This is a pool reference
            return pool_manager.resolve(obj, crawler_context)
        else:
            # Regular dict - recurse into values
            return {
                k: resolve_pool_references(v, pool_manager, crawler_context) for k, v in obj.items()
            }
    elif isinstance(obj, list):
        return [resolve_pool_references(item, pool_manager, crawler_context) for item in obj]
    else:
        return obj
