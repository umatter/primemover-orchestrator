"""Upload a lock: resolve, create (or append), write provenance, upload crawlers."""

from __future__ import annotations

import getpass
import json
import socket
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pmspec.spec.time_utils import API_TIMEZONE, format_iso8601

from .anchor import UPLOAD_ANCHOR, resolve_anchor
from .io import is_lock_document, load_lock, sha256_text
from .model import Lock
from .resolve import resolve_for_upload
from .secrets import SecretStore, find_placeholders

MODES = ("real", "test")
TEST_RUNNER_USER_ID = 3
_PREF_MAX = 255


class UploadRefused(ValueError):
    pass


@dataclass
class UploadOptions:
    mode: str
    experiment_id: int | None = None
    allow_dirty: bool = False
    allow_past: bool = False
    contact: str = "pmspec"
    now: datetime | None = None
    uploaded_by: str | None = None


@dataclass
class UploadResult:
    experiment_id: int
    experiment_name: str
    crawler_count: int
    total_jobs: int
    lock_sha256: str
    anchor: str
    mode: str
    runner_user_id: int | None
    record_line: str


def _clip(value: Any) -> str:
    text = "" if value is None else str(value)
    return text[:_PREF_MAX]


def provenance_preferences(
    lock: Lock,
    *,
    lock_sha256: str,
    anchor_iso: str,
    mode: str,
    uploaded_at: str,
    uploaded_by: str,
    label: str = "upload 1",
) -> list[dict[str, str]]:
    p = lock.provenance
    values = {
        "pmspec.lock_sha256": lock_sha256,
        "pmspec.spec_sha256": p.spec_sha256,
        "pmspec.seed": p.seed,
        "pmspec.anchor": anchor_iso,
        "pmspec.git_commit": p.git_commit or "",
        "pmspec.generator": p.generator or "",
        "pmspec.version": p.pmspec_version,
        "pmspec.mode": mode,
        "pmspec.lax": "true" if p.lax else "false",
        "pmspec.uploaded_at": uploaded_at,
        "pmspec.uploaded_by": uploaded_by,
    }
    return [{"name": k, "description": label, "value": _clip(v)} for k, v in values.items()]


def effective_experiment(lock: Lock, mode: str, today: date) -> tuple[str, int | None]:
    name = lock.experiment["name"]
    runner = lock.experiment.get("runner_user_id")
    if mode == "test":
        return f"test-{name}-{today:%Y%m%d}", TEST_RUNNER_USER_ID
    return name, runner


def _check_commit(lock: Lock, options: UploadOptions) -> None:
    commit = lock.provenance.git_commit
    if options.mode == "test" or options.allow_dirty:
        return
    if commit is None:
        raise UploadRefused("lock has no git commit; commit the spec repo or pass --allow-dirty")
    if commit.endswith("-dirty"):
        raise UploadRefused(
            f"lock was compiled from a dirty tree ({commit}); commit, recompile, or pass --allow-dirty"
        )


def upload_lock(
    lock_text: str, *, session: Any, store: SecretStore | None, options: UploadOptions
) -> UploadResult:
    if options.mode not in MODES:
        raise UploadRefused(f"mode must be one of {MODES}")
    if not is_lock_document(lock_text):
        raise UploadRefused("not a lock: compile the spec first (pmspec compile)")
    lock = load_lock(lock_text)
    _check_commit(lock, options)

    now = options.now or datetime.now(API_TIMEZONE)
    anchor_dt = resolve_anchor(lock.schedule["anchor"], now=now)
    if lock.schedule["anchor"] != UPLOAD_ANCHOR and anchor_dt < now and not options.allow_past:
        raise UploadRefused(
            f"anchor {lock.schedule['anchor']} is in the past; pass --allow-past to upload anyway"
        )

    needed = find_placeholders(lock.crawlers)
    if needed and store is None:
        raise UploadRefused(
            f"lock references {len(needed)} secret(s) but no secrets store was given"
        )

    # A specific missing key is more useful than a summary, and it must
    # surface before any request is made -- resolve_for_upload below is the
    # first (and only) place a placeholder is actually looked up, so a
    # missing secret raises MissingSecret straight out of SecretStore.get()
    # with nothing sent to the API yet.
    crawlers, _ = resolve_for_upload(lock, store=store, now=now)
    lock_sha = sha256_text(lock_text)
    anchor_iso = format_iso8601(anchor_dt)
    uploaded_at = format_iso8601(now.replace(microsecond=0))
    uploaded_by = options.uploaded_by or f"{getpass.getuser()}@{socket.gethostname()}"
    name, runner = effective_experiment(lock, options.mode, now.date())

    if options.experiment_id is None:
        payload: dict[str, Any] = {
            "name": name,
            "description": lock.experiment.get("description") or "Uploaded by pmspec",
            "contact": lock.experiment.get("contact") or options.contact,
            "active": 1,
            "preferences": provenance_preferences(
                lock,
                lock_sha256=lock_sha,
                anchor_iso=anchor_iso,
                mode=options.mode,
                uploaded_at=uploaded_at,
                uploaded_by=uploaded_by,
            ),
        }
        if runner is not None:
            payload["runner_user_id"] = runner
        created = session.post("experiments", payload)
        experiment_id = int(created["id"])
    else:
        experiment_id = options.experiment_id
        live = session.get(f"experiments/{experiment_id}")
        live_prefs = live.get("preferences", [])
        if options.mode == "test":
            # Append neither renames nor re-targets the experiment, so test
            # crawlers added to a real study would run on ex63 under its name.
            live_modes = {p.get("value") for p in live_prefs if p.get("name") == "pmspec.mode"}
            if "test" not in live_modes:
                raise UploadRefused(
                    f"experiment {experiment_id} was not uploaded in test mode "
                    f"(pmspec.mode is {', '.join(sorted(m for m in live_modes if m)) or 'absent'}); "
                    "append test crawlers to a test experiment, or create one by omitting -e"
                )
        existing = [p for p in live_prefs if p.get("name") == "pmspec.lock_sha256"]
        label = f"upload {len(existing) + 1}"
        session.put(
            f"experiments/{experiment_id}",
            {
                "name": live["name"],
                "user_id": live["user_id"],
                "description": live.get("description"),
                "contact": live.get("contact"),
                "preferences": provenance_preferences(
                    lock,
                    lock_sha256=lock_sha,
                    anchor_iso=anchor_iso,
                    mode=options.mode,
                    uploaded_at=uploaded_at,
                    uploaded_by=uploaded_by,
                    label=label,
                ),
            },
        )
        name = live["name"]
        runner = None  # nothing is PUT for it: the experiment keeps its runner

    session.post_file(
        f"experiments/{experiment_id}/upload",
        "json",
        "crawlers.json",
        json.dumps(crawlers, indent=2, ensure_ascii=False),
        "application/json",
    )
    del crawlers  # resolved secrets: nothing below may see them

    total_jobs = sum(len(q["jobs"]) for c in lock.crawlers for q in c["queues"])
    record = (
        f"{uploaded_at} exp={experiment_id} mode={options.mode} lock={lock_sha[:12]} "
        f"commit={lock.provenance.git_commit or 'none'} api={session.base_url}"
    )
    return UploadResult(
        experiment_id=experiment_id,
        experiment_name=name,
        crawler_count=len(lock.crawlers),
        total_jobs=total_jobs,
        lock_sha256=lock_sha,
        anchor=anchor_iso,
        mode=options.mode,
        runner_user_id=runner,
        record_line=record,
    )
