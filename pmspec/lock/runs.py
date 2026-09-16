"""The base half of a runs spec: load the fleet lock it names, and expand
`from_base: true` into one crawler block per base crawler.

A runs spec adds sessions to bots that already exist. Everything about *which*
bots exist -- their names, their locations, their treatment preferences -- is
the fleet lock's answer, so this module reads it and hands the values to the
spec as `{base.<var>}` substitution variables. Nothing here randomizes: the
randomization already happened, once, in the base.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pmspec.spec.substitute import NamedFormatError, substitute_named

from .io import load_lock, sha256_text
from .model import Lock, LockError


@dataclass
class BaseLock:
    """A fleet lock a runs spec compiles against."""

    lock: Lock
    sha256: str
    #: What the runs lock records as ``provenance.base_spec_path`` -- the *fleet
    #: lock's* own recorded spec path, not the path the runs spec named it by.
    path: str

    @property
    def names(self) -> list[str]:
        return [str(c.get("name", "")) for c in self.lock.crawlers]


def load_base(base_dir: Path, runs_for: str) -> BaseLock:
    """Load the fleet lock at ``runs_for`` (relative to the runs spec's directory).

    Raises ``LockError`` when the file is missing, is not a lock, or is itself a
    runs lock -- a runs lock is bound to exactly one base, and chaining them
    would make "which fleet was this bot randomized into" unanswerable.
    """
    if not runs_for or not str(runs_for).strip():
        raise LockError(["runs_for: a fleet lock path is required"])
    path = Path(runs_for)
    if not path.is_absolute():
        path = base_dir / path
    try:
        text = path.read_text()
    except OSError as e:
        raise LockError([f"runs_for: cannot read {runs_for}: {e.strerror or e}"]) from e
    lock = load_lock(text)
    if lock.is_runs:
        raise LockError(
            [f"runs_for: {runs_for} is a runs lock; a runs spec compiles against a fleet lock"]
        )
    return BaseLock(lock=lock, sha256=sha256_text(text), path=lock.provenance.spec_path)


def base_variables(crawler: dict[str, Any]) -> dict[str, str]:
    """The `{base.<var>}` variables one base crawler exposes.

    ``name``, ``location`` (the *agent's*, which is what a bot's geography
    actually is) and one entry per ``configuration.preferences`` name. Values
    are strings: a preference is a string on the wire, and a runs spec
    substitutes them into strings.
    """
    values: dict[str, str] = {}
    for preference in (crawler.get("configuration") or {}).get("preferences") or []:
        if isinstance(preference, dict) and preference.get("name") is not None:
            values[str(preference["name"])] = str(preference.get("value", ""))
    # Written after the preferences, so the crawler's own name and its agent's
    # location win over any preference of the same name. Both cases are real: a
    # fleet lock always carries a `location` *preference* (the parser adds one
    # from `defaults.location`), and a study is free to name a preference
    # `name`. `{base.name}` and `{base.location}` mean the bot's identity and
    # the bot's geography -- what a runs spec is actually addressing -- and a
    # preference that shadowed either would make them mean something else on
    # some fleets and not others.
    values["name"] = str(crawler.get("name", ""))
    values["location"] = str((crawler.get("agent") or {}).get("location", ""))
    return values


def _substitute_block(
    node: Any,
    values: dict[str, str],
    path: str,
    failures: dict[tuple[str, str], list[NamedFormatError]],
) -> Any:
    """``substitute_named`` over a crawler block, recording where each format
    failure happened rather than raising on the first one.

    The path is what makes the collapsed message useful: one bad token in a
    block expanded over 500 bots is one mistake in one field, and saying so
    once (with the count) beats 500 lines that differ only in a value.
    """
    if isinstance(node, str):
        try:
            return substitute_named(node, "base", values)
        except NamedFormatError as e:
            failures.setdefault((path, e.token), []).append(e)
            return node
    if isinstance(node, dict):
        return {
            k: _substitute_block(v, values, f"{path}.{k}" if path else str(k), failures)
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_substitute_block(v, values, f"{path}[{i}]", failures) for i, v in enumerate(node)]
    return node


def expand_from_base(raw: dict[str, Any], base: BaseLock) -> tuple[dict[str, Any], list[str]]:
    """Replace every ``from_base: true`` crawler block with one block per base
    crawler, in base order, with ``{base.<var>}`` substituted throughout.

    Returns the rewritten spec and any errors (a format spec the base value
    cannot satisfy). A block with no ``from_base`` is copied through untouched,
    so explicit and generated crawlers can sit side by side in one spec.

    A ``{base.x}`` naming no variable is left in place on purpose -- the
    converter's unresolved-variable scan reports it with a full path, which is
    more use than an error raised here without one.
    """
    crawlers = raw.get("crawlers")
    if not isinstance(crawlers, list):
        return raw, []

    errors: list[str] = []
    expanded: list[Any] = []
    for i, crawler in enumerate(crawlers):
        if not isinstance(crawler, dict) or not crawler.get("from_base"):
            expanded.append(crawler)
            continue
        template = {k: v for k, v in crawler.items() if k != "from_base"}
        failures: dict[tuple[str, str], list[NamedFormatError]] = {}
        for base_crawler in base.lock.crawlers:
            values = base_variables(base_crawler)
            expanded.append(_substitute_block(template, values, "", failures))
        for (path, token), failed in failures.items():
            errors.append(
                f"crawler[{i}].{path}: {token} cannot be formatted from the base value "
                f"({len(failed)} crawler(s); e.g. {failed[0].value!r})"
            )
    return {**raw, "crawlers": expanded}, errors


def unknown_name_errors(names: list[str], base: BaseLock) -> list[str]:
    """One error per crawler name the base lock does not have.

    A runs lock is applied by *name*; a name with no bot behind it is a session
    that would silently go nowhere.
    """
    known = set(base.names)
    return [
        f"runs spec names crawler '{name}' which the base lock does not have"
        for name in names
        if name not in known
    ]
