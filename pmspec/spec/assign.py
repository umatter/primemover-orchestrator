"""`assign:` -- one pool row per crawler, exposed as `{name.column}` variables.

The generalisation of `proxy: {from_pool: ...}`. That block draws one proxy row
per bot because a study needs one exit IP per bot; the same shape serves every
other per-bot datum a lab study carries -- the panelist a bot replays, its own
outlet list, today's Comscore session -- none of which a factor (shared levels)
or a sampled parameter (a scalar) can express.

```yaml
crawlers:
  - assign:
      panelist: {from_pool: panelists, match_field: region, select: random}
    configuration:
      preferences:
        - {name: comscore_person_id, value: "{panelist.person_id}"}
```

Three rules carry the feature:

*Without replacement, across the whole crawler list.* Two bots replaying one
panelist is not a smaller study, it is a broken one, so a pool that runs out is
a compile error naming the crawler, the pool and the count -- never a shared
row. The bookkeeping (`taken`) is the *same dict* `_assign_pool_proxies` uses,
so one pool serving both an `assign` and a `proxy:` cannot hand a row out
twice.

*Every column is data.* The drawn row's columns are all substitutable, anywhere
in the crawler; a column referenced nowhere is not a drop, because the row is
data and a study is free to carry a column it does not use this time. The
reverse -- a `{panelist.…}` that survives -- *is* a drop, reported by the
converter's unresolved-variable scan with a full path, which is why a failed
draw leaves its placeholders exactly as written rather than guessing a value.

*One seeded sequence per assignment name.* `random.Random(f"{seed}:assign:
{name}")` is created once for the whole crawler list, so crawler i's draw
depends on the draws before it -- which is what "without replacement" means.
A per-crawler RNG would make each draw independent of the others and turn the
free-row set into the only thing keeping them apart.
"""

from __future__ import annotations

import random
from typing import Any

from .substitute import NAMED_RE, NamedFormatError, substitute_named

#: The keys one `assign` entry may carry. The validator's whitelist (anything
#: else is a drop) and the schema's property set, from one declaration.
ASSIGN_KEYS = frozenset({"from_pool", "match_field", "match_value", "select"})

#: The `select` modes an assignment accepts; `random` is the default.
ASSIGN_SELECTS = frozenset({"random", "first"})


def _substitute_crawler(
    crawler: dict[str, Any], prefix: str, values: dict[str, Any], failures: dict[str, Any]
) -> dict[str, Any]:
    """`substitute_named` over every string of a crawler block, recording
    format failures instead of raising on the first one.

    The failing string is returned untouched: it keeps its `{name.col:fmt}`
    token, which the converter's unresolved scan then reports as a drop, so a
    compile with a formatting mistake fails twice over rather than writing a
    half-formatted literal to a lock.
    """

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            if not NAMED_RE.search(node):
                return node
            try:
                return substitute_named(node, prefix, values)
            except NamedFormatError as e:
                failures.setdefault(e.token, e)
                return node
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(crawler)  # type: ignore[no-any-return]


def resolve_assignments(
    crawlers: list[dict[str, Any]],
    pool_manager: Any,
    seed: int | None,
    taken: dict[str, set[int]],
) -> list[str]:
    """Draw one pool row per `assign` entry per crawler and substitute it.

    Runs after design expansion (so `match_value: "{city}"` and a `from_base:`
    block's `{base.…}` are already concrete) and before every later
    substitution pass, so an assigned value can feed a pool scoring context and
    a template's `with:` values.

    `crawlers` is rewritten in place (each crawler replaced by a substituted
    copy); `taken` is the shared per-pool set of row indices already handed
    out. Returns the errors found. Every failure leaves the crawler's
    `{name.col}` placeholders as written -- the converter's unresolved scan
    reports them, so no compile that hit one can produce a lock -- but the
    `assign` block itself is always removed, failure or not: nothing
    downstream reads it, and leaving it would put a pool name in the API JSON.
    """
    errors: list[str] = []
    rngs: dict[str, random.Random] = {}

    def draw(i: int, crawler: dict[str, Any], name: str, entry: Any) -> dict[str, Any] | None:
        """The row this crawler's `name` assignment gets, or None with an error."""
        prefix = f"crawlers[{i}].assign.{name}"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: must be a mapping with `from_pool:`")
            return None
        pool_name = entry.get("from_pool")
        if not pool_name:
            errors.append(f"{prefix}: from_pool is required")
            return None
        pool_name = str(pool_name)
        rows = pool_manager.rows(pool_name) if pool_manager is not None else None
        if rows is None:
            errors.append(f"{prefix}: unknown pool '{pool_name}' (define it under data_pools)")
            return None
        if seed is None:
            # Every draw is seeded, `select: first` included: the seed is what
            # makes a lock reproducible, and a spec that draws half its rows
            # deterministically and half not is not a randomization record.
            errors.append(f"{prefix}: assignments need a seed")
            return None
        select = entry.get("select") or "random"
        if select not in ASSIGN_SELECTS:
            errors.append(f"{prefix}: select must be random or first, got {select!r}")
            return None

        field = entry.get("match_field")
        where = ""
        if field:
            value = entry.get("match_value")
            if value is None:
                value = (crawler.get("agent") or {}).get("location")
            if value in (None, ""):
                errors.append(f"{prefix}: match_field needs agent.location or match_value")
                return None
            value = str(value)
            where = f" with {field} == {value!r}"
            candidates = [k for k, row in enumerate(rows) if str(row.get(field)) == value]
        else:
            candidates = list(range(len(rows)))

        pool_taken = taken.setdefault(pool_name, set())
        free = [k for k in candidates if k not in pool_taken]
        if not candidates:
            errors.append(f"{prefix}: pool '{pool_name}' has no row{where}")
            return None
        if not free:
            errors.append(
                f"{prefix}: pool '{pool_name}' has no free row{where} "
                f"({len(candidates) - len(free)} taken)"
            )
            return None

        if select == "first":
            index = free[0]
        else:
            index = rngs.setdefault(name, random.Random(f"{seed}:assign:{name}")).choice(free)
        pool_taken.add(index)
        row: dict[str, Any] = rows[index]
        return row

    for i, crawler in enumerate(crawlers):
        if not isinstance(crawler, dict):
            continue
        assignments = crawler.get("assign")
        if not isinstance(assignments, dict) or not assignments:
            if "assign" in crawler:
                crawlers[i] = {k: v for k, v in crawler.items() if k != "assign"}
            continue
        resolved = {k: v for k, v in crawler.items() if k != "assign"}
        for name, entry in assignments.items():
            row = draw(i, crawler, str(name), entry)
            if row is None:
                continue
            failures: dict[str, NamedFormatError] = {}
            resolved = _substitute_crawler(resolved, str(name), row, failures)
            for token, failure in failures.items():
                errors.append(f"crawlers[{i}]: {token} cannot format {failure.value!r}")
        crawlers[i] = resolved

    return errors
