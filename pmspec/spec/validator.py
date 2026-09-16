"""
Spec Validator - Validate experiment specifications.

Performs:
1. JSON Schema validation (structural)
2. Semantic validation (business rules)
3. Design validation (factorial completeness)
"""

import json
import os
import re
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:
    import jsonschema

    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

from .draws import is_draw, is_valid_name, select_error
from .drops import Drops
from .job_registry import JobRegistry
from .time_utils import is_relative_time

_TEMPLATE_VAR_RE = re.compile(r"\$\{(?!secret:)[^}]*\}|\{[A-Za-z_][A-Za-z0-9_]*(?::[^}]*)?\}")
_INDEX_RE = re.compile(r"\[\d+\]")
# A `{name}` / `{name:fmt}` design placeholder (not a `${var}` template
# variable), capturing the name -- see _referenced_placeholders.
_PLACEHOLDER_RE = re.compile(r"(?<!\$)\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}")
# `{crawler.name}` inside a data-pool `score:`/`filter:` expression: the
# ExpressionEvaluator reads it from the crawler's configuration.params (see
# SpecConverter.convert, which builds that context before the sampled-name
# strip runs), so it consumes a sampled parameter just as `{name}` does.
_CRAWLER_REF_RE = re.compile(r"\{crawler\.([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class ValidationResult:
    """Result of validation."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)
    dropped: int = 0


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

    # Registry behavior name -> YAML shorthand keys that can supply it.
    #
    # Bridges two namespaces: JobRegistry BehaviorSchema names are the *API*
    # behavior names emitted after expansion, while the validator sees the raw
    # YAML mapping, whose keys spec/parser.py::_expand_shorthand deliberately
    # renames (function -> linkValue, pixels -> length, target/selector ->
    # selector). Behaviors not listed here are looked up under their own name.
    #
    # Note switch/try_switch: the registry calls the required behavior 'target'
    # but the parser always emits 'selector' and accepts either YAML key, so
    # both are listed. Keep this table in sync with _expand_shorthand.
    _BEHAVIOR_KEYS: dict[str, dict[str, tuple[str, ...]]] = {
        "switch": {"target": ("target", "selector")},
        "try_switch": {"target": ("target", "selector")},
        "single_select": {"selector": ("selector", "target")},
        "single_select_url": {"selector": ("selector", "target")},
        "scroll_length": {"length": ("pixels", "length")},
        "lambda_select": {"linkValue": ("function", "link_value")},
    }

    # The decision function reads these four as int(round(x)) == 1, so they are
    # binary switches, not intensities: 0.5 rounds to 0 and silently disables the
    # arm. See CLAUDE.md and docs/integration-contract.md.
    _SWITCH_PARAMS: tuple[str, ...] = ("pi", "psi", "kappa", "tau")

    # Params the current converter/API/runner/decision-function pipeline actually
    # carries. Anything else survives the DSL but is dropped downstream today, so
    # it is reported as a drop rather than silently lost. (Making this channel
    # generic is the "flexible treatment" work; until then, unknown keys do not
    # arrive.)
    _KNOWN_PARAMS: frozenset[str] = frozenset(
        {
            "pi",
            "psi",
            "kappa",
            "tau",
            "alpha",
            "beta",
            "active",
            "search_terms",
            "media_outlet_urls",
        }
    )

    # Keys the parser/converter/design-expander actually read, per level. These
    # are the whitelist for _validate_unknown_keys, and also the authority
    # schema_gen.py builds experiment-spec.schema.json's `additionalProperties:
    # false` object definitions from -- the two cannot drift apart again (see
    # test_schema_gen.py). A key that is missing here but read somewhere is a
    # false error, bounded by the example/research-spec whitelist test in
    # test_strict_contract.py.
    _KEYS_ROOT: frozenset[str] = frozenset(
        {
            "version",
            "experiment",
            "defaults",
            "templates",
            "design",
            "data_pools",
            "strategy_schemas",
            "crawlers",
        }
    )
    _KEYS_EXPERIMENT: frozenset[str] = frozenset(
        {"name", "description", "contact", "runner_user_id", "generator"}
    )
    _KEYS_CRAWLER: frozenset[str] = frozenset(
        {
            "from_design",
            "name",
            "description",
            "active",
            "testing",
            "configuration",
            "proxy",
            "agent",
            "session",
            "additional_sessions",
            "queues",
        }
    )
    # A *runs* spec (one that names a fleet lock with `runs_for:`) has its own
    # whitelists. It adds sessions to bots the base lock already randomized, so
    # everything that describes a bot is absent here -- and absent as an
    # *error*, not a drop: an author who wrote `agent:` on a runs crawler meant
    # to write a fleet spec, and silently dropping it would upload sessions
    # against a bot configured differently than the spec says.
    _KEYS_ROOT_RUNS: frozenset[str] = frozenset(
        {
            "version",
            "runs_for",
            "defaults",
            "templates",
            "data_pools",
            "strategy_schemas",
            "crawlers",
        }
    )
    _KEYS_CRAWLER_RUNS: frozenset[str] = frozenset(
        {
            "from_base",
            "name",
            "description",
            "session",
            "additional_sessions",
            "queues",
            "updates",
            "assign",
        }
    )
    # A runs spec's `defaults:` may only carry what a *session* reads; a
    # `location:` or `proxy:` there would describe bots it does not own.
    _KEYS_DEFAULTS_RUNS: frozenset[str] = frozenset(
        {
            "timezone",
            "timezone_utc_offset",
            "captcha_mode",
            "typing_mode",
            "selection_type",
            "active",
        }
    )
    # Root blocks a runs spec refuses outright, with why.
    _RUNS_ROOT_REFUSALS: dict[str, str] = {
        "design": (
            "a runs spec adds sessions to a fleet the base lock already randomized; "
            "design lives in the fleet spec"
        ),
        "experiment": (
            "a runs spec takes its experiment from the base lock; remove the experiment block"
        ),
    }
    _RUNS_CRAWLER_REFUSALS: tuple[str, ...] = ("agent", "proxy", "configuration")

    _KEYS_SESSION: frozenset[str] = frozenset(
        {
            "name",
            "description",
            "start",
            "start_at",
            "timezone",
            "timezone_utc_offset",
            "active",
            "workflow",
            "jobs",
        }
    )
    _KEYS_PROXY: frozenset[str] = frozenset(
        {
            "name",
            "description",
            "hostname",
            "host",
            "port",
            "username",
            "password",
            "type",
            "active",
            "location",
            "from_pool",
            "pool",
            "match_field",
        }
    )
    _KEYS_AGENT: frozenset[str] = frozenset(
        {
            "name",
            "description",
            "identification",
            "location",
            "multilogin_id",
            "multilogin_profile",
            "active",
            "custom_start_urls",
        }
    )
    _KEYS_CONFIGURATION: frozenset[str] = frozenset(
        {"name", "description", "params", "preferences", "active"}
    )
    # `proxy` is read by SpecConverter._convert_crawler as the fallback for a
    # crawler without its own proxy block (and the example specs use it).
    _KEYS_DEFAULTS: frozenset[str] = frozenset(
        {
            "location",
            "browser",
            "timezone",
            "timezone_utc_offset",
            "captcha_mode",
            "proxy_type",
            "typing_mode",
            "selection_type",
            "active",
            "proxy",
        }
    )
    _KEYS_DESIGN: frozenset[str] = frozenset(
        {"type", "seed", "factors", "parameters", "replications", "total_crawlers"}
    )
    # The only keys SpecParser._expand_workflow_item reads on a template/repeat
    # item (see _expand_template_ref / _expand_repeat).
    _TEMPLATE_ITEM_KEYS: frozenset[str] = frozenset({"template", "repeat", "with", "jobs"})

    # The only keys SpecParser._normalize_job reads on a full job definition
    # (`type:` holding a registered API type string, e.g. `visitjob` --
    # JobRegistry.is_api_type -- as opposed to the `type:` *shorthand*, whose
    # value is a dict or a non-API-type string).
    _FULL_JOB_KEYS: frozenset[str] = frozenset(
        {"type", "name", "description", "active", "behaviors"}
    )

    # The only keys DesignExpander._parse_factor reads on a design factor
    # definition. Note there is no per-factor "description" or "proportions":
    # a level's own weight is `levels[i].proportion` (singular, nested inside
    # the level item), not a factor-level key.
    _KEYS_FACTOR: frozenset[str] = frozenset({"levels", "maps_to"})

    # Per-shorthand dict-form option keys SpecParser._expand_shorthand actually
    # reads (verified against every branch, not copied from job_registry.py's
    # BehaviorSchema names, which are the *API* behavior names emitted after
    # expansion -- see _BEHAVIOR_KEYS above for that distinction). Used to build
    # the generated JSON schema's per-shorthand `options` object (schema_gen.py)
    # and by _validate_job_options to report an option key outside this set as
    # a drop ("the runner never sees it").
    #
    # visit/wait have no dict form at all today: _expand_shorthand uses the
    # scalar value directly (visit) or only accepts int/float/str
    # (wait/_parse_duration), so a dict value here is silently mishandled, not
    # read key-by-key.
    _OPTION_KEYS: dict[str, frozenset[str]] = {
        "visit": frozenset(),
        "wait": frozenset(),
        "click": frozenset({"selector", "selection_type", "captcha_mode", "task", "flag"}),
        "try_click": frozenset({"selector", "selection_type", "captcha_mode", "task", "flag"}),
        "type": frozenset(
            {"text", "selector", "selection_type", "typing_mode", "submit", "captcha_mode"}
        ),
        "try_type": frozenset(
            {"text", "selector", "selection_type", "typing_mode", "submit", "captcha_mode"}
        ),
        "scroll": frozenset({"duration", "direction"}),
        "try_scroll": frozenset({"duration", "direction"}),
        "scroll_length": frozenset({"pixels", "length", "direction"}),
        "try_scroll_length": frozenset({"pixels", "length", "direction"}),
        "lambda_select": frozenset(
            {"task", "link_type", "function", "link_value", "captcha_mode", "strategy", "treatment"}
        ),
        # switch/try_switch/single_select/single_select_url share one code
        # branch in _expand_shorthand, so they share one option-key set.
        "single_select": frozenset({"selector", "target", "selection_type", "captcha_mode"}),
        "single_select_url": frozenset({"selector", "target", "selection_type", "captcha_mode"}),
        "switch": frozenset({"selector", "target", "selection_type", "captcha_mode"}),
        "try_switch": frozenset({"selector", "target", "selection_type", "captcha_mode"}),
        "handle_alert": frozenset({"action"}),
        "try_handle_alert": frozenset({"action"}),
    }

    def __init__(
        self,
        schema_path: str | None = None,
        *,
        lax: bool = False,
        base_dir: Path | None = None,
        runs: bool = False,
    ):
        """
        Initialize validator.

        Args:
            schema_path: Path to JSON Schema file (uses default if not specified)
            lax: Report drops as warnings-plus-count instead of errors
            base_dir: Directory a spec-relative ``strategy_schemas`` path is resolved
                against (the spec's own directory for the CLI, a materialised temp
                dir for the MCP). Defaults to the current working directory.
            runs: Validate as a *runs* spec (one carrying ``runs_for:``) -- the
                runs key sets above, and the refusals that go with them.
        """
        self.schema = None
        self.lax = lax
        self.base_dir = base_dir
        self.runs = runs
        self.drops = Drops(lax)
        self._strategy_schemas: dict[str, Any] = {}
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
        self.drops = Drops(self.lax)
        self._strategy_schemas = spec.get("strategy_schemas") or {}

        # 0. Unknown keys (first, so the message a reader sees first names the typo)
        self._validate_unknown_keys(spec)

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

        # 3b. Runs-spec refusals (a construct that belongs to the fleet spec)
        if self.runs:
            errors.extend(self._validate_runs(spec))

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

        # 7. Treatment-param validation (0/1 switch semantics + unknown-key drops)
        switch_errors, switch_warnings = self._validate_treatment_params(spec)
        errors.extend(switch_errors)
        warnings.extend(switch_warnings)

        self.drops.merge_into(errors, warnings)

        return ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            info=info,
            dropped=self.drops.count,
        )

    @staticmethod
    def _is_switch_value(value: Any) -> bool:
        """True only for a numeric 0 or 1 (the two meaningful switch values)."""
        if isinstance(value, bool):
            return True  # True/False == 1/0
        try:
            return float(value) in (0.0, 1.0)
        except (TypeError, ValueError):
            return False

    def _validate_treatment_params(self, spec: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Validate treatment params: pi/psi/kappa/tau are 0/1 switches, and any
        param outside the pipeline's known set is dropped downstream (a drop).

        Checks both the design's ``maps_to`` level values and each crawler's
        direct ``configuration.params``. This is the one guard that catches the
        classic ``{left: -0.5, center: 0, right: 0.5}`` mistake, where every level
        rounds to 0 and the "treatment" is three identical control arms.
        """
        errors: list[str] = []
        warnings: list[str] = []

        design = spec.get("design") or {}
        for fname, fdef in (design.get("factors") or {}).items():
            if not isinstance(fdef, dict):
                continue  # _validate_design has already reported it as an error
            for path, level_map in (fdef.get("maps_to") or {}).items():
                parts = str(path).split(".")
                leaf = parts[-1]
                if not isinstance(level_map, dict):
                    continue
                if leaf in self._SWITCH_PARAMS:
                    for level, value in level_map.items():
                        if not self._is_switch_value(value):
                            errors.append(
                                f"design.factors.{fname}.maps_to.{path}: level '{level}' -> "
                                f"{value!r} is not a 0/1 switch value. '{leaf}' is read as "
                                "int(round(x))==1, so only 0 or 1 are meaningful (e.g. 0.5 "
                                "rounds to 0 and silently disables the treatment arm)."
                            )
                elif "params" in parts and leaf not in self._KNOWN_PARAMS:
                    self.drops.report(
                        f"design.factors.{fname}.maps_to.{path}",
                        f"'{leaf}' is not carried by the API/runner/decision-function pipeline",
                        "route it through a template `with:` variable or a lambda_select "
                        "`strategy` + `treatment` block",
                    )

        # A sampled parameter (design.parameters.<name>) is written by
        # DesignExpander._apply_cell_to_template into configuration.params.<name>
        # of every design crawler. For a standard name that is the point; for
        # any other name the API drops the key, so the draw reaches nobody --
        # unless the spec also uses it as a `{name}` / `{name:fmt}` substitution
        # variable, in which case the value has already landed where it was
        # referenced and SpecConverter._convert_configuration strips the
        # leftover params key (see `sampled_substitution_params`).
        parameters = design.get("parameters")
        if isinstance(parameters, dict):
            non_standard = [name for name in parameters if name not in self._KNOWN_PARAMS]
            if non_standard:
                referenced = self._referenced_placeholders(spec)
                for name in non_standard:
                    if name not in referenced:
                        self.drops.report(
                            f"design.parameters.{name}",
                            "sampled value lands in configuration.params, which the API drops",
                            f"use it as a {{{name}}} substitution variable or give it a "
                            "standard name",
                        )

        for i, crawler in enumerate(spec.get("crawlers", [])):
            params = (crawler.get("configuration") or {}).get("params")
            if not isinstance(params, dict):
                continue
            for key, value in params.items():
                if key in self._SWITCH_PARAMS:
                    if not self._is_switch_value(value):
                        errors.append(
                            f"crawler[{i}].configuration.params.{key}: {value!r} is not 0 or 1 "
                            f"('{key}' is a switch read as int(round(x))==1; 0.5 rounds to 0)."
                        )
                elif key not in self._KNOWN_PARAMS:
                    self.drops.report(
                        f"crawler[{i}].configuration.params.{key}",
                        "unknown parameter, dropped by the pipeline",
                        "only pi/psi/kappa/tau/alpha/beta/active/search_terms/media_outlet_urls "
                        "reach the bot; use a configuration preference or a treatment block",
                    )

        return errors, warnings

    @staticmethod
    def _reachable_templates(spec: dict[str, Any]) -> dict[str, Any]:
        """The templates some crawler actually calls, transitively.

        A `template:` item anywhere under crawlers (a workflow item, or one
        nested in `repeat.jobs`) names a template; a template body is itself a
        list of workflow items expanded through the same
        `SpecParser._expand_workflow_item`, so it can call further templates.
        A template nobody reaches is never spliced into any crawler, and a
        placeholder in it is consumed by nobody.
        """
        templates = spec.get("templates")
        if not isinstance(templates, dict):
            return {}

        def refs(node: Any, out: set[str]) -> None:
            if isinstance(node, dict):
                name = node.get("template")
                if isinstance(name, str):
                    out.add(name)
                for key, value in node.items():
                    if key != "multilogin_profile":
                        refs(value, out)
            elif isinstance(node, list):
                for value in node:
                    refs(value, out)

        pending: set[str] = set()
        refs(spec.get("crawlers"), pending)
        reached: dict[str, Any] = {}
        while pending:
            name = pending.pop()
            if name in reached or name not in templates:
                continue
            reached[name] = templates[name]
            refs(templates[name], pending)
        return reached

    @classmethod
    def _referenced_placeholders(cls, spec: dict[str, Any]) -> set[str]:
        """Names used as `{name}` / `{name:fmt}`, or as `{crawler.name}` in a
        pool expression, under crawlers or a template some crawler reaches.

        `{crawler.name}` counts because the converter resolves pool references
        from a context built out of `configuration.params` *before*
        `_convert_configuration` strips the sampled name (`SpecConverter.convert`
        orders it that way on purpose), so a draw used only in a `score:` or
        `filter:` expression has already done its work by the time the key is
        removed. Reached templates count because the parser splices them into the
        crawler's workflow before the expander substitutes, so `{alpha_draw}`
        in such a template body is consumed exactly like one written on the
        crawler. An orphan template (`_reachable_templates`) does not: its
        placeholder would satisfy the rule while the draw reached nobody, and
        the converter would then strip the value from params -- a silent drop
        with extra steps. `${var}` template variables are a different
        namespace (excluded by the lookbehind), and the Multilogin profile is a
        JSON document with its own placeholders, skipped as `_scan_unresolved`
        skips it.
        """
        names: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, str):
                for m in _PLACEHOLDER_RE.finditer(node):
                    names.add(m.group(1))
                for m in _CRAWLER_REF_RE.finditer(node):
                    names.add(m.group(1))
            elif isinstance(node, dict):
                for key, value in node.items():
                    if key != "multilogin_profile":
                        walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(spec.get("crawlers"))
        walk(list(cls._reachable_templates(spec).values()))
        return names

    def _validate_unknown_keys(self, spec: dict[str, Any]) -> None:
        """Keys the pipeline never reads are dropped silently; report them.

        This is still the authority for these key sets (not
        ``additionalProperties`` on the JSON schema, even though schema_gen.py
        now builds the schema's object definitions from these same
        ``_KEYS_*`` frozensets and can therefore no longer disagree with this
        function -- see `_validate_schema`'s docstring). Workflow items are
        not checked here: an unknown job *type*, and a stray sibling key on a
        matched shorthand item, are `_validate_workflow_item`'s job; option
        keys inside a known job vary per type and false positives there would
        cost more than the rare typo they would catch.
        """

        def check(obj: Any, allowed: AbstractSet[str], path: str) -> None:
            if not isinstance(obj, dict):
                return
            for key in obj:
                if key not in allowed:
                    self.drops.report(
                        f"{path}.{key}" if path else str(key),
                        "unknown key",
                        "remove it or fix the spelling",
                    )

        root_keys: AbstractSet[str]
        crawler_keys: AbstractSet[str]
        defaults_keys: AbstractSet[str]
        if self.runs:
            # The refused blocks are named by `_validate_runs` as errors; they
            # must not also be reported here as drops.
            root_keys = self._KEYS_ROOT_RUNS | set(self._RUNS_ROOT_REFUSALS)
            crawler_keys = self._KEYS_CRAWLER_RUNS | set(self._RUNS_CRAWLER_REFUSALS)
            defaults_keys = self._KEYS_DEFAULTS_RUNS
        else:
            root_keys = self._KEYS_ROOT
            crawler_keys = self._KEYS_CRAWLER
            defaults_keys = self._KEYS_DEFAULTS

        check(spec, root_keys, "")
        check(spec.get("experiment"), self._KEYS_EXPERIMENT, "experiment")
        if not self.runs:
            # In runs mode a defaults key outside the session-level set is an
            # error (`_validate_runs`), not a drop.
            check(spec.get("defaults"), defaults_keys, "defaults")
        check(spec.get("design"), self._KEYS_DESIGN, "design")
        check((spec.get("defaults") or {}).get("proxy"), self._KEYS_PROXY, "defaults.proxy")
        for i, crawler in enumerate(spec.get("crawlers") or []):
            prefix = f"crawler[{i}]"
            check(crawler, crawler_keys, prefix)
            if not isinstance(crawler, dict):
                continue
            check(crawler.get("configuration"), self._KEYS_CONFIGURATION, f"{prefix}.configuration")
            check(crawler.get("proxy"), self._KEYS_PROXY, f"{prefix}.proxy")
            check(crawler.get("agent"), self._KEYS_AGENT, f"{prefix}.agent")
            check(crawler.get("session"), self._KEYS_SESSION, f"{prefix}.session")
            for j, extra in enumerate(crawler.get("additional_sessions") or []):
                check(extra, self._KEYS_SESSION, f"{prefix}.additional_sessions[{j}]")
            for j, queue in enumerate(crawler.get("queues") or []):
                check(queue, self._KEYS_SESSION, f"{prefix}.queues[{j}]")

    def _validate_runs(self, spec: dict[str, Any]) -> list[str]:
        """Refusals that only apply to a runs spec.

        Errors, never drops. Each names a construct that belongs to the fleet
        spec: dropping it would leave a spec that compiles and means something
        other than what its author wrote.
        """
        errors: list[str] = []

        for key, why in self._RUNS_ROOT_REFUSALS.items():
            if key in spec:
                errors.append(f"{key}: {why}")

        defaults = spec.get("defaults")
        if isinstance(defaults, dict):
            allowed = ", ".join(sorted(self._KEYS_DEFAULTS_RUNS))
            for key in defaults:
                if key not in self._KEYS_DEFAULTS_RUNS:
                    errors.append(
                        f"defaults.{key}: a runs spec's defaults may only carry session-level "
                        f"keys ({allowed})"
                    )

        for i, crawler in enumerate(spec.get("crawlers") or []):
            if not isinstance(crawler, dict):
                continue
            prefix = f"crawler[{i}]"
            for key in self._RUNS_CRAWLER_REFUSALS:
                if key in crawler:
                    errors.append(
                        f"{prefix}.{key}: a runs spec adds sessions to existing bots; "
                        "agent/proxy/configuration live in the fleet spec"
                    )
            errors.extend(self._validate_updates(crawler.get("updates"), prefix))

        return errors

    def _validate_updates(self, updates: Any, prefix: str) -> list[str]:
        """``updates: {preferences: {<name>: <scalar>}}`` and nothing else.

        Params, agent and proxy fields are refused rather than dropped:
        changing those mid-study is a different experiment, and the upload
        path has no way to apply them.
        """
        if updates is None:
            return []
        if not isinstance(updates, dict):
            return [f"{prefix}.updates: must be a mapping of preferences"]
        errors: list[str] = []
        for key, value in updates.items():
            if key != "preferences":
                errors.append(f"{prefix}.updates.{key}: only preferences can be updated")
                continue
            if not isinstance(value, dict):
                errors.append(f"{prefix}.updates.preferences: must be a mapping of name -> value")
                continue
            for name, pref_value in value.items():
                if isinstance(pref_value, (dict, list)):
                    errors.append(f"{prefix}.updates.preferences.{name}: must be a scalar value")
        return errors

    def _validate_schema(self, spec: dict[str, Any]) -> list[str]:
        """Validate against JSON Schema.

        schema_gen.py builds the schema's `additionalProperties: false` object
        definitions from the exact key sets two other, lax-aware, Python-level
        checks already use, so an ``additionalProperties`` violation here is
        never new information:

        - root, ``experiment``, ``defaults``, ``design``, each crawler, its
          ``configuration``/``proxy``/``agent``/``session`` (and
          ``defaults.proxy``) -- ``_validate_unknown_keys``.
        - a workflow item's job-shorthand key and its stray sibling keys --
          ``_validate_workflow_item``.

        Surfacing the same violation again here too, as a hard error
        jsonschema has no concept of ``--lax`` for, would make a spec whose
        only problem is a lax-downgraded drop still fail ``--lax``
        validation. So ``additionalProperties`` errors are filtered out
        below; every other kind of schema violation (required/type/enum/
        pattern/...) is not covered by those checks and is still reported as
        before.

        ``design.factors.<name>``'s own keys (the ``factor`` definition, from
        `_KEYS_FACTOR`) are covered the same way: `_validate_design` reports
        a key outside `_KEYS_FACTOR` as a drop (``design.factors.<f>.<key>``),
        so filtering the schema's ``additionalProperties`` error for it loses
        nothing.
        """
        if not HAS_JSONSCHEMA or not self.schema:
            return []

        errors = []
        try:
            validator = jsonschema.Draft7Validator(self.schema)
            relevant = (
                e for e in validator.iter_errors(spec) if e.validator != "additionalProperties"
            )
            best = jsonschema.exceptions.best_match(relevant)
            if best is not None:
                path = (
                    ".".join(str(p) for p in best.absolute_path) if best.absolute_path else "root"
                )
                errors.append(f"Schema validation error at '{path}': {best.message}")
        except jsonschema.SchemaError as e:
            errors.append(f"Schema error: {e.message}")

        return errors

    def _validate_required_fields(self, spec: dict[str, Any]) -> list[str]:
        """Validate required fields are present."""
        errors = []

        # Experiment name is required -- except in a runs spec, where the
        # experiment is the base lock's and an `experiment:` block is refused.
        if not self.runs and not spec.get("experiment", {}).get("name"):
            errors.append("experiment.name is required")
        if self.runs and not spec.get("runs_for"):
            errors.append("runs_for is required: a runs spec names the fleet lock it adds to")

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
            if not isinstance(factor_def, dict):
                errors.append(f"design.factors.{factor_name}: must be a mapping")
                continue

            # DesignExpander._parse_factor reads exactly `levels` and `maps_to`
            # and nothing else, so `mapsto:` beside `levels:` is a factor with
            # no mapping at all -- every cell gets the same params -- and no
            # message anywhere. A drop.
            for key in factor_def:
                if key not in self._KEYS_FACTOR:
                    self.drops.report(
                        f"design.factors.{factor_name}.{key}",
                        "unknown key; the design expander never reads it",
                        "a factor has only levels and maps_to (a level's weight is "
                        "levels[i].proportion)",
                    )

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
            if not isinstance(factor_def, dict):
                continue  # _validate_design has already reported it as an error
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

            # Validate proxy if defined.
            #
            # A warning, not a drop: nothing the author wrote is discarded; the
            # converter substitutes 127.0.0.1:8080, which upload refuses at the
            # credential check anyway. Skipped for a self-configuring type
            # (oxylabs: the runner supplies endpoint and account, and a spec
            # must NOT carry a host) and for a pooled proxy (from_pool: true,
            # host comes from the pool row).
            proxy = crawler.get("proxy", {})
            if (
                isinstance(proxy, dict)
                and proxy
                and proxy.get("from_pool") is not True
                and proxy.get("type") not in ("oxylabs",)
            ):
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
            for j, extra in enumerate(crawler.get("additional_sessions") or []):
                self._validate_session(
                    extra, f"{prefix}.additional_sessions[{j}]", errors, warnings, spec
                )

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
            self.drops.report(
                prefix, "no start time", "every session needs start: now | +5m | <ISO wall clock>"
            )
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

    def _is_valid_timestamp(self, value: Any) -> bool:
        """
        Check if timestamp format is valid.

        Accepts:
            - "now"
            - Relative offsets: +5m, -1h, +1d, +30s
            - ISO 8601 timestamps: 2025-02-01T10:00:00Z

        An unquoted YAML timestamp (`start: 2026-10-01T10:00:00`) is parsed by
        PyYAML into a `datetime`/`date`, not a `str` -- normalise it to the ISO
        string the author wrote before pattern-matching it.
        """
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
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

        # A template/repeat item: the parser reads exactly these four keys and
        # nothing else, so `weight: 0.7` beside `template: t` is silently ignored.
        if "template" in item or "repeat" in item:
            for key in item:
                if key not in self._TEMPLATE_ITEM_KEYS:
                    self.drops.report(
                        f"{prefix}.{key}",
                        "unknown key on a template/repeat item",
                        "only template, repeat, with and jobs are read here; move it into the "
                        "template's jobs or remove it",
                    )

            # A dict-valued `with:` entry must be a pool draw
            # (from_pool/field, see draws.py) -- anything else is silently
            # stringified by the parser's substitution (`str({...})`), which
            # is a drop.
            with_block = item.get("with")
            if isinstance(with_block, dict):
                for key, value in with_block.items():
                    if not isinstance(value, dict):
                        continue  # a literal template variable, not a draw
                    from_pool, draw_field = value.get("from_pool"), value.get("field")
                    if (
                        isinstance(from_pool, str)
                        and isinstance(draw_field, str)
                        and not (is_valid_name(from_pool) and is_valid_name(draw_field))
                    ):
                        self.drops.report(
                            f"{prefix}.with.{key}",
                            "pool/field names may only contain letters, digits, _ . -",
                            "rename the pool or the field so from_pool/field fit that charset",
                        )
                    elif select_error(value) is not None:
                        self.drops.report(
                            f"{prefix}.with.{key}",
                            f"unknown select {value.get('select')!r}",
                            "select must be 'random' or 'first'",
                        )
                    elif not is_draw(value):
                        self.drops.report(
                            f"{prefix}.with.{key}",
                            "not a valid pool draw (missing/invalid from_pool or field)",
                            "a with: dict value must be "
                            "{from_pool: <pool>, field: <field>, select: random|first}",
                        )
                    elif "count" in value:
                        self.drops.report(
                            f"{prefix}.with.{key}",
                            "count is not accepted on a with: draw",
                            "one value per with: key -- N distinct values are N template calls",
                        )

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
            # The parser expands each nested job through the same item logic, so
            # a bad job type in here is a drop exactly like one at the top level.
            # This is what lets compile_spec suppress the parser's own duplicate
            # report: the validator must see every workflow item the parser does.
            nested = item.get("jobs")
            if isinstance(nested, list):
                for k, sub in enumerate(nested):
                    self._validate_workflow_item(sub, f"{prefix}.jobs[{k}]", errors, warnings, spec)

        if "template" in item or "repeat" in item:
            return  # not a job shorthand; everything on it was checked above

        # A full job definition: `type` holds a registered API type string
        # (e.g. `visitjob`), not a `type:` shorthand value (a dict, or a
        # non-API-type string). Must be checked before the shorthand loop
        # below for the same reason SpecParser._expand_workflow_item checks
        # it first: `type` is also the shorthand key for the `type:` (enter
        # text) job, and a full job's `type: visitjob` would otherwise be
        # validated as that shorthand instead (and fail it, since the value
        # is a string, not a dict). Checked as its own shape entirely
        # separate from the shorthand path below -- an unknown key here is a
        # stray key on the job, not an unrecognised job-shorthand option.
        if JobRegistry.is_api_type(item.get("type")):
            for key in item:
                if key not in self._FULL_JOB_KEYS:
                    self.drops.report(
                        f"{prefix}.{key}",
                        "key not read by a job definition",
                        "a full job has only type, name, description, active and behaviors",
                    )
            return

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

        matched_types = [job_type for job_type in job_types if job_type in item]
        for job_type in matched_types:
            self._validate_job_shorthand(job_type, item[job_type], prefix, errors, warnings)
            self._validate_job_options(job_type, item[job_type], prefix)

        # SpecParser._expand_workflow_item iterates its own shorthand_keys list
        # (the same order as job_types above) and returns on the *first* match
        # -- it never looks at the item again after that. So every other key on
        # a matched item -- a second job-type key (`visit` + `wait` on one
        # item), or an arbitrary typo (`weight: 0.7` beside `visit:`) -- is
        # silently ignored by the parser exactly like a template/repeat item's
        # stray keys above, and is a drop for the same reason. `with`/`jobs`
        # are not exempt: they only pair with template/repeat items (handled
        # above, which return before reaching here), so on a plain shorthand
        # item they too are unread.
        if matched_types:
            used = matched_types[0]
            for key in item:
                if key != used:
                    self.drops.report(
                        f"{prefix}.{key}",
                        f"ignored: only the first job shorthand ('{used}') is used",
                        "one job per workflow item; move it to its own item",
                    )

        # An item that is neither a template/repeat block (returned above) nor a
        # known shorthand is skipped by the parser ("Unknown workflow item
        # format") -- a drop. `with` only accompanies a template reference,
        # `jobs` a repeat block; anything else on such an item is a job type the
        # registry lacks, and an item left with nothing but those has no job at
        # all.
        if not matched_types:
            unknown = [key for key in item if key not in ("with", "jobs")]
            for key in unknown:
                self.drops.report(prefix, f"unknown job type '{key}'", "see `pmspec schema jobs`")
            if not unknown:
                self.drops.report(
                    prefix, "workflow item has no job type", "see `pmspec schema jobs`"
                )

    def _strategy_schema(self, strategy: str, prefix: str) -> dict[str, Any] | None:
        """Resolve a decision strategy's JSON schema, reporting a drop when none can.

        Resolution order:
        1. ``spec["strategy_schemas"][strategy]`` -- an inline dict is the schema
           itself; a string is a path resolved against ``self.base_dir`` (or the
           cwd, if none was given). A declared path that is missing or not JSON
           is its own drop, naming the path -- not "no schema", which would send
           the reader looking in the fallback directories for a file the spec
           already points at.
        2. Each directory in ``PMSPEC_STRATEGY_SCHEMA_DIRS`` (``os.pathsep``
           separated), as ``<dir>/<strategy>.schema.json``.
        3. The two sibling bot-decision-yt locations (decision symlink, then the
           checked-out repo) -- a soft dependency the orchestrator does not
           hard-require.

        Not found returns None after reporting a drop, since the treatment is
        otherwise forwarded to the decision function unchecked.
        """
        declared = self._strategy_schemas.get(strategy)
        if isinstance(declared, dict):
            return declared

        if isinstance(declared, str):
            path = (self.base_dir or Path.cwd()) / declared
            try:
                loaded: dict[str, Any] = json.loads(path.read_text())
                return loaded
            except (OSError, ValueError):
                self.drops.report(
                    f"strategy_schemas.{strategy}",
                    f"declared path '{declared}' is missing or not JSON (resolved to {path})",
                    "fix the path (relative to the spec's directory) or inline the schema",
                )
                return None

        candidates: list[Path] = []
        for d in filter(None, os.environ.get("PMSPEC_STRATEGY_SCHEMA_DIRS", "").split(os.pathsep)):
            candidates.append(Path(d) / f"{strategy}.schema.json")

        repo_root = Path(__file__).resolve().parents[2]
        candidates += [
            repo_root
            / "orchestrator"
            / "lambda_service"
            / "decision"
            / "strategies"
            / f"{strategy}.schema.json",
            repo_root.parent
            / "bot-decision-yt"
            / "bot-decision-EN"
            / "strategies"
            / f"{strategy}.schema.json",
        ]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    found: dict[str, Any] = json.loads(candidate.read_text())
                    return found
            except (OSError, json.JSONDecodeError):
                continue
        # A drop: the treatment is still forwarded to the decision function
        # unchecked, and this validator cannot see whether it is well-formed.
        self.drops.report(
            f"{prefix}.lambda_select",
            f"no schema for strategy '{strategy}', so its treatment cannot be validated",
            "add strategy_schemas: {<name>: <path or inline schema>} or set "
            "PMSPEC_STRATEGY_SCHEMA_DIRS",
        )
        return None

    def _validate_lambda_select_treatment(
        self, value: dict[str, Any], prefix: str, errors: list[str], warnings: list[str]
    ) -> None:
        """Validate a lambda_select job's behaviours-channel treatment.

        The behaviours channel activates only when a ``strategy`` marker is present
        (matching the decision function's gate). When it is, the ``treatment``
        block is validated against that strategy's schema; when a ``treatment`` is
        set without a ``strategy``, it is silently ignored downstream -- a drop.
        """
        strategy = value.get("strategy")
        treatment = value.get("treatment")

        if not strategy:
            if isinstance(treatment, dict) and treatment:
                self.drops.report(
                    f"{prefix}.lambda_select",
                    "'treatment' is set but 'strategy' is not, so the decision function "
                    "ignores it",
                    "add strategy: <name>",
                )
            return

        schema = self._strategy_schema(str(strategy), prefix)
        if schema is None:
            return  # the drop (declared path broken, or nothing found) is already reported

        if not HAS_JSONSCHEMA:
            return

        # A strategy marker with no treatment activates the behaviours channel
        # for a job that carries no arm (a neutral bank shared by every arm);
        # the schema describes the treatment, so there is nothing to check.
        if not isinstance(treatment, dict) or not treatment:
            return
        # Template parameters (`${reliability}`) have no value until a workflow
        # step supplies it with `with:`; the concrete values are validated after
        # expansion by validate_expanded(). Checking the placeholder against an
        # enum here would refuse every templated treatment.
        if any(isinstance(v, str) and _TEMPLATE_VAR_RE.search(v) for v in treatment.values()):
            return

        config = dict(treatment)
        config["strategy"] = strategy
        try:
            jsonschema.validate(config, schema)
        except jsonschema.ValidationError as exc:
            location = ".".join(str(part) for part in exc.absolute_path) or "(root)"
            errors.append(
                f"{prefix}.lambda_select.treatment: invalid for strategy '{strategy}' "
                f"at {location}: {exc.message}"
            )

    def validate_expanded(self, api_crawlers: list[dict[str, Any]]) -> list[str]:
        """Validate every expanded job's treatment against its strategy schema.

        After expansion a lambda_select's treatment is flat: `strategy` and the
        treatment keys sit next to the job-control behaviours (task, linkType,
        linkValue, ...). Only the keys the schema declares are collected, so a
        schema with additionalProperties: false does not see the job controls.
        Identical failures are grouped across crawlers -- a bad template value
        would otherwise be reported once per bot.
        """
        if not HAS_JSONSCHEMA:
            return []
        groups: dict[tuple[str, str], list[str]] = {}
        for i, crawler in enumerate(api_crawlers):
            for j, queue in enumerate(crawler.get("queues") or []):
                for k, job in enumerate(queue.get("jobs") or []):
                    behaviours = {
                        b.get("name"): b.get("value")
                        for b in job.get("behaviors") or []
                        if isinstance(b, dict)
                    }
                    strategy = behaviours.get("strategy")
                    if not strategy:
                        continue
                    schema = self._strategy_schema(str(strategy), f"crawlers[{i}]")
                    if schema is None:
                        continue
                    declared = set((schema.get("properties") or {}).keys()) - {"strategy"}
                    config = {key: behaviours[key] for key in declared if key in behaviours}
                    if not config:
                        continue  # marker-only job
                    config["strategy"] = strategy
                    try:
                        jsonschema.validate(config, schema)
                    except jsonschema.ValidationError as exc:
                        location = ".".join(str(p) for p in exc.absolute_path) or "(root)"
                        path = f"crawlers[{i}].queues[{j}].jobs[{k}]"
                        message = (
                            f"treatment invalid for strategy '{strategy}' at {location}: "
                            f"{exc.message}"
                        )
                        groups.setdefault((_INDEX_RE.sub("[*]", path), message), []).append(path)
        out = []
        for (generic, message), paths in groups.items():
            if len(paths) == 1:
                out.append(f"{paths[0]}: {message}")
            else:
                out.append(f"{generic}: {message} ({len(paths)} place(s), first: {paths[0]})")
        return out

    def _validate_job_options(self, job_type: str, value: Any, prefix: str) -> None:
        """A dict-form option key SpecParser._expand_shorthand never reads is a drop.

        `_expand_shorthand` reads the mapping form of each shorthand with
        `value.get(<key>)` for a fixed set of keys (`_OPTION_KEYS`) and never
        looks at the rest, so `click: {selector: x, sped: fast}` silently loses
        `sped` -- the runner never sees it. visit/wait have no dict form at all
        (`_OPTION_KEYS` is empty for both): a dict value there is already
        rejected by `_validate_job_shorthand`, so it is skipped here rather
        than reported a second time key-by-key. Only the top level is checked:
        lambda_select's `treatment` sub-keys are the strategy schema's business
        (`_validate_lambda_select_treatment`), not option keys.
        """
        if not isinstance(value, dict) or job_type in ("visit", "wait"):
            return
        allowed = self._OPTION_KEYS.get(job_type)
        if allowed is None:  # unreachable: callers pass only registered shorthands
            return
        for key in value:
            if key not in allowed:
                self.drops.report(
                    f"{prefix}.{job_type}.{key}",
                    "unknown option; the runner never sees it",
                    f"{job_type} reads only: {', '.join(sorted(allowed))}",
                )

    def _validate_job_shorthand(
        self, job_type: str, value: Any, prefix: str, errors: list[str], warnings: list[str]
    ) -> None:
        """Validate job shorthand syntax."""
        schema = JobRegistry.get_schema(job_type)
        if not schema:  # unreachable: callers pass only registered types
            self.drops.report(prefix, f"unknown job type '{job_type}'", "see `pmspec schema jobs`")
            return

        errors_before = len(errors)

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
                self._validate_lambda_select_treatment(value, prefix, errors, warnings)
            else:
                errors.append(f"{prefix}.lambda_select: must be an object")

        # Schema-driven completeness check against JobRegistry. Only meaningful
        # for the mapping form - the scalar form (visit: URL, click: "//button")
        # supplies its required behavior positionally.
        #
        # Deliberately a warning, not an error: the parser silently substitutes
        # defaults today (scroll duration=10, scroll_length length=500,
        # handle_alert action="accept", empty selectors), so erroring here would
        # reject specs that validate now. Skipped when the hardcoded checks above
        # already rejected this item, so their messages are not duplicated.
        if isinstance(value, dict) and len(errors) == errors_before:
            aliases = self._BEHAVIOR_KEYS.get(job_type, {})
            for behavior in (b.name for b in schema.behaviors if b.required):
                keys = aliases.get(behavior, (behavior,))
                # Explicit None/"" test, not truthiness: 0 is a legitimate value
                # for numeric behaviors such as scroll length and duration.
                if not any(value.get(k) is not None and value.get(k) != "" for k in keys):
                    warnings.append(
                        f"{prefix}.{job_type}: required behavior '{behavior}' has no value "
                        f"(expected one of: {', '.join(keys)}); "
                        "the generated job will fall back to a default"
                    )

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
        """Placeholder for the pre-expansion ``{var}`` heuristic, now retired.

        The raw-YAML guess ("variable '{x}' may not be defined") could not see
        template ``with:`` bindings or pool fields and was wrong often enough to
        be ignored. Unresolved ``{var}`` / ``${var}`` placeholders are caught
        after expansion instead, on the values that actually reach the API, by
        ``SpecConverter._scan_unresolved``. Kept as a no-op step so validate()'s
        numbered pipeline and its callers stay stable.
        """
        return [], []


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
