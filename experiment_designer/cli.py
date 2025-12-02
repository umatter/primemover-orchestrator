#!/usr/bin/env python3
"""
PrimeMover Experiment Spec CLI (pmspec)

Command-line tool for working with YAML experiment specifications.

Commands:
    pmspec validate <file.yaml>       Validate a specification
    pmspec generate <file.yaml>       Convert to API JSON
    pmspec preview <file.yaml>        Preview design expansion
    pmspec summary <file.yaml>        Generate experiment summary
    pmspec upload <file.yaml>         Upload to API
    pmspec schema jobs                List all job types
    pmspec schema job <type>          Show job type details
"""

import sys
import json
from pathlib import Path
from typing import Optional

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not required

try:
    import click
except ImportError:
    print("Error: 'click' package is required. Install with: pip install click")
    sys.exit(1)

from .spec.parser import SpecParser
from .spec.converter import SpecConverter
from .spec.design_expander import DesignExpander
from .spec.job_registry import JobRegistry
from .spec.validator import SpecValidator


@click.group()
@click.version_option(version="0.1.0", prog_name="pmspec")
def cli():
    """PrimeMover Experiment Specification Tool.

    Convert YAML experiment specs to API-compatible JSON with
    factorial design expansion, template support, and validation.
    """
    pass


@cli.command()
@click.argument('file', type=click.Path(exists=True))
@click.option('--format', '-f', type=click.Choice(['text', 'json']), default='text',
              help='Output format')
@click.option('--strict', is_flag=True, help='Treat warnings as errors')
@click.option('--seed', '-s', type=int, help='Random seed for reproducible randomization')
def validate(file: str, format: str, strict: bool, seed: Optional[int]):
    """Validate an experiment specification.

    Performs three levels of validation:
    - Structural: YAML syntax and JSON Schema validation
    - Semantic: Business rules, job types, required fields
    - Design: Factorial completeness, variable references

    Use --seed for reproducible file selections with !file tags.
    """
    import yaml as yaml_lib
    from .spec.file_loader import create_file_tag_loader, resolve_file_references

    file_path = Path(file)
    base_dir = file_path.parent

    with open(file, 'r') as f:
        content = f.read()

    # Create custom YAML loader with !file tag support
    FileTagLoader, file_loader = create_file_tag_loader(base_dir=base_dir, seed=seed)

    # Parse YAML first (using custom loader for !file tag support)
    try:
        raw_spec = yaml_lib.load(content, Loader=FileTagLoader)
        # Resolve file references for schema validation
        raw_spec = resolve_file_references(raw_spec, file_loader)
    except yaml_lib.YAMLError as e:
        if format == 'json':
            click.echo(json.dumps({"valid": False, "errors": [f"YAML parse error: {e}"], "warnings": []}))
        else:
            click.secho("Validation FAILED", fg='red', bold=True)
            click.secho(f"  YAML parse error: {e}", fg='red')
        sys.exit(1)

    # Run enhanced validator on raw spec
    validator = SpecValidator()
    validation_result = validator.validate(raw_spec)

    # Also parse and convert to catch additional errors
    parser = SpecParser(base_dir=base_dir, seed=seed)
    parsed = parser.parse(content)

    converter = SpecConverter()
    result = converter.convert(parsed)

    # Combine all errors and warnings
    all_errors = list(set(
        validation_result.errors +
        parsed.errors +
        result.errors
    ))
    all_warnings = list(set(
        validation_result.warnings +
        parsed.warnings +
        result.warnings
    ))

    # In strict mode, warnings become errors
    if strict:
        all_errors.extend(all_warnings)
        all_warnings = []

    if format == 'json':
        output = {
            "valid": len(all_errors) == 0,
            "errors": all_errors,
            "warnings": all_warnings,
            "summary": {
                "experiment_name": parsed.experiment.get("name", ""),
                "crawler_count": result.crawler_count,
                "total_jobs": result.total_jobs,
                "has_design": parsed.design is not None,
            },
            "design_info": validation_result.info.get("design_info", None)
        }
        click.echo(json.dumps(output, indent=2))
    else:
        if all_errors:
            click.secho("Validation FAILED", fg='red', bold=True)
            click.echo()
            for error in all_errors:
                click.secho(f"  ERROR: {error}", fg='red')
        else:
            click.secho("Validation PASSED", fg='green', bold=True)

        if all_warnings:
            click.echo()
            for warning in all_warnings:
                click.secho(f"  WARNING: {warning}", fg='yellow')

        click.echo()
        click.echo(f"Experiment: {parsed.experiment.get('name', 'N/A')}")
        click.echo(f"Crawlers:   {result.crawler_count}")
        click.echo(f"Total Jobs: {result.total_jobs}")

        if parsed.design:
            expander = DesignExpander()
            preview = expander.preview(parsed.design)
            click.echo(f"Design:     {preview.get('formula', 'N/A')}")

        # Show design info if available
        design_info = validation_result.info.get("design_info")
        if design_info:
            click.echo()
            click.secho("Design Details:", bold=True)
            for factor in design_info.get("factors", []):
                maps_to = ", ".join(factor["maps_to"]) if factor["maps_to"] else "none"
                click.echo(f"  {factor['name']}: {factor['num_levels']} levels -> {maps_to}")

    sys.exit(0 if not all_errors else 1)


