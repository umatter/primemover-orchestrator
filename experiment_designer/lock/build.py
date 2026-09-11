"""spec -> lock. Everything random is drawn here, once, from one seed."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from experiment_designer.spec.converter import SpecConverter
from experiment_designer.spec.parser import SpecParser

from .anchor import AnchorError, derive_anchor
from .io import sha256_text
from .model import Lock, Provenance
from .provenance import git_commit, git_toplevel, pmspec_version
from .secrets import find_literal_credentials


@dataclass
class CompileResult:
    lock: Lock | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


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
    # Pools/file selections draw from seed+1, the design expander from seed:
    # two independent streams, one recorded number.
    parser = SpecParser(base_dir=base_dir, seed=None if seed is None else seed + 1)
    parsed = parser.parse(spec_text)
    if parsed.errors:
        return CompileResult(None, list(parsed.errors), list(parsed.warnings))

    effective_seed = seed if seed is not None else (parsed.design or {}).get("seed")
    if effective_seed is None:
        return CompileResult(
            None,
            ["no seed: pass --seed N or set design.seed; a lock is never unseeded"],
            list(parsed.warnings),
        )
    if seed is None:
        # design.seed only: re-parse so the pool stream is seeded from it too.
        parser = SpecParser(base_dir=base_dir, seed=effective_seed + 1)
        parsed = parser.parse(spec_text)

    converter = SpecConverter(seed=effective_seed, resolve_times=False)
    result = converter.convert(parsed)
    errors = list(result.errors)
    warnings = list(result.warnings)

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
        return CompileResult(None, errors, warnings)

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
    return CompileResult(lock, [], warnings)
