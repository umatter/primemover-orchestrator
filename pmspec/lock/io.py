"""Canonical lock serialization. Same lock in → same bytes out, always."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from .model import Lock, LockError, check_runs_crawlers

_SCHEMA_PATHS = {
    1: Path(__file__).parent.parent / "schema" / "lock-1.schema.json",
    2: Path(__file__).parent.parent / "schema" / "lock-2.schema.json",
}


class _CanonicalDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        # Without this override, PyYAML writes block-sequence items at the
        # same indentation as their parent key (e.g. "crawlers:\n- name:
        # ..."), which is valid YAML but breaks the naive "does this line
        # start with a space" top-level-key check used elsewhere (and looks
        # like a stray top-level key). Force real indentation instead.
        super().increase_indent(flow=flow, indentless=False)


_RESOLVER = yaml.resolver.Resolver()


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    # Quote anything YAML would otherwise re-type (timestamps, "yes", numbers),
    # so a round trip through any YAML reader gives the string back.
    plain = _RESOLVER.resolve(yaml.ScalarNode, value, (True, False))
    style = None if plain == "tag:yaml.org,2002:str" else '"'
    if "\n" in value:
        style = "|"
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_CanonicalDumper.add_representer(str, _represent_str)


def dump_lock(lock: Lock) -> str:
    text: str = yaml.dump(
        lock.to_dict(),
        Dumper=_CanonicalDumper,
        sort_keys=False,
        allow_unicode=True,
        width=10**6,
        default_flow_style=False,
    )
    return text


def validate_lock_dict(d: dict[str, Any]) -> list[str]:
    import jsonschema

    version = d.get("pmspec_lock")
    schema_path = None
    # bool is an int subclass in Python, so isinstance(version, int) alone is
    # True for `pmspec_lock: true` (== 1) -- excluded explicitly so it can't
    # silently validate against the lock-1 schema.
    if isinstance(version, int) and not isinstance(version, bool):
        schema_path = _SCHEMA_PATHS.get(version)
    if schema_path is None:
        return [f"pmspec_lock: expected 1 or 2, got {version!r}"]

    with open(schema_path) as f:
        schema = json.load(f)
    validator = jsonschema.Draft202012Validator(schema)
    errors = []
    for e in sorted(validator.iter_errors(d), key=lambda e: list(e.absolute_path)):
        path = "/".join(str(p) for p in e.absolute_path) or "<root>"
        errors.append(f"{path}: {e.message}")
    return errors


def load_lock(text: str) -> Lock:
    try:
        d = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise LockError([f"YAML parse error: {e}"]) from e
    if not isinstance(d, dict):
        raise LockError(["not a mapping"])
    if d.get("kind") == "runs":
        # Ahead of the schema on purpose. The runs crawler item is
        # `additionalProperties: false`, so a hand-edited lock carrying a
        # leftover `agent:` would otherwise be told "Additional properties are
        # not allowed" -- true, and no help at all. The schema stays as the
        # second net for anything this does not name.
        check_runs_crawlers(d.get("crawlers"))
    errors = validate_lock_dict(d)
    if errors:
        raise LockError(errors)
    return Lock.from_dict(d)


def is_lock_document(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped.startswith("pmspec_lock:")
    return False


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