@cli.command()
@click.argument('file', type=click.Path(exists=True))
@click.option('--output', '-o', type=click.Path(), help='Output file (default: stdout)')
@click.option('--pretty/--compact', default=True, help='Pretty-print JSON')
@click.option('--seed', '-s', type=int, help='Random seed for reproducible randomization')
def generate(file: str, output: Optional[str], pretty: bool, seed: Optional[int]):
    """Generate API JSON from YAML spec.

    Converts the specification to JSON format compatible with
    the PrimeMover API upload endpoint.

    Use --seed for reproducible file selections and parameter distributions.
    """
    file_path = Path(file)
    base_dir = file_path.parent

    with open(file, 'r') as f:
        content = f.read()

    parser = SpecParser(base_dir=base_dir, seed=seed)
    parsed = parser.parse(content)

    if parsed.errors:
        click.secho("Cannot generate: validation errors found", fg='red')
        for error in parsed.errors:
            click.secho(f"  {error}", fg='red')
        sys.exit(1)

    converter = SpecConverter()
    result = converter.convert(parsed)

    if result.errors:
        click.secho("Conversion errors:", fg='red')
        for error in result.errors:
            click.secho(f"  {error}", fg='red')
        sys.exit(1)

    json_output = result.to_json(pretty=pretty)

    if output:
        with open(output, 'w') as f:
            f.write(json_output)
        click.secho(f"Generated: {output}", fg='green')
        click.echo(f"  Crawlers: {result.crawler_count}")
        click.echo(f"  Jobs:     {result.total_jobs}")
    else:
        click.echo(json_output)


