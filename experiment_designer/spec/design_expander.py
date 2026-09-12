"""
Design Expander - Expand factorial designs into crawler definitions.

Handles:
- Factorial design parsing
- Factor level combinations
- Parameter mapping (maps_to)
- Replication with naming patterns
- Variable substitution in crawler templates
- Parameter distributions (uniform, discrete, normal, gumbel, etc.)
- Proportional factor levels (stratified sampling)
"""

import copy
import itertools
import random
import re
from dataclasses import dataclass, field
from typing import Any

from .distributions import ParameterSampler

# Matches any `{name}` or `{name:spec}` token left after the bare-form
# replacements in `_substitute_variables` -- the general format-spec pass.
_GENERAL_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


@dataclass
class FactorLevel:
    """A single level of a factor."""

    id: str
    value: Any
    proportion: float | None = None  # For stratified designs


@dataclass
class Factor:
    """A factor in the experimental design."""

    name: str
    levels: list[FactorLevel]
    maps_to: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def has_proportions(self) -> bool:
        """Check if this factor uses proportional levels."""
        return any(level.proportion is not None for level in self.levels)

    def get_level_proportions(self) -> dict[str, float]:
        """Get mapping of level IDs to proportions."""
        result = {}
        for level in self.levels:
            if level.proportion is not None:
                result[level.id] = level.proportion
            else:
                # Equal distribution if no proportion specified
                result[level.id] = 1.0 / len(self.levels)
        return result


@dataclass
class DesignCell:
    """A single cell in the factorial design (one combination of levels)."""

    factor_values: dict[str, str]  # factor_name -> level_id
    parameter_values: dict[str, Any]  # Resolved parameter values from maps_to
    sampled_parameters: dict[str, Any] = field(default_factory=dict)  # Sampled from distributions
    replica: int = 1
    cell_index: int = 0  # Index within the design


