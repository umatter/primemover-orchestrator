"""The lock: a fully expanded, self-contained experiment that upload and verify read.

Everything random is already drawn, every pool already inlined. The two things
deliberately *not* resolved are secrets (placeholders, so the file can be
committed) and queue start times (offsets from `schedule.anchor`, so the same
file can be uploaded on a different day)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .parts import DanglingRefError, factor_crawlers, materialise

LOCK_FORMAT_VERSION = 2

# Provenance fields written only when they carry a value. A fleet lock has
# neither, and must keep the bytes it had before the runs kind existed.
_OPTIONAL_PROVENANCE = ("base_lock_sha256", "base_spec_path")

# A runs lock adds sessions to crawlers that already exist; anything that
# describes the *bot* belongs to the fleet lock that randomized it.
_FLEET_ONLY_CRAWLER_KEYS = ("configuration", "agent", "proxy")


class LockError(ValueError):
    """A lock document that cannot be used: schema violations, an unknown
    format, or a crawler referencing a part the file does not carry. Every
    consumer (validate/summary/audit/verify/upload) catches this and nothing
    else, so anything ``load_lock`` can refuse must surface as one."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class Provenance:
    spec_path: str
    spec_sha256: str
    generator: str | None
    git_commit: str | None
    pmspec_version: str
    seed: int
    lax: bool = False
    pools: dict[str, str] = field(default_factory=dict)
    # Runs locks only: which fleet lock this one appends sessions to. Emitted
    # only when set, so a fleet lock's bytes are exactly what they were.
    base_lock_sha256: str | None = None
    base_spec_path: str | None = None


@dataclass
class Lock:
    provenance: Provenance
    experiment: dict[str, Any]
    schedule: dict[str, Any]
    crawlers: list[dict[str, Any]] = field(default_factory=list)
    kind: str = "fleet"

    @property
    def is_runs(self) -> bool:
        """A runs lock adds queues to an existing fleet's crawlers by name; it
        carries no configuration, agent or proxy of its own."""
        return self.kind == "runs"

    def to_dict(self) -> dict[str, Any]:
        # Key order here IS the canonical order dump_lock() writes.
        parts, refs = factor_crawlers(self.crawlers)
        provenance = {
            k: v
            for k, v in asdict(self.provenance).items()
            if v is not None or k not in _OPTIONAL_PROVENANCE
        }
        return {
            "pmspec_lock": LOCK_FORMAT_VERSION,
            **({"kind": self.kind} if self.is_runs else {}),
            "provenance": provenance,
            "experiment": {
                "name": self.experiment.get("name", ""),
                "description": self.experiment.get("description", ""),
                "runner_user_id": self.experiment.get("runner_user_id"),
                "contact": self.experiment.get("contact"),
            },
            "schedule": {"anchor": self.schedule["anchor"]},
            "parts": {
                "queues": dict(sorted(parts["queues"].items())),
                "agents": dict(sorted(parts["agents"].items())),
            },
            "crawlers": refs,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Lock:
        prov = dict(d["provenance"])
        prov.setdefault("generator", None)
        prov.setdefault("git_commit", None)
        prov.setdefault("lax", False)
        prov.setdefault("base_lock_sha256", None)
        prov.setdefault("base_spec_path", None)
        kind = d.get("kind", "fleet")
        if kind == "runs":
            # Before materialising: a stray `agent:` here is a raw body, not a
            # ref, so materialise would fail with a bare KeyError instead of
            # saying what the author actually got wrong.
            check_runs_crawlers(d.get("crawlers"))
        if d.get("pmspec_lock") == 2:
            try:
                crawlers = materialise(d["parts"], d["crawlers"])
            except DanglingRefError as e:
                # The schema checks each ref's *shape*; only materialising
                # can tell whether the part it names exists.
                raise LockError([str(e)]) from e
        else:
            crawlers = list(d["crawlers"])
        return cls(
            provenance=Provenance(**prov),
            # Normalise so a lock written before `contact` existed round-trips equal.
            experiment={
                "name": d["experiment"].get("name", ""),
                "description": d["experiment"].get("description", ""),
                "runner_user_id": d["experiment"].get("runner_user_id"),
                "contact": d["experiment"].get("contact"),
            },
            schedule=dict(d["schedule"]),
            crawlers=crawlers,
            kind=kind,
        )


def check_runs_crawlers(crawlers: Any) -> None:
    """Refuse a runs lock carrying anything that belongs to its base fleet.

    ``load_lock`` calls this *before* the schema, so a hand-edited lock is told
    what it actually got wrong ("carries agent; a runs lock adds queues only")
    rather than jsonschema's "Additional properties are not allowed". That puts
    it ahead of every shape check, so it takes the document as it comes: a
    ``crawlers`` that is not a list, or an item that is not a mapping, is not
    this function's complaint -- it returns and lets the schema say so.
    """
    if not isinstance(crawlers, list):
        return
    errors: list[str] = []
    for crawler in crawlers:
        if not isinstance(crawler, dict):
            continue
        name = crawler.get("name")
        for key in _FLEET_ONLY_CRAWLER_KEYS:
            if key in crawler:
                errors.append(
                    f"runs lock crawler '{name}' carries {key}; a runs lock adds queues only"
                )
        updates = crawler.get("updates")
        for key in updates if isinstance(updates, dict) else {}:
            if key != "preferences":
                errors.append(
                    f"runs lock crawler '{name}': updates may only contain preferences (got {key})"
                )
    if errors:
        raise LockError(errors)