@cli.command()
@click.argument('file', type=click.Path(exists=True))
@click.option('--experiment-id', '-e', type=int, help='Add to existing experiment ID')
@click.option('--dry-run', is_flag=True, help='Validate and convert without uploading')
@click.option('--api-url', envvar='PM_API_BASE_URL', help='API base URL')
@click.option('--email', envvar='PM_API_USERNAME', help='API username/email')
@click.option('--password', envvar='PM_API_PASSWORD', help='API password')
@click.option('--seed', '-s', type=int, help='Random seed for reproducible randomization')
def upload(file: str, experiment_id: Optional[int], dry_run: bool,
           api_url: Optional[str], email: Optional[str], password: Optional[str],
           seed: Optional[int]):
    """Upload experiment spec to PrimeMover API.

    Validates the YAML spec, converts to JSON, and uploads to the API.
    Use --dry-run to validate without uploading.

    Set PM_API_BASE_URL, PM_API_USERNAME, and PM_API_PASSWORD environment
    variables or use the --api-url, --email, --password options.

    Use --seed for reproducible file selections and parameter distributions.
    """
    import os

    # Robust fallbacks for credentials from environment
    # Click's envvar doesn't always work reliably, so check os.getenv directly
    if not api_url:
        api_url = os.getenv('PM_API_BASE_URL')
    if not email:
        email = os.getenv('PM_API_USERNAME') or os.getenv('PM_API_EMAIL')
    if not password:
        password = os.getenv('PM_API_PASSWORD')

    file_path = Path(file)
    base_dir = file_path.parent

    with open(file, 'r') as f:
        content = f.read()

    # Parse and validate
    parser = SpecParser(base_dir=base_dir, seed=seed)
    parsed = parser.parse(content)

    if parsed.errors:
        click.secho("Validation FAILED", fg='red', bold=True)
        for error in parsed.errors:
            click.secho(f"  {error}", fg='red')
        sys.exit(1)

    # Convert to JSON
    converter = SpecConverter()
    result = converter.convert(parsed)

    if result.errors:
        click.secho("Conversion FAILED", fg='red', bold=True)
        for error in result.errors:
            click.secho(f"  {error}", fg='red')
        sys.exit(1)

    if result.warnings:
        for warning in result.warnings:
            click.secho(f"  WARNING: {warning}", fg='yellow')

    click.echo()
    click.echo(f"Experiment: {result.experiment_name}")
    click.echo(f"Crawlers:   {result.crawler_count}")
    click.echo(f"Jobs:       {result.total_jobs}")
    click.echo()

    if dry_run:
        click.secho("Dry run - not uploading", fg='cyan')
        click.echo("Use 'pmspec generate' to save JSON file")
        sys.exit(0)

    # Check credentials
    if not all([api_url, email, password]):
        click.secho("API credentials not configured", fg='red')
        click.echo()
        click.echo("Set environment variables:")
        click.echo("  PM_API_BASE_URL=http://localhost:8090/api/v1")
        click.echo("  PM_API_USERNAME=admin@primemover.example")
        click.echo("  PM_API_PASSWORD=password")
        click.echo()
        click.echo("Or use --api-url, --email, --password options")
        sys.exit(1)

    # Upload to API
    try:
        import requests

        # Authenticate
        click.echo("Authenticating...")
        auth_response = requests.post(
            f"{api_url}/login",
            json={"email": email, "password": password},
            headers={"Accept": "application/json"}
        )
        auth_response.raise_for_status()
        token = auth_response.json().get("access_token")

        if not token:
            click.secho("Authentication failed: no token received", fg='red')
            sys.exit(1)

        # Step 1: If no experiment_id, create a new experiment first
        if not experiment_id:
            click.echo("Creating new experiment...")
            from datetime import datetime
            exp_name = result.experiment_name or f"Experiment {datetime.now().strftime('%Y%m%d_%H%M%S')}"

            create_response = requests.post(
                f"{api_url}/experiments",
                json={
                    "name": exp_name,
                    "description": result.experiment_description or "Created via experiment designer CLI",
                    "contact": "Experiment Designer CLI",
                    "active": 1
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json"
                }
            )
            create_response.raise_for_status()
            create_result = create_response.json()

            # Extract experiment ID from response (may be nested in 'data')
            experiment_id = (
                create_result.get("data", {}).get("id") or
                create_result.get("id")
            )

            if not experiment_id:
                click.secho("Failed to create experiment: no ID returned", fg='red')
                click.echo(f"Response: {json.dumps(create_result, indent=2)}")
                sys.exit(1)

            click.echo(f"Created experiment: {exp_name} (ID: {experiment_id})")
        else:
            click.echo(f"Adding to experiment {experiment_id}...")

        # Step 2: Upload crawlers to the experiment using multipart form data
        click.echo("Uploading crawlers...")

        # Convert to JSON string for file upload
        json_content = json.dumps(result.json_data, indent=2)

        upload_response = requests.post(
            f"{api_url}/experiments/{experiment_id}/upload",
            files={
                "json": ("crawlers.json", json_content, "application/json")
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json"
            }
        )
        upload_response.raise_for_status()
        upload_result = upload_response.json()

        click.echo()
        click.secho("Upload SUCCESS", fg='green', bold=True)
        click.echo(f"Experiment ID: {experiment_id}")
        click.echo(f"Crawlers created: {result.crawler_count}")

    except requests.exceptions.HTTPError as e:
        click.secho(f"Upload failed: {e}", fg='red')
        # Show response body for debugging
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_detail = e.response.json()
                click.echo(f"API Error: {json.dumps(error_detail, indent=2)}")
            except Exception:
                click.echo(f"Response: {e.response.text[:1000]}")
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        click.secho(f"Request failed: {e}", fg='red')
        sys.exit(1)
    except Exception as e:
        click.secho(f"Error: {e}", fg='red')
        sys.exit(1)


