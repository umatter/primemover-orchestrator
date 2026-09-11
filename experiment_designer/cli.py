#!/usr/bin/env python3
"""
PrimeMover Experiment Spec CLI (pmspec)

Command-line tool for working with YAML experiment specifications.

Commands:
    pmspec validate <file.yaml|.lock.yaml>  Validate a specification or a lock
    pmspec compile <file.yaml>         Compile a spec into a committable lock
    pmspec generate <file.yaml|.lock.yaml>  Convert to API JSON
    pmspec preview <file.yaml>        Preview design expansion
    pmspec summary <file.yaml>        Generate experiment summary
    pmspec upload <file.yaml|.lock.yaml>  Upload a lock (compiling a spec first) and verify it
    pmspec verify <exp_id> <lock>     Check a live experiment is bound to, and identical to, a lock
    pmspec secrets check <lock>       List a lock's secret placeholders and which are missing
    pmspec schema jobs                List all job types
    pmspec schema job <type>          Show job type details
"""

import json
import sys
from pathlib import Path

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

from .lock.api import ApiError, ApiSession
from .lock.build import compile_spec
from .lock.io import LockError, dump_lock, is_lock_document, load_lock
from .lock.model import Lock
from .lock.resolve import resolve_for_upload
from .lock.secrets import InsecureSecretsFile, MissingSecret, SecretStore, find_placeholders
from .lock.upload import MODES, UploadOptions, UploadRefused, upload_lock
from .lock.verify import verify_lock
from .spec.converter import SpecConverter
from .spec.design_expander import DesignExpander
from .spec.job_registry import JobRegistry
from .spec.parser import SpecParser
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
@click.argument("file", type=click.Path(exists=True))
@click.option(
    "--format", "-f", type=click.Choice(["text", "json"]), default="text", help="Output format"
)
@click.option("--strict", is_flag=True, help="Treat warnings as errors")
@click.option("--seed", "-s", type=int, help="Random seed for reproducible randomization")
def validate(file: str, format: str, strict: bool, seed: int | None):
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

    with open(file) as f:
        content = f.read()

    if is_lock_document(content):
        try:
            lock = load_lock(content)
        except LockError as e:
            click.secho("Lock validation FAILED", fg="red", bold=True)
            for error in e.errors:
                click.secho(f"  ERROR: {error}", fg="red")
            sys.exit(1)
        click.secho("Lock: valid", fg="green", bold=True)
        click.echo(f"Experiment: {lock.experiment['name']}")
        click.echo(f"Crawlers:   {len(lock.crawlers)}")
        click.echo(f"Total Jobs: {sum(len(q['jobs']) for c in lock.crawlers for q in c['queues'])}")
        click.echo(f"Anchor:     {lock.schedule['anchor']}")
        sys.exit(0)

    # Create custom YAML loader with !file tag support
    FileTagLoader, file_loader = create_file_tag_loader(base_dir=base_dir, seed=seed)

    # Parse YAML first (using custom loader for !file tag support)
    try:
        raw_spec = yaml_lib.load(content, Loader=FileTagLoader)
        # Resolve file references for schema validation
        raw_spec = resolve_file_references(raw_spec, file_loader)
    except yaml_lib.YAMLError as e:
        if format == "json":
            click.echo(
                json.dumps({"valid": False, "errors": [f"YAML parse error: {e}"], "warnings": []})
            )
        else:
            click.secho("Validation FAILED", fg="red", bold=True)
            click.secho(f"  YAML parse error: {e}", fg="red")
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
    all_errors = list(set(validation_result.errors + parsed.errors + result.errors))
    all_warnings = list(set(validation_result.warnings + parsed.warnings + result.warnings))

    # In strict mode, warnings become errors
    if strict:
        all_errors.extend(all_warnings)
        all_warnings = []

    if format == "json":
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
            "design_info": validation_result.info.get("design_info", None),
        }
        click.echo(json.dumps(output, indent=2))
    else:
        if all_errors:
            click.secho("Validation FAILED", fg="red", bold=True)
            click.echo()
            for error in all_errors:
                click.secho(f"  ERROR: {error}", fg="red")
        else:
            click.secho("Validation PASSED", fg="green", bold=True)

        if all_warnings:
            click.echo()
            for warning in all_warnings:
                click.secho(f"  WARNING: {warning}", fg="yellow")

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
@click.argument("file", type=click.Path(exists=True))
@click.option("--output", "-o", type=click.Path(), help="Lock path (default: <stem>.lock.yaml)")
@click.option("--seed", "-s", type=int, help="The one seed every draw derives from")
@click.option("--anchor", help="Schedule anchor: 'upload' or a Europe/Zurich wall-clock ISO time")
@click.option("--generator", help="Free-text provenance: what produced the spec")
@click.option("--lax", is_flag=True, help="Downgrade strict-contract errors to warnings (recorded)")
def compile(
    file: str,
    output: str | None,
    seed: int | None,
    anchor: str | None,
    generator: str | None,
    lax: bool,
):
    """Compile a spec into a self-contained, committable lock file."""
    file_path = Path(file)
    result = compile_spec(
        file_path.read_text(),
        base_dir=file_path.parent,
        spec_path=str(file_path),
        seed=seed,
        anchor=anchor,
        generator=generator,
        lax=lax,
    )
    for warning in result.warnings:
        click.secho(f"  WARNING: {warning}", fg="yellow")
    if result.lock is None:
        click.secho("Compile FAILED", fg="red", bold=True)
        for error in result.errors:
            click.secho(f"  ERROR: {error}", fg="red")
        sys.exit(1)
    out_path = Path(output) if output else file_path.with_name(f"{file_path.stem}.lock.yaml")
    out_path.write_text(dump_lock(result.lock))
    lock = result.lock
    click.secho(f"Compiled: {out_path}", fg="green")
    click.echo(f"  Crawlers: {len(lock.crawlers)}")
    click.echo(f"  Seed:     {lock.provenance.seed}")
    click.echo(f"  Anchor:   {lock.schedule['anchor']}")
    click.echo(f"  Commit:   {lock.provenance.git_commit or 'none'}")


