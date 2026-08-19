"""
Spec Validator - Validate experiment specifications.

Performs:
1. JSON Schema validation (structural)
2. Semantic validation (business rules)
3. Design validation (factorial completeness)
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import jsonschema

    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

from .job_registry import JobRegistry
from .time_utils import is_relative_time


@dataclass
class ValidationResult:
    """Result of validation."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)


class SpecValidator:
    """
    Validate experiment specifications.

    Combines JSON Schema validation with semantic rules.

    Example usage:
        validator = SpecValidator()
        result = validator.validate(parsed_spec)

        if not result.valid:
            print("Errors:", result.errors)
    """

    def __init__(self, schema_path: str | None = None):
        """
        Initialize validator.

        Args:
            schema_path: Path to JSON Schema file (uses default if not specified)
        """
        self.schema = None
        self._load_schema(schema_path)

    def _load_schema(self, schema_path: str | None = None) -> None:
        """Load JSON Schema for validation."""
        if not HAS_JSONSCHEMA:
            return

        resolved_path: str | Path
        if schema_path is None:
            # Use default schema path
            schema_dir = Path(__file__).parent.parent / "schema"
            resolved_path = schema_dir / "experiment-spec.schema.json"
        else:
            resolved_path = schema_path

        try:
            with open(resolved_path) as f:
                self.schema = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            # Schema not found or invalid - continue without it
            pass

    def validate(self, spec: dict[str, Any]) -> ValidationResult:
        """
        Validate a specification.

        Args:
            spec: Parsed specification dict (raw YAML data)

        Returns:
            ValidationResult with errors, warnings, and info
        """
        errors: list[str] = []
        warnings: list[str] = []
        info: dict[str, Any] = {}

        # 1. JSON Schema validation (if available)
        schema_errors = self._validate_schema(spec)
        errors.extend(schema_errors)

        # 2. Required field validation
        field_errors = self._validate_required_fields(spec)
        errors.extend(field_errors)

        # 3. Design validation
        if "design" in spec and spec["design"]:
            design_errors, design_warnings = self._validate_design(spec["design"])
            errors.extend(design_errors)
            warnings.extend(design_warnings)
            info["has_design"] = True
            info["design_info"] = self._get_design_info(spec["design"])
        else:
            info["has_design"] = False

        # 4. Crawler validation
        crawler_errors, crawler_warnings = self._validate_crawlers(spec)
        errors.extend(crawler_errors)
        warnings.extend(crawler_warnings)

        # 5. Template validation
        template_errors, template_warnings = self._validate_templates(spec)
        errors.extend(template_errors)
        warnings.extend(template_warnings)

        # 6. Variable reference validation
        ref_errors, ref_warnings = self._validate_variable_references(spec)
        errors.extend(ref_errors)
        warnings.extend(ref_warnings)

        return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings, info=info)

    def _validate_schema(self, spec: dict[str, Any]) -> list[str]:
        """Validate against JSON Schema."""
        if not HAS_JSONSCHEMA or not self.schema:
            return []

        errors = []
        try:
            jsonschema.validate(spec, self.schema)
        except jsonschema.ValidationError as e:
            # Get a human-readable path
            path = ".".join(str(p) for p in e.absolute_path) if e.absolute_path else "root"
            errors.append(f"Schema validation error at '{path}': {e.message}")
        except jsonschema.SchemaError as e:
            errors.append(f"Schema error: {e.message}")

        return errors

    def _validate_required_fields(self, spec: dict[str, Any]) -> list[str]:
        """Validate required fields are present."""
        errors = []

        # Experiment name is required
        if not spec.get("experiment", {}).get("name"):
            errors.append("experiment.name is required")

        # At least one crawler is required
        crawlers = spec.get("crawlers", [])
        if not crawlers:
            errors.append("At least one crawler must be defined")

        return errors

    def _validate_design(self, design: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Validate factorial design."""
        errors: list[str] = []
        warnings: list[str] = []

        factors = design.get("factors", {})
        if not factors:
            errors.append("Design must have at least one factor")
            return errors, warnings

        for factor_name, factor_def in factors.items():
            # Validate factor has levels
            levels = factor_def.get("levels", [])
            if not levels:
                errors.append(f"Factor '{factor_name}' must have at least one level")
                continue

            # Check for duplicate levels
            level_ids = []
            for level in levels:
                if isinstance(level, dict):
                    level_id = level.get("id", level.get("name", ""))
                else:
                    level_id = str(level)
                level_ids.append(level_id)

            if len(level_ids) != len(set(level_ids)):
                errors.append(f"Factor '{factor_name}' has duplicate levels")

            # Validate maps_to completeness
            maps_to = factor_def.get("maps_to", {})
            for param_path, level_map in maps_to.items():
                if not isinstance(level_map, dict):
                    errors.append(f"maps_to.{param_path} must be a mapping of level->value")
                    continue

                # Check all levels are covered
                mapped_levels = {str(k).lower() for k in level_map}
                defined_levels = {str(level).lower() for level in level_ids}

                missing = defined_levels - mapped_levels
                if missing:
                    warnings.append(
                        f"Factor '{factor_name}' maps_to.{param_path} missing levels: {missing}"
                    )

        # Validate replications
        replications = design.get("replications", 1)
        if not isinstance(replications, int) or replications < 1:
            errors.append("replications must be a positive integer")

        return errors, warnings

    def _get_design_info(self, design: dict[str, Any]) -> dict[str, Any]:
        """Get design summary information."""
        factors = design.get("factors", {})
        replications = design.get("replications", 1)

        factor_info = []
        total_cells = 1

        for name, factor_def in factors.items():
            levels = factor_def.get("levels", [])
            num_levels = len(levels)
            total_cells *= num_levels

            factor_info.append(
                {
                    "name": name,
                    "num_levels": num_levels,
                    "maps_to": list(factor_def.get("maps_to", {}).keys()),
                }
            )

        return {
            "factors": factor_info,
            "total_cells": total_cells,
            "replications": replications,
            "total_crawlers": total_cells * replications,
        }

    def _validate_crawlers(self, spec: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Validate crawler definitions."""
        errors = []
        warnings = []

        crawlers = spec.get("crawlers", [])
        design = spec.get("design")

        for i, crawler in enumerate(crawlers):
            prefix = f"crawler[{i}]"

            # Check if using design without design defined
            if crawler.get("from_design") and not design:
                errors.append(f"{prefix}: from_design=true but no design is defined")

            # Validate proxy if defined
            proxy = crawler.get("proxy", {})
            if proxy and proxy != "auto":
                if not proxy.get("hostname") and not proxy.get("host"):
                    warnings.append(f"{prefix}.proxy: no hostname specified")
                if not proxy.get("port"):
                    warnings.append(f"{prefix}.proxy: no port specified")

            # Validate session/queue
            session = crawler.get("session")
            queues = crawler.get("queues", [])

            if session:
                self._validate_session(session, f"{prefix}.session", errors, warnings, spec)
            for j, queue in enumerate(queues):
                self._validate_session(queue, f"{prefix}.queues[{j}]", errors, warnings, spec)

        return errors, warnings

    def _validate_session(
        self,
        session: dict[str, Any],
        prefix: str,
        errors: list[str],
        warnings: list[str],
        spec: dict[str, Any],
    ) -> None:
        """Validate a session/queue definition."""
        # Check for start time
        start = session.get("start") or session.get("start_at")
        if not start:
            warnings.append(f"{prefix}: no start time specified, will use current time")
        else:
            # Validate timestamp format
            if not self._is_valid_timestamp(start):
                errors.append(
                    f"{prefix}: invalid start time format '{start}'. "
                    "Use 'now', relative offset (+5m, +1h, +1d), or ISO 8601 timestamp"
                )

        # Validate workflow
        workflow = session.get("workflow", [])
        for i, item in enumerate(workflow):
            self._validate_workflow_item(item, f"{prefix}.workflow[{i}]", errors, warnings, spec)

    def _is_valid_timestamp(self, value: str) -> bool:
        """
        Check if timestamp format is valid.

        Accepts:
            - "now"
            - Relative offsets: +5m, -1h, +1d, +30s
            - ISO 8601 timestamps: 2025-02-01T10:00:00Z
        """
        if is_relative_time(value):
            return True
        # Check ISO 8601 pattern (date with optional time)
        if re.match(r"^\d{4}-\d{2}-\d{2}", value):
            return True
        return False

    def _validate_workflow_item(
        self, item: Any, prefix: str, errors: list[str], warnings: list[str], spec: dict[str, Any]
    ) -> None:
        """Validate a single workflow item."""
        if not isinstance(item, dict):
            errors.append(f"{prefix}: workflow item must be an object")
            return

        # Check for template reference
        if "template" in item:
            template_name = item["template"]
            templates = spec.get("templates", {})
            if template_name not in templates:
                errors.append(f"{prefix}: template '{template_name}' not found")

        # Check for repeat
        if "repeat" in item:
            repeat = item["repeat"]
            if not isinstance(repeat, int) or repeat < 1:
                errors.append(f"{prefix}: repeat must be a positive integer")

            if "template" not in item and "jobs" not in item:
                errors.append(f"{prefix}: repeat requires 'template' or 'jobs'")

        # Validate job types
        job_types = [
            "visit",
            "wait",
            "click",
            "try_click",
            "type",
            "try_type",
            "scroll",
            "scroll_length",
            "lambda_select",
            "single_select",
            "single_select_url",
            "switch",
            "try_switch",
            "handle_alert",
            "try_handle_alert",
        ]

        for job_type in job_types:
            if job_type in item:
                self._validate_job_shorthand(job_type, item[job_type], prefix, errors, warnings)

    def _validate_job_shorthand(
        self, job_type: str, value: Any, prefix: str, errors: list[str], warnings: list[str]
    ) -> None:
        """Validate job shorthand syntax."""
        schema = JobRegistry.get_schema(job_type)
        if not schema:
            warnings.append(f"{prefix}: unknown job type '{job_type}'")
            return

        # Check based on value type
        if job_type == "visit":
            if not isinstance(value, str) or not value:
                errors.append(f"{prefix}.visit: URL is required")

        elif job_type == "wait":
            if not isinstance(value, (int, float, str)):
                errors.append(f"{prefix}.wait: duration must be a number or string")

        elif job_type in ["type", "try_type"]:
            if isinstance(value, dict):
                if not value.get("text"):
                    errors.append(f"{prefix}.{job_type}: 'text' is required")
                if not value.get("selector"):
                    errors.append(f"{prefix}.{job_type}: 'selector' is required")
            else:
                errors.append(f"{prefix}.{job_type}: must be an object with 'text' and 'selector'")

        elif job_type == "lambda_select":
            if isinstance(value, dict):
                if not value.get("task"):
                    errors.append(f"{prefix}.lambda_select: 'task' is required")
                if not value.get("function") and not value.get("link_value"):
                    errors.append(f"{prefix}.lambda_select: 'function' or 'link_value' is required")
            else:
                errors.append(f"{prefix}.lambda_select: must be an object")

    def _validate_templates(self, spec: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Validate template definitions."""
        errors: list[str] = []
        warnings: list[str] = []

        templates = spec.get("templates", {})

        for name, template in templates.items():
            if not isinstance(template, list):
                errors.append(f"template.{name}: must be a list of workflow items")
                continue

            for i, item in enumerate(template):
                self._validate_workflow_item(item, f"template.{name}[{i}]", errors, warnings, spec)

        return errors, warnings

    def _validate_variable_references(self, spec: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Validate variable references in crawler templates."""
        errors: list[str] = []
        warnings: list[str] = []

        design = spec.get("design")
        if not design:
            return errors, warnings

        # Collect available variables from design
        available_vars = {"replica"}
        factors = design.get("factors", {})

        for factor_name, factor_def in factors.items():
            available_vars.add(factor_name)
            available_vars.add(f"{factor_name}:value")

            # Add mapped parameter names
            for param_path in factor_def.get("maps_to", {}).keys():
                # Add both full path and last segment
                available_vars.add(param_path)
                available_vars.add(param_path.split(".")[-1])

        # Check crawler templates for undefined variables
        for i, crawler in enumerate(spec.get("crawlers", [])):
            if crawler.get("from_design"):
                undefined = self._find_undefined_variables(crawler, available_vars)
                for var in undefined:
                    warnings.append(f"crawler[{i}]: variable '{{{var}}}' may not be defined")

        return errors, warnings

    def _find_undefined_variables(self, obj: Any, available_vars: set) -> set:
        """Find undefined variable references in an object."""
        undefined: set[str] = set()

        if isinstance(obj, str):
            # Skip strings that look like JSON (start and end with { })
            stripped = obj.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                # Check if it's a JSON object vs a simple variable
                if '"' in stripped or ":" in stripped:
                    # Looks like JSON, skip it
                    return undefined

            # Find all {var} or {var:format} patterns
            # Only match simple variable names (alphanumeric + underscore)
            pattern = r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::\d*d)?\}"
            for match in re.finditer(pattern, obj):
                var_name = match.group(1)
                if var_name not in available_vars:
                    undefined.add(var_name)

        elif isinstance(obj, dict):
            for value in obj.values():
                undefined.update(self._find_undefined_variables(value, available_vars))

        elif isinstance(obj, list):
            for item in obj:
                undefined.update(self._find_undefined_variables(item, available_vars))

        return undefined


def validate_spec(spec: dict[str, Any]) -> ValidationResult:
    """
    Convenience function to validate a specification.

    Args:
        spec: Parsed specification dict

    Returns:
        ValidationResult
    """
    validator = SpecValidator()
    return validator.validate(spec)