@cli.command()
@click.argument('file', type=click.Path(exists=True))
@click.option('--seed', '-s', type=int, help='Random seed for reproducible randomization')
def preview(file: str, seed: Optional[int]):
    """Preview design expansion without generating JSON.

    Shows how many crawlers will be created and the factor combinations.

    Use --seed for reproducible file selections and parameter distributions.
    """
    file_path = Path(file)
    base_dir = file_path.parent

    with open(file, 'r') as f:
        content = f.read()

    parser = SpecParser(base_dir=base_dir, seed=seed)
    parsed = parser.parse(content)

    if parsed.errors:
        click.secho("Parse errors:", fg='red')
        for error in parsed.errors:
            click.secho(f"  {error}", fg='red')
        sys.exit(1)

    click.secho(f"Experiment: {parsed.experiment.get('name', 'N/A')}", bold=True)
    click.echo()

    if not parsed.design:
        click.echo("No factorial design specified.")
        click.echo(f"Crawlers defined: {len(parsed.crawlers)}")
        return

    expander = DesignExpander(seed=seed)
    preview_info = expander.preview(parsed.design)

    click.secho("Factorial Design", bold=True, underline=True)
    click.echo()

    click.echo(f"Design Type: {preview_info.get('design_type', 'factorial')}")
    click.echo()

    click.secho("Factors:", bold=True)
    for factor in preview_info.get('factors', []):
        click.echo(f"  {factor['name']}: {factor['levels']}")
        if factor.get('maps_to'):
            click.echo(f"    -> maps to: {', '.join(factor['maps_to'])}")

    click.echo()
    click.echo(f"Replications: {preview_info.get('replications', 1)}")
    click.echo()

    click.secho(preview_info.get('formula', ''), fg='cyan', bold=True)


@cli.command()
@click.argument('files', nargs=-1, type=click.Path(exists=True))
@click.option('--experiment-id', '-e', type=int, help='API experiment ID to summarize')
@click.option('--json-spec', is_flag=True, help='Treat input as JSON spec instead of YAML')
@click.option('--compare', is_flag=True, help='Compare two experiments')
@click.option('--output', '-o', type=click.Path(), help='Output file path')
@click.option('--format', '-f', type=click.Choice(['text', 'json', 'html', 'both']),
              default='text', help='Output format')
