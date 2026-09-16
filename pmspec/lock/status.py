"""One screen: what pmspec uploaded (provenance), how it is running (health), and --
given a lock -- whether the two still agree (verify)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .verify import VerifyResult, verify_lock

_HEALTH_KEYS = ("bad_runs", "bad_jobs", "treatment_lost_jobs", "degraded_jobs")


def _updates_count(blob: str) -> str:
    """How many crawlers an upload's ``pmspec.updates`` names.

    The value is the JSON diff the upload recorded -- unless it was longer than
    a preference can hold, in which case it is a pointer at the lock and the
    count is not in it. Then the honest answer is that there were updates."""
    try:
        parsed = json.loads(blob)
    except ValueError:
        return "yes"
    return str(len(parsed)) if isinstance(parsed, dict) else "yes"


@dataclass
class ProvenanceUpload:
    label: str
    values: dict[str, str] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        """``fleet`` unless the upload said otherwise: ``pmspec.kind`` is
        written only by a runs upload, so its absence means fleet."""
        return self.values.get("kind") or "fleet"


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
        """With a lock, verify's verdict; without one, whether any lock was ever
        bound (spec §5: an experiment with no pmspec provenance exits 1). Either
        way, a lost treatment is never ok."""
        lost = self.health.get("treatment_lost_jobs")
        clean = self.verify.ok if self.verify is not None else self.bound_to_any_lock
        return clean and not (isinstance(lost, int) and lost > 0)

    def render(self) -> str:
        runner = "default" if self.runner_user_id is None else str(self.runner_user_id)
        lines = [f'experiment {self.experiment_id}  "{self.name}"  runner={runner}']
        if not self.uploads:
            lines.append("no pmspec provenance: not uploaded by pmspec")
        for u in self.uploads:
            v = u.values
            sha = v.get("lock_sha256", "")[:12]
            extra = ""
            if u.kind == "runs":
                # Which fleet these sessions were appended to is the first
                # question a runs upload raises, and only the base sha answers it.
                extra += f" kind=runs base={v.get('base_lock_sha256', '')[:12] or '?'}"
            if "updates" in v:
                extra += f" updates={_updates_count(v['updates'])}"
            lines.append(
                f"{u.label}: mode={v.get('mode', '?')} lock={sha or '?'} "
                f"commit={v.get('git_commit') or 'none'} generator={v.get('generator') or '-'} "
                f"seed={v.get('seed', '?')} anchor={v.get('anchor', '?')} "
                f"uploaded={v.get('uploaded_at', '?')} by {v.get('uploaded_by', '?')}"
                + (" lax" if v.get("lax") == "true" else "")
                + extra
            )
        if self.uploads:
            runs = sum(1 for u in self.uploads if u.kind == "runs")
            counted = f"{len(self.uploads) - runs} fleet" + (f" + {runs} runs" if runs else "")
            last = self.uploads[-1].values.get("uploaded_at") or "?"
            lines.append(f"uploads: {counted}, last {last}")
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
