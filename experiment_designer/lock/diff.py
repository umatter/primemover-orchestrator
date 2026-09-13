"""Two locks, one question: does the reproducibility artifact still say the
same thing? Both shapes are built against the same fixed reference anchor
(2000-01-01) rather than each lock's own ``schedule.anchor``, so ``start_at``
differences compare *offsets*, not dates -- a lock re-anchored to a different
day but otherwise unchanged reads as identical apart from the anchor itself,
which is reported once, not once per queue."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .anchor import resolve_anchor
from .io import load_lock
from .verify import Difference, compare_shapes, lock_shape

_REFERENCE_ANCHOR = "2000-01-01T00:00:00"
_PROVENANCE_KEYS = ("spec_path", "spec_sha256", "git_commit", "generator", "seed", "lax")


def _provenance_lines(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    lines = ["provenance:"]
    for key in _PROVENANCE_KEYS:
        va, vb = a.get(key), b.get(key)
        if key == "spec_sha256":
            va, vb = (va or "")[:12], (vb or "")[:12]
        lines.append(f"  {key}: {va!r} vs {vb!r}")
    return lines


def _pools_changed(pools_a: dict[str, str], pools_b: dict[str, str]) -> list[str]:
    names = set(pools_a) | set(pools_b)
    return sorted(name for name in names if pools_a.get(name) != pools_b.get(name))


def _pool_lines(names: list[str], pools_a: dict[str, str], pools_b: dict[str, str]) -> list[str]:
    lines = []
    for name in names:
        va, vb = pools_a.get(name), pools_b.get(name)
        if va is None:
            lines.append(f"pools: {name} only in b")
        elif vb is None:
            lines.append(f"pools: {name} only in a")
        else:
            lines.append(f"pools: {name} {va[:12]} -> {vb[:12]}")
    return lines


@dataclass
class LockDiff:
    provenance_a: dict[str, Any]
    provenance_b: dict[str, Any]
    anchor_a: str
    anchor_b: str
    differences: list[Difference] = field(default_factory=list)
    only_in_a: list[str] = field(default_factory=list)
    only_in_b: list[str] = field(default_factory=list)
    pools_changed: list[str] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        # Pool provenance is not structure -- a pool changing under an
        # otherwise-unchanged lock is reported (render() names it) but does
        # not itself make two locks "different" any more than git_commit does.
        return (
            not self.differences
            and not self.only_in_a
            and not self.only_in_b
            and self.anchor_a == self.anchor_b
        )

    def render(self) -> str:
        lines = _provenance_lines(self.provenance_a, self.provenance_b)
        lines.extend(
            _pool_lines(
                self.pools_changed,
                self.provenance_a.get("pools") or {},
                self.provenance_b.get("pools") or {},
            )
        )
        if self.identical:
            # Provenance may still differ line by line above; say what is identical.
            lines.append("structure identical")
            return "\n".join(lines)
        if self.anchor_a != self.anchor_b:
            lines.append(f"anchor: {self.anchor_a!r} vs {self.anchor_b!r}")
        if self.differences:
            lines.append(f"crawlers: {len(self.differences)} difference(s):")
            for d in self.differences:
                lines.append(f"  {d.path}: {d.expected!r} vs {d.actual!r}")
        else:
            lines.append("crawlers: identical")
        if self.only_in_a:
            lines.append("only in A: " + ", ".join(self.only_in_a))
        if self.only_in_b:
            lines.append("only in B: " + ", ".join(self.only_in_b))
        return "\n".join(lines)


def diff_locks(text_a: str, text_b: str, *, deep: bool = True) -> LockDiff:
    lock_a = load_lock(text_a)
    lock_b = load_lock(text_b)

    # Same fixed anchor on both sides: start_at differences must come from a
    # different start_offset, never from the two locks having been compiled
    # (or re-anchored) on different days.
    anchor_dt = resolve_anchor(_REFERENCE_ANCHOR)
    crawlers_a = lock_shape(lock_a, anchor_dt, deep=deep)["crawlers"]
    crawlers_b = lock_shape(lock_b, anchor_dt, deep=deep)["crawlers"]

    names_a, names_b = set(crawlers_a), set(crawlers_b)
    common = names_a & names_b

    differences: list[Difference] = []
    for name in sorted(common):
        differences.extend(compare_shapes(crawlers_a[name], crawlers_b[name], f"crawlers.{name}"))

    return LockDiff(
        provenance_a=asdict(lock_a.provenance),
        provenance_b=asdict(lock_b.provenance),
        anchor_a=lock_a.schedule["anchor"],
        anchor_b=lock_b.schedule["anchor"],
        differences=differences,
        only_in_a=sorted(names_a - names_b),
        only_in_b=sorted(names_b - names_a),
        pools_changed=_pools_changed(lock_a.provenance.pools, lock_b.provenance.pools),
    )
