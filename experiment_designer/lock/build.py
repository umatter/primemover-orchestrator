"""spec -> lock. Everything random is drawn here, once, from one seed."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from experiment_designer.spec.converter import SpecConverter
from experiment_designer.spec.file_loader import create_file_tag_loader, resolve_file_references
from experiment_designer.spec.parser import SpecParser
from experiment_designer.spec.validator import SpecValidator

from .anchor import AnchorError, derive_anchor
from .io import sha256_text
from .model import Lock, Provenance
from .provenance import git_commit, git_toplevel, pmspec_version
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


def _finish(
    errors: list[list[str]], warnings: list[list[str]], dropped: int, lax: bool
) -> tuple[list[str], list[str]]:
    """De-duplicate errors/warnings across components, then collapse however many
    per-component "N construct(s) would have been dropped" lines are in the merged
    warnings (up to three: validator, parser, converter) into the one true total.
    Every return path -- the two early returns and the main one -- must go through
    this, or a hard failure alongside lax drops prints one stale count line per
    component instead of one correct combined line. The line is a lax-mode line
    only: in strict mode the drops are already the errors."""
    merged_errors = _merge(*errors)
    merged_warnings = [w for w in _merge(*warnings) if not _DROP_COUNT_LINE_RE.match(w)]
    if dropped and lax:
        merged_warnings.append(f"{dropped} construct(s) would have been dropped (lax mode)")
    return merged_errors, merged_warnings


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
    FileTagLoader, file_loader = create_file_tag_loader(
        base_dir=base_dir, seed=None if seed is None else seed + 1
    )
    try:
        raw = yaml.load(spec_text, Loader=FileTagLoader)
        raw = resolve_file_references(raw, file_loader)
    except yaml.YAMLError as e:
        return CompileResult(None, [f"YAML parse error: {e}"], [])
    validation = SpecValidator(base_dir=base_dir, lax=lax).validate(
        raw if isinstance(raw, dict) else {}
    )

    # Pools/file selections draw from seed+1, the design expander from seed:
    # two independent streams, one recorded number.
    # The validator above already names every unknown workflow item; the parser
    # must not report it a second time (report_unknown_items=False).
    parser = SpecParser(
        base_dir=base_dir,
        seed=None if seed is None else seed + 1,
        lax=lax,
        report_unknown_items=False,
    )
    parsed = parser.parse(spec_text)
    if parsed.errors:
        dropped = validation.dropped + parsed.dropped
        errors, warnings = _finish(
            [validation.errors, parsed.errors],
            [validation.warnings, parsed.warnings],
            dropped,
            lax,
        )
        return CompileResult(None, errors, warnings, dropped)

    effective_seed = seed if seed is not None else (parsed.design or {}).get("seed")
    if effective_seed is None:
        dropped = validation.dropped + parsed.dropped
        errors, warnings = _finish(
            [
                validation.errors,
                ["no seed: pass --seed N or set design.seed; a lock is never unseeded"],
            ],
            [validation.warnings, parsed.warnings],
            dropped,
            lax,
        )
        return CompileResult(None, errors, warnings, dropped)
    if seed is None:
        # design.seed only: re-parse so the pool stream is seeded from it too.
        parser = SpecParser(
            base_dir=base_dir, seed=effective_seed + 1, lax=lax, report_unknown_items=False
        )
        parsed = parser.parse(spec_text)

    converter = SpecConverter(seed=effective_seed, resolve_times=False, lax=lax)
    result = converter.convert(parsed)
    dropped = validation.dropped + parsed.dropped + result.dropped
    errors, warnings = _finish(
        [validation.errors, parsed.errors, result.errors],
        [validation.warnings, parsed.warnings, result.warnings],
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
    provenance = Provenance(
        spec_path=recorded_spec_path(spec_path, repo_dir),
        spec_sha256=sha256_text(spec_text),
        generator=generator or parsed.experiment.get("generator"),
        git_commit=git_commit(repo_dir),
        pmspec_version=pmspec_version(),
        seed=int(effective_seed),
        lax=lax,
    )
    lock = Lock(
        provenance=provenance,
        experiment={
            "name": result.experiment_name,
            "description": result.experiment_description,
            "runner_user_id": result.experiment_runner_user_id,
        },
        schedule={"anchor": anchor_value},
        crawlers=crawlers,
    )
    return CompileResult(lock, [], warnings, dropped)
