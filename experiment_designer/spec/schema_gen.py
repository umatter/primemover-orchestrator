"""
Schema Generator - Build the experiment-spec JSON Schema from the code that
actually reads a spec, so the checked-in schema cannot drift from it again.

Before this module, `experiment_designer/schema/experiment-spec.schema.json`
was hand-written and had drifted: it lacked `data_pools`, `strategy_schemas`,
`additional_sessions`, several proxy/agent/configuration/defaults keys, and its
`job.type` enum was missing two job types
(`tryscrollbydurationjob`/`tryscrollbylengthjob`). `additionalProperties` was
therefore never set to `false` anywhere (see
docs/superpowers/plans/2026-09-12-pmspec-strict-contract-phase2.md's deviation
note) -- doing so against a drifted schema would have rejected every real spec.

`build_schema()` instead reads the same key sets `SpecValidator` uses for its
own "unknown key" checks (`_KEYS_ROOT` .. `_KEYS_SESSION`, `_KEYS_FACTOR`,
`_OPTION_KEYS`, `_KNOWN_PARAMS`) and `JobRegistry.list_all()`, and turns each
into a `type: object, additionalProperties: false` definition. The two
descriptions cannot disagree anymore: change a key set in validator.py, run
`make schema`, and the checked-in file picks it up automatically.
`test_schema_gen.py::test_checked_in_schema_is_generated` fails CI if the file
on disk stops matching `build_schema()`'s output.

Usage:
    python -m experiment_designer.spec.schema_gen   # regenerate the checked-in file
    make schema                                      # same, via the Makefile
"""

import json
from pathlib import Path
from typing import Any

from .job_registry import JobRegistry
from .validator import SpecValidator

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "experiment-spec.schema.json"

# The API's Proxy::TYPES (primemover_api/app/Modules/Proxy/Model/Proxy.php:30).
# Verified directly against that PHP source, not derived from anything in this
# repo -- "socks5" in the old hand-written schema was never a real API value.
_PROXY_TYPES = ["http", "brightdata", "oxylabs"]

_BROWSER_TYPES = ["mlx", "chrome", "firefox"]
_CAPTCHA_MODES = ["never", "always", "ondemand"]
_TYPING_MODES = ["INSTANT", "SIMULATED", "SIMULATED_FIXINGTYPOS"]
_SELECTION_TYPES = ["XPATH", "CSS", "ID", "NAME"]
_SCROLL_DIRECTIONS = ["UP", "DOWN", "LEFT", "RIGHT"]

# Distribution types DistributionFactory.create() accepts (distributions.py).
_DISTRIBUTION_TYPES = [
    "uniform",
    "discrete",
    "normal",
    "gumbel",
    "truncated_normal",
    "beta",
    "fixed",
]