@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("--output", "-o", type=click.Path(), help="Output file (default: stdout)")
@click.option("--pretty/--compact", default=True, help="Pretty-print JSON")
@click.option("--seed", "-s", type=int, help="Only when FILE is a spec: the seed to compile with")
@click.option(
    "--resolve-secrets", is_flag=True, help="Resolve ${secret:...} from the secrets store"
)
def generate(file: str, output: str | None, pretty: bool, seed: int | None, resolve_secrets: bool):
    """Generate API upload JSON from a spec or a lock. Prefer `compile` + `upload`;
    this output carries no provenance and is only for the GUI's upload form."""
    file_path = Path(file)
    content = file_path.read_text()
    if is_lock_document(content):
        try:
            lock = load_lock(content)
        except LockError as e:
            for error in e.errors:
                click.secho(f"  ERROR: {error}", fg="red")
            sys.exit(1)
    else:
        result = compile_spec(
            content, base_dir=file_path.parent, spec_path=str(file_path), seed=seed
        )
        if result.lock is None:
            click.secho("Cannot generate: compile errors", fg="red")
            for error in result.errors:
                click.secho(f"  {error}", fg="red")
            sys.exit(1)
        lock = result.lock

    store = SecretStore.load() if resolve_secrets else None
    crawlers, _ = resolve_for_upload(lock, store=store)
    if store is None and find_placeholders(crawlers):
        n = len(find_placeholders(crawlers))
        click.secho(
            f"WARNING: {n} secret placeholder(s) left unresolved (pass --resolve-secrets)",
            fg="yellow",
            err=True,
        )
    json_output = json.dumps(crawlers, indent=2 if pretty else None, ensure_ascii=False)
    if output:
        Path(output).write_text(json_output)
        click.secho(f"Generated: {output}", fg="green")
        click.echo(f"  Crawlers: {len(crawlers)}")
    else:
        click.echo(json_output)


