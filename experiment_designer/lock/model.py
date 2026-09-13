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


@dataclass
class Lock:
    provenance: Provenance
    experiment: dict[str, Any]
    schedule: dict[str, Any]
    crawlers: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        # Key order here IS the canonical order dump_lock() writes.
        parts, refs = factor_crawlers(self.crawlers)
        return {
            "pmspec_lock": LOCK_FORMAT_VERSION,
            "provenance": asdict(self.provenance),
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
        )