def _object(
    keys: frozenset[str],
    description: str,
    fragments: dict[str, Any],
    *,
    required: list[str] | None = None,
) -> dict[str, Any]:
    """A `type: object, additionalProperties: false` definition whose property
    set is exactly `keys` (a validator `_KEYS_*`/`_OPTION_KEYS` frozenset) --
    the whitelist *is* the schema now, so this can never list a key the
    validator's own "unknown key" check would reject, or vice versa.

    `fragments` supplies the per-key schema (type/enum/description/...) for
    keys worth documenting; a key present in `keys` but absent from
    `fragments` gets `{}` (accept anything) rather than being silently
    dropped, so a new validator key always appears here even before its
    schema fragment is written.
    """
    props = {key: fragments.get(key, {}) for key in sorted(keys)}
    schema: dict[str, Any] = {
        "type": "object",
        "description": description,
        "properties": props,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _shorthand_options(
    yaml_name: str,
    description: str,
    fragments: dict[str, Any],
    *,
    required: list[str] | None = None,
) -> dict[str, Any]:
    """The dict-form options object for one job shorthand, from
    `SpecValidator._OPTION_KEYS[yaml_name]`.

    Deliberately `additionalProperties: true`, unlike the structural `_object`
    definitions above: an option key outside this set is the *validator's*
    drop to report (phase 5 task 2, `_OPTION_KEYS`-driven), not a hard schema
    rejection here -- matching how `params` treats `_KNOWN_PARAMS` (see
    `_params_schema` below).
    """
    keys = SpecValidator._OPTION_KEYS[yaml_name]
    props = {key: fragments.get(key, {}) for key in sorted(keys)}
    schema: dict[str, Any] = {
        "type": "object",
        "description": description,
        "properties": props,
        "additionalProperties": True,
    }
    if required:
        schema["required"] = required
    return schema


def _params_schema() -> dict[str, Any]:
    """`configuration.params`: keys from `SpecValidator._KNOWN_PARAMS`, with
    `additionalProperties: true` -- an unknown param is the validator's drop
    (`_validate_treatment_params`), not the schema's rejection.

    `pi`/`psi`/`kappa`/`tau`/`alpha`/`beta` carry no `type`/`enum` restriction
    on purpose: pre-expansion, a raw spec legitimately holds an unresolved
    `"{pi}"` template placeholder (see `websearch-polarization-study.yaml`,
    a shipped example) or, for a hand-authored crawler with no design, a
    literal number outside {0, 1} that only `_validate_treatment_params`'s
    `_is_switch_value` check -- a semantic rule, not a structural one -- is
    positioned to catch.
    """
    fragments = {
        "pi": {"description": "Treatment switch; see docs/integration-contract.md and CLAUDE.md"},
        "psi": {"description": "Treatment switch; see docs/integration-contract.md and CLAUDE.md"},
        "kappa": {
            "description": "Treatment switch; see docs/integration-contract.md and CLAUDE.md"
        },
        "tau": {"description": "Treatment switch; see docs/integration-contract.md and CLAUDE.md"},
        "alpha": {"description": "Alpha parameter"},
        "beta": {"description": "Beta parameter"},
        "active": {"type": "boolean", "description": "Whether the configuration is active"},
        "search_terms": {
            "description": "Search terms with weights, or an unresolved data-pool reference",
            "oneOf": [
                {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"term": {"type": "string"}, "pi": {"type": "number"}},
                    },
                },
                {"type": "object", "required": ["from_pool"]},
            ],
        },
        "media_outlet_urls": {
            "description": "Media outlet URLs with weights, or an unresolved data-pool reference",
            "oneOf": [
                {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "domain": {"type": "string"},
                            "url": {"type": "string"},
                            "pi": {"type": "number"},
                        },
                    },
                },
                {"type": "object", "required": ["from_pool"]},
            ],
        },
    }
    props = {key: fragments.get(key, {}) for key in sorted(SpecValidator._KNOWN_PARAMS)}
    return {
        "type": "object",
        "description": "Algorithm parameters read by the runner/decision function",
        "properties": props,
        "additionalProperties": True,
    }