def _session_from_env() -> ApiSession:
    import os

    api_url = os.getenv("PM_API_BASE_URL")
    email = os.getenv("PM_API_USERNAME") or os.getenv("PM_API_EMAIL")
    password = os.getenv("PM_API_PASSWORD")
    if not all([api_url, email, password]):
        click.secho("API credentials not configured", fg="red")
        click.echo("Set environment variables:")
        click.echo("  PM_API_BASE_URL=http://localhost:8090/api/v1")
        click.echo("  PM_API_USERNAME=admin@primemover.example")
        click.echo("  PM_API_PASSWORD=password")
        sys.exit(1)
    return ApiSession(str(api_url), str(email), str(password))


def _load_store(path: str | None) -> SecretStore | None:
    try:
        return SecretStore.load(Path(path) if path else None)
    except FileNotFoundError:
        return None
    except InsecureSecretsFile as e:
        click.secho(f"Refusing secrets file: {e}", fg="red")
        sys.exit(1)


def _lock_text_for(file: str, seed: int | None) -> str:
    """A lock's text; a spec is compiled to its sibling lock first."""
    file_path = Path(file)
    content = file_path.read_text()
    if is_lock_document(content):
        return content
    result = compile_spec(content, base_dir=file_path.parent, spec_path=str(file_path), seed=seed)
    if result.lock is None:
        click.secho("Compile FAILED", fg="red", bold=True)
        for error in result.errors:
            click.secho(f"  ERROR: {error}", fg="red")
        sys.exit(1)
    lock_path = file_path.with_name(f"{file_path.stem}.lock.yaml")
    text = dump_lock(result.lock)
    lock_path.write_text(text)
    click.secho(
        f"Compiled {lock_path} -- commit it: this hash is what the server records", fg="cyan"
    )
    return text


def _load_lock_or_exit(lock_text: str) -> Lock:
    try:
        return load_lock(lock_text)
    except LockError as e:
        click.secho("Lock invalid", fg="red", bold=True)
        for error in e.errors:
            click.secho(f"  ERROR: {error}", fg="red")
        sys.exit(1)


def _secrets_report(lock_text: str, store: SecretStore | None) -> list[str]:
    """Print referenced keys with OK/MISSING; return the missing ones."""
    needed = sorted(find_placeholders(_load_lock_or_exit(lock_text).crawlers))
    if not needed:
        click.echo("Secrets: none referenced")
        return []
    missing = store.missing(needed) if store else list(needed)
    click.echo(f"Secrets ({store.source if store else 'no store found'}):")
    for key in needed:
        click.echo(f"  {'MISSING' if key in missing else 'OK':7} {key}")
    return missing


