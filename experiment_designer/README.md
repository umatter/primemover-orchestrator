# Experiment Designer

A YAML-based Domain-Specific Language (DSL) for defining PrimeMover browser automation experiments with factorial design support.

## Features

- **YAML DSL**: Human-readable experiment specifications
- **Factorial Design**: Automatic expansion of experimental conditions with replications
- **Template System**: Reusable workflow patterns
- **Shorthand Syntax**: Concise job definitions (`visit: URL`, `wait: 10s`)
- **Validation**: JSON Schema + semantic validation
- **MCP Integration**: Tools for Claude AI assistance
- **Multi-Interface**: CLI, Python API, MCP tools, PHP/Laravel commands, HTTP API

## Quick Start

### Installation

```bash
pip install -r requirements.txt
```

### CLI Usage

```bash
# Validate a specification
python3 -m experiment_designer.cli validate examples/specs/youtube-factorial-study.yaml

# Preview factorial design expansion
python3 -m experiment_designer.cli preview examples/specs/youtube-factorial-study.yaml

# Generate API-compatible JSON
python3 -m experiment_designer.cli generate spec.yaml -o output.json

# Upload to API (dry-run first)
python3 -m experiment_designer.cli upload spec.yaml --dry-run

# Upload to API
export PM_API_BASE_URL=http://localhost:8090/api/v1
export PM_API_USERNAME=admin@primemover.example
export PM_API_PASSWORD=password
python3 -m experiment_designer.cli upload spec.yaml

# List job types
python3 -m experiment_designer.cli schema jobs

# Get template
python3 -m experiment_designer.cli init -t youtube -o my-study.yaml

# Generate experiment summary (text)
python3 -m experiment_designer.cli summary spec.yaml

# Generate HTML report with charts
python3 -m experiment_designer.cli summary spec.yaml --format html --output report.html

# Generate JSON summary (for programmatic use)
python3 -m experiment_designer.cli summary spec.yaml --format json

# Compare two experiments
python3 -m experiment_designer.cli summary exp1.yaml exp2.yaml --compare

# Summarize an existing API experiment
python3 -m experiment_designer.cli summary --experiment-id 123
```

### Laravel/PHP Usage

```bash
# Validate
php artisan experiment:spec validate path/to/spec.yaml

# Generate JSON
php artisan experiment:spec generate spec.yaml --output=output.json

# Preview design
php artisan experiment:spec preview spec.yaml

# Upload (dry-run)
php artisan experiment:spec upload spec.yaml --dry-run

# Upload to existing experiment
php artisan experiment:spec upload spec.yaml --experiment-id=5
```

### HTTP API

```bash
# Validate YAML
curl -X POST http://localhost:8080/api/v1/experiments/spec/validate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"yaml": "experiment:\n  name: Test\ncrawlers:\n  - name: Bot"}'

# Get template
curl http://localhost:8080/api/v1/experiments/spec/template/youtube \
  -H "Authorization: Bearer $TOKEN"

# Convert to JSON
curl -X POST http://localhost:8080/api/v1/experiments/spec/convert \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"yaml": "..."}'
```

## YAML Specification Format

### Basic Structure

```yaml
version: "1.0"

experiment:
  name: "My Experiment"
  description: "Description of the experiment"

defaults:
  location: US-IL-CHICAGO
  browser: mlx
  timezone: America/Chicago
  captcha_mode: never

templates:
  # Reusable workflow patterns
  cookie_consent:
    - try_click: "//button[contains(text(),'Accept')]"
    - wait: 2s

design:
  # Optional factorial design
  type: factorial
  factors:
    condition:
      levels: [control, treatment]
      maps_to:
        configuration.params.pi: {control: 0, treatment: 0.5}
  replications: 3

crawlers:
  - from_design: true  # Expand using factorial design
    name: "bot_{condition}_{replica:02d}"
    # ... crawler configuration
```

### Factorial Design

The `design` section defines factorial experiments that automatically expand:

```yaml
design:
  type: factorial
  factors:
    political_orientation:
      levels: [left, center, right]
      maps_to:
        # Map to configuration params
        configuration.params.pi:
          left: -0.5
          center: 0
          right: 0.5
        # Map to preferences
        configuration.preferences.preference_political_videos:
          left: "1"
          center: "0"
          right: "-1"
        # Map to custom variables for substitution
        search_term:
          left: "progressive news"
          center: "news today"
          right: "conservative news"

    content_type:
      levels: [news, entertainment]
      maps_to:
        starting_channel:
          news: "@CNN"
          entertainment: "@MrBeast"

  replications: 5  # 3 × 2 × 5 = 30 crawlers
```

