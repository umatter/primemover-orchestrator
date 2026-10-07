"""
Job Registry - Defines all job types and their behavior schemas.

Maps YAML shorthand to API job types with required/optional behaviors.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BehaviorSchema:
    """Schema for a job behavior."""

    name: str
    required: bool = True
    default: Any = None
    description: str = ""


@dataclass
class JobTypeSchema:
    """Schema for a job type."""

    yaml_name: str  # Shorthand name in YAML (e.g., 'visit', 'wait')
    api_type: str  # API enum value (e.g., 'visitjob', 'waitjob')
    display_name: str  # Human-readable name
    behaviors: list[BehaviorSchema] = field(default_factory=list)
    description: str = ""


class JobRegistry:
    """
    Registry of all available job types.

    Provides:
    - Shorthand to API type mapping
    - Behavior validation
    - Default behavior injection
    """

    _job_types: dict[str, JobTypeSchema] = {}
    _api_type_map: dict[str, str] = {}  # yaml_name -> api_type

    @classmethod
    def register(cls, schema: JobTypeSchema) -> None:
        """Register a job type."""
        cls._job_types[schema.yaml_name] = schema
        cls._api_type_map[schema.yaml_name] = schema.api_type

    @classmethod
    def get_schema(cls, yaml_name: str) -> JobTypeSchema | None:
        """Get schema for a job type by YAML name."""
        return cls._job_types.get(yaml_name)

    @classmethod
    def get_api_type(cls, yaml_name: str) -> str | None:
        """Get API type for a YAML shorthand."""
        return cls._api_type_map.get(yaml_name)

    @classmethod
    def is_api_type(cls, value: Any) -> bool:
        """True if `value` is a registered API job type string (e.g.
        'visitjob'), the marker a full job definition's `type:` carries.

        Needed because `type` is also the YAML key for the `type:` (enter
        text) shorthand -- a full job (`{type: visitjob, name: ..., behaviors:
        [...]}`) and a typing shorthand (`{type: {text: ..., selector: ...}}`)
        both use the same top-level key, so the two must be told apart by the
        *value*'s shape before either the parser or the validator decide
        which one they are looking at.
        """
        return isinstance(value, str) and value in cls._api_type_map.values()

    @classmethod
    def list_all(cls) -> list[JobTypeSchema]:
        """List all registered job types."""
        return list(cls._job_types.values())

    @classmethod
    def get_required_behaviors(cls, yaml_name: str) -> list[str]:
        """Get required behavior names for a job type."""
        schema = cls._job_types.get(yaml_name)
        if not schema:
            return []
        return [b.name for b in schema.behaviors if b.required]

    @classmethod
    def validate_behaviors(cls, yaml_name: str, behaviors: dict[str, Any]) -> list[str]:
        """
        Validate behaviors for a job type.

        Returns list of error messages (empty if valid).
        """
        errors = []
        schema = cls._job_types.get(yaml_name)

        if not schema:
            return [f"Unknown job type: {yaml_name}"]

        # Check required behaviors
        for behavior in schema.behaviors:
            if behavior.required and behavior.name not in behaviors:
                errors.append(f"Job '{yaml_name}' requires behavior '{behavior.name}'")

        return errors


# =============================================================================
# Register all job types from PrimeMover API
# =============================================================================

# Visit job - Navigate to URL
JobRegistry.register(
    JobTypeSchema(
        yaml_name="visit",
        api_type="visitjob",
        display_name="Visit URL",
        description="Navigate to a specified URL",
        behaviors=[
            BehaviorSchema("url", required=True, description="Target URL"),
            BehaviorSchema(
                "captchaMode",
                required=False,
                default="never",
                description="Captcha handling: 'never' or 'always'",
            ),
            BehaviorSchema(
                "consent",
                required=False,
                description=(
                    "Cookie/GDPR dialog answer for this job: 'accept', 'reject' or 'ignore'. "
                    "Overrides the crawler's cookie_pref preference and the runner's "
                    "consent.choice default (runner PR #61)"
                ),
            ),
        ],
    )
)

# Wait job - Pause execution
JobRegistry.register(
    JobTypeSchema(
        yaml_name="wait",
        api_type="waitjob",
        display_name="Wait",
        description="Pause execution for specified seconds",
        behaviors=[
            BehaviorSchema("seconds", required=True, description="Wait duration in seconds"),
            BehaviorSchema("captchaMode", required=False, default="never"),
        ],
    )
)

# Click job - Click element
JobRegistry.register(
    JobTypeSchema(
        yaml_name="click",
        api_type="clickjob",
        display_name="Click",
        description="Click an element on the page",
        behaviors=[
            BehaviorSchema("selector", required=True, description="Element selector"),
            BehaviorSchema(
                "selectionType",
                required=False,
                default="XPATH",
                description="Selector type: XPATH, CSS, ID, etc.",
            ),
            BehaviorSchema("captchaMode", required=False, default="never"),
        ],
    )
)

# Try Click job - Attempt click with fallback
JobRegistry.register(
    JobTypeSchema(
        yaml_name="try_click",
        api_type="tryclickjob",
        display_name="Try Click",
        description="Attempt to click an element (continues on failure)",
        behaviors=[
            BehaviorSchema("selector", required=True),
            BehaviorSchema("selectionType", required=False, default="XPATH"),
            BehaviorSchema("task", required=False),
            BehaviorSchema("flag", required=False),
            BehaviorSchema("captchaMode", required=False, default="never"),
        ],
    )
)

# Enter text field job
JobRegistry.register(
    JobTypeSchema(
        yaml_name="type",
        api_type="entertextfieldjob",
        display_name="Type Text",
        description="Enter text into a field",
        behaviors=[
            BehaviorSchema("text", required=True, description="Text to enter"),
            BehaviorSchema("selector", required=True, description="Field selector"),
            BehaviorSchema("selectionType", required=False, default="XPATH"),
            BehaviorSchema(
                "appendReturn",
                required=False,
                default="false",
                description="Press Enter after typing",
            ),
            BehaviorSchema(
                "typingMode",
                required=False,
                default="SIMULATED_FIXINGTYPOS",
                description="Typing simulation mode",
            ),
            BehaviorSchema("captchaMode", required=False, default="never"),
        ],
    )
)

# Try enter text field job
JobRegistry.register(
    JobTypeSchema(
        yaml_name="try_type",
        api_type="tryentertextfieldjob",
        display_name="Try Type Text",
        description="Attempt to enter text (continues on failure)",
        behaviors=[
            BehaviorSchema("text", required=True),
            BehaviorSchema("selector", required=True),
            BehaviorSchema("selectionType", required=False, default="XPATH"),
            BehaviorSchema("appendReturn", required=False, default="false"),
            BehaviorSchema("typingMode", required=False, default="SIMULATED_FIXINGTYPOS"),
            BehaviorSchema("captchaMode", required=False, default="never"),
        ],
    )
)

# Scroll by duration job
JobRegistry.register(
    JobTypeSchema(
        yaml_name="scroll",
        api_type="scrollbydurationjob",
        display_name="Scroll",
        description="Scroll page for specified duration",
        behaviors=[
            BehaviorSchema("duration", required=True, description="Scroll duration in seconds"),
            BehaviorSchema(
                "scrollDirection",
                required=False,
                default="DOWN",
                description="Scroll direction: UP or DOWN",
            ),
            BehaviorSchema("captchaMode", required=False, default="never"),
        ],
    )
)

# Scroll by length job
JobRegistry.register(
    JobTypeSchema(
        yaml_name="scroll_length",
        api_type="scrollbylengthjob",
        display_name="Scroll by Length",
        description="Scroll page by pixel amount",
        behaviors=[
            BehaviorSchema("length", required=True, description="Pixels to scroll"),
            BehaviorSchema("scrollDirection", required=False, default="DOWN"),
            BehaviorSchema("captchaMode", required=False, default="never"),
        ],
    )
)

# Try scroll by duration job
JobRegistry.register(
    JobTypeSchema(
        yaml_name="try_scroll",
        api_type="tryscrollbydurationjob",
        display_name="Try Scroll",
        description="Scroll page for specified duration (continues on failure)",
        behaviors=[
            BehaviorSchema("duration", required=True, description="Scroll duration in seconds"),
            BehaviorSchema(
                "scrollDirection",
                required=False,
                default="DOWN",
                description="Scroll direction: UP or DOWN",
            ),
            BehaviorSchema("captchaMode", required=False, default="never"),
        ],
    )
)

# Try scroll by length job
JobRegistry.register(
    JobTypeSchema(
        yaml_name="try_scroll_length",
        api_type="tryscrollbylengthjob",
        display_name="Try Scroll by Length",
        description="Scroll page by pixel amount (continues on failure)",
        behaviors=[
            BehaviorSchema("length", required=True, description="Pixels to scroll"),
            BehaviorSchema("scrollDirection", required=False, default="DOWN"),
            BehaviorSchema("captchaMode", required=False, default="never"),
        ],
    )
)

# Lambda single select job
JobRegistry.register(
    JobTypeSchema(
        yaml_name="lambda_select",
        api_type="lambdasingleselectjob",
        display_name="Lambda Select",
        description="Use AWS Lambda for element selection",
        behaviors=[
            BehaviorSchema("task", required=True, description="Task identifier"),
            BehaviorSchema(
                "linkType",
                required=False,
                default="FUNCTION_NAME",
                description="FUNCTION_NAME or FUNCTION_ARN",
            ),
            BehaviorSchema("linkValue", required=True, description="Function name or ARN"),
            BehaviorSchema("captchaMode", required=False, default="never"),
            BehaviorSchema(
                "strategy",
                required=False,
                description=(
                    "Decision strategy selector (e.g. 'youtube_v1'). Its presence "
                    "activates the behaviours treatment channel; strategy-specific "
                    "config rides alongside as further behaviours (see the DSL "
                    "'treatment' block). Absent = legacy selectionConfiguration."
                ),
            ),
        ],
    )
)

# Single select job
JobRegistry.register(
    JobTypeSchema(
        yaml_name="single_select",
        api_type="singleselectjob",
        display_name="Single Select",
        description="Select a single element",
        behaviors=[
            BehaviorSchema("selector", required=True),
            BehaviorSchema("selectionType", required=False, default="XPATH"),
            BehaviorSchema("captchaMode", required=False, default="never"),
        ],
    )
)

# Single select URL job
JobRegistry.register(
    JobTypeSchema(
        yaml_name="single_select_url",
        api_type="singleselecturljob",
        display_name="Single Select URL",
        description="Select a single URL element",
        behaviors=[
            BehaviorSchema("selector", required=True),
            BehaviorSchema("selectionType", required=False, default="XPATH"),
            BehaviorSchema("captchaMode", required=False, default="never"),
        ],
    )
)

# Switch job
JobRegistry.register(
    JobTypeSchema(
        yaml_name="switch",
        api_type="switchjob",
        display_name="Switch",
        description="Switch browser context (tab/frame)",
        behaviors=[
            BehaviorSchema("target", required=True),
            BehaviorSchema("captchaMode", required=False, default="never"),
        ],
    )
)

# Try switch job
JobRegistry.register(
    JobTypeSchema(
        yaml_name="try_switch",
        api_type="tryswitchjob",
        display_name="Try Switch",
        description="Attempt to switch context (continues on failure)",
        behaviors=[
            BehaviorSchema("target", required=True),
            BehaviorSchema("captchaMode", required=False, default="never"),
        ],
    )
)

# Handle alert job
JobRegistry.register(
    JobTypeSchema(
        yaml_name="handle_alert",
        api_type="handlealertjob",
        display_name="Handle Alert",
        description="Handle browser alert dialog",
        behaviors=[
            BehaviorSchema("action", required=True, description="accept or dismiss"),
            BehaviorSchema("captchaMode", required=False, default="never"),
        ],
    )
)

# Try handle alert job
JobRegistry.register(
    JobTypeSchema(
        yaml_name="try_handle_alert",
        api_type="tryhandlealertjob",
        display_name="Try Handle Alert",
        description="Attempt to handle alert (continues on failure)",
        behaviors=[
            BehaviorSchema("action", required=True),
            BehaviorSchema("captchaMode", required=False, default="never"),
        ],
    )
)

# Assert proxy job
#
# The runner has had this since the Oxylabs work: AssertProxyByOxylabsJobHandler checks
# the exit against ip.oxylabs.io/location, and PrimemoverApiConverter accepts the api type
# under the provider-neutral name "assertproxyjob" -- the name it expects the spec DSL to
# use. The DSL never had it, so no spec could ask for the check and a bot that exited in
# the wrong city ran to completion and reported success. Recording the exit says so
# afterwards; asserting it fails the session instead of quietly producing data from the
# wrong place.
#
# Every behavior is optional individually because the check is partial by nature -- a
# country alone is a useful assertion -- but the parser requires at least one, as the
# runner's AssertProxyJob does.
JobRegistry.register(
    JobTypeSchema(
        yaml_name="assert_proxy",
        api_type="assertproxyjob",
        display_name="Assert Proxy",
        description="Fail the session unless the proxy exit matches the expected geography",
        behaviors=[
            BehaviorSchema("ip", required=False, description="Exact exit IP to require"),
            BehaviorSchema("country", required=False, description="ISO country code, e.g. US"),
            BehaviorSchema("city", required=False, description="City name, e.g. boston"),
        ],
    )
)
