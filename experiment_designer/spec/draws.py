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

import re
from typing import Any

# <pool>:<field>:<select>:<ordinal> -- ordinal is this draw's index within the
# `with:` block it came from (or, under `repeat:`, within its repetition), so
# two keys drawing from the same pool are distinct draws.
DRAW_TOKEN_RE = re.compile(
    r"%%pmspec-draw:([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):(random|first):(\d+)%%"
)

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


def is_draw(value: Any) -> bool:
    """A `with:` value is a pool draw iff it is a dict carrying a string
    `from_pool` and a string `field`. `count` is not accepted here -- one
    value per `with:` key; N distinct values are N template calls."""
    if not isinstance(value, dict):
        return False
    return isinstance(value.get("from_pool"), str) and isinstance(value.get("field"), str)