@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option(
    "--mode",
    type=click.Choice(MODES),
    required=True,
    help="test = VM runner, test- name prefix; real = as in the lock",
)
@click.option("--experiment-id", "-e", type=int, help="Append crawlers to an existing experiment")
@click.option("--dry-run", is_flag=True)
@click.option("--record", type=click.Path(), help="Append the one-line upload record to this file")
@click.option("--allow-dirty", is_flag=True)
@click.option("--allow-past", is_flag=True)
@click.option(
    "--deep", is_flag=True, help="Post-upload verify down to jobs and behaviours (uses the export)"
)
@click.option(
    "--secrets",
    "secrets_path",
    type=click.Path(),
    help="Secrets TOML (default: $PMSPEC_SECRETS_FILE or ~/.config/primemover/secrets.toml)",
)
@click.option("--seed", "-s", type=int, help="Only when FILE is a spec: the seed to compile with")
def upload(
    file: str,
    mode: str,
    experiment_id: int | None,
    dry_run: bool,
    record: str | None,
    allow_dirty: bool,
    allow_past: bool,
    deep: bool,
    secrets_path: str | None,
    seed: int | None,
) -> None:
    """Upload a lock (or compile a spec, then upload the lock) and verify the result."""
    lock_text = _lock_text_for(file, seed)
    store = _load_store(secrets_path)
    missing = _secrets_report(lock_text, store)
    if missing:
        click.secho(f"{len(missing)} secret(s) missing", fg="red")
        sys.exit(1)
    if dry_run:
        click.secho("Dry run - not uploading", fg="cyan")
        sys.exit(0)

    session = _session_from_env()
    options = UploadOptions(
        mode=mode, experiment_id=experiment_id, allow_dirty=allow_dirty, allow_past=allow_past
    )
    try:
        result = upload_lock(lock_text, session=session, store=store, options=options)
    except (UploadRefused, MissingSecret) as e:
        click.secho(f"Refused: {e}", fg="red")
        sys.exit(1)
    except ApiError as e:
        click.secho(f"Upload failed: {e}", fg="red")
        if e.detail:
            click.echo(f"  {e.detail}")
        sys.exit(1)

    click.secho("Upload SUCCESS", fg="green", bold=True)
    click.echo(
        f"Experiment ID: {result.experiment_id}  ({result.experiment_name}, mode={result.mode})"
    )
    click.echo(
        f"Crawlers: {result.crawler_count}  Jobs: {result.total_jobs}  Anchor: {result.anchor}"
    )

    if record:
        with open(record, "a") as f:
            f.write(result.record_line + "\n")
    click.echo(result.record_line)

    try:
        verdict = verify_lock(
            lock_text, session=session, experiment_id=result.experiment_id, deep=deep
        )
    except ApiError as e:
        click.secho(f"Verify failed after upload: {e}", fg="red")
        if e.detail:
            click.echo(f"  {e.detail}")
        sys.exit(1)
    click.echo(verdict.render())
    sys.exit(0 if verdict.ok else 1)


@cli.command()
@click.argument("experiment_id", type=int)
@click.argument("lock", type=click.Path(exists=True))
@click.option(
    "--deep", is_flag=True, help="Also compare jobs and behaviours (uses the experiment export)"
)
def verify(experiment_id: int, lock: str, deep: bool) -> None:
    """Check that a live experiment is bound to, and identical to, a lock."""
    session = _session_from_env()
    try:
        result = verify_lock(
            Path(lock).read_text(), session=session, experiment_id=experiment_id, deep=deep
        )
    except ApiError as e:
        click.secho(f"Verify failed: {e}", fg="red")
        sys.exit(1)
    except LockError as e:
        click.secho("Lock invalid", fg="red", bold=True)
        for error in e.errors:
            click.secho(f"  ERROR: {error}", fg="red")
        sys.exit(1)
    click.echo(result.render())
    sys.exit(0 if result.ok else 1)


@cli.group()
def secrets() -> None:
    """Secret placeholders in locks."""


@secrets.command("check")
@click.argument("lock", type=click.Path(exists=True))
@click.option("--secrets", "secrets_path", type=click.Path())
def secrets_check(lock: str, secrets_path: str | None) -> None:
    """List the secrets a lock references and which are missing from the store."""
    missing = _secrets_report(Path(lock).read_text(), _load_store(secrets_path))
    sys.exit(1 if missing else 0)


@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("--seed", "-s", type=int, help="Random seed for reproducible randomization")
def preview(file: str, seed: int | None):
    """Preview design expansion without generating JSON.

    Shows how many crawlers will be created and the factor combinations.

    Use --seed for reproducible file selections and parameter distributions.
    """
    file_path = Path(file)
    base_dir = file_path.parent

    with open(file) as f:
        content = f.read()

    parser = SpecParser(base_dir=base_dir, seed=seed)
    parsed = parser.parse(content)

    if parsed.errors:
        click.secho("Parse errors:", fg="red")
        for error in parsed.errors:
            click.secho(f"  {error}", fg="red")
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
    for factor in preview_info.get("factors", []):
        click.echo(f"  {factor['name']}: {factor['levels']}")
        if factor.get("maps_to"):
            click.echo(f"    -> maps to: {', '.join(factor['maps_to'])}")

    click.echo()
    click.echo(f"Replications: {preview_info.get('replications', 1)}")
    click.echo()

    click.secho(preview_info.get("formula", ""), fg="cyan", bold=True)


