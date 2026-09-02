"""
YAML Spec Parser - Parse and expand experiment specifications.

Handles:
- YAML parsing with anchor/alias support
- Template expansion
- Shorthand syntax expansion (e.g., 'visit: URL' -> full job spec)
- Defaults resolution (system -> experiment -> crawler)
- Duration parsing (10s, 5m, 1h -> seconds)
- External file references (!file tag)
- Data pools with selection strategies
"""

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .data_pools import DataPoolManager, resolve_pool_references
from .file_loader import FileLoader, create_file_tag_loader, resolve_file_references
from .job_registry import JobRegistry


@dataclass
class ParsedSpec:
    """Parsed and expanded experiment specification."""

    version: str
    experiment: dict[str, Any]
    defaults: dict[str, Any]
    templates: dict[str, Any]
    design: dict[str, Any] | None
    data_pools: dict[str, Any]  # Data pool definitions
    crawlers: list[dict[str, Any]]
    raw: dict[str, Any]  # Original parsed YAML
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# System-level defaults (can be overridden by experiment defaults)
SYSTEM_DEFAULTS = {
    "location": "US-IL-CHICAGO",
    "browser": "mlx",
    "proxy_type": "http",
    "timezone": "America/Chicago",
    "timezone_utc_offset": "-06:00",
    "captcha_mode": "never",
    "typing_mode": "SIMULATED_FIXINGTYPOS",
    "selection_type": "XPATH",
    "active": True,
}


