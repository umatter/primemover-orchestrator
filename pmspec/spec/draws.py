"""Per-bot template draws: deferred pool-draw tokens.

A `with:` value on a `template:`/`repeat:` workflow item may be a pool draw
(``{from_pool: <pool>, field: <field>, select: random|first}``) instead of a
literal. The parser cannot resolve a draw itself -- which row a crawler gets
depends on the crawler index and the lock's seed, both known only to the
converter -- so it substitutes a deferred token in place of the value and the
converter resolves it later, once per crawler, without replacement.

The token is never written to a lock: a surviving token is a strict-contract
drop (see ``drops.py``) and the compile fails.
"""

from __future__ import annotations

import random
import re
from typing import Any

# Pool/field names a token can round-trip through: letters, digits, `_ . -`.
# Anything else cannot be embedded in a token unambiguously, so it is a drop
# naming the offending key rather than a token `DRAW_TOKEN_RE` could ever match.
_NAME_PATTERN = r"[A-Za-z0-9_.-]+"
_NAME_RE = re.compile(_NAME_PATTERN)

# <pool>:<field>:<select>:<ordinal> -- ordinal is this draw's index within the
# `with:` block it came from (or, under `repeat:`, within its repetition), so
# two keys drawing from the same pool are distinct draws.
DRAW_TOKEN_RE = re.compile(
    rf"%%pmspec-draw:({_NAME_PATTERN}):({_NAME_PATTERN}):(random|first):(\d+)%%"
)

#: Anything that *looks like* a draw token, well-formed or not. The strict
#: scan uses this one: a token `DRAW_TOKEN_RE` cannot parse (an invalid
#: select, a truncated body) is still a literal that must not reach a bot.
ANY_DRAW_TOKEN_RE = re.compile(r"%%pmspec-draw:[^%]*%%")

#: The only `select` modes a draw accepts.
DRAW_SELECTS = frozenset({"random", "first"})

_TOKEN_FMT = "%%pmspec-draw:{pool}:{field}:{select}:{ordinal}%%"


def make_token(pool: str, field: str, select: str, ordinal: int) -> str:
    """Build a deferred draw token."""
    return _TOKEN_FMT.format(pool=pool, field=field, select=select, ordinal=ordinal)


def parse_token(text: str) -> tuple[str, str, str, int] | None:
    """Parse a deferred draw token. Fullmatch only -- a token embedded in a
    larger string (e.g. via further substitution) does not count."""
    match = DRAW_TOKEN_RE.fullmatch(text)
    if match is None:
        return None
    pool, field, select, ordinal = match.groups()
    return pool, field, select, int(ordinal)


def is_valid_name(value: str) -> bool:
    """Whether `value` fits the token charset -- the only names `make_token`
    can embed and `DRAW_TOKEN_RE`/`parse_token` can later recover."""
    return bool(_NAME_RE.fullmatch(value))


def select_error(value: dict[str, Any]) -> str | None:
    """Why this draw's `select` is unusable, or ``None`` when it is absent or
    one of :data:`DRAW_SELECTS`. Shared by the parser and the validator so a
    `select: randm` is refused with the same words on every path."""
    select = value.get("select")
    if select is None or select in DRAW_SELECTS:
        return None
    return f"select must be random or first, got {select!r}"


def is_draw(value: Any) -> bool:
    """A `with:` value is a pool draw iff it is a dict carrying a string
    `from_pool` and a string `field`, both restricted to the token charset
    (anything else could not survive round-tripping through a token), and a
    `select` that is absent or one of :data:`DRAW_SELECTS` -- any other select
    would be embedded in a token `DRAW_TOKEN_RE` can never match, and ship to
    the bot as a literal. `count` is not accepted here -- one value per `with:`
    key; N distinct values are N template calls."""
    if not isinstance(value, dict):
        return False
    pool, field = value.get("from_pool"), value.get("field")
    return (
        isinstance(pool, str)
        and isinstance(field, str)
        and is_valid_name(pool)
        and is_valid_name(field)
        and select_error(value) is None
    )


def resolve_draws(crawlers: list[dict[str, Any]], pool_manager: Any, seed: int | None) -> list[str]:
    """Resolve every deferred draw token in `crawlers`, in place.

    Draws are seeded from `random.Random(f"{seed}:{i}")` for crawler `i`, so
    they are deterministic per (seed, crawler index) and differ across
    crawlers. Within one crawler, draws from the same pool never repeat a row.

    Returns the errors found along the way (unknown pool, a field missing from
    the drawn row -- naming the pool, field and the row's columns -- or a pool
    exhausted -- naming the crawler, pool and how many draws it had already
    made). A token that could not be resolved is left exactly as it was: the
    converter's strict scan reports a surviving token as a drop, so a compile
    with any such error still fails rather than writing the token to a lock.
    """
    errors: list[str] = []

    def draw(
        i: int,
        rng: random.Random,
        taken: dict[str, set[int]],
        pool_name: str,
        field_name: str,
        select: str,
    ) -> str | None:
        rows = pool_manager.rows(pool_name) if pool_manager is not None else None
        if rows is None:
            errors.append(f"crawlers[{i}]: unknown pool '{pool_name}' (define it under data_pools)")
            return None
        pool_taken = taken.setdefault(pool_name, set())
        candidates = [k for k in range(len(rows)) if k not in pool_taken]
        if not candidates:
            errors.append(
                f"crawlers[{i}]: pool '{pool_name}' exhausted after {len(pool_taken)} draw(s)"
            )
            return None
        idx = candidates[0] if select == "first" else rng.choice(candidates)
        row = rows[idx]
        if field_name not in row:
            columns = sorted(str(k) for k in row)
            pool_taken.add(idx)
            errors.append(
                f"crawlers[{i}]: pool '{pool_name}' has no field "
                f"'{field_name}' (columns: {columns})"
            )
            return None
        pool_taken.add(idx)
        return str(row[field_name])

    def resolve_text(i: int, rng: random.Random, taken: dict[str, set[int]], text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            pool_name, field_name, select, _ordinal = match.groups()
            value = draw(i, rng, taken, pool_name, field_name, select)
            return match.group(0) if value is None else value

        return DRAW_TOKEN_RE.sub(replace, text)

    def walk(i: int, rng: random.Random, taken: dict[str, set[int]], node: Any) -> Any:
        if isinstance(node, str):
            return resolve_text(i, rng, taken, node) if DRAW_TOKEN_RE.search(node) else node
        if isinstance(node, dict):
            return {
                k: (v if k == "multilogin_profile" else walk(i, rng, taken, v))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [walk(i, rng, taken, v) for v in node]
        return node

    for i, crawler in enumerate(crawlers):
        crawlers[i] = walk(i, random.Random(f"{seed}:{i}"), {}, crawler)

    return errors