@cli.command()
@click.argument("files", nargs=-1, type=click.Path(exists=True))
@click.option("--experiment-id", "-e", type=int, help="API experiment ID to summarize")
@click.option("--json-spec", is_flag=True, help="Treat input as JSON spec instead of YAML")
@click.option("--compare", is_flag=True, help="Compare two experiments")
@click.option("--output", "-o", type=click.Path(), help="Output file path")
@click.option(
    "--format",
    "-f",
    type=click.Choice(["text", "json", "html", "both"]),
    default="text",
    help="Output format",
)
@click.option("--seed", "-s", type=int, help="Random seed for reproducible sampling")
@click.option("--api-url", envvar="PM_API_BASE_URL", help="API base URL")
@click.option("--email", envvar="PM_API_USERNAME", help="API username/email")
@click.option("--password", envvar="PM_API_PASSWORD", help="API password")
def summary(
    files,
    experiment_id: int | None,
    json_spec: bool,
    compare: bool,
    output: str | None,
    format: str,
    seed: int | None,
    api_url: str | None,
    email: str | None,
    password: str | None,
):
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
        email = os.getenv("PM_API_EMAIL")

    from .summary import (
        ComparisonHTMLGenerator,
        ExperimentVisualizer,
        HTMLGenerator,
        SummaryGenerator,
        compare_summaries,
    )

    generator = SummaryGenerator(seed=seed)

    # Determine what to summarize
    if experiment_id:
        # Summarize API experiment
        if not api_url or not email or not password:
            click.secho("API credentials required for --experiment-id", fg="red")
            click.echo()
            click.echo("Set environment variables:")
            click.echo("  PM_API_BASE_URL=http://localhost:8090/api/v1")
            click.echo("  PM_API_USERNAME=admin@primemover.example")
            click.echo("  PM_API_PASSWORD=password")
            sys.exit(1)

        try:
            summary_data = generator.from_api(experiment_id, api_url, email, password)
        except Exception as e:
            click.secho(f"Failed to fetch experiment: {e}", fg="red")
            sys.exit(1)

    elif json_spec and files:
        # Summarize JSON spec
        try:
            summary_data = generator.from_json_file(files[0])
        except Exception as e:
            click.secho(f"Failed to parse JSON: {e}", fg="red")
            sys.exit(1)

    elif files:
        # Summarize YAML spec
        try:
            summary_data = generator.from_yaml_file(files[0], seed=seed)
        except Exception as e:
            click.secho(f"Failed to parse YAML: {e}", fg="red")
            sys.exit(1)

    else:
        click.secho("Provide a file or --experiment-id", fg="red")
        sys.exit(1)

    # Handle comparison mode
    if compare:
        if len(files) < 2 and not experiment_id:
            click.secho("Comparison requires two sources (files or --experiment-id)", fg="red")
            sys.exit(1)

        # Get second summary
        if len(files) >= 2:
            summary_b = generator.from_yaml_file(files[1], seed=seed)
        elif experiment_id and files:
            summary_b = generator.from_yaml_file(files[0], seed=seed)
            summary_data, summary_b = summary_b, summary_data  # Swap so API is B
        else:
            click.secho("Need two sources for comparison", fg="red")
            sys.exit(1)

        comparison = compare_summaries(summary_data, summary_b)

        if format == "text":
            click.echo(comparison.to_text())
        elif format == "json":
            if output:
                with open(output, "w") as f:
                    f.write(comparison.to_json())
                click.secho(f"Written: {output}", fg="green")
            else:
                click.echo(comparison.to_json())
        elif format in ("html", "both"):
            # Generate comparison charts
            try:
                from .summary.visualizer import create_comparison_charts

                charts = create_comparison_charts(summary_data, summary_b)
            except ImportError:
                charts = {}
                click.secho("Warning: matplotlib not available, skipping charts", fg="yellow")

            html_gen: ComparisonHTMLGenerator | HTMLGenerator = ComparisonHTMLGenerator(
                comparison, charts
            )
            html = html_gen.generate()

            if output:
                html_path = output if output.endswith(".html") else f"{output}.html"
                with open(html_path, "w") as f:
                    f.write(html)
                click.secho(f"Report: {html_path}", fg="green")
            else:
                click.echo(html)

            if format == "both" and output:
                json_path = (
                    output.replace(".html", ".json")
                    if output.endswith(".html")
                    else f"{output}.json"
                )
                with open(json_path, "w") as f:
                    f.write(comparison.to_json())
                click.secho(f"JSON: {json_path}", fg="green")
        return

    # Output based on format
    if format == "text":
        click.echo(summary_data.to_text())

    elif format == "json":
        json_output = summary_data.to_json()
        if output:
            with open(output, "w") as f:
                f.write(json_output)
            click.secho(f"Written: {output}", fg="green")
        else:
            click.echo(json_output)

    elif format in ("html", "both"):
        # Generate visualizations
        try:
            visualizer = ExperimentVisualizer(summary_data)
            charts = visualizer.generate_all()
        except ImportError:
            charts = {}
            click.secho("Warning: matplotlib not available, skipping charts", fg="yellow")
        except Exception as e:
            charts = {}
            click.secho(f"Warning: chart generation failed: {e}", fg="yellow")

        html_gen = HTMLGenerator(summary_data, charts)
        html = html_gen.generate()

        if output:
            html_path = output if output.endswith(".html") else f"{output}.html"
            with open(html_path, "w") as f:
                f.write(html)
            click.secho(f"Report: {html_path}", fg="green")
            click.echo(f"  Crawlers: {summary_data.crawler_count}")
            click.echo(f"  Jobs: {summary_data.total_jobs}")
            click.echo(f"  Charts: {len(charts)}")
        else:
            click.echo(html)

        if format == "both" and output:
            json_path = (
                output.replace(".html", ".json") if output.endswith(".html") else f"{output}.json"
            )
            with open(json_path, "w") as f:
                f.write(summary_data.to_json())
            click.secho(f"JSON: {json_path}", fg="green")