class SpecParser:
    """
    Parse YAML experiment specifications.

    Example usage:
        parser = SpecParser()
        parsed = parser.parse(yaml_content)
        if parsed.errors:
            print("Errors:", parsed.errors)
        else:
            print(f"Parsed {len(parsed.crawlers)} crawlers")

    With external file support:
        parser = SpecParser(base_dir=Path("./specs"), seed=42)
        parsed = parser.parse(yaml_content)
        # Files referenced via !file tag are resolved relative to base_dir
    """

    def __init__(self, base_dir: Path | None = None, seed: int | None = None):
        """
        Initialize the spec parser.

        Args:
            base_dir: Base directory for resolving file paths in !file tags
            seed: Random seed for reproducible file selections
        """
        self.base_dir = base_dir or Path.cwd()
        self.seed = seed
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.file_loader: FileLoader | None = None
        self.pool_manager: DataPoolManager | None = None

    def parse(self, yaml_content: str) -> ParsedSpec:
        """
        Parse YAML content into a ParsedSpec.

        Args:
            yaml_content: YAML string

        Returns:
            ParsedSpec with expanded templates and shorthand
        """
        self.errors = []
        self.warnings = []

        # Create custom YAML loader with !file tag support
        FileTagLoader, self.file_loader = create_file_tag_loader(
            base_dir=self.base_dir, seed=self.seed
        )

        try:
            raw = yaml.load(yaml_content, Loader=FileTagLoader)
        except yaml.YAMLError as e:
            return ParsedSpec(
                version="",
                experiment={},
                defaults={},
                templates={},
                design=None,
                data_pools={},
                crawlers=[],
                raw={},
                errors=[f"YAML parsing error: {e}"],
            )

        if not isinstance(raw, dict):
            return ParsedSpec(
                version="",
                experiment={},
                defaults={},
                templates={},
                design=None,
                data_pools={},
                crawlers=[],
                raw={},
                errors=["YAML root must be an object"],
            )

        # Resolve !file references in the raw YAML data
        raw = resolve_file_references(raw, self.file_loader)

        # Propagate file loader errors/warnings
        self.errors.extend(self.file_loader.errors)
        self.warnings.extend(self.file_loader.warnings)

        # Extract sections
        version = raw.get("version", "1.0")
        experiment = raw.get("experiment", {})
        defaults = self._merge_defaults(raw.get("defaults", {}))
        templates = raw.get("templates", {})
        design = raw.get("design")
        data_pools_raw = raw.get("data_pools", {})
        crawlers_raw = raw.get("crawlers", [])

        # Initialize pool manager and load pools
        self.pool_manager = DataPoolManager(base_dir=self.base_dir, seed=self.seed)
        if data_pools_raw:
            self.pool_manager.load_pools_from_spec(data_pools_raw)
            self.errors.extend(self.pool_manager.errors)
            self.warnings.extend(self.pool_manager.warnings)
            self.pool_manager.errors = []
            self.pool_manager.warnings = []

        # Validate required fields
        if not experiment.get("name"):
            self.errors.append("experiment.name is required")

        # Expand crawlers
        crawlers = []
        for i, crawler_raw in enumerate(crawlers_raw):
            try:
                crawler = self._expand_crawler(crawler_raw, defaults, templates, i)
                crawlers.append(crawler)
            except Exception as e:
                self.errors.append(f"Error expanding crawler {i}: {e}")

        return ParsedSpec(
            version=version,
            experiment=experiment,
            defaults=defaults,
            templates=templates,
            design=design,
            data_pools=data_pools_raw,
            crawlers=crawlers,
            raw=raw,
            errors=self.errors,
            warnings=self.warnings,
        )

    def _merge_defaults(self, experiment_defaults: dict[str, Any]) -> dict[str, Any]:
        """Merge system defaults with experiment defaults."""
        merged = copy.deepcopy(SYSTEM_DEFAULTS)
        merged.update(experiment_defaults)
        return merged

    def get_pool_manager(self) -> DataPoolManager | None:
        """Get the data pool manager (available after parse() is called)."""
        return self.pool_manager

    def resolve_pool_references_in_crawler(
        self, crawler: dict[str, Any], crawler_context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Resolve data pool references in a crawler configuration.

        This method should be called after design expansion when crawler
        parameters (like pi, kappa, alpha) are known.

        Args:
            crawler: Crawler definition with potential pool references
            crawler_context: Crawler parameters for scoring expressions
                (e.g., {"pi": -0.5, "kappa": 1, "alpha": 0.35})

        Returns:
            Crawler with pool references resolved to actual data

        Example pool reference in crawler:
            configuration:
              params:
                media_outlet_urls:
                  from_pool: outlets
                  select: top
                  count: 10
                  score: "{alpha} - {tau} * pow(abs({crawler.pi} - {item.pi}), 2)"
        """
        if not self.pool_manager:
            return crawler

        context = crawler_context or {}

        # Build context from crawler params if not provided
        if not context and "configuration" in crawler:
            params = crawler["configuration"].get("params", {})
            context = {k: v for k, v in params.items() if not isinstance(v, (dict, list))}

        # Resolve pool references recursively
        resolved: dict[str, Any] = resolve_pool_references(crawler, self.pool_manager, context)

        # Propagate any errors/warnings
        self.errors.extend(self.pool_manager.errors)
        self.warnings.extend(self.pool_manager.warnings)
        self.pool_manager.errors = []
        self.pool_manager.warnings = []

        return resolved

    def _expand_crawler(
        self,
        crawler: dict[str, Any],
        defaults: dict[str, Any],
        templates: dict[str, Any],
        index: int,
    ) -> dict[str, Any]:
        """
        Expand a crawler definition with defaults and templates.

        Args:
            crawler: Raw crawler definition
            defaults: Merged defaults
            templates: Template definitions
            index: Crawler index for error messages

        Returns:
            Expanded crawler definition
        """
        expanded = copy.deepcopy(crawler)

        # Apply defaults to configuration if not specified
        if "configuration" not in expanded:
            expanded["configuration"] = {}

        if "params" not in expanded.get("configuration", {}):
            expanded["configuration"]["params"] = {}

        if "preferences" not in expanded.get("configuration", {}):
            expanded["configuration"]["preferences"] = []

        # Apply location default to preferences if not set
        prefs = expanded["configuration"]["preferences"]
        if not any(p.get("name") == "location" for p in prefs):
            prefs.append(
                {
                    "name": "location",
                    "value": defaults.get("location", SYSTEM_DEFAULTS["location"]),
                    "description": "location",
                    "active": True,
                }
            )

        # Apply browser default to agent if not specified
        if "agent" not in expanded:
            expanded["agent"] = {}
        if "identification" not in expanded["agent"]:
            expanded["agent"]["identification"] = defaults.get("browser", "mlx")

        # Expand session/queues
        if "session" in expanded:
            # Convert session shorthand to queues array
            session = expanded.pop("session")
            expanded["queues"] = [self._expand_session(session, defaults, templates)]
        elif "queues" in expanded:
            expanded["queues"] = [
                self._expand_session(q, defaults, templates) for q in expanded["queues"]
            ]
        else:
            expanded["queues"] = []

        return expanded

    def _expand_session(
        self, session: dict[str, Any], defaults: dict[str, Any], templates: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Expand a session/queue definition.

        Args:
            session: Raw session definition
            defaults: Merged defaults
            templates: Template definitions

        Returns:
            Expanded queue definition
        """
        expanded = copy.deepcopy(session)

        # Apply timezone defaults
        if "timezone" not in expanded:
            expanded["timezone"] = defaults.get("timezone", "America/Chicago")
        if "timezone_utc_offset" not in expanded:
            expanded["timezone_utc_offset"] = defaults.get("timezone_utc_offset", "-06:00")

        # Convert 'start' shorthand to 'start_at'
        if "start" in expanded:
            expanded["start_at"] = expanded.pop("start")

        # Expand workflow to jobs
        if "workflow" in expanded:
            workflow = expanded.pop("workflow")
            expanded["jobs"] = self._expand_workflow(workflow, defaults, templates)
        elif "jobs" not in expanded:
            expanded["jobs"] = []

        # Ensure required fields
        if "name" not in expanded:
            expanded["name"] = "Session"
        if "description" not in expanded:
            expanded["description"] = "Generated session"
        if "active" not in expanded:
            expanded["active"] = True

        return expanded

    def _expand_workflow(
        self, workflow: list[Any], defaults: dict[str, Any], templates: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        Expand workflow items into job definitions.

        Handles:
        - Shorthand syntax (visit: URL)
        - Template references (template: name)
        - Repeat blocks (repeat: N, template: name)
        - Full job definitions

        Args:
            workflow: List of workflow items
            defaults: Merged defaults
            templates: Template definitions

        Returns:
            List of expanded job definitions
        """
        jobs = []

        for item in workflow:
            expanded_jobs = self._expand_workflow_item(item, defaults, templates)
            jobs.extend(expanded_jobs)

        return jobs

    def _expand_workflow_item(
        self, item: Any, defaults: dict[str, Any], templates: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        Expand a single workflow item into job(s).

        Args:
            item: Workflow item (shorthand, template ref, or full job)
            defaults: Merged defaults
            templates: Template definitions

        Returns:
            List of job definitions (usually 1, more for repeats/templates)
        """
        if not isinstance(item, dict):
            self.warnings.append(f"Skipping invalid workflow item: {item}")
            return []

        # Check for repeat block
        if "repeat" in item:
            return self._expand_repeat(item, defaults, templates)

        # Check for template reference
        if "template" in item:
            return self._expand_template_ref(item, defaults, templates)

        # Check for shorthand syntax (single key like 'visit', 'wait', etc.)
        shorthand_keys = [
            "visit",
            "wait",
            "click",
            "try_click",
            "type",
            "try_type",
            "scroll",
            "scroll_length",
            "try_scroll",
            "try_scroll_length",
            "lambda_select",
            "single_select",
            "single_select_url",
            "switch",
            "try_switch",
            "handle_alert",
            "try_handle_alert",
        ]

        for key in shorthand_keys:
            if key in item:
                return [self._expand_shorthand(key, item, defaults)]

        # Check if it's a full job definition with 'type'
        if "type" in item:
            return [self._normalize_job(item, defaults)]

        # Unknown format
        self.warnings.append(f"Unknown workflow item format: {list(item.keys())}")
        return []

    def _expand_shorthand(
        self, job_type: str, item: dict[str, Any], defaults: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Expand shorthand job syntax to full job definition.

        Examples:
            visit: https://example.com
            wait: 10s
            click: //button[@id='submit']
            type:
              text: hello
              selector: //input
              submit: true

        Args:
            job_type: The job type (visit, wait, click, etc.)
            item: The workflow item containing the shorthand
            defaults: Merged defaults

        Returns:
            Full job definition
        """
        value = item[job_type]
        api_type = JobRegistry.get_api_type(job_type)

        if api_type is None:
            self.errors.append(f"Unknown job type: {job_type}")
            api_type = job_type + "job"

        job = {
            "name": self._generate_job_name(job_type),
            "type": api_type,
            "description": f"Auto-generated {job_type} job",
            "active": True,
            "behaviors": [],
        }

        # Handle different shorthand formats
        if job_type == "visit":
            job["behaviors"] = self._make_behaviors(
                {"url": value, "captchaMode": defaults.get("captcha_mode", "never")}
            )

        elif job_type == "wait":
            seconds = self._parse_duration(value)
            job["behaviors"] = self._make_behaviors(
                {"seconds": seconds, "captchaMode": defaults.get("captcha_mode", "never")}
            )

        elif job_type in ["click", "try_click"]:
            if isinstance(value, str):
                job["behaviors"] = self._make_behaviors(
                    {
                        "selector": value,
                        "selectionType": defaults.get("selection_type", "XPATH"),
                        "captchaMode": defaults.get("captcha_mode", "never"),
                    }
                )
            elif isinstance(value, dict):
                behaviors = {
                    "selector": value.get("selector"),
                    "selectionType": value.get(
                        "selection_type", defaults.get("selection_type", "XPATH")
                    ),
                    "captchaMode": value.get("captcha_mode", defaults.get("captcha_mode", "never")),
                }
                if "task" in value:
                    behaviors["task"] = value["task"]
                if "flag" in value:
                    behaviors["flag"] = value["flag"]
                job["behaviors"] = self._make_behaviors(behaviors)

        elif job_type in ["type", "try_type"]:
            if isinstance(value, dict):
                behaviors = {
                    "text": value.get("text", ""),
                    "selector": value.get("selector", ""),
                    "selectionType": value.get(
                        "selection_type", defaults.get("selection_type", "XPATH")
                    ),
                    "typingMode": value.get(
                        "typing_mode", defaults.get("typing_mode", "SIMULATED_FIXINGTYPOS")
                    ),
                    "appendReturn": str(value.get("submit", False)).upper(),
                    "captchaMode": value.get("captcha_mode", defaults.get("captcha_mode", "never")),
                }
                job["behaviors"] = self._make_behaviors(behaviors)
            else:
                self.errors.append("'type' job requires dict with 'text' and 'selector'")

        elif job_type in ["scroll", "try_scroll"]:
            duration = (
                self._parse_duration(value)
                if isinstance(value, (str, int))
                else value.get("duration", 10)
            )
            direction = "DOWN" if not isinstance(value, dict) else value.get("direction", "DOWN")
            job["behaviors"] = self._make_behaviors(
                {
                    "duration": duration,
                    "scrollDirection": direction.upper(),
                    "captchaMode": defaults.get("captcha_mode", "never"),
                }
            )

        elif job_type in ["scroll_length", "try_scroll_length"]:
            if isinstance(value, dict):
                job["behaviors"] = self._make_behaviors(
                    {
                        "length": value.get("pixels", value.get("length", 500)),
                        "scrollDirection": value.get("direction", "DOWN").upper(),
                        "captchaMode": defaults.get("captcha_mode", "never"),
                    }
                )
            else:
                job["behaviors"] = self._make_behaviors(
                    {
                        "length": int(value),
                        "scrollDirection": "DOWN",
                        "captchaMode": defaults.get("captcha_mode", "never"),
                    }
                )

        elif job_type == "lambda_select":
            if isinstance(value, dict):
                behaviors = {
                    "task": value.get("task", ""),
                    "linkType": value.get("link_type", "FUNCTION_NAME"),
                    "linkValue": value.get("function", value.get("link_value", "")),
                    "captchaMode": value.get("captcha_mode", defaults.get("captcha_mode", "never")),
                }
                # Treatment via the behaviours channel: an explicit `strategy`
                # selects the decision strategy, and `treatment` carries its config.
                # Both become job behaviours the decision function reads (it uses
                # them only when the `strategy` marker is present). Job-control keys
                # win over treatment keys, so treatment cannot clobber task/linkType.
                if value.get("strategy"):
                    behaviors["strategy"] = value["strategy"]
                treatment = value.get("treatment")
                if isinstance(treatment, dict):
                    for key, treatment_value in treatment.items():
                        if key not in behaviors:
                            behaviors[key] = treatment_value
                job["behaviors"] = self._make_behaviors(behaviors)
            else:
                self.errors.append("'lambda_select' requires dict with 'task' and 'function'")

        elif job_type in ["handle_alert", "try_handle_alert"]:
            action = value if isinstance(value, str) else value.get("action", "accept")
            job["behaviors"] = self._make_behaviors(
                {"action": action, "captchaMode": defaults.get("captcha_mode", "never")}
            )

        elif job_type in ["switch", "try_switch", "single_select", "single_select_url"]:
            if isinstance(value, str):
                job["behaviors"] = self._make_behaviors(
                    {
                        "selector": value,
                        "selectionType": defaults.get("selection_type", "XPATH"),
                        "captchaMode": defaults.get("captcha_mode", "never"),
                    }
                )
            elif isinstance(value, dict):
                job["behaviors"] = self._make_behaviors(
                    {
                        "selector": value.get("selector", value.get("target", "")),
                        "selectionType": value.get(
                            "selection_type", defaults.get("selection_type", "XPATH")
                        ),
                        "captchaMode": value.get(
                            "captcha_mode", defaults.get("captcha_mode", "never")
                        ),
                    }
                )

        return job

    def _expand_repeat(
        self, item: dict[str, Any], defaults: dict[str, Any], templates: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        Expand a repeat block.

        Example:
            repeat: 10
              template: watch_and_select

        Args:
            item: Repeat block definition
            defaults: Merged defaults
            templates: Template definitions

        Returns:
            List of repeated jobs
        """
        count = item.get("repeat", 1)
        jobs = []

        # Get the template or jobs to repeat
        if "template" in item:
            template_name = item["template"]
            template_jobs = self._get_template_jobs(template_name, templates, defaults)
            for _ in range(count):
                jobs.extend(copy.deepcopy(template_jobs))
        elif "jobs" in item:
            for _ in range(count):
                for job in item["jobs"]:
                    expanded = self._expand_workflow_item(job, defaults, templates)
                    jobs.extend(copy.deepcopy(expanded))

        return jobs

    def _expand_template_ref(
        self, item: dict[str, Any], defaults: dict[str, Any], templates: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        Expand a template reference.

        Example:
            template: cookie_consent
            with:
              button_text: "Accept All"

        Args:
            item: Template reference
            defaults: Merged defaults
            templates: Template definitions

        Returns:
            List of jobs from template
        """
        template_name = item["template"]
        variables = item.get("with", {})

        template_jobs = self._get_template_jobs(template_name, templates, defaults)

        # Apply variable substitution if provided
        if variables:
            template_jobs = self._apply_variables(template_jobs, variables)

        return template_jobs

    def _get_template_jobs(
        self, template_name: str, templates: dict[str, Any], defaults: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Get expanded jobs from a template."""
        if template_name not in templates:
            self.errors.append(f"Template not found: {template_name}")
            return []

        template = templates[template_name]

        # Template can be a list of workflow items
        if isinstance(template, list):
            return self._expand_workflow(template, defaults, templates)
        else:
            self.warnings.append(f"Template '{template_name}' has unexpected format")
            return []

    def _apply_variables(
        self, jobs: list[dict[str, Any]], variables: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        Apply variable substitution to jobs.

        Note: We use a recursive approach instead of YAML round-trip to avoid
        issues with YAML interpreting substituted values (e.g., {var} as a dict).
        """
        substituted: list[dict[str, Any]] = self._substitute_in_structure(jobs, variables)
        return substituted

    def _substitute_in_structure(self, obj: Any, variables: dict[str, Any]) -> Any:
        """Recursively substitute variables in any data structure."""
        if isinstance(obj, str):
            result = obj
            for key, value in variables.items():
                result = result.replace(f"${{{key}}}", str(value))
                result = result.replace(f"${key}", str(value))
            return result
        elif isinstance(obj, dict):
            return {k: self._substitute_in_structure(v, variables) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._substitute_in_structure(item, variables) for item in obj]
        else:
            return obj

    def _normalize_job(self, job: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
        """Normalize a full job definition."""
        normalized = copy.deepcopy(job)

        if "name" not in normalized:
            normalized["name"] = self._generate_job_name(job.get("type", "unknown"))
        if "description" not in normalized:
            normalized["description"] = f"Job: {normalized['name']}"
        if "active" not in normalized:
            normalized["active"] = True

        # Convert behaviors from dict to list format if needed
        if "behaviors" in normalized and isinstance(normalized["behaviors"], dict):
            normalized["behaviors"] = self._make_behaviors(normalized["behaviors"])

        return normalized

    def _make_behaviors(self, behavior_dict: dict[str, Any]) -> list[dict[str, Any]]:
        """Convert behavior dict to API format list."""
        behaviors = []
        for name, value in behavior_dict.items():
            if value is not None:
                behaviors.append({"name": name, "value": value, "description": "", "active": True})
        return behaviors

    def _generate_job_name(self, job_type: str) -> str:
        """Generate a human-readable job name."""
        name_map = {
            "visit": "Visit",
            "visitjob": "Visit",
            "wait": "Wait",
            "waitjob": "Wait",
            "click": "Click",
            "clickjob": "Click",
            "try_click": "Try Click",
            "tryclickjob": "Try Click",
            "type": "Type",
            "entertextfieldjob": "Type",
            "scroll": "Scroll",
            "scrollbydurationjob": "Scroll",
            "scroll_length": "Scroll by Length",
            "scrollbylengthjob": "Scroll by Length",
            "try_scroll": "Try Scroll",
            "tryscrollbydurationjob": "Try Scroll",
            "try_scroll_length": "Try Scroll by Length",
            "tryscrollbylengthjob": "Try Scroll by Length",
            "lambda_select": "Lambda Select",
            "lambdasingleselectjob": "Lambda Select",
        }
        return name_map.get(job_type, job_type.replace("job", "").replace("_", " ").title())

    def _parse_duration(self, value: str | int | float) -> int:
        """
        Parse duration string to seconds.

        Examples:
            "10s" -> 10
            "5m" -> 300
            "1h" -> 3600
            10 -> 10
        """
        if isinstance(value, (int, float)):
            return int(value)

        if isinstance(value, str):
            value = value.strip().lower()

            # Try to match duration pattern
            match = re.match(
                r"^(\d+(?:\.\d+)?)\s*(s|sec|seconds?|m|min|minutes?|h|hr|hours?)?$", value
            )
            if match:
                num = float(match.group(1))
                unit = match.group(2) or "s"

                if unit.startswith("h"):
                    return int(num * 3600)
                elif unit.startswith("m"):
                    return int(num * 60)
                else:
                    return int(num)

            # Try plain number
            try:
                return int(float(value))
            except ValueError:
                self.warnings.append(f"Could not parse duration: {value}, using 0")
                return 0

        return 0
