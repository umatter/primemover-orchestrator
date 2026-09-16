"""`{prefix.name}` substitution, one namespace at a time.

A *named* placeholder carries its namespace in the token itself --
``{base.ideology}``, ``{panelist.person_id}`` -- which is what keeps it apart
from the design expander's bare ``{arm}`` / ``{replica:02d}`` placeholders and
from a template's ``${var}``. Two features share it: `from_base:` in a runs
spec (this task) and `assign:` per-crawler pool rows.

An unknown column is left exactly as written. That is deliberate: the caller
cannot tell a typo from a placeholder another pass will fill, so the decision
belongs to the converter's unresolved-variable scan, which reports whatever
survives every pass as a drop.
"""

from __future__ import annotations

import re
from typing import Any

# A column may carry a hyphen (CSV headers do); the prefix may not, so it can
# never swallow a bare `{name}` design placeholder.
NAMED_RE = re.compile(r"\{(?P<prefix>[A-Za-z_]\w*)\.(?P<col>[A-Za-z_][\w-]*)(?::(?P<fmt>[^}]+))?\}")


def substitute_named(text: str, prefix: str, values: dict[str, Any]) -> str:
    """Replace every ``{<prefix>.<col>}`` / ``{<prefix>.<col>:<fmt>}`` in *text*.

    Tokens with another prefix, and tokens whose column is not in *values*, are
    returned untouched. A format spec that the value cannot satisfy raises
    ``ValueError`` naming the column -- silently leaving the token in place
    would turn an author's formatting mistake into a literal the bot receives.
    """

    def replace(match: re.Match[str]) -> str:
        if match.group("prefix") != prefix:
            return match.group(0)
        column = match.group("col")
        if column not in values:
            return match.group(0)
        value = values[column]
        fmt = match.group("fmt")
        if fmt is None:
            return str(value)
        try:
            return format(value, fmt)
        except (ValueError, TypeError) as e:
            raise ValueError(f"{{{prefix}.{column}:{fmt}}}: cannot format {value!r} ({e})") from e

    return NAMED_RE.sub(replace, text)


def substitute_named_in(node: Any, prefix: str, values: dict[str, Any]) -> Any:
    """``substitute_named`` over every string in a nested structure.

    Dict *keys* are left alone: every placeholder this serves sits in a value.
    """
    if isinstance(node, str):
        return substitute_named(node, prefix, values)
    if isinstance(node, dict):
        return {k: substitute_named_in(v, prefix, values) for k, v in node.items()}
    if isinstance(node, list):
        return [substitute_named_in(v, prefix, values) for v in node]
    return node