@cli.group()
def schema():
    """Show schema information for job types and preferences."""
    pass


@schema.command("jobs")
def schema_jobs():
    """List all available job types."""
    click.secho("Available Job Types", bold=True, underline=True)
    click.echo()

    for job in JobRegistry.list_all():
        click.secho(f"{job.yaml_name}", fg="green", bold=True)
        click.echo(f"  API type: {job.api_type}")
        click.echo(f"  {job.description}")

        required = [b.name for b in job.behaviors if b.required]
        optional = [b.name for b in job.behaviors if not b.required]

        if required:
            click.echo(f"  Required: {', '.join(required)}")
        if optional:
            click.echo(f"  Optional: {', '.join(optional)}")
        click.echo()


@schema.command("job")
@click.argument("job_type")
def schema_job(job_type: str):
    """Show detailed schema for a specific job type."""
    schema = JobRegistry.get_schema(job_type)

    if not schema:
        click.secho(f"Unknown job type: {job_type}", fg="red")
        click.echo()
        click.echo("Available types:")
        for job in JobRegistry.list_all():
            click.echo(f"  {job.yaml_name}")
        sys.exit(1)

    click.secho(f"Job Type: {schema.yaml_name}", bold=True, fg="green")
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
@click.option(
    "--template",
    "-t",
    type=click.Choice(["default", "youtube", "google"]),
    default="default",
    help="Template to use",
)
@click.option("--output", "-o", type=click.Path(), help="Output file")
def init(template: str, output: str | None):
    """Create a new experiment spec from template."""
    templates = {
        "default": _get_default_template(),
        "youtube": _get_youtube_template(),
        "google": _get_google_template(),
    }

    spec = templates.get(template, templates["default"])

    if output:
        with open(output, "w") as f:
            f.write(spec)
        click.secho(f"Created: {output}", fg="green")
    else:
        click.echo(spec)


def _get_default_template() -> str:
    return """# Experiment Specification
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
"""


def _get_youtube_template() -> str:
    return """# YouTube Research Experiment
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
"""


def _get_google_template() -> str:
    return """# Google Search Experiment
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
"""


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