### Variable Substitution

Variables are substituted in crawler templates:

- `{factor_name}` - Level ID (e.g., "left", "news")
- `{factor_name:value}` - Level value
- `{param_name}` - Mapped parameter value from `maps_to`
- `{replica}` - Replication number (1, 2, 3, ...)
- `{replica:02d}` - Formatted replication number (01, 02, 03, ...)

```yaml
crawlers:
  - from_design: true
    name: "yt_{political_orientation}_{content_type}_{replica:02d}"
    session:
      workflow:
        - type:
            text: "{search_term}"  # Substituted from maps_to
            selector: "//input[@name='q']"
```

### Workflow Shorthand

Jobs can be defined with shorthand syntax:

```yaml
workflow:
  # Visit URL
  - visit: https://youtube.com

  # Wait (supports duration strings)
  - wait: 10s
  - wait: 5m
  - wait: 1h

  # Click element
  - click: "//button[@id='submit']"

  # Try click (continues on failure)
  - try_click:
      selector: "//button[contains(text(),'Accept')]"
      task: cookie_consent
      flag: consent_accepted

  # Type text
  - type:
      text: "search query"
      selector: "//input[@name='q']"
      submit: true
      typing_mode: SIMULATED_FIXINGTYPOS

  # Scroll
  - scroll: 30s
  - scroll:
      duration: 30
      direction: DOWN

  # Lambda selection (AWS Lambda)
  - lambda_select:
      task: youtube_video_select
      function: bot-decision-yt
      link_type: FUNCTION_NAME

  # Use template
  - template: cookie_consent

  # Template with variables
  - template: search_template
    with:
      query: "climate change"

  # Repeat
  - repeat: 10
    template: watch_and_recommend
```

### Templates

Define reusable workflow patterns:

```yaml
templates:
  cookie_consent:
    - try_click: "//button[contains(text(),'Accept all')]"
    - wait: 2s

  youtube_search:
    - type:
        text: "${search_term}"  # Template variable
        selector: "//*[@id='search']"
        submit: true
    - wait: 5s

  watch_and_recommend:
    - wait: 10s
    - lambda_select:
        task: youtube_video_select
        function: bot-decision-yt
```

Use templates in workflow:

```yaml
workflow:
  - template: youtube_search
    with:
      search_term: "breaking news"
```

### Available Job Types

| YAML Name | API Type | Description |
|-----------|----------|-------------|
| `visit` | visitjob | Navigate to URL |
| `wait` | waitjob | Pause execution |
| `click` | clickjob | Click element |
| `try_click` | tryclickjob | Click (continue on fail) |
| `type` | entertextfieldjob | Enter text |
| `try_type` | tryentertextfieldjob | Enter text (continue on fail) |
| `scroll` | scrollbydurationjob | Scroll by duration |
| `scroll_length` | scrollbylengthjob | Scroll by pixels |
| `lambda_select` | lambdasingleselectjob | AWS Lambda selection |
| `single_select` | singleselectjob | Select single element |
| `single_select_url` | singleselecturljob | Select URL element |
| `switch` | switchjob | Switch frame/window |
| `try_switch` | tryswitchjob | Switch (continue on fail) |
| `handle_alert` | handlealertjob | Handle browser alert |
| `try_handle_alert` | tryhandlealertjob | Handle alert (continue on fail) |

## Crawler Configuration

```yaml
crawlers:
  - name: "Crawler Name"
    description: "Description"
    active: true
    testing: false

    configuration:
      name: "Config Name"
      params:
        pi: -0.5
        psi: 0
        alpha: 1
        tau: 0
        kappa: 0
        beta: 0
      preferences:
        - name: location
          value: "US-IL-CHICAGO"
        - name: cookie_pref
          value: '{"accept_all": true}'   # consumed by the runner: accept_all true -> "Accept all", false -> "Reject all"
        - name: usage_type
          value: only_search

    proxy:
      name: "Proxy Name"
      hostname: "proxy.example.com"
      port: 8080
      username: "user"
      password: "pass"
      type: http  # or socks5

    agent:
      name: "Agent Name"
      identification: mlx  # or chrome, firefox
      location: US-IL-CHICAGO
      multilogin_id: null  # Optional existing profile ID

    session:
      name: "Daily Session"
      start: "2025-02-01T10:00:00-06:00"
      timezone: America/Chicago
      timezone_utc_offset: "-06:00"

      workflow:
        - visit: https://youtube.com
        - wait: 5s
```