@click.option('--seed', '-s', type=int, help='Random seed for reproducible sampling')
@click.option('--api-url', envvar='PM_API_BASE_URL', help='API base URL')
@click.option('--email', envvar='PM_API_USERNAME', help='API username/email')
@click.option('--password', envvar='PM_API_PASSWORD', help='API password')
def summary(files, experiment_id: Optional[int], json_spec: bool, compare: bool,
            output: Optional[str], format: str, seed: Optional[int],
            api_url: Optional[str], email: Optional[str], password: Optional[str]):
    """Generate experiment summary with visualizations.

    Creates a summary report showing experiment structure, parameter distributions,
    job type breakdowns, and more. Supports YAML specs, JSON specs, and API experiments.

    Examples:

        pmspec summary experiment.yaml              # Text summary

        pmspec summary experiment.yaml -f html -o report.html   # HTML report

        pmspec summary --experiment-id 123          # Summarize API experiment

        pmspec summary --compare spec1.yaml spec2.yaml   # Compare two specs
    """
    import os

    # Fallback for email: try PM_API_EMAIL if PM_API_USERNAME not set
    if not email:
        email = os.getenv('PM_API_EMAIL')

    from .summary import (
        SummaryGenerator,
        ExperimentVisualizer,
        HTMLGenerator,
        ComparisonHTMLGenerator,
        compare_summaries,
    )

    generator = SummaryGenerator(seed=seed)

    # Determine what to summarize
    if experiment_id:
        # Summarize API experiment
        if not all([api_url, email, password]):
            click.secho("API credentials required for --experiment-id", fg='red')
            click.echo()
            click.echo("Set environment variables:")
            click.echo("  PM_API_BASE_URL=http://localhost:8090/api/v1")
            click.echo("  PM_API_USERNAME=admin@primemover.example")
            click.echo("  PM_API_PASSWORD=password")
            sys.exit(1)

        try:
            summary_data = generator.from_api(experiment_id, api_url, email, password)
        except Exception as e:
            click.secho(f"Failed to fetch experiment: {e}", fg='red')
            sys.exit(1)

    elif json_spec and files:
        # Summarize JSON spec
        try:
            summary_data = generator.from_json_file(files[0])
        except Exception as e:
            click.secho(f"Failed to parse JSON: {e}", fg='red')
            sys.exit(1)

    elif files:
        # Summarize YAML spec
        try:
            summary_data = generator.from_yaml_file(files[0], seed=seed)
        except Exception as e:
            click.secho(f"Failed to parse YAML: {e}", fg='red')
            sys.exit(1)

    else:
        click.secho("Provide a file or --experiment-id", fg='red')
        sys.exit(1)

    # Handle comparison mode
    if compare:
        if len(files) < 2 and not experiment_id:
            click.secho("Comparison requires two sources (files or --experiment-id)", fg='red')
            sys.exit(1)

        # Get second summary
        if len(files) >= 2:
            summary_b = generator.from_yaml_file(files[1], seed=seed)
        elif experiment_id and files:
            summary_b = generator.from_yaml_file(files[0], seed=seed)
            summary_data, summary_b = summary_b, summary_data  # Swap so API is B
        else:
            click.secho("Need two sources for comparison", fg='red')
            sys.exit(1)

        comparison = compare_summaries(summary_data, summary_b)

        if format == 'text':
            click.echo(comparison.to_text())
        elif format == 'json':
            if output:
                with open(output, 'w') as f:
                    f.write(comparison.to_json())
                click.secho(f"Written: {output}", fg='green')
            else:
                click.echo(comparison.to_json())
        elif format in ('html', 'both'):
            # Generate comparison charts
            try:
                from .summary.visualizer import create_comparison_charts
                charts = create_comparison_charts(summary_data, summary_b)
            except ImportError:
                charts = {}
                click.secho("Warning: matplotlib not available, skipping charts", fg='yellow')

            html_gen = ComparisonHTMLGenerator(comparison, charts)
            html = html_gen.generate()

            if output:
                html_path = output if output.endswith('.html') else f"{output}.html"
                with open(html_path, 'w') as f:
                    f.write(html)
                click.secho(f"Report: {html_path}", fg='green')
            else:
                click.echo(html)

            if format == 'both' and output:
                json_path = output.replace('.html', '.json') if output.endswith('.html') else f"{output}.json"
                with open(json_path, 'w') as f:
                    f.write(comparison.to_json())
                click.secho(f"JSON: {json_path}", fg='green')
        return

    # Output based on format
    if format == 'text':
        click.echo(summary_data.to_text())

    elif format == 'json':
        json_output = summary_data.to_json()
        if output:
            with open(output, 'w') as f:
                f.write(json_output)
            click.secho(f"Written: {output}", fg='green')
        else:
            click.echo(json_output)

    elif format in ('html', 'both'):
        # Generate visualizations
        try:
            visualizer = ExperimentVisualizer(summary_data)
            charts = visualizer.generate_all()
        except ImportError:
            charts = {}
            click.secho("Warning: matplotlib not available, skipping charts", fg='yellow')
        except Exception as e:
            charts = {}
            click.secho(f"Warning: chart generation failed: {e}", fg='yellow')

        html_gen = HTMLGenerator(summary_data, charts)
        html = html_gen.generate()

        if output:
            html_path = output if output.endswith('.html') else f"{output}.html"
            with open(html_path, 'w') as f:
                f.write(html)
            click.secho(f"Report: {html_path}", fg='green')
            click.echo(f"  Crawlers: {summary_data.crawler_count}")
            click.echo(f"  Jobs: {summary_data.total_jobs}")
            click.echo(f"  Charts: {len(charts)}")
        else:
            click.echo(html)

        if format == 'both' and output:
            json_path = output.replace('.html', '.json') if output.endswith('.html') else f"{output}.json"
            with open(json_path, 'w') as f:
                f.write(summary_data.to_json())
            click.secho(f"JSON: {json_path}", fg='green')


