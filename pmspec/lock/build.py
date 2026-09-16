"""spec -> lock. Everything random is drawn here, once, from one seed."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pmspec.spec.converter import ConversionResult, SpecConverter
from pmspec.spec.file_loader import create_file_tag_loader, resolve_file_references
from pmspec.spec.parser import ParsedSpec, SpecParser
from pmspec.spec.validator import SpecValidator

from .anchor import AnchorError, derive_anchor
from .io import sha256_text
from .model import Lock, LockError, Provenance
from .provenance import git_commit, git_toplevel, pmspec_version
from .runs import BaseLock, expand_from_base, load_base, unknown_name_errors
from .secrets import find_literal_credentials

_DROP_COUNT_LINE_RE = re.compile(r"^\d+ construct\(s\) would have been dropped")


@dataclass
class CompileResult:
    lock: Lock | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dropped: int = 0


def _merge(*lists: list[str]) -> list[str]:
    """Concatenate, de-duplicated, order preserved."""
    seen: set[str] = set()
    merged: list[str] = []
    for items in lists:
        for item in items:
            if item not in seen:
                seen.add(item)
                merged.append(item)
    return merged


def merge_reports(
    errors: list[list[str]], warnings: list[list[str]], dropped: int, lax: bool
) -> tuple[list[str], list[str]]:
    """De-duplicate errors/warnings across components, then collapse however many
    per-component "N construct(s) would have been dropped" lines are in the merged
    warnings (up to three: validator, parser, converter) into the one true total.
    Every return path -- the two early returns and the main one -- must go through
    this, or a hard failure alongside lax drops prints one stale count line per
    component instead of one correct combined line. The line is a lax-mode line
    only: in strict mode the drops are already the errors. `pmspec validate` and
    the MCP validate_experiment_spec run the same three components and combine
    their reports through this too."""
    merged_errors = _merge(*errors)
    merged_warnings = [w for w in _merge(*warnings) if not _DROP_COUNT_LINE_RE.match(w)]
    if dropped and lax:
        merged_warnings.append(f"{dropped} construct(s) would have been dropped (lax mode)")
    return merged_errors, merged_warnings


def parser_seed(seed: int | None) -> int | None:
    """The seed for the parser's stream (pools, `!file` selections): ``seed + 1``.
    The converter's design-expansion stream draws from ``seed`` itself. Two
    independent streams, one recorded number -- and ``0`` is a seed, so this
    tests ``is not None``, never truthiness."""
    return None if seed is None else seed + 1


def spec_seed(raw: Any, runs: bool) -> Any:
    """The seed a spec declares for itself, exactly as written.

    A runs spec has no ``design:`` block to hold ``design.seed`` (it is
    refused), so its seed is a root ``seed:`` key; a fleet spec's is
    ``design.seed``. Never the base lock's: that number drew the
    randomization, and a runs lock's draws are a different question.

    Returned *unvalidated* -- a `seed: later` comes back as the string, because
    ``compile_spec`` is the one caller that must name it in an error rather
    than quietly ignore it. Everything else wants :func:`effective_seed`.
    """
    if not isinstance(raw, dict):
        return None
    if runs:
        return raw.get("seed")
    design = raw.get("design")
    return design.get("seed") if isinstance(design, dict) else None


def effective_seed(raw: Any, runs: bool, override: int | None = None) -> int | None:
    """The seed a parser/converter should actually run with: *override* if
    given, else the spec's own declared seed when it is a usable integer.

    The one resolution `compile`, `validate`, `preview` and `summary` share, so
    a spec that compiles cannot fail validation for want of a seed -- which is
    exactly what happened to `assign:`, whose draws are refused unseeded while
    validate was handing its converter nothing but ``--seed``.

    A declared seed that is not an integer yields ``None`` here rather than
    reaching ``parser_seed``, which would raise `TypeError` on ``"later" + 1``;
    compile reports it, and the readers below degrade to unseeded instead of
    crashing.
    """
    if override is not None:
        return override
    declared = spec_seed(raw, runs)
    if isinstance(declared, bool) or not isinstance(declared, int):
        return None
    return declared


def parse_and_convert(
    spec_text: str,
    *,
    base_dir: Path | None,
    seed: int | None,
    lax: bool = False,
    resolve_times: bool = True,
    report_unknown_items: bool = True,
    raw: dict[str, Any] | None = None,
    runs: bool = False,
) -> tuple[ParsedSpec, ConversionResult]:
    """Parse and convert a spec under the one seed rule (see ``parser_seed``).

    Every path that expands a spec -- compile_spec, the summary generator,
    `pmspec preview`/`validate` -- goes through here, so a summary and a lock
    built from the same seed draw the same random pool rows. Nothing else in
    the pipeline may construct a seeded SpecParser or SpecConverter.

    *raw* is an already-loaded, already-`!file`-resolved spec mapping to parse
    instead of *spec_text* -- the runs path, where ``from_base:`` has rewritten
    the crawler list before anything is parsed. *runs* puts the parser and the
    converter in runs mode (no experiment of its own; queues only)."""
    parser = SpecParser(
        base_dir=base_dir,
        seed=parser_seed(seed),
        lax=lax,
        report_unknown_items=report_unknown_items,
        runs=runs,
    )
    parsed = parser.parse_raw(raw) if raw is not None else parser.parse(spec_text)
    converter = SpecConverter(seed=seed, resolve_times=resolve_times, lax=lax, runs=runs)
    return parsed, converter.convert(parsed)


def load_spec_raw(
    spec_text: str, *, base_dir: Path, seed: int | None
) -> tuple[Any, list[str], list[str]]:
    """The raw YAML of a spec, `!file` tags resolved.

    Returns ``(raw, errors, warnings)``. The errors are the YAML parse error
    (with ``raw`` None) *or* whatever the `!file` loader could not do -- a
    missing file, an unreadable one, a selection larger than the pool. Every
    caller must merge them into its own report: on the fleet path the parser
    re-loads the text and names them a second time, but the runs path parses
    the mapping this function returns and never builds a loader of its own, so
    a `workflow: !file {path: ./gone.yaml}` would otherwise compile to an empty
    workflow and validate as PASSED.
    """
    FileTagLoader, file_loader = create_file_tag_loader(base_dir=base_dir, seed=parser_seed(seed))
    try:
        raw = yaml.load(spec_text, Loader=FileTagLoader)
    except yaml.YAMLError as e:
        return None, [f"YAML parse error: {e}"], []
    resolved = resolve_file_references(raw, file_loader)
    return resolved, list(file_loader.errors), list(file_loader.warnings)


def prepare_runs_spec(raw: Any, *, base_dir: Path) -> tuple[Any, BaseLock | None, list[str]]:
    """For a spec carrying ``runs_for:``: load its base fleet lock and expand
    every ``from_base:`` crawler block against it.

    Returns ``(raw, base, errors)`` -- ``base`` is None for a fleet spec (and
    for a runs spec whose base could not be loaded, which is a hard error).
    Shared by ``compile_spec`` and ``pmspec validate`` so the two cannot
    disagree about what a runs spec is."""
    if not isinstance(raw, dict) or "runs_for" not in raw:
        return raw, None, []
    runs_for = raw["runs_for"]
    if not isinstance(runs_for, str) or not runs_for.strip():
        # `str(None)` is the string "None", which would go on to be reported as
        # a missing file called None -- an answer about the wrong question.
        return raw, None, ["runs_for: a fleet lock path is required"]
    try:
        base = load_base(base_dir, runs_for)
    except LockError as e:
        return raw, None, list(e.errors)
    expanded, errors = expand_from_base(raw, base)
    return expanded, base, errors


def recorded_spec_path(spec_path: str, repo_dir: Path) -> str:
    """The spec path as the lock records it: relative to the spec repo's toplevel,
    so the same spec compiled from any cwd (or any checkout) yields the same bytes;
    relative to the cwd when there is no repo; as given otherwise."""
    resolved = Path(spec_path).resolve()
    top = git_toplevel(repo_dir)
    if top is not None and resolved.is_relative_to(top):
        return os.path.relpath(resolved, top)
    cwd = Path.cwd().resolve()
    if resolved.is_relative_to(cwd):
        return os.path.relpath(resolved, cwd)
    return spec_path


def compile_spec(
    spec_text: str,
    *,
    base_dir: Path,
    spec_path: str,
    seed: int | None,
    anchor: str | None = None,
    generator: str | None = None,
    lax: bool = False,
    git_root: Path | None = None,
) -> CompileResult:
    # The raw YAML, fully resolved (!file tags included), is what the strict
    # contract validates -- structurally and semantically, before a single
    # construct has been expanded or converted.
    raw, load_errors, load_warnings = load_spec_raw(spec_text, base_dir=base_dir, seed=seed)
    if raw is None and load_errors:
        return CompileResult(None, load_errors, load_warnings)

    # A spec naming a fleet lock with `runs_for:` is a *runs* spec: its
    # `from_base:` blocks are expanded against that lock before anything else
    # sees the spec, and every component below runs in runs mode.
    raw, base, runs_errors = prepare_runs_spec(raw, base_dir=base_dir)
    is_runs = isinstance(raw, dict) and "runs_for" in raw
    if is_runs and base is None:
        return CompileResult(None, _merge(load_errors, runs_errors), load_warnings)

    validator = SpecValidator(base_dir=base_dir, lax=lax, runs=is_runs)
    validation = validator.validate(raw if isinstance(raw, dict) else {})

    # The validator above already names every unknown workflow item; the parser
    # must not report it a second time (report_unknown_items=False).
    parsed, result = parse_and_convert(
        spec_text,
        base_dir=base_dir,
        seed=seed,
        lax=lax,
        resolve_times=False,
        report_unknown_items=False,
        raw=raw if is_runs else None,
        runs=is_runs,
    )
    if parsed.errors:
        dropped = validation.dropped + parsed.dropped
        errors, warnings = merge_reports(
            [load_errors, validation.errors, parsed.errors],
            [load_warnings, validation.warnings, parsed.warnings],
            dropped,
            lax,
        )
        return CompileResult(None, errors, warnings, dropped)

    declared_seed = spec_seed(raw, is_runs)
    if declared_seed is not None and (
        isinstance(declared_seed, bool) or not isinstance(declared_seed, int)
    ):
        # Caught before the re-parse below, which would otherwise fail on
        # `parser_seed("later")` with a TypeError instead of a message.
        dropped = validation.dropped + parsed.dropped
        key = "seed" if is_runs else "design.seed"
        errors, warnings = merge_reports(
            [load_errors, validation.errors, [f"{key}: must be an integer, got {declared_seed!r}"]],
            [load_warnings, validation.warnings, parsed.warnings],
            dropped,
            lax,
        )
        return CompileResult(None, errors, warnings, dropped)
    run_seed = effective_seed(raw, is_runs, seed)
    if run_seed is None:
        dropped = validation.dropped + parsed.dropped
        no_seed = (
            "no seed: pass --seed N or set a root seed: in the runs spec; "
            "a lock is never unseeded"
            if is_runs
            else "no seed: pass --seed N or set design.seed; a lock is never unseeded"
        )
        errors, warnings = merge_reports(
            [load_errors, validation.errors, [no_seed]],
            [load_warnings, validation.warnings, parsed.warnings],
            dropped,
            lax,
        )
        return CompileResult(None, errors, warnings, dropped)
    if seed is None:
        # The spec's own seed only: redo both streams from it, so the pool
        # draws are seeded from the same number the lock records.
        if is_runs:
            # The runs path parses the mapping loaded above, and that load ran
            # under the *flag* -- None here -- so a `!file {select: random}`
            # drew from an unseeded Random and the lock's bytes moved every
            # compile. Reload and re-expand under the spec's own seed; the
            # fleet branch reaches the same place by re-reading `spec_text`.
            raw, reload_errors, reload_warnings = load_spec_raw(
                spec_text, base_dir=base_dir, seed=run_seed
            )
            raw, base, reload_runs_errors = prepare_runs_spec(raw, base_dir=base_dir)
            load_errors = _merge(load_errors, reload_errors, reload_runs_errors)
            load_warnings = _merge(load_warnings, reload_warnings)
            if base is None:
                return CompileResult(None, load_errors, load_warnings)
        parsed, result = parse_and_convert(
            spec_text,
            base_dir=base_dir,
            seed=run_seed,
            lax=lax,
            resolve_times=False,
            report_unknown_items=False,
            raw=raw if is_runs else None,
            runs=is_runs,
        )

    # Treatment values are concrete only now (`with:` and design variables
    # substituted); the raw pass above skipped templated ones.
    expanded_errors = validator.validate_expanded(result.json_data)
    dropped = validation.dropped + parsed.dropped + result.dropped
    # A name with no bot behind it is a session that would go nowhere: checked
    # after conversion, so a name built from `{base.name}` or a design variable
    # is judged as the string that would actually be uploaded.
    name_errors = (
        unknown_name_errors([str(c.get("name", "")) for c in result.json_data], base)
        if base is not None
        else []
    )
    errors, warnings = merge_reports(
        [
            load_errors,
            validation.errors,
            parsed.errors,
            result.errors,
            expanded_errors,
            runs_errors,
            name_errors,
        ],
        [load_warnings, validation.warnings, parsed.warnings, result.warnings],
        dropped,
        lax,
    )

    crawlers: list[dict[str, Any]] = result.json_data
    literal = find_literal_credentials(crawlers)
    if literal:
        errors.extend(
            f"{path}: literal credential; use ${{secret:<key>}} so the lock can be committed"
            for path in literal
        )

    # Before the anchor, not after: a spec that already failed may carry a queue
    # whose `start_at` the converter could not resolve (a `{from, jitter}` with
    # an unusable jitter, say), and `derive_anchor` is written for the strings a
    # *valid* conversion produces. Reporting the real error beats a traceback
    # from the shape it left behind.
    if errors:
        return CompileResult(None, errors, warnings, dropped)

    raw_starts = [q["start_at"] for c in crawlers for q in c.get("queues", [])]
    try:
        anchor_value, offsets = derive_anchor(raw_starts, override=anchor)
    except AnchorError as e:
        errors.append(str(e))
        anchor_value, offsets = "upload", []

    if errors:
        return CompileResult(None, errors, warnings, dropped)

    it = iter(offsets)
    for crawler in crawlers:
        for queue in crawler.get("queues", []):
            queue.pop("start_at", None)
            queue["start_offset"] = next(it)
            # Canonical queue key order: name, description, start_offset, then the rest.
            ordered = {k: queue[k] for k in ("name", "description", "start_offset") if k in queue}
            ordered.update({k: v for k, v in queue.items() if k not in ordered})
            queue.clear()
            queue.update(ordered)

    repo_dir = git_root or base_dir
    pools: dict[str, str] = {}
    if parsed.pool_manager is not None:
        for pool_name in sorted(parsed.pool_manager.sources):
            path = parsed.pool_manager.sources[pool_name]
            pools[pool_name] = hashlib.sha256(path.read_bytes()).hexdigest()
    provenance = Provenance(
        spec_path=recorded_spec_path(spec_path, repo_dir),
        spec_sha256=sha256_text(spec_text),
        generator=generator or parsed.experiment.get("generator"),
        git_commit=git_commit(repo_dir),
        pmspec_version=pmspec_version(),
        seed=int(run_seed),
        lax=lax,
        pools=pools,
        base_lock_sha256=base.sha256 if base is not None else None,
        base_spec_path=base.path if base is not None else None,
    )
    # A runs lock's experiment is the base's, verbatim: upload refuses a
    # mismatch, and a second spelling of the name here would be a second
    # source of truth for it.
    experiment = (
        dict(base.lock.experiment)
        if base is not None
        else {
            "name": result.experiment_name,
            "description": result.experiment_description,
            "runner_user_id": result.experiment_runner_user_id,
            "contact": result.experiment_contact,
        }
    )
    lock = Lock(
        provenance=provenance,
        experiment=experiment,
        schedule={"anchor": anchor_value},
        crawlers=crawlers,
        kind="runs" if base is not None else "fleet",
    )
    return CompileResult(lock, [], warnings, dropped)