## MCP Tools (Claude Integration)

When using Claude with the PrimeMover MCP server, these tools are available:

| Tool | Description |
|------|-------------|
| `validate_experiment_spec` | Validate YAML spec |
| `convert_spec_to_json` | Convert to API JSON |
| `preview_factorial_design` | Preview design expansion |
| `list_job_types` | List available job types |
| `get_job_type_schema` | Get job type details |
| `get_spec_template` | Get starter template |
| `upload_experiment_from_yaml` | Convert and upload to API |
| `summarize_yaml_spec` | Generate summary from YAML spec |
| `summarize_experiment` | Generate summary from API experiment |
| `compare_experiments` | Compare two experiments side-by-side |

## Python API

```python
from experiment_designer.spec import (
    SpecParser,
    SpecConverter,
    SpecValidator,
    DesignExpander,
    JobRegistry,
)

# Parse YAML
parser = SpecParser()
parsed = parser.parse(yaml_content)

if parsed.errors:
    print("Errors:", parsed.errors)

# Validate
validator = SpecValidator()
result = validator.validate(raw_yaml_dict)

# Convert to JSON
converter = SpecConverter()
result = converter.convert(parsed)
json_str = result.to_json()

# Preview design
expander = DesignExpander()
preview = expander.preview(design_dict)
```

### Summary Generation

```python
from experiment_designer.summary import (
    SummaryGenerator,
    ExperimentVisualizer,
    HTMLGenerator,
    compare_summaries,
)

# Generate summary from YAML file
generator = SummaryGenerator(seed=42)
summary = generator.from_yaml_file("experiment.yaml")

# Or from JSON spec
summary = generator.from_json_file("crawlers.json")

# Or from API experiment
summary = generator.from_api(
    experiment_id=123,
    api_url="http://localhost:8080/api/v1",
    email="admin@primemover.example",
    password="password"
)

# Output formats
print(summary.to_text())           # Terminal output
print(summary.to_json(indent=2))   # JSON for programmatic use

# Generate charts (requires matplotlib)
visualizer = ExperimentVisualizer(summary)
charts = visualizer.generate_all()  # Dict of base64-encoded PNGs

# Generate HTML report with embedded charts
html_gen = HTMLGenerator(summary, charts)
html_content = html_gen.generate()

# Compare two experiments
summary_a = generator.from_yaml_file("exp_a.yaml")
summary_b = generator.from_yaml_file("exp_b.yaml")
comparison = compare_summaries(summary_a, summary_b)
print(comparison.to_text())
```

## Testing

```bash
# Run test suite
python3 experiment_designer/tests/test_spec.py

# Or with pytest
pytest experiment_designer/tests/ -v
```

## Examples

See `examples/specs/` for complete examples:
- `youtube-factorial-study.yaml` - 3×2 factorial YouTube study with 5 replications

## File Structure

```
experiment_designer/
├── __init__.py
├── cli.py                    # CLI tool (pmspec)
├── spec/
│   ├── __init__.py
│   ├── parser.py             # YAML parsing, template expansion
│   ├── converter.py          # JSON conversion
│   ├── design_expander.py    # Factorial design expansion
│   ├── job_registry.py       # Job type schemas
│   └── validator.py          # Validation rules
├── summary/
│   ├── __init__.py
│   ├── models.py             # ExperimentSummary dataclasses
│   ├── generator.py          # SummaryGenerator (YAML/JSON/API)
│   ├── analyzer.py           # Statistical analysis
│   ├── visualizer.py         # matplotlib chart generation
│   ├── html_generator.py     # HTML report templates
│   └── comparator.py         # Experiment comparison
├── schema/
│   └── experiment-spec.schema.json  # JSON Schema
├── examples/
│   ├── simple-distribution-study.yaml
│   └── websearch-study.yaml
└── tests/
    └── test_spec.py          # Test suite
```