@cli.group()
def schema():
    """Show schema information for job types and preferences."""
    pass


@schema.command('jobs')
def schema_jobs():
    """List all available job types."""
    click.secho("Available Job Types", bold=True, underline=True)
    click.echo()

    for job in JobRegistry.list_all():
        click.secho(f"{job.yaml_name}", fg='green', bold=True)
        click.echo(f"  API type: {job.api_type}")
        click.echo(f"  {job.description}")

        required = [b.name for b in job.behaviors if b.required]
        optional = [b.name for b in job.behaviors if not b.required]

        if required:
            click.echo(f"  Required: {', '.join(required)}")
        if optional:
            click.echo(f"  Optional: {', '.join(optional)}")
        click.echo()


@schema.command('job')
@click.argument('job_type')
def schema_job(job_type: str):
    """Show detailed schema for a specific job type."""
    schema = JobRegistry.get_schema(job_type)

    if not schema:
        click.secho(f"Unknown job type: {job_type}", fg='red')
        click.echo()
        click.echo("Available types:")
        for job in JobRegistry.list_all():
            click.echo(f"  {job.yaml_name}")
        sys.exit(1)

    click.secho(f"Job Type: {schema.yaml_name}", bold=True, fg='green')
    click.echo(f"API Type: {schema.api_type}")
    click.echo(f"Display Name: {schema.display_name}")
    click.echo()
    click.echo(f"Description: {schema.description}")
    click.echo()

    click.secho("Behaviors:", bold=True)
    for behavior in schema.behaviors:
        req = "required" if behavior.required else "optional"
        default = f" (default: {behavior.default})" if behavior.default else ""
        click.echo(f"  {behavior.name}: {req}{default}")
        if behavior.description:
            click.echo(f"    {behavior.description}")

    click.echo()
    click.secho("YAML Examples:", bold=True)
    click.echo()

    # Show examples based on job type
    examples = _get_job_examples(job_type)
    for example in examples:
        click.echo(f"  {example}")


def _get_job_examples(job_type: str) -> list:
    """Get YAML examples for a job type."""
    examples = {
        "visit": [
            "# Shorthand:",
            "visit: https://example.com",
            "",
            "# With options:",
            "visit:",
            "  url: https://example.com",
            "  captcha_mode: always",
        ],
        "wait": [
            "# Shorthand (seconds):",
            "wait: 10",
            "",
            "# With duration string:",
            "wait: 30s",
            "wait: 5m",
            "wait: 1h",
        ],
        "click": [
            "# Shorthand (XPATH selector):",
            "click: //button[@id='submit']",
            "",
            "# With options:",
            "click:",
            "  selector: //button[@id='submit']",
            "  selection_type: XPATH",
        ],
        "try_click": [
            "# With task/flag:",
            "try_click:",
            "  selector: //button[contains(text(),'Accept')]",
            "  task: cookie_consent",
            "  flag: consent_accepted",
        ],
        "type": [
            "# Full syntax:",
            "type:",
            "  text: Hello World",
            "  selector: //input[@name='q']",
            "  submit: true",
            "  typing_mode: SIMULATED_FIXINGTYPOS",
        ],
        "scroll": [
            "# Duration in seconds:",
            "scroll: 30s",
            "",
            "# With direction:",
            "scroll:",
            "  duration: 30",
            "  direction: DOWN",
        ],
        "lambda_select": [
            "lambda_select:",
            "  task: youtube_video_select",
            "  function: bot-decision-yt",
            "  link_type: FUNCTION_NAME",
        ],
    }
    return examples.get(job_type, ["# No examples available"])


@cli.command()
@click.option('--template', '-t', type=click.Choice(['default', 'youtube', 'google']),
              default='default', help='Template to use')