def build_schema() -> dict[str, Any]:
    """Build the full experiment-spec JSON Schema from the validator's key
    sets, the job registry, and the parser's shorthand option keys."""
    sv = SpecValidator

    root_fragments = {
        "version": {
            "type": "string",
            "description": "Specification version",
            "default": "1.0",
            "examples": ["1.0"],
        },
        "experiment": {"$ref": "#/definitions/experiment"},
        "defaults": {"$ref": "#/definitions/defaults"},
        "templates": {
            "type": "object",
            "description": "Reusable workflow templates",
            "additionalProperties": {
                "type": "array",
                "items": {"$ref": "#/definitions/workflowItem"},
            },
        },
        "design": {"$ref": "#/definitions/design"},
        "data_pools": {
            "type": "object",
            "description": ("External data pools (CSV/JSON/YAML) referenced by from_pool draws"),
            "additionalProperties": {
                "oneOf": [
                    {"type": "string", "description": "Shorthand: just the source path"},
                    {"$ref": "#/definitions/dataPool"},
                ]
            },
        },
        "strategy_schemas": {
            "type": "object",
            "description": (
                "Per-strategy JSON Schema for a lambda_select job's treatment block: an "
                "inline schema, or a path resolved against the spec's directory"
            ),
            "additionalProperties": {"oneOf": [{"type": "object"}, {"type": "string"}]},
        },
        "crawlers": {
            "type": "array",
            "description": "Crawler definitions",
            "items": {"$ref": "#/definitions/crawler"},
            "minItems": 1,
        },
    }

    experiment_fragments = {
        "name": {"type": "string", "description": "Experiment name", "minLength": 1},
        "description": {"type": "string", "description": "Experiment description"},
        "contact": {"type": "string", "description": "Contact info for the experiment owner"},
        "runner_user_id": {
            "type": "integer",
            "description": (
                "Runner account that executes this experiment; omit for the default "
                "runner (1 = ex63). 3 = the runner-only VM."
            ),
        },
        "generator": {
            "type": "string",
            "description": "Free-text provenance: what produced this spec",
        },
    }

    defaults_fragments = {
        "location": {
            "type": "string",
            "description": "Default location code",
            "examples": ["US-IL-CHICAGO", "US-CA-LOS_ANGELES", "DE-BE-BERLIN"],
        },
        "browser": {
            "type": "string",
            "description": "Browser identification",
            "enum": _BROWSER_TYPES,
            "default": "mlx",
        },
        "timezone": {
            "type": "string",
            "description": "Timezone identifier",
            "examples": ["America/Chicago", "America/New_York", "Europe/Berlin"],
        },
        "timezone_utc_offset": {
            "type": "string",
            "description": "UTC offset",
            "pattern": r"^[+-]\d{2}:\d{2}$",
            "examples": ["-06:00", "+01:00"],
        },
        "captcha_mode": {
            "type": "string",
            "description": "Captcha handling mode",
            "enum": _CAPTCHA_MODES,
            "default": "never",
        },
        "proxy_type": {
            "type": "string",
            "description": "Default proxy type",
            "enum": _PROXY_TYPES,
            "default": "http",
        },
        "typing_mode": {
            "type": "string",
            "description": "Text input simulation mode",
            "enum": _TYPING_MODES,
            "default": "SIMULATED_FIXINGTYPOS",
        },
        "selection_type": {
            "type": "string",
            "description": "Default element selection type",
            "enum": _SELECTION_TYPES,
            "default": "XPATH",
        },
        "active": {"type": "boolean", "description": "Default active flag"},
        "proxy": {"$ref": "#/definitions/proxy"},
    }

    proxy_fragments = {
        "name": {"type": "string", "description": "Proxy name"},
        "description": {"type": "string", "description": "Proxy description"},
        "hostname": {"type": "string", "description": "Proxy hostname or IP"},
        "host": {"type": "string", "description": "Alias for hostname"},
        "port": {"type": "integer", "description": "Proxy port", "minimum": 1, "maximum": 65535},
        "username": {"type": "string", "description": "Proxy username"},
        "password": {"type": "string", "description": "Proxy password"},
        "type": {"type": "string", "description": "Proxy type", "enum": _PROXY_TYPES},
        "active": {"type": "boolean", "description": "Whether the proxy is active"},
        "location": {"type": "string", "description": "Geo-targeting location code"},
        "from_pool": {
            "type": "boolean",
            "description": "Assign this crawler its own row of a data pool (needs pool/match_field)",
        },
        "pool": {"type": "string", "description": "Pool name to draw a proxy row from"},
        "match_field": {
            "type": "string",
            "description": "Pool column to match against the agent's location",
        },
    }

    agent_fragments = {
        "name": {"type": "string", "description": "Agent name"},
        "description": {"type": "string", "description": "Agent description"},
        "identification": {
            "type": "string",
            "description": "Browser identification type",
            "enum": _BROWSER_TYPES,
        },
        "location": {"type": "string", "description": "Agent location code"},
        "multilogin_id": {"type": "string", "description": "Existing Multilogin profile ID"},
        "multilogin_profile": {
            "type": "object",
            "description": "Multilogin profile configuration overrides",
        },
        "active": {"type": "boolean", "description": "Whether the agent is active"},
        "custom_start_urls": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Startup URLs; switches the profile to a custom startup behaviour",
        },
    }

    configuration_fragments = {
        "name": {"type": "string", "description": "Configuration name"},
        "description": {"type": "string", "description": "Configuration description"},
        "params": {"$ref": "#/definitions/params"},
        "preferences": {
            "type": "array",
            "description": "Preference settings",
            "items": {"$ref": "#/definitions/preference"},
        },
        "active": {"type": "boolean", "description": "Whether the configuration is active"},
    }

    session_fragments = {
        "name": {"type": "string", "description": "Session name"},
        "description": {"type": "string", "description": "Session description"},
        "start": {
            "type": "string",
            "description": "Start time: 'now', relative offset (+5m, +1h, +1d), or ISO 8601 timestamp",
            "examples": ["now", "+5m", "+1h", "+1d", "2025-02-01T10:00:00Z"],
        },
        "start_at": {
            "type": "string",
            "description": (
                "Alias for start: 'now', relative offset (+5m, +1h, +1d), or ISO 8601 timestamp"
            ),
            "examples": ["now", "+5m", "+1h", "+1d", "2025-02-01T10:00:00Z"],
        },
        "timezone": {"type": "string", "description": "Session timezone"},
        "timezone_utc_offset": {"type": "string", "description": "UTC offset"},
        "active": {
            "type": "boolean",
            "description": "Whether the session is active",
            "default": True,
        },
        "workflow": {
            "type": "array",
            "description": "Workflow steps",
            "items": {"$ref": "#/definitions/workflowItem"},
        },
        "jobs": {
            "type": "array",
            "description": "Full job definitions (alternative to workflow)",
            "items": {"$ref": "#/definitions/job"},
        },
    }

    crawler_fragments = {
        "from_design": {
            "type": "boolean",
            "description": "Whether to expand this crawler using the factorial design",
            "default": False,
        },
        "name": {"type": "string", "description": "Crawler name (supports variable substitution)"},
        "description": {"type": "string", "description": "Crawler description"},
        "active": {"type": "boolean", "description": "Whether crawler is active", "default": True},
        "testing": {
            "type": "boolean",
            "description": "Whether crawler is in testing mode",
            "default": False,
        },
        "configuration": {"$ref": "#/definitions/configuration"},
        "proxy": {"$ref": "#/definitions/proxy"},
        "agent": {"$ref": "#/definitions/agent"},
        "session": {"$ref": "#/definitions/session"},
        "additional_sessions": {
            "type": "array",
            "description": "Further sessions beyond the first (converted to extra queues)",
            "items": {"$ref": "#/definitions/session"},
        },
        "queues": {
            "type": "array",
            "description": "Queue definitions (alternative to session/additional_sessions)",
            "items": {"$ref": "#/definitions/session"},
        },
    }

    design_fragments = {
        "type": {
            "type": "string",
            "description": "Design type",
            "enum": ["factorial"],
            "default": "factorial",
        },
        "seed": {
            "type": "integer",
            "description": "Random seed for reproducible sampling/design expansion",
        },
        "factors": {
            "type": "object",
            "description": "Factor definitions",
            "additionalProperties": {"$ref": "#/definitions/factor"},
            "minProperties": 1,
        },
        "parameters": {
            "type": "object",
            "description": "Parameter distributions sampled once per crawler",
            "additionalProperties": {"$ref": "#/definitions/designParameter"},
        },
        "replications": {
            "type": "integer",
            "description": "Number of replications per cell",
            "minimum": 1,
            "default": 1,
        },
        "total_crawlers": {
            "type": "integer",
            "description": "Total crawlers for a stratified design (proportional factor levels)",
            "minimum": 1,
        },
    }

    factor_fragments = {
        "levels": {
            "type": "array",
            "description": "Factor levels",
            "items": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "number"},
                    {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "value": {},
                            "proportion": {
                                "type": "number",
                                "description": "Stratified-sampling weight for this level",
                            },
                        },
                    },
                ]
            },
            "minItems": 1,
        },
        "maps_to": {
            "type": "object",
            "description": "Parameter path -> {level: value} mapping",
            "additionalProperties": {"type": "object", "description": "Level-to-value mapping"},
        },
    }

    definitions: dict[str, Any] = {
        "experiment": _object(
            sv._KEYS_EXPERIMENT, "Experiment metadata", experiment_fragments, required=["name"]
        ),
        "defaults": _object(
            sv._KEYS_DEFAULTS, "Default values inherited by all crawlers", defaults_fragments
        ),
        "design": _object(sv._KEYS_DESIGN, "Factorial experimental design", design_fragments),
        "factor": _object(
            sv._KEYS_FACTOR,
            "A factor in the experimental design",
            factor_fragments,
            required=["levels"],
        ),
        "crawler": _object(sv._KEYS_CRAWLER, "Crawler definition", crawler_fragments),
        "configuration": _object(
            sv._KEYS_CONFIGURATION, "Crawler configuration", configuration_fragments
        ),
        "params": _params_schema(),
        "preference": {
            "type": "object",
            "description": "A configuration preference",
            "required": ["name", "value"],
            "properties": {
                "name": {"type": "string", "description": "Preference name"},
                "value": {"description": "Preference value (string or JSON)"},
                "description": {"type": "string", "description": "Preference description"},
                "active": {
                    "type": "boolean",
                    "description": "Whether preference is active",
                    "default": True,
                },
            },
        },
        "proxy": _object(sv._KEYS_PROXY, "Proxy configuration", proxy_fragments),
        "agent": _object(sv._KEYS_AGENT, "Browser agent configuration", agent_fragments),
        "session": _object(sv._KEYS_SESSION, "Session/queue definition", session_fragments),
        "dataPool": {
            "type": "object",
            "description": "A data pool loaded from an external file (CSV/JSON/YAML)",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "File path (relative to the spec) or an s3:// URI",
                },
                "format": {
                    "type": "string",
                    "description": "File format; auto-detected from source if omitted",
                    "enum": ["csv", "json", "yaml"],
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Expected columns (for documentation/validation)",
                },
                "column_mapping": {
                    "type": "object",
                    "description": "Rename source columns on load",
                },
            },
            "required": ["source"],
            # Loose on purpose: `type`/`description` appear on shipped pool
            # definitions (see websearch-polarization-study.yaml) but are not
            # read by DataPoolManager.load_pool -- documentary-only keys, not a
            # drop, and not this task's problem to newly flag.
            "additionalProperties": True,
        },
        "designParameter": {
            "type": "object",
            "description": (
                "A parameter distribution (design.parameters.<name>), or a fixed `value:` "
                "shorthand with no 'distribution' key"
            ),
            "properties": {
                "distribution": {"type": "string", "enum": _DISTRIBUTION_TYPES},
                "value": {"description": "Fixed value shorthand"},
            },
            "additionalProperties": True,
        },
        "workflowItem": {
            "type": "object",
            "description": "A workflow step (job shorthand, template reference, or repeat block)",
            "properties": {
                "visit": {"type": "string", "description": "URL to visit"},
                "wait": {
                    # A single {"type": ["integer", "number"]} branch, not two
                    # separate oneOf alternatives: "integer" is a subset of
                    # "number", so a plain int like `wait: 5` would satisfy
                    # both and fail oneOf's "valid under exactly one" rule.
                    "oneOf": [
                        {"type": ["integer", "number"]},
                        {"type": "string", "pattern": r"^\d+(\.\d+)?\s*[a-z]*$"},
                    ],
                    "description": "Wait duration (e.g., 10, '10s', '5m')",
                },
                "click": {
                    "oneOf": [
                        {"type": "string"},
                        _shorthand_options(
                            "click",
                            "Click options",
                            {
                                "selector": {"type": "string"},
                                "selection_type": {"type": "string", "enum": _SELECTION_TYPES},
                                "task": {"type": "string"},
                                "flag": {"type": "string"},
                                "captcha_mode": {"type": "string", "enum": _CAPTCHA_MODES},
                            },
                            required=["selector"],
                        ),
                    ],
                    "description": "Element selector or click options",
                },
                "try_click": {
                    "oneOf": [
                        {"type": "string"},
                        _shorthand_options(
                            "try_click",
                            "Try-click options",
                            {
                                "selector": {"type": "string"},
                                "selection_type": {"type": "string", "enum": _SELECTION_TYPES},
                                "task": {"type": "string"},
                                "flag": {"type": "string"},
                                "captcha_mode": {"type": "string", "enum": _CAPTCHA_MODES},
                            },
                            required=["selector"],
                        ),
                    ],
                    "description": "Try to click (continues on failure)",
                },
                "type": {
                    # `type` is overloaded: it is the shorthand key for the
                    # `type:` (enter text) job (an object), and it is also
                    # the key a *full* job definition uses to name its API
                    # job type (a string such as "visitjob") --
                    # JobRegistry.is_api_type is what SpecParser and
                    # SpecValidator use to tell the two apart at runtime, so
                    # the schema must accept both shapes here.
                    "anyOf": [
                        {
                            "type": "string",
                            "enum": sorted(j.api_type for j in JobRegistry.list_all()),
                            "description": (
                                "Full job definition: a registered API job type. See the "
                                "`job` definition for the other allowed keys (name, "
                                "description, active, behaviors)."
                            ),
                        },
                        _shorthand_options(
                            "type",
                            "Type text into field",
                            {
                                "text": {"type": "string"},
                                "selector": {"type": "string"},
                                "selection_type": {"type": "string", "enum": _SELECTION_TYPES},
                                "typing_mode": {"type": "string", "enum": _TYPING_MODES},
                                "submit": {"type": "boolean"},
                                "captcha_mode": {"type": "string", "enum": _CAPTCHA_MODES},
                            },
                            required=["text", "selector"],
                        ),
                    ],
                },
                "try_type": {
                    **_shorthand_options(
                        "try_type",
                        "Try to type (continues on failure)",
                        {
                            "text": {"type": "string"},
                            "selector": {"type": "string"},
                            "selection_type": {"type": "string", "enum": _SELECTION_TYPES},
                            "typing_mode": {"type": "string", "enum": _TYPING_MODES},
                            "submit": {"type": "boolean"},
                            "captcha_mode": {"type": "string", "enum": _CAPTCHA_MODES},
                        },
                        required=["text", "selector"],
                    ),
                },
                "scroll": {
                    # No "number" branch: unlike wait/_parse_duration, the
                    # scalar-form scroll/try_scroll branch in
                    # _expand_shorthand only special-cases `isinstance(value,
                    # (str, int))`; a bare float falls into the `else` branch
                    # and calls `.get()` on it, raising AttributeError. A
                    # float scalar was never a supported value, so the schema
                    # must not accept it either.
                    "oneOf": [
                        {"type": "integer"},
                        {"type": "string"},
                        _shorthand_options(
                            "scroll",
                            "Scroll options",
                            {
                                "duration": {"type": ["integer", "number", "string"]},
                                "direction": {"type": "string", "enum": _SCROLL_DIRECTIONS},
                            },
                        ),
                    ],
                    "description": "Scroll duration or options",
                },
                "try_scroll": {
                    "oneOf": [
                        {"type": "integer"},
                        {"type": "string"},
                        _shorthand_options(
                            "try_scroll",
                            "Best-effort scroll options",
                            {
                                "duration": {"type": ["integer", "number", "string"]},
                                "direction": {"type": "string", "enum": _SCROLL_DIRECTIONS},
                            },
                        ),
                    ],
                    "description": (
                        "Best-effort scroll by duration (does not fail the job on error)"
                    ),
                },
                "scroll_length": {
                    "oneOf": [
                        {"type": "integer"},
                        _shorthand_options(
                            "scroll_length",
                            "Scroll-by-length options",
                            {
                                "pixels": {"type": ["integer", "number"]},
                                "length": {"type": ["integer", "number"]},
                                "direction": {"type": "string", "enum": _SCROLL_DIRECTIONS},
                            },
                        ),
                    ],
                    "description": "Scroll by pixel amount",
                },
                "try_scroll_length": {
                    "oneOf": [
                        {"type": "integer"},
                        _shorthand_options(
                            "try_scroll_length",
                            "Best-effort scroll-by-length options",
                            {
                                "pixels": {"type": ["integer", "number"]},
                                "length": {"type": ["integer", "number"]},
                                "direction": {"type": "string", "enum": _SCROLL_DIRECTIONS},
                            },
                        ),
                    ],
                    "description": (
                        "Best-effort scroll by pixel amount (does not fail the job on error)"
                    ),
                },
                "lambda_select": {
                    **_shorthand_options(
                        "lambda_select",
                        "AWS Lambda element selection",
                        {
                            "task": {"type": "string"},
                            "function": {"type": "string"},
                            "link_value": {"type": "string"},
                            "link_type": {
                                "type": "string",
                                "enum": ["FUNCTION_NAME", "FUNCTION_ARN"],
                            },
                            "captcha_mode": {"type": "string", "enum": _CAPTCHA_MODES},
                            "strategy": {
                                "type": "string",
                                "description": "Decision strategy selector; see strategy_schemas",
                            },
                            "treatment": {
                                "type": "object",
                                "description": "Strategy-specific treatment config, validated against strategy_schemas",
                            },
                        },
                        required=["task"],
                    ),
                },
                "single_select": {
                    "oneOf": [
                        {"type": "string"},
                        _shorthand_options(
                            "single_select",
                            "Select single element",
                            {
                                "selector": {"type": "string"},
                                "target": {"type": "string", "description": "Alias for selector"},
                                "selection_type": {"type": "string", "enum": _SELECTION_TYPES},
                                "captcha_mode": {"type": "string", "enum": _CAPTCHA_MODES},
                            },
                        ),
                    ],
                    "description": "Select single element",
                },
                "single_select_url": {
                    "oneOf": [
                        {"type": "string"},
                        _shorthand_options(
                            "single_select_url",
                            "Select single URL element",
                            {
                                "selector": {"type": "string"},
                                "target": {"type": "string", "description": "Alias for selector"},
                                "selection_type": {"type": "string", "enum": _SELECTION_TYPES},
                                "captcha_mode": {"type": "string", "enum": _CAPTCHA_MODES},
                            },
                        ),
                    ],
                    "description": "Select single URL element",
                },
                "template": {"type": "string", "description": "Template name to expand"},
                "with": {"type": "object", "description": "Variables to pass to the template"},
                "repeat": {
                    "type": "integer",
                    "description": "Number of times to repeat",
                    "minimum": 1,
                },
                "jobs": {
                    "type": "array",
                    "description": "Workflow items to repeat (alternative to template, with repeat:)",
                    "items": {"$ref": "#/definitions/workflowItem"},
                },
                "handle_alert": {
                    "oneOf": [
                        {"type": "string", "enum": ["accept", "dismiss"]},
                        _shorthand_options(
                            "handle_alert",
                            "Handle browser alert options",
                            {
                                "action": {"type": "string", "enum": ["accept", "dismiss"]},
                            },
                        ),
                    ],
                    "description": "Handle browser alert",
                },
                "try_handle_alert": {
                    "oneOf": [
                        {"type": "string", "enum": ["accept", "dismiss"]},
                        _shorthand_options(
                            "try_handle_alert",
                            "Try-handle browser alert options",
                            {"action": {"type": "string", "enum": ["accept", "dismiss"]}},
                        ),
                    ],
                    "description": "Try to handle alert",
                },
                "switch": {
                    "oneOf": [
                        {"type": "string"},
                        _shorthand_options(
                            "switch",
                            "Switch to frame/window options",
                            {
                                "selector": {"type": "string"},
                                "target": {"type": "string", "description": "Alias for selector"},
                                "selection_type": {"type": "string", "enum": _SELECTION_TYPES},
                                "captcha_mode": {"type": "string", "enum": _CAPTCHA_MODES},
                            },
                        ),
                    ],
                    "description": "Switch to frame/window",
                },
                "try_switch": {
                    "oneOf": [
                        {"type": "string"},
                        _shorthand_options(
                            "try_switch",
                            "Try-switch options",
                            {
                                "selector": {"type": "string"},
                                "target": {"type": "string", "description": "Alias for selector"},
                                "selection_type": {"type": "string", "enum": _SELECTION_TYPES},
                                "captcha_mode": {"type": "string", "enum": _CAPTCHA_MODES},
                            },
                        ),
                    ],
                    "description": "Try to switch",
                },
            },
            "additionalProperties": False,
        },
        "job": {
            "type": "object",
            "description": "Full job definition",
            "required": ["type"],
            "properties": {
                "name": {"type": "string"},
                "type": {
                    "type": "string",
                    "enum": sorted(j.api_type for j in JobRegistry.list_all()),
                },
                "description": {"type": "string"},
                "active": {"type": "boolean", "default": True},
                "behaviors": {
                    "oneOf": [
                        {"type": "array", "items": {"$ref": "#/definitions/behavior"}},
                        {"type": "object", "additionalProperties": True},
                    ]
                },
            },
        },
        "behavior": {
            "type": "object",
            "description": "Job behavior parameter",
            "required": ["name", "value"],
            "properties": {
                "name": {"type": "string"},
                "value": {},
                "description": {"type": "string"},
                "active": {"type": "boolean", "default": True},
            },
        },
    }

    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "https://primemover.io/schemas/experiment-spec.schema.json",
        "title": "PrimeMover Experiment Specification",
        "description": "YAML/JSON schema for defining PrimeMover browser automation experiments",
        "type": "object",
        "required": ["experiment", "crawlers"],
        "properties": {key: root_fragments[key] for key in sorted(sv._KEYS_ROOT)},
        "additionalProperties": False,
        "definitions": definitions,
    }


def main() -> None:
    """Regenerate the checked-in schema file (`make schema`)."""
    schema = build_schema()
    SCHEMA_PATH.write_text(json.dumps(schema, indent=2, sort_keys=False) + "\n")
    print(f"Wrote {SCHEMA_PATH}")


if __name__ == "__main__":
    main()
