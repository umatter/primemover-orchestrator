#!/usr/bin/env python3
"""
PrimeMover Experiment Spec CLI (pmspec)

Command-line tool for working with YAML experiment specifications.

Commands:
    pmspec validate <file.yaml|.lock.yaml>  Validate a specification or a lock
    pmspec compile <file.yaml>         Compile a spec into a committable lock
    pmspec generate <file.yaml|.lock.yaml>  Convert to API JSON
    pmspec preview <file.yaml|.lock.yaml>  Preview design expansion (a lock: its counts and provenance)
    pmspec summary <file.yaml|.lock.yaml>  Generate experiment summary
    pmspec upload <file.yaml|.lock.yaml>  Upload a lock (compiling a spec first) and verify it
    pmspec verify <exp_id> <lock>     Check a live experiment is bound to, and identical to, a lock
    pmspec status <exp_id> [lock]     Provenance + health of a live experiment; with lock, also verify
    pmspec audit <exp_id> <lock>      Did finished jobs apply the treatment the lock says they should
    pmspec diff <lock_a> <lock_b>     Diff two locks: provenance, anchor, crawler-level differences
    pmspec secrets check <lock>       List a lock's secret placeholders and which are missing
    pmspec secrets init <lock>        Write (or --force merge) a TOML skeleton for its secrets
    pmspec schema jobs                List all job types
    pmspec schema job <type>          Show job type details
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on the 3.10 CI job
    import tomli as tomllib

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

from .lock.anchor import AnchorError
from .lock.api import ApiError, ApiSession
from .lock.audit import audit_experiment, lock_strategies
from .lock.build import (
    compile_spec,
    load_and_prepare,
    merge_reports,
    parse_and_convert,
)
from .lock.cleanup import CleanupError, delete_experiments, find_test_experiments, parse_age
from .lock.diff import diff_locks
from .lock.io import LockError, dump_lock, is_lock_document, load_lock
from .lock.model import Lock
from .lock.resolve import resolve_for_upload
from .lock.runs import unknown_name_errors
from .lock.secrets import (
    DEFAULT_SECRETS_FILE,
    InsecureSecretsFile,
    MissingSecret,
    SecretStore,
    find_placeholders,
)
from .lock.status import experiment_status
from .lock.upload import (
    MODES,
    UploadOptions,
    UploadRefused,
    plan_runs_upload,
    upload_lock,
)
from .lock.verify import verify_lock
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
@click.argument("file", type=click.Path(exists=True))
@click.option(
    "--format", "-f", type=click.Choice(["text", "json"]), default="text", help="Output format"
)
@click.option("--strict", is_flag=True, help="Treat warnings as errors")
@click.option("--lax", is_flag=True, help="Downgrade strict-contract errors (drops) to warnings")
@click.option("--seed", "-s", type=int, help="Random seed for reproducible randomization")
def validate(file: str, format: str, strict: bool, lax: bool, seed: int | None):
    """Validate an experiment specification.

    Performs three levels of validation:
    - Structural: YAML syntax and JSON Schema validation
    - Semantic: Business rules, job types, required fields
    - Design: Factorial completeness, variable references

    Use --seed for reproducible file selections with !file tags.
    """

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

    # The one preparation every reader of a spec shares: the YAML loaded under
    # the seed *compile* would run with (so validate judges the very draw a
    # `!file {select: random}` would compile), `!file` errors kept rather than
    # silently emptied, and a `runs_for:` spec expanded against its base fleet
    # lock. Without it validate could pass a spec compile refuses, or refuse one
    # it accepts.
    prepared = load_and_prepare(content, base_dir=base_dir, seed=seed)
    raw_spec, is_runs, base = prepared.raw, prepared.is_runs, prepared.base
    prep_errors, prep_warnings = prepared.errors, prepared.warnings
    if (raw_spec is None and prep_errors) or (is_runs and base is None):
        if format == "json":
            click.echo(json.dumps({"valid": False, "errors": prep_errors, "warnings": []}))
        else:
            click.secho("Validation FAILED", fg="red", bold=True)
            for error in prep_errors:
                click.secho(f"  ERROR: {error}", fg="red")
        sys.exit(1)

    # `assign:` refuses to draw unseeded, so validate handing its converter
    # nothing but `--seed` made `pmspec validate` reject specs `pmspec compile`
    # accepted. With no seed anywhere, both report "assignments need a seed".
    run_seed = prepared.run_seed

    # Run enhanced validator on raw spec
    validator = SpecValidator(base_dir=base_dir, lax=lax, runs=is_runs)
    validation_result = validator.validate(raw_spec)

    # Also parse and convert to catch additional errors. All three components
    # get `lax`, or a parser/converter drop stays an error under --lax; the
    # validator has already named every unknown workflow item, so the parser
    # does not report it again. Seeded the way compile_spec seeds, and
    # `resolve_times=False` for the same reason compile_spec passes it: a
    # `start_at: {from, jitter}` is drawn after conversion from the lock's
    # seed, and only a *time-resolving* path (`generate`, which records no
    # seed) has nothing to draw from. Resolving here would reject a spec this
    # very seed compiles.
    parsed, result = parse_and_convert(
        content,
        base_dir=base_dir,
        seed=run_seed,
        lax=lax,
        resolve_times=False,
        report_unknown_items=False,
        raw=raw_spec if is_runs else None,
        runs=is_runs,
    )

    # Combine all errors and warnings: de-duplicated, one drop-count line
    dropped = validation_result.dropped + parsed.dropped + result.dropped
    name_errors = (
        unknown_name_errors([str(c.get("name", "")) for c in result.json_data], base)
        if base is not None
        else []
    )
    all_errors, all_warnings = merge_reports(
        [
            prep_errors,
            validation_result.errors,
            parsed.errors,
            result.errors,
            validator.validate_expanded(result.json_data),
            name_errors,
        ],
        [prep_warnings, validation_result.warnings, parsed.warnings, result.warnings],
        dropped,
        lax,
    )

    # In strict mode, warnings become errors
    if strict:
        all_errors.extend(all_warnings)
        all_warnings = []

    if format == "json":
        output = {
            "valid": len(all_errors) == 0,
            "errors": all_errors,
            "warnings": all_warnings,
            "dropped": dropped,
            "kind": "runs" if is_runs else "fleet",
            "summary": {
                "experiment_name": (
                    base.lock.experiment.get("name", "")
                    if base is not None
                    else parsed.experiment.get("name", "")
                ),
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
        if base is not None:
            click.echo(f"Runs spec for: {base.lock.experiment.get('name', 'N/A')}")
            click.echo(f"Base lock:  {base.sha256[:12]}")
        else:
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
    if lock.is_runs:
        base_sha = lock.provenance.base_lock_sha256 or ""
        click.echo(f"  Kind: runs  Base: {base_sha[:12]}")
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
    try:
        crawlers, _ = resolve_for_upload(lock, store=store)
    except AnchorError as e:
        click.secho(f"Cannot generate: {e}", fg="red")
        sys.exit(1)
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
        total_jobs = sum(len(q.get("jobs", [])) for c in crawlers for q in c.get("queues", []))
        click.secho(f"Generated: {output}", fg="green")
        click.echo(f"  Crawlers: {len(crawlers)}")
        click.echo(f"  Jobs: {total_jobs}")
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
    except tomllib.TOMLDecodeError as e:
        click.secho(f"Secrets file is not valid TOML: {e}", fg="red")
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


def _is_runs_lock(lock_text: str) -> bool:
    return _load_lock_or_exit(lock_text).is_runs


def _print_runs_plan(lock_text: str, options: UploadOptions) -> None:
    """What a runs upload would do, asked of the live experiment.

    Exits non-zero on any refusal: a dry run that cannot say "this would work"
    is the answer the author wanted, and silence here would be read as a yes.
    """
    session = _session_from_env()
    try:
        plan = plan_runs_upload(lock_text, session=session, options=options)
    except (UploadRefused, AnchorError) as e:
        click.secho(f"Refused: {e}", fg="red")
        sys.exit(1)
    except LockError as e:
        click.secho("Lock invalid", fg="red", bold=True)
        for error in e.errors:
            click.secho(f"  ERROR: {error}", fg="red")
        sys.exit(1)
    except ApiError as e:
        click.secho(f"Plan failed: {e}", fg="red")
        if e.detail:
            click.echo(f"  {e.detail}")
        sys.exit(1)
    click.secho(f"Runs lock -> experiment {plan['experiment_id']} (base lock OK)", fg="green")
    click.echo(f"  Crawlers: {plan['crawlers']}  Anchor: {plan['anchor']}")
    click.echo(
        f"  Queues:   {plan['queues']} to append, {plan['would_skip']} already live "
        f"of {plan['queues_in_lock']}"
    )
    click.echo(
        f"  Updates:  {plan['updates']} crawler(s); "
        f"API preference sync: {'yes' if plan['preference_update_supported'] else 'n/a'}"
    )


def _lock_header(lock: Lock) -> str:
    """The one line that says which lock this is.

    A runs lock names its base, because "which fleet were these bots
    randomized into" is the first question anyone reading one has, and the
    experiment name alone does not answer it (a fleet can have many runs
    locks, and a name can be reused).
    """
    name = lock.experiment.get("name", "N/A")
    if lock.is_runs:
        base = (lock.provenance.base_lock_sha256 or "")[:12]
        return f"Runs lock for {name} (base {base})"
    return f"Experiment: {name}"


def _runs_lock_header(file_path: str) -> str | None:
    """`_lock_header` for FILE when it is a runs lock, else None."""
    try:
        content = Path(file_path).read_text()
    except OSError:
        return None
    if not is_lock_document(content):
        return None
    try:
        lock = load_lock(content)
    except LockError:
        return None
    return _lock_header(lock) if lock.is_runs else None


def _load_summary(generator: Any, file_path: str, seed: int | None, json_spec: bool) -> Any:
    """A summary for FILE, whichever of spec/JSON-spec/lock it turns out to be."""
    content = Path(file_path).read_text()
    if is_lock_document(content):
        summary = generator.from_lock_text(content)
        summary.source_file = str(file_path)
        return summary
    if json_spec:
        return generator.from_json_file(file_path)
    return generator.from_yaml_file(file_path, seed=seed)


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


_MISSING_LEAF = object()


class _SkeletonConflictError(Exception):
    """A shape in an existing secrets file that `secrets init --force` refuses to touch
    or rewrite, rather than silently destroying or mangling a value."""


def _get_leaf(data: dict[str, Any], key: str) -> Any:
    """The string at a dotted path in a parsed TOML table, or `_MISSING_LEAF`.

    Raises `_SkeletonConflictError` if the path exists but does not hold a string --
    a table where the lock needs a leaf (`[oxylabs.a.username]`), or a number. Either
    way `SecretStore.get` refuses it and `secrets check` reports the key MISSING, so
    reporting it "already present" here would leave the user with a store that init
    says is complete and check says is not; and adding a leaf beside it would mangle
    the existing shape. The reverse collision (a value where a table is needed) is
    `_set_leaf`'s.
    """
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING_LEAF
        node = node[part]
    if not isinstance(node, str):
        what = "a table" if isinstance(node, dict) else "not a string"
        raise _SkeletonConflictError(
            f"{key}: existing value is {what} in the existing secrets file; "
            "refusing to rewrite it"
        )
    return node


def _set_leaf(data: dict[str, Any], key: str, value: str) -> None:
    """Set a dotted path in a parsed TOML table, creating intermediate tables.

    Raises `_SkeletonConflictError` rather than clobbering an existing value: if the store
    already has, say, `[oxylabs] a = "secret"` and the lock now needs
    `oxylabs.a.username`, `a` cannot become a table without destroying the string that
    is already there. `_set_leaf` is only ever called for a key `_get_leaf` reported
    missing, so the collision is always an existing *value* sitting where a table
    is needed -- never the reverse.
    """
    parts = key.split(".")
    node = data
    for i, part in enumerate(parts[:-1]):
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        elif not isinstance(child, dict):
            colliding = ".".join(parts[: i + 1])
            raise _SkeletonConflictError(
                f"{key}: {colliding!r} already holds a value in the existing secrets "
                "file, not a table"
            )
        node = child
    node[parts[-1]] = value


_TOML_SHORT_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}
_TOML_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_escape(value: str) -> str:
    """Escape a string for a TOML basic string: the named short escapes, plus a
    `\\uXXXX` for every other control character (U+0000-U+001F, U+007F) -- a bare
    literal control character (e.g. an unescaped newline) is not valid inside a TOML
    basic string and would make the file we write unparseable."""
    out = []
    for ch in value:
        if ch in _TOML_SHORT_ESCAPES:
            out.append(_TOML_SHORT_ESCAPES[ch])
        elif ch == "\x7f" or ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return "".join(out)


def _toml_key(name: str) -> str:
    """A bare key if `name` is safe as one, else a quoted TOML basic string key."""
    if _TOML_BARE_KEY_RE.fullmatch(name):
        return name
    return f'"{_toml_escape(name)}"'


def _dump_secrets_toml(data: dict[str, Any]) -> str:
    """Serialize a nested dict of string leaves as TOML: top-level scalars first
    (no header), then one `[a.b.c]` table per nesting level. No writer dependency --
    the only shapes `secrets init` ever produces or merges are string leaves and
    tables, so this is the entire grammar it needs.

    Raises `_SkeletonConflictError` if an existing leaf is not a string: stringifying it
    (an int, bool, float, array, ...) would silently change its type the next time the
    file is read, so a value this store cannot serve as a secret is left untouched by
    refusing outright instead.
    """
    lines: list[str] = []

    def emit(node: dict[str, Any], path: tuple[str, ...]) -> None:
        leaves = {k: v for k, v in node.items() if not isinstance(v, dict)}
        tables = {k: v for k, v in node.items() if isinstance(v, dict)}
        if path:
            if lines:
                lines.append("")
            lines.append(f"[{'.'.join(_toml_key(p) for p in path)}]")
        for key in sorted(leaves):
            value = leaves[key]
            if not isinstance(value, str):
                dotted = ".".join(path + (key,))
                raise _SkeletonConflictError(
                    f"{dotted}: existing value is not a string; refusing to rewrite it"
                )
            lines.append(f'{_toml_key(key)} = "{_toml_escape(value)}"')
        for key in sorted(tables):
            emit(tables[key], path + (key,))

    emit(data, ())
    return "\n".join(lines) + "\n" if lines else ""


def _write_secrets_file(path: Path, text: str) -> None:
    """Write TOML at mode 600, atomically. The complete text is written to a sibling
    temp file created with O_EXCL (so a reader can never observe a half-written file),
    then moved into place with `os.replace`, which is atomic on POSIX -- the target
    always either has its old content or its complete new content, never a partial
    write from a killed process.

    `path` is resolved first: `~/.config/primemover/secrets.toml` is commonly a symlink
    into a dotfiles or password-store checkout, and `os.replace` onto the link itself
    would swap the link for a regular file, silently detaching the store from where
    the user keeps it. Resolving puts the temp file beside the real target and lands
    the rename on it, keeping the link (dangling or not) intact.

    A leftover `<name>.tmp` is ours by naming convention (an earlier run killed
    between create and rename) and is removed rather than left to make this run die
    on O_EXCL."""
    import os

    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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
    """Upload a lock (or compile a spec, then upload the lock) and verify the result.

    A `kind: runs` lock appends queues to an existing experiment's crawlers and
    needs `-e <id>`. Its dry run is not local: every refusal it prints (base
    lock, mode, crawler names, preference support) is a question to the live
    experiment, so it prints the plan the apply would carry out.
    """
    lock_text = _lock_text_for(file, seed)
    store = _load_store(secrets_path)
    missing = _secrets_report(lock_text, store)
    if missing:
        click.secho(f"{len(missing)} secret(s) missing", fg="red")
        sys.exit(1)

    options = UploadOptions(
        mode=mode, experiment_id=experiment_id, allow_dirty=allow_dirty, allow_past=allow_past
    )
    if dry_run:
        if _is_runs_lock(lock_text):
            _print_runs_plan(lock_text, options)
        click.secho("Dry run - not uploading", fg="cyan")
        sys.exit(0)

    session = _session_from_env()
    try:
        result = upload_lock(lock_text, session=session, store=store, options=options)
    except (UploadRefused, AnchorError, MissingSecret) as e:
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
    if result.kind == "runs":
        click.echo(
            f"Crawlers: {result.crawler_count}  Queues: {result.queue_count} "
            f"(skipped {result.skipped_existing} already live)  "
            f"Updates: {result.updates_applied}  Anchor: {result.anchor}"
        )
    else:
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
    except AnchorError as e:
        click.secho(f"Refused: {e}", fg="red")
        sys.exit(1)
    except LockError as e:
        click.secho("Lock invalid", fg="red", bold=True)
        for error in e.errors:
            click.secho(f"  ERROR: {error}", fg="red")
        sys.exit(1)
    click.echo(result.render())
    sys.exit(0 if result.ok else 1)


@cli.command()
@click.argument("experiment_id", type=int)
@click.argument("lock", required=False, type=click.Path(exists=True))
@click.option("--hours", default=24, show_default=True, type=int, help="Health window")
@click.option("--deep", is_flag=True, help="With LOCK, also compare jobs and behaviours")
def status(experiment_id: int, lock: str | None, hours: int, deep: bool) -> None:
    """Provenance + health of a live experiment; with LOCK, also verify."""
    session = _session_from_env()
    try:
        st = experiment_status(
            session,
            experiment_id,
            hours=hours,
            lock_text=Path(lock).read_text() if lock else None,
            deep=deep,
        )
    except ApiError as e:
        click.secho(f"Status failed: {e}", fg="red")
        sys.exit(1)
    except AnchorError as e:
        click.secho(f"Refused: {e}", fg="red")
        sys.exit(1)
    except LockError as e:
        click.secho("Lock invalid", fg="red", bold=True)
        for error in e.errors:
            click.secho(f"  ERROR: {error}", fg="red")
        sys.exit(1)
    click.echo(st.render())
    sys.exit(0 if st.ok else 1)


def _strategy_schemas_for_lock(lock: Lock, base_dir: Path) -> dict[str, dict[str, Any]]:
    """Resolve every strategy the lock's jobs name to its JSON schema.

    Mirrors how ``pmspec validate`` resolves a spec's ``strategy_schemas``:
    ``SpecValidator(base_dir=...)`` is built with no spec-supplied
    ``strategy_schemas`` (the lock format carries none), so each lookup falls
    through to ``PMSPEC_STRATEGY_SCHEMA_DIRS`` or the sibling ``bot-decision-yt``
    checkout. A ``strategy_schemas`` directory placed next to the lock and
    pointed at by ``PMSPEC_STRATEGY_SCHEMA_DIRS`` works the same way it would
    for a spec. A strategy with no resolvable schema (``youtube_v1`` included --
    it needs none, since :func:`~pmspec.lock.audit.expected_filters`
    hard-codes its table) is simply left out; ``audit_experiment`` falls back to
    the generic truthy-key rule for any strategy missing from ``schemas``.
    """
    validator = SpecValidator(base_dir=base_dir)
    schemas: dict[str, dict[str, Any]] = {}
    for strategy in sorted(lock_strategies(lock)):
        schema = validator._strategy_schema(strategy, "audit")
        if schema is not None:
            schemas[strategy] = schema
    return schemas


@cli.command()
@click.argument("experiment_id", type=int)
@click.argument("lock", type=click.Path(exists=True))
@click.option(
    "--hours", default=24, show_default=True, type=click.IntRange(min=1), help="Audit window"
)
@click.option(
    "--sample",
    type=click.IntRange(min=1),
    default=None,
    help="Audit a seeded random sample of K crawlers",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Also fail on unreported/degraded/missing decision reports, and when no job was audited",
)
def audit(experiment_id: int, lock: str, hours: int, sample: int | None, strict: bool) -> None:
    """Did each finished lambda job in EXPERIMENT_ID apply the treatment LOCK says it should?"""
    session = _session_from_env()
    lock_text = Path(lock).read_text()
    try:
        parsed = load_lock(lock_text)
    except LockError as e:
        click.secho("Lock invalid", fg="red", bold=True)
        for error in e.errors:
            click.secho(f"  ERROR: {error}", fg="red")
        sys.exit(1)
    schemas = _strategy_schemas_for_lock(parsed, Path(lock).parent)
    try:
        result = audit_experiment(
            session,
            experiment_id,
            lock_text,
            hours=hours,
            sample=sample,
            strict=strict,
            schemas=schemas,
        )
    except ApiError as e:
        click.secho(f"Audit failed: {e}", fg="red")
        sys.exit(1)
    except AnchorError as e:
        click.secho(f"Refused: {e}", fg="red")
        sys.exit(1)
    except LockError as e:
        click.secho("Lock invalid", fg="red", bold=True)
        for error in e.errors:
            click.secho(f"  ERROR: {error}", fg="red")
        sys.exit(1)
    click.echo(result.render())
    sys.exit(0 if result.ok(strict) else 1)


@cli.command()
@click.option("--older-than", help="Only experiments at least this old, e.g. 7d, 36h, 90m")
@click.option("--dry-run", is_flag=True, help="List candidates and exit; never delete")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
@click.option(
    "--force", is_flag=True, help="Also delete candidates with in-flight (unprocessed) queues"
)
def cleanup(older_than: str | None, dry_run: bool, yes: bool, force: bool) -> None:
    """Delete pmspec TEST experiments only.

    A candidate is an experiment whose name starts with "test-" AND whose preferences carry
    pmspec.mode == "test" -- the name only narrows which experiments are fetched; the
    preference is what actually authorises deletion. A candidate with an in-flight
    (unprocessed) queue is skipped unless --force is given.
    """
    try:
        age = parse_age(older_than) if older_than else None
    except ValueError as e:
        click.secho(str(e), fg="red")
        sys.exit(1)

    session = _session_from_env()
    try:
        candidates = find_test_experiments(session, older_than=age)
    except ApiError as e:
        click.secho(f"Failed to list experiments: {e}", fg="red")
        sys.exit(1)

    if not candidates:
        click.echo("No test experiments to clean up.")
        sys.exit(0)

    click.echo(f"{'id':>6}  {'name':<40}  {'mode':<6}  {'in-flight':<9}  uploaded_at")
    for c in candidates:
        in_flight = "yes" if c.in_flight else "no"
        click.echo(
            f"{c.experiment_id:>6}  {c.name:<40}  {c.mode:<6}  {in_flight:<9}  {c.uploaded_at or '-'}"
        )

    if dry_run:
        sys.exit(0)

    in_flight_count = sum(1 for c in candidates if c.in_flight)
    to_delete_count = len(candidates) if force else len(candidates) - in_flight_count
    if in_flight_count and not force:
        click.echo(
            f"{in_flight_count} in-flight experiment(s) will be skipped (use --force to include them)."
        )

    if not yes and not click.confirm(f"Delete {to_delete_count} test experiment(s)?"):
        click.echo("Aborted.")
        sys.exit(0)

    try:
        result = delete_experiments(
            session, [c.experiment_id for c in candidates], candidates=candidates, force=force
        )
    except CleanupError as e:
        click.secho(
            f"Deleted {e.deleted} before failing on experiment {e.failed_id}: {e.cause}",
            fg="red",
        )
        sys.exit(1)

    click.secho(f"Deleted {len(result.deleted)} test experiment(s).", fg="green")
    if result.skipped_in_flight:
        click.secho(
            f"Skipped {len(result.skipped_in_flight)} in-flight experiment(s).", fg="yellow"
        )
    sys.exit(0)


@cli.command()
@click.argument("lock_a", type=click.Path(exists=True))
@click.argument("lock_b", type=click.Path(exists=True))
def diff(lock_a: str, lock_b: str) -> None:
    """Diff two locks: provenance, anchor, and crawler-level structural differences."""
    try:
        result = diff_locks(Path(lock_a).read_text(), Path(lock_b).read_text())
    except LockError as e:
        click.secho("Lock invalid", fg="red", bold=True)
        for error in e.errors:
            click.secho(f"  ERROR: {error}", fg="red")
        sys.exit(1)
    click.echo(result.render())
    sys.exit(0 if result.identical else 1)


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


@secrets.command("init")
@click.argument("lock", type=click.Path(exists=True))
@click.option("--secrets", "secrets_path", type=click.Path())
@click.option("--force", is_flag=True, help="Merge missing keys into an existing secrets file")
def secrets_init(lock: str, secrets_path: str | None, force: bool) -> None:
    """Write a TOML skeleton with one key per secret placeholder LOCK references.

    Each new leaf is written as `key = ""`; `secrets check` reports it MISSING until
    filled in by hand. Refuses to touch an existing file unless --force, which merges:
    every existing value (filled or not, and any table the lock does not reference) is
    kept, and only the missing leaves are added. Never prints a value.
    """
    import os

    needed = sorted(find_placeholders(_load_lock_or_exit(Path(lock).read_text()).crawlers))
    if not needed:
        click.echo("Secrets: none referenced")
        return

    target = (
        Path(secrets_path)
        if secrets_path
        else Path(os.environ.get("PMSPEC_SECRETS_FILE") or DEFAULT_SECRETS_FILE)
    )
    file_exists = target.exists()

    data: dict[str, Any] = {}
    if file_exists:
        if not force:
            click.secho(
                f"{target} already exists; rerun with --force to add missing keys", fg="red"
            )
            sys.exit(1)
        try:
            data = SecretStore.load(target).data
        except InsecureSecretsFile as e:
            click.secho(f"Refusing secrets file: {e}", fg="red")
            sys.exit(1)
        except tomllib.TOMLDecodeError as e:
            click.secho(f"Secrets file is not valid TOML: {e}", fg="red")
            sys.exit(1)

    added: list[str] = []
    present: list[str] = []
    try:
        for key in needed:
            # `_get_leaf` raises on a non-string at the path, so "present" here
            # means a string leaf (filled or empty) -- what `secrets check` counts.
            if _get_leaf(data, key) is _MISSING_LEAF:
                _set_leaf(data, key, "")
                added.append(key)
            else:
                present.append(key)

        if not added:
            click.echo(f"{target}: all {len(present)} referenced key(s) already present")
            return

        text = _dump_secrets_toml(data)
    except _SkeletonConflictError as e:
        click.secho(f"Refusing to rewrite {target}: {e}", fg="red")
        sys.exit(1)

    _write_secrets_file(target, text)

    click.secho(f"Wrote {target} (mode 600)", fg="green")
    click.echo("Added (fill in before uploading):")
    for key in added:
        click.echo(f"  {key}")
    if present:
        click.echo("Already present:")
        for key in present:
            click.echo(f"  {key}")


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

    if is_lock_document(content):
        try:
            lock = load_lock(content)
        except LockError as e:
            click.secho("Lock invalid", fg="red", bold=True)
            for error in e.errors:
                click.secho(f"  ERROR: {error}", fg="red")
            sys.exit(1)
        total_queues = sum(len(c["queues"]) for c in lock.crawlers)
        total_jobs = sum(len(q["jobs"]) for c in lock.crawlers for q in c["queues"])
        click.secho(_lock_header(lock), bold=True)
        click.echo()
        click.secho("Lock (post-expansion -- no design formula)", bold=True, underline=True)
        click.echo()
        click.echo(f"Crawlers:    {len(lock.crawlers)}")
        click.echo(f"Queues:      {total_queues}")
        click.echo(f"Jobs:        {total_jobs}")
        click.echo(f"Anchor:      {lock.schedule['anchor']}")
        click.echo(f"Seed:        {lock.provenance.seed}")
        click.echo(f"Spec:        {lock.provenance.spec_path}")
        click.echo(f"Commit:      {lock.provenance.git_commit or 'unknown'}")
        return

    # The same preparation `validate` and `compile` do, so a runs spec previews
    # as what it would compile to rather than as an unnamed spec with no bots.
    prepared = load_and_prepare(content, base_dir=base_dir, seed=seed)
    raw_spec, is_runs, base = prepared.raw, prepared.is_runs, prepared.base
    prep_errors = prepared.errors
    if (raw_spec is None and prep_errors) or (is_runs and base is None):
        click.secho("Parse errors:", fg="red")
        for error in prep_errors:
            click.secho(f"  {error}", fg="red")
        sys.exit(1)

    parsed, result = parse_and_convert(
        content,
        base_dir=base_dir,
        seed=prepared.run_seed,
        resolve_times=False,  # previewing is not generating -- see `validate`
        raw=raw_spec if is_runs else None,
        runs=is_runs,
    )

    # `prep_errors` first: a `!file` the loader could not read is what every
    # emptiness below would otherwise be blamed on.
    preview_errors = merge_reports([prep_errors, list(parsed.errors)], [], 0, False)[0]
    if preview_errors:
        click.secho("Parse errors:", fg="red")
        for error in preview_errors:
            click.secho(f"  {error}", fg="red")
        sys.exit(1)

    if base is not None:
        base_name = base.lock.experiment.get("name", "N/A")
        click.secho(f"Runs spec for {base_name} (base {base.sha256[:12]})", bold=True)
        click.echo()
        total_queues = sum(len(c.get("queues", [])) for c in result.json_data)
        click.echo(f"Crawlers:    {result.crawler_count}")
        click.echo(f"Queues:      {total_queues}")
        click.echo(f"Jobs:        {result.total_jobs}")
        click.echo(f"Base spec:   {base.path}")
        return

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

    elif files:
        # Summarize a YAML spec, a JSON spec, or a lock (auto-detected)
        try:
            summary_data = _load_summary(generator, files[0], seed=seed, json_spec=json_spec)
        except Exception as e:
            click.secho(f"Failed to summarize {files[0]}: {e}", fg="red")
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
            summary_b = _load_summary(generator, files[1], seed=seed, json_spec=json_spec)
        elif experiment_id and files:
            summary_b = _load_summary(generator, files[0], seed=seed, json_spec=json_spec)
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
        if files:
            header = _runs_lock_header(files[0])
            if header:
                click.secho(header, bold=True)
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
        configuration.params.pi:
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