@click.option('--output', '-o', type=click.Path(), help='Output file')
def init(template: str, output: Optional[str]):
    """Create a new experiment spec from template."""
    templates = {
        'default': _get_default_template(),
        'youtube': _get_youtube_template(),
        'google': _get_google_template(),
    }

    spec = templates.get(template, templates['default'])

    if output:
        with open(output, 'w') as f:
            f.write(spec)
        click.secho(f"Created: {output}", fg='green')
    else:
        click.echo(spec)


def _get_default_template() -> str:
    return '''# Experiment Specification
version: "1.0"

experiment:
  name: "My Experiment"
  description: "Experiment description"

defaults:
  location: US-IL-CHICAGO
  browser: mlx
  timezone: America/Chicago

# Uncomment for factorial design:
# design:
#   type: factorial
#   factors:
#     condition:
#       levels: [control, treatment]
#       maps_to:
#         params.pi: {control: 0, treatment: 0.5}
#   replications: 3

crawlers:
  - name: "Crawler 1"
    description: "First crawler"

    configuration:
      params:
        pi: 0
        alpha: 1

    proxy:
      hostname: "127.0.0.1"
      port: 8080
      type: http

    agent:
      identification: mlx
      location: US-IL-CHICAGO

    session:
      name: "Session 1"
      start: "2025-01-15T10:00:00Z"

      workflow:
        - visit: https://example.com
        - wait: 5s
'''


def _get_youtube_template() -> str:
    return '''# YouTube Research Experiment
version: "1.0"

experiment:
  name: "YouTube Recommendation Study"
  description: "Studying recommendation algorithm pathways"

defaults:
  location: US-IL-CHICAGO
  browser: mlx
  timezone: America/Chicago
  captcha_mode: always

templates:
  cookie_consent:
    - try_click: "//button[contains(text(),'Accept')]"
    - wait: 2s

  watch_and_recommend:
    - wait: 10s
    - lambda_select:
        task: youtube_video_select
        function: bot-decision-yt

design:
  type: factorial
  factors:
    political_orientation:
      levels: [left, center, right]
      maps_to:
        preferences.preference_political_videos:
          left: 1
          center: 0
          right: -1
        params.pi:
          left: -0.5
          center: 0
          right: 0.5
    starting_content:
      levels: [news, entertainment]
      maps_to:
        initial_channel:
          news: "@CNN"
          entertainment: "@MrBeast"
  replications: 3

crawlers:
  - from_design: true
    name: "yt_{political_orientation}_{starting_content}_{replica:02d}"
    description: "YouTube bot"

    configuration:
      params:
        alpha: 1
      preferences:
        - name: usage_type
          value: only_search

    proxy:
      hostname: "proxy.example.com"
      port: 8080
      type: http

    agent:
      identification: mlx

    session:
      name: "Daily Session"
      start: "2025-02-01T10:00:00Z"
      schedule: daily

      workflow:
        - visit: https://youtube.com
        - wait: 5s
        - template: cookie_consent
        - type:
            text: "{initial_search}"
            selector: "//*[@id='search']"
            submit: true
        - wait: 5s
        - repeat: 10
            template: watch_and_recommend
'''


def _get_google_template() -> str:
    return '''# Google Search Experiment
version: "1.0"

experiment:
  name: "Google Search Study"
  description: "Studying search result personalization"

defaults:
  location: US-IL-CHICAGO
  browser: mlx
  timezone: America/Chicago

templates:
  google_search:
    - visit: https://google.com
    - wait: 3s
    - type:
        text: ${query}
        selector: "//input[@name='q']"
        submit: true
    - wait: 5s

crawlers:
  - name: "Search Bot"
    description: "Google search crawler"

    configuration:
      params:
        pi: 0
        alpha: 1

    proxy:
      hostname: "127.0.0.1"
      port: 8080
      type: http

    agent:
      identification: mlx
      location: US-IL-CHICAGO

    session:
      name: "Search Session"
      start: "2025-01-15T10:00:00Z"

      workflow:
        - template: google_search
          with:
            query: "climate change"
        - scroll: 30s
        - wait: 5s
'''


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == '__main__':
    main()