@dataclass
class ExpandedDesign:
    """Result of expanding a factorial design."""

    cells: list[DesignCell]
    total_crawlers: int
    factors: dict[str, Factor]
    replications: int
    parameters: dict[str, Any] = field(default_factory=dict)  # Parameter distributions
    design_type: str = "factorial"
    seed: int | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class DesignExpander:
    """
    Expand factorial designs into crawler definitions.

    Example design:
        design:
          type: factorial
          seed: 42  # For reproducible randomization

          # Parameter distributions (sampled per crawler)
          parameters:
            pi:
              distribution: uniform
              min: -1
              max: 1
            kappa:
              distribution: discrete
              values:
                0: 0.25
                1: 0.25
                2: 0.50

          factors:
            political:
              levels: [left, center, right]
              maps_to:
                preference_political_videos: {left: 1, center: 0, right: -1}
                params.pi: {left: -0.5, center: 0, right: 0.5}
            content:
              levels: [news, entertainment]
              maps_to:
                starting_channel: {news: "@CNN", entertainment: "@MrBeast"}

          # Or with proportions:
          factors:
            usage_type:
              levels:
                - id: only_visits
                  proportion: 0.25
                - id: only_searches
                  proportion: 0.25
                - id: searches_and_visits
                  proportion: 0.50

          replications: 3

    This generates 3 x 2 x 3 = 18 crawlers (factorial)
    or 100 crawlers with proportional sampling (stratified).
    """

    def __init__(self, seed: int | None = None):
        """
        Initialize design expander.

        Args:
            seed: Random seed for reproducible sampling
        """
        self.seed = seed
        self.rng = random.Random(seed)
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def expand(
        self,
        design: dict[str, Any] | None,
        crawler_template: dict[str, Any],
        replications_override: int | None = None,
        total_crawlers_override: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Expand a design into multiple crawler definitions.

        Args:
            design: Design specification (or None for no expansion)
            crawler_template: Template crawler to expand
            replications_override: Override replications count
            total_crawlers_override: Override total crawler count (for stratified)

        Returns:
            List of expanded crawler definitions
        """
        self.errors = []
        self.warnings = []

        # If no design, just return the template as-is
        if not design:
            return [crawler_template]

        # Use seed from design if not set in constructor
        if self.seed is None and "seed" in design:
            self.seed = design["seed"]
            self.rng = random.Random(self.seed)

        # Parse the design
        parsed = self._parse_design(design, replications_override, total_crawlers_override)

        if parsed.errors:
            self.errors.extend(parsed.errors)
            return [crawler_template]

        # Generate crawlers from cells
        crawlers = []
        for cell in parsed.cells:
            crawler = self._apply_cell_to_template(
                crawler_template, cell, parsed.factors, parsed.parameters
            )
            crawlers.append(crawler)

        return crawlers

    def _parse_design(
        self,
        design: dict[str, Any],
        replications_override: int | None = None,
        total_crawlers_override: int | None = None,
    ) -> ExpandedDesign:
        """Parse design specification into ExpandedDesign."""
        errors = []
        warnings = []

        design_type = design.get("type", "factorial")
        seed = self.seed if self.seed is not None else design.get("seed")

        # Parse parameter distributions
        parameters = design.get("parameters", {})
        param_sampler = None
        if parameters:
            param_sampler = ParameterSampler(seed=seed)
            param_sampler.add_parameters(parameters)
            if param_sampler.errors:
                errors.extend(param_sampler.errors)

        # Parse factors
        factors_raw = design.get("factors", {})
        factors = {}

        for name, factor_def in factors_raw.items():
            factor = self._parse_factor(name, factor_def)
            if factor:
                factors[name] = factor
            else:
                errors.append(f"Could not parse factor: {name}")

        # Check if any factor has proportions (stratified design)
        has_proportions = any(f.has_proportions for f in factors.values())
        if has_proportions:
            design_type = "stratified"

        # Get replications and total crawlers
        replications = replications_override or design.get("replications", 1)
        total_crawlers = total_crawlers_override or design.get("total_crawlers")

        # Validate factor proportions if stratified
        if design_type == "stratified":
            for factor_name, factor in factors.items():
                if factor.has_proportions:
                    total_prop = sum(
                        level.proportion for level in factor.levels if level.proportion
                    )
                    if abs(total_prop - 1.0) > 0.01:
                        warnings.append(
                            f"Factor '{factor_name}' proportions sum to {total_prop:.2f}, "
                            f"not 1.0. Normalizing..."
                        )

        if not factors and not parameters:
            errors.append("No valid factors or parameters defined in design")
            return ExpandedDesign(
                cells=[],
                total_crawlers=0,
                factors={},
                replications=0,
                parameters=parameters,
                design_type=design_type,
                seed=seed,
                errors=errors,
                warnings=warnings,
            )

        # Generate cells based on design type
        if design_type == "stratified" and total_crawlers:
            cells = self._generate_stratified_cells(factors, total_crawlers, param_sampler)
        else:
            cells = self._generate_factorial_cells(factors, replications, param_sampler)

        return ExpandedDesign(
            cells=cells,
            total_crawlers=len(cells),
            factors=factors,
            replications=replications,
            parameters=parameters,
            design_type=design_type,
            seed=seed,
            errors=errors,
            warnings=warnings,
        )

    def _parse_factor(self, name: str, factor_def: dict[str, Any]) -> Factor | None:
        """Parse a single factor definition."""
        levels_raw = factor_def.get("levels", [])
        maps_to = factor_def.get("maps_to", {})

        if not levels_raw:
            return None

        levels = []
        for level in levels_raw:
            if isinstance(level, dict):
                # Format: {id: "left", value: -0.5, proportion: 0.25}
                level_id = str(level.get("id", level.get("name", "")))
                level_value = level.get("value", level_id)
                proportion = level.get("proportion")
            else:
                # Simple format: just the level name/id
                level_id = str(level)
                level_value = level
                proportion = None

            levels.append(FactorLevel(id=level_id, value=level_value, proportion=proportion))

        return Factor(name=name, levels=levels, maps_to=maps_to)

    def _generate_factorial_cells(
        self,
        factors: dict[str, Factor],
        replications: int,
        param_sampler: ParameterSampler | None = None,
    ) -> list[DesignCell]:
        """Generate all factorial combinations with replications."""
        cells = []
        cell_index = 0

        # Get all level combinations
        factor_names = list(factors.keys())
        level_lists = [factors[name].levels for name in factor_names] if factors else [[]]

        for combo in itertools.product(*level_lists):
            # Create factor_values dict for this combination
            factor_values = (
                {factor_names[i]: combo[i].id for i in range(len(factor_names))} if factors else {}
            )

            # Resolve parameter values from maps_to
            parameter_values = self._resolve_parameter_values(factors, factor_values)

            # Create cells for each replication
            for replica in range(1, replications + 1):
                # Sample from parameter distributions
                sampled_parameters = {}
                if param_sampler:
                    sampled_parameters = param_sampler.sample()

                cells.append(
                    DesignCell(
                        factor_values=factor_values.copy(),
                        parameter_values=parameter_values.copy(),
                        sampled_parameters=sampled_parameters,
                        replica=replica,
                        cell_index=cell_index,
                    )
                )
                cell_index += 1

        return cells

    def _generate_stratified_cells(
        self,
        factors: dict[str, Factor],
        total_crawlers: int,
        param_sampler: ParameterSampler | None = None,
    ) -> list[DesignCell]:
        """
        Generate cells using stratified sampling with proportions.

        Args:
            factors: Factor definitions (some may have proportions)
            total_crawlers: Total number of crawlers to generate
            param_sampler: Optional parameter sampler for distributions

        Returns:
            List of design cells
        """
        cells = []

        # Separate factors with and without proportions
        proportion_factors = {n: f for n, f in factors.items() if f.has_proportions}
        factorial_factors = {n: f for n, f in factors.items() if not f.has_proportions}

        # Calculate how many crawlers for each level of proportioned factors
        level_counts: dict[str, dict[str, int]] = {}
        for factor_name, factor in proportion_factors.items():
            level_counts[factor_name] = {}
            remaining = total_crawlers
            for i, level in enumerate(factor.levels):
                prop = level.proportion or (1.0 / len(factor.levels))
                if i == len(factor.levels) - 1:
                    # Last level gets remaining to avoid rounding issues
                    count = remaining
                else:
                    count = int(total_crawlers * prop)
                    remaining -= count
                level_counts[factor_name][level.id] = count

        # Generate factorial combinations for non-proportioned factors
        factorial_combos = []
        if factorial_factors:
            factor_names = list(factorial_factors.keys())
            level_lists = [factorial_factors[name].levels for name in factor_names]
            for combo in itertools.product(*level_lists):
                factorial_combos.append(
                    {factor_names[i]: combo[i].id for i in range(len(factor_names))}
                )
        else:
            factorial_combos = [{}]

        # Generate cells with proportional sampling
        cell_index = 0
        replica_counters: dict[str, int] = {}

        for _ in range(total_crawlers):
            # Select factor levels (proportioned ones based on remaining counts)
            factor_values = {}

            # For proportioned factors, select based on remaining counts
            for factor_name, factor in proportion_factors.items():
                counts = level_counts[factor_name]
                # Select a level with remaining count
                for level in factor.levels:
                    if counts.get(level.id, 0) > 0:
                        factor_values[factor_name] = level.id
                        counts[level.id] -= 1
                        break

            # For factorial factors, cycle through combinations
            if factorial_combos:
                combo_index = cell_index % len(factorial_combos)
                factor_values.update(factorial_combos[combo_index])

            # Resolve parameter values from maps_to
            parameter_values = self._resolve_parameter_values(factors, factor_values)

            # Sample from parameter distributions
            sampled_parameters = {}
            if param_sampler:
                sampled_parameters = param_sampler.sample()

            # Calculate replica number for this combination
            combo_key = "_".join(f"{k}={v}" for k, v in sorted(factor_values.items()))
            replica_counters[combo_key] = replica_counters.get(combo_key, 0) + 1
            replica = replica_counters[combo_key]

            cells.append(
                DesignCell(
                    factor_values=factor_values.copy(),
                    parameter_values=parameter_values.copy(),
                    sampled_parameters=sampled_parameters,
                    replica=replica,
                    cell_index=cell_index,
                )
            )
            cell_index += 1

        return cells

    def _resolve_parameter_values(
        self, factors: dict[str, Factor], factor_values: dict[str, str]
    ) -> dict[str, Any]:
        """
        Resolve parameter values from factor maps_to definitions.

        Args:
            factors: Factor definitions with maps_to
            factor_values: Current factor level selections

        Returns:
            Dict of parameter paths to values
        """
        params = {}

        for factor_name, level_id in factor_values.items():
            factor = factors[factor_name]

            for param_path, level_map in factor.maps_to.items():
                if level_id in level_map:
                    params[param_path] = level_map[level_id]
                elif level_id.lower() in {k.lower() for k in level_map}:
                    # Case-insensitive fallback
                    for k, v in level_map.items():
                        if k.lower() == level_id.lower():
                            params[param_path] = v
                            break

        return params

    def _apply_cell_to_template(
        self,
        template: dict[str, Any],
        cell: DesignCell,
        factors: dict[str, Factor],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Apply a design cell to a crawler template.

        Args:
            template: Crawler template
            cell: Design cell with factor values and sampled parameters
            factors: Factor definitions
            parameters: Parameter distribution definitions (for reference)

        Returns:
            Expanded crawler definition
        """
        crawler = copy.deepcopy(template)

        # Apply name substitution
        if "name" in crawler:
            crawler["name"] = self._substitute_variables(crawler["name"], cell, factors)

        # Apply description substitution
        if "description" in crawler:
            crawler["description"] = self._substitute_variables(
                crawler["description"], cell, factors
            )

        # Apply parameter values from maps_to
        for param_path, value in cell.parameter_values.items():
            self._set_nested_value(crawler, param_path, value)

        # Apply sampled parameter values
        for param_name, value in cell.sampled_parameters.items():
            # Store sampled values in configuration.params by default
            param_path = f"configuration.params.{param_name}"
            self._set_nested_value(crawler, param_path, value)

        # Substitute variables throughout the entire crawler structure
        substituted: dict[str, Any] = self._substitute_all_variables(crawler, cell, factors)

        return substituted

    def _substitute_all_variables(
        self, obj: Any, cell: DesignCell, factors: dict[str, Factor]
    ) -> Any:
        """
        Recursively substitute variables in any nested structure.

        Args:
            obj: Any object (dict, list, string, etc.)
            cell: Design cell with variable values
            factors: Factor definitions

        Returns:
            Object with variables substituted
        """
        if isinstance(obj, str):
            return self._substitute_variables(obj, cell, factors)
        elif isinstance(obj, dict):
            return {k: self._substitute_all_variables(v, cell, factors) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._substitute_all_variables(item, cell, factors) for item in obj]
        else:
            return obj

    def _substitute_variables(self, text: str, cell: DesignCell, factors: dict[str, Factor]) -> str:
        """
        Substitute variables in text.

        Supports:
            {factor_name} - Level ID
            {factor_name:value} - Level value
            {param_name} - Parameter value from maps_to or sampled distributions
            {pi}, {kappa}, {alpha} - Sampled parameter values
            {replica} - Replication number
            {replica:02d} - Formatted replication number (zero-padded)
            {cell_index} - Cell index in the design
            {cell_index:03d} - Formatted cell index (zero-padded)
            {name:spec} - Any of the above (except the {factor:value} form)
                with an arbitrary str.format() spec, e.g. {pi:.2f},
                {alpha:.3f} -- see `_general_substitute`.
        """
        result = text

        # Substitute factor values
        for factor_name, level_id in cell.factor_values.items():
            # {factor_name} -> level_id
            result = result.replace(f"{{{factor_name}}}", str(level_id))

            # {factor_name:value} -> level value
            factor = factors.get(factor_name)
            if factor:
                for level in factor.levels:
                    if level.id == level_id:
                        result = result.replace(f"{{{factor_name}:value}}", str(level.value))
                        break

        # Substitute parameter values from maps_to (e.g., {initial_search})
        for param_path, value in cell.parameter_values.items():
            # Use the last part of the path as the variable name
            var_name = param_path.split(".")[-1]
            result = result.replace(f"{{{var_name}}}", str(value))
            # Also try with full path
            result = result.replace(f"{{{param_path}}}", str(value))

        # Substitute sampled parameter values (e.g., {pi}, {kappa}, {alpha})
        for param_name, value in cell.sampled_parameters.items():
            if isinstance(value, float):
                # Format floats with reasonable precision
                result = result.replace(f"{{{param_name}}}", f"{value:.6g}")
            else:
                result = result.replace(f"{{{param_name}}}", str(value))

        # Substitute cell index (useful for unique naming)
        result = result.replace("{cell_index}", str(cell.cell_index))

        # Handle formatted cell index (e.g., {cell_index:03d})
        cell_index_format_pattern = r"\{cell_index:(\d+)d\}"
        for match in re.finditer(cell_index_format_pattern, result):
            width = int(match.group(1))
            formatted = str(cell.cell_index).zfill(width)
            result = result.replace(match.group(0), formatted)

        # Substitute replica
        result = result.replace("{replica}", str(cell.replica))

        # Handle formatted replica (e.g., {replica:02d})
        replica_format_pattern = r"\{replica:(\d+)d\}"
        for match in re.finditer(replica_format_pattern, result):
            width = int(match.group(1))
            formatted = str(cell.replica).zfill(width)
            result = result.replace(match.group(0), formatted)

        # General format-spec pass: {name} or {name:spec} for any of the
        # variable sources above, with an arbitrary str.format() spec
        # (e.g. {pi:.2f}, {alpha:.3f}). Runs last, so it only ever sees
        # whatever the bare-token replacements above left behind -- which is
        # everything that carried a `:spec` other than the handful of bare
        # forms special-cased above.
        result = _GENERAL_VAR_RE.sub(lambda m: self._general_substitute(m, cell, factors), result)

        return result

    def _general_substitute(
        self, match: "re.Match[str]", cell: DesignCell, factors: dict[str, Factor]
    ) -> str:
        """Resolve one `{name}` / `{name:spec}` token against every variable source.

        Lookup order: factor level id/value, `maps_to` parameter values
        (last path segment, then full path), sampled parameters, `replica`,
        `cell_index`. An unknown name is left untouched -- it is either a
        template variable (`${...}`, never seen here) or a genuinely
        unresolved design variable, and either way `_scan_unresolved` in the
        converter is what should report it.
        """
        name = match.group(1)
        spec = match.group(2)  # None when the token has no ":spec"

        # {factor_name:value} is a special form (the level's *value*, not a
        # format spec on the level id) -- keep it working here too, since the
        # regex above already normalises it away when the level is found.
        if spec == "value" and name in cell.factor_values:
            factor = factors.get(name)
            level_id = cell.factor_values[name]
            if factor:
                for level in factor.levels:
                    if level.id == level_id:
                        return str(level.value)
            # Falls through: no matching level, so `value` is treated as an
            # (invalid) format spec below and the token is left unresolved.

        value: Any = None
        found = False

        if name in cell.factor_values:
            value = cell.factor_values[name]
            found = True
        else:
            for param_path, pvalue in cell.parameter_values.items():
                if name == param_path or name == param_path.split(".")[-1]:
                    value = pvalue
                    found = True
                    break
            if not found and name in cell.sampled_parameters:
                value = cell.sampled_parameters[name]
                found = True
            elif not found and name == "replica":
                value = cell.replica
                found = True
            elif not found and name == "cell_index":
                value = cell.cell_index
                found = True

        if not found:
            return match.group(0)

        if spec is not None:
            try:
                return format(value, spec)
            except (ValueError, TypeError):
                return match.group(0)

        if isinstance(value, float):
            return f"{value:.6g}"
        return str(value)

    def _set_nested_value(self, obj: dict[str, Any], path: str, value: Any) -> None:
        """
        Set a value in a nested dict using dot notation path.

        Examples:
            "params.pi" -> obj["params"]["pi"] = value
            "configuration.params.pi" -> obj["configuration"]["params"]["pi"] = value
            "preferences.location" -> Special handling for preferences array

        Args:
            obj: Target dictionary
            path: Dot-notation path
            value: Value to set
        """
        parts = path.split(".")

        # Special handling for preferences (array of {name, value} objects)
        if "preferences" in parts:
            pref_idx = parts.index("preferences")
            pref_name = parts[pref_idx + 1] if pref_idx + 1 < len(parts) else None

            if pref_name:
                # Navigate to preferences array
                current = obj
                for part in parts[:pref_idx]:
                    if part not in current:
                        current[part] = {}
                    current = current[part]

                if "preferences" not in current:
                    current["preferences"] = []

                # Find or create the preference
                prefs = current["preferences"]
                found = False
                for pref in prefs:
                    if pref.get("name") == pref_name:
                        pref["value"] = value
                        found = True
                        break

                if not found:
                    prefs.append(
                        {
                            "name": pref_name,
                            "value": value,
                            "description": pref_name,
                            "active": True,
                        }
                    )
                return

        # Standard nested dict assignment
        current = obj
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]

        current[parts[-1]] = value

    def preview(self, design: dict[str, Any] | None) -> dict[str, Any]:
        """
        Preview what a design will generate without full expansion.

        Args:
            design: Design specification

        Returns:
            Preview information
        """
        if not design:
            return {"has_design": False, "message": "No factorial design specified"}

        factors_raw = design.get("factors", {})
        replications = design.get("replications", 1)

        factors_info = []
        total_levels = 1

        for name, factor_def in factors_raw.items():
            levels = factor_def.get("levels", [])
            num_levels = len(levels)
            total_levels *= num_levels

            level_names = []
            for level in levels:
                if isinstance(level, dict):
                    level_names.append(str(level.get("id", level.get("name", ""))))
                else:
                    level_names.append(str(level))

            factors_info.append(
                {
                    "name": name,
                    "levels": level_names,
                    "num_levels": num_levels,
                    "maps_to": list(factor_def.get("maps_to", {}).keys()),
                }
            )

        total_crawlers = total_levels * replications

        return {
            "has_design": True,
            "design_type": design.get("type", "factorial"),
            "factors": factors_info,
            "num_factors": len(factors_info),
            "total_cells": total_levels,
            "replications": replications,
            "total_crawlers": total_crawlers,
            "formula": " × ".join([f"{f['name']}({f['num_levels']})" for f in factors_info])
            + f" × {replications} replications = {total_crawlers} crawlers",
        }
