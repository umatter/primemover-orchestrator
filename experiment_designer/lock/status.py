"""One screen: what pmspec uploaded (provenance), how it is running (health), and --
given a lock -- whether the two still agree (verify)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .verify import VerifyResult, verify_lock

_HEALTH_KEYS = ("bad_runs", "bad_jobs", "treatment_lost_jobs", "degraded_jobs")
_UPLOAD_KEYS = (
    "mode",
    "lock_sha256",
    "git_commit",
    "generator",
    "version",
    "seed",
    "anchor",
    "uploaded_at",
    "uploaded_by",
    "lax",
)


@dataclass
class ProvenanceUpload:
    label: str
    values: dict[str, str] = field(default_factory=dict)


@dataclass
class ExperimentStatus:
    experiment_id: int
    name: str
    runner_user_id: int | None
    uploads: list[ProvenanceUpload]
    health: dict[str, Any]
    verify: VerifyResult | None = None

    @property
    def bound_to_any_lock(self) -> bool:
        return any("lock_sha256" in u.values for u in self.uploads)

    @property
    def ok(self) -> bool:
        lost = self.health.get("treatment_lost_jobs")
        return (self.verify is None or self.verify.ok) and not (isinstance(lost, int) and lost > 0)

    def render(self) -> str:
        runner = "default" if self.runner_user_id is None else str(self.runner_user_id)
        lines = [f'experiment {self.experiment_id}  "{self.name}"  runner={runner}']
        if not self.uploads:
            lines.append("no pmspec provenance: not uploaded by pmspec")
        for u in self.uploads:
            v = u.values
            sha = v.get("lock_sha256", "")[:12]
            lines.append(
                f"{u.label}: mode={v.get('mode', '?')} lock={sha or '?'} "
                f"commit={v.get('git_commit') or 'none'} generator={v.get('generator') or '-'} "
                f"seed={v.get('seed', '?')} anchor={v.get('anchor', '?')} "
                f"uploaded={v.get('uploaded_at', '?')} by {v.get('uploaded_by', '?')}"
                + (" lax" if v.get("lax") == "true" else "")
            )
        hours = self.health.get("window_hours", "?")
        counts = " ".join(f"{k}={self.health.get(k, 'unknown')}" for k in _HEALTH_KEYS)
        lines.append(
            f"health (last {hours}h): bots={self.health.get('bots', '?')} "
            f"runs={self.health.get('runs', {})} jobs={self.health.get('jobs', {})} {counts} "
            f"last_finished_at={self.health.get('last_finished_at') or 'never'}"
        )
        if self.verify is not None:
            lines.append(self.verify.render())
        return "\n".join(lines)


def _uploads(prefs: list[dict[str, Any]]) -> list[ProvenanceUpload]:
    grouped: dict[str, ProvenanceUpload] = {}
    for p in prefs:
        name = str(p.get("name", ""))
        if not name.startswith("pmspec."):
            continue
        label = str(p.get("description") or "upload 1")
        grouped.setdefault(label, ProvenanceUpload(label)).values[name[len("pmspec.") :]] = str(
            p.get("value", "")
        )
    return sorted(grouped.values(), key=lambda u: (len(u.label), u.label))


def experiment_status(
    session: Any,
    experiment_id: int,
    *,
    hours: int = 24,
    lock_text: str | None = None,
    deep: bool = False,
) -> ExperimentStatus:
    exp = session.get(f"experiments/{experiment_id}")
    health = session.get(f"experiments/{experiment_id}/health", params={"hours": hours})
    verify = (
        verify_lock(lock_text, session=session, experiment_id=experiment_id, deep=deep)
        if lock_text is not None
        else None
    )
    return ExperimentStatus(
        experiment_id=experiment_id,
        name=str(exp.get("name", "")),
        runner_user_id=exp.get("runner_user_id"),
        uploads=_uploads(exp.get("preferences") or []),
        health=health if isinstance(health, dict) else {},
        verify=verify,
    )
