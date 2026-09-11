"""The lock: a fully expanded, self-contained experiment that upload and verify read.

Everything random is already drawn, every pool already inlined. The two things
deliberately *not* resolved are secrets (placeholders, so the file can be
committed) and queue start times (offsets from `schedule.anchor`, so the same
file can be uploaded on a different day)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

LOCK_FORMAT_VERSION = 1


@dataclass
class Provenance:
    spec_path: str
    spec_sha256: str
    generator: str | None
    git_commit: str | None
    pmspec_version: str
    seed: int
    lax: bool = False


@dataclass
class Lock:
    provenance: Provenance
    experiment: dict[str, Any]
    schedule: dict[str, Any]
    crawlers: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        # Key order here IS the canonical order dump_lock() writes.
        return {
            "pmspec_lock": LOCK_FORMAT_VERSION,
            "provenance": asdict(self.provenance),
            "experiment": {
                "name": self.experiment.get("name", ""),
                "description": self.experiment.get("description", ""),
                "runner_user_id": self.experiment.get("runner_user_id"),
            },
            "schedule": {"anchor": self.schedule["anchor"]},
            "crawlers": self.crawlers,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Lock:
        prov = dict(d["provenance"])
        prov.setdefault("generator", None)
        prov.setdefault("git_commit", None)
        prov.setdefault("lax", False)
        return cls(
            provenance=Provenance(**prov),
            experiment=dict(d["experiment"]),
            schedule=dict(d["schedule"]),
            crawlers=list(d["crawlers"]),
        )
