"""Upload a lock: resolve, create (or append), write provenance, upload crawlers.

Two kinds, two paths. A **fleet** lock creates (or appends crawlers to) an
experiment -- ``_upload_fleet``, unchanged since it was written. A **runs** lock
appends *queues* to crawlers that already exist, matched by name --
``_upload_runs``, which writes into a study that is already running and is
therefore built around two rules: every refusal is a read and happens before the
first write, and re-running the same lock adds nothing twice.
"""

from __future__ import annotations

import getpass
import json
import socket
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pmspec.spec.time_utils import API_TIMEZONE, format_iso8601

from .anchor import UPLOAD_ANCHOR, resolve_anchor
from .io import is_lock_document, load_lock, sha256_text
from .model import Lock, LockError
from .resolve import resolve_for_upload
from .secrets import SecretStore, find_placeholders
from .verify import _paginate, _wall

MODES = ("real", "test")
TEST_RUNNER_USER_ID = 3
_PREF_MAX = 255

NO_PREFERENCE_SYNC = (
    "this API cannot update preferences (PUT configurations/{id} strips them); "
    "deploy primemover_api with preferences sync, or drop `updates:`"
)


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
    #: "fleet" or "runs". A runs upload appends queues to existing crawlers, so
    #: the three counters below describe queues, not crawlers.
    kind: str = "fleet"
    #: Queues actually posted (a runs upload skips the ones already live).
    queue_count: int = 0
    skipped_existing: int = 0
    #: Crawlers whose `updates:` were applied.
    updates_applied: int = 0


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


def _load_for_upload(lock_text: str, options: UploadOptions) -> Lock:
    if options.mode not in MODES:
        raise UploadRefused(f"mode must be one of {MODES}")
    if not is_lock_document(lock_text):
        raise UploadRefused("not a lock: compile the spec first (pmspec compile)")
    return load_lock(lock_text)


def upload_lock(
    lock_text: str, *, session: Any, store: SecretStore | None, options: UploadOptions
) -> UploadResult:
    lock = _load_for_upload(lock_text, options)
    if lock.is_runs:
        return _upload_runs(lock, lock_text, session=session, options=options)
    return _upload_fleet(lock, lock_text, session=session, store=store, options=options)


def _upload_fleet(
    lock: Lock,
    lock_text: str,
    *,
    session: Any,
    store: SecretStore | None,
    options: UploadOptions,
) -> UploadResult:
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


# --- a runs lock: queues appended to crawlers that already exist ------------


def supports_preference_update(session: Any, configuration: dict[str, Any]) -> bool:
    """Can this API apply `updates:`?

    ``PUT configurations/{id}`` answers 200 on both API versions: the old one
    validates ``name``/``description``/``active`` only and silently drops a
    ``preferences`` key, the new one syncs it. They differ in the *response* --
    the new repository returns the configuration with its ``preferences``
    relation loaded. So the probe sends the configuration's current values plus
    an **empty** preferences list (which changes nothing under either version)
    and reads the answer's shape.

    A session may implement ``supports_preference_update(configuration)``
    itself, which is how ``ApiSession``/``_McpSession`` cache the probe across
    calls; otherwise the probe is made here through ``put``.
    """
    own = getattr(session, "supports_preference_update", None)
    if own is not None:
        return bool(own(configuration))
    return probe_preference_update(session, configuration)


def probe_preference_update(session: Any, configuration: dict[str, Any]) -> bool:
    data = session.put(
        f"configurations/{configuration['id']}",
        {
            "name": configuration.get("name"),
            "description": configuration.get("description"),
            "active": configuration.get("active", 1),
            "preferences": [],
        },
    )
    return isinstance(data, dict) and isinstance(data.get("preferences"), list)


def _first_upload_preference(live_prefs: list[dict[str, Any]], name: str) -> str | None:
    """The value of ``name`` as the experiment's *first* upload recorded it.

    Later uploads append their own rows with a "upload N" description, so the
    fleet lock this experiment was created from is the one labelled "upload 1".
    """
    for p in live_prefs:
        if p.get("name") == name and p.get("description") == "upload 1":
            return None if p.get("value") is None else str(p["value"])
    return None


@dataclass
class _RunsContext:
    experiment_id: int
    live: dict[str, Any]
    by_name: dict[str, dict[str, Any]]
    crawlers: list[dict[str, Any]]  # resolved: start_offset -> start_at
    anchor_iso: str
    existing: dict[int, set[tuple[str, str]]]
    base_sha: str
    label: str
    pref_update_supported: bool | None


def _prepare_runs(lock: Lock, *, session: Any, options: UploadOptions) -> _RunsContext:
    """Every check a runs upload makes, in order, with nothing written.

    This is the whole of ``--dry-run``: by the time it returns, the only thing
    left to do is post queues and record provenance.
    """
    experiment_id = options.experiment_id
    if experiment_id is None:
        raise UploadRefused("a runs lock appends to an experiment: pass -e <id>")

    base_sha = lock.provenance.base_lock_sha256
    if not base_sha:
        raise UploadRefused(
            "runs lock has no provenance.base_lock_sha256: recompile it against its fleet lock"
        )

    live = session.get(f"experiments/{experiment_id}") or {}
    live_prefs = live.get("preferences", []) or []
    live_sha = _first_upload_preference(live_prefs, "pmspec.lock_sha256")
    if live_sha != base_sha:
        raise UploadRefused(
            f"experiment {experiment_id} was created from lock {(live_sha or 'none')[:12]}, "
            f"this runs lock is based on {base_sha[:12]}"
        )
    live_mode = _first_upload_preference(live_prefs, "pmspec.mode")
    if live_mode != options.mode:
        shown = live_mode or "unknown"
        raise UploadRefused(
            f"experiment {experiment_id} is a {shown} experiment; "
            f"upload this runs lock with --mode {shown}"
        )
    _check_commit(lock, options)

    by_name: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for crawler in live.get("crawlers", []) or []:
        name = str(crawler.get("name", ""))
        if name in by_name:
            duplicates.append(name)
        by_name[name] = crawler
    if duplicates:
        raise UploadRefused(
            f"experiment {experiment_id} has more than one crawler named "
            f"{', '.join(sorted(set(duplicates)))}; a runs lock matches by name"
        )
    missing = [str(c["name"]) for c in lock.crawlers if str(c["name"]) not in by_name]
    if missing:
        raise UploadRefused(
            f"experiment {experiment_id} has no crawler named {', '.join(missing)}; "
            "a runs lock appends to the fleet it was compiled against"
        )

    # A runs lock carries no proxy, so it carries no secrets; resolving one
    # would need a store the runs path deliberately never loads.
    needed = find_placeholders(lock.crawlers)
    if needed:
        raise LockError(
            [f"runs lock references a secret ({sorted(needed)[0]}); a runs lock carries none"]
        )

    now = options.now or datetime.now(API_TIMEZONE)
    crawlers, anchor_dt = resolve_for_upload(lock, store=None, now=now)
    if lock.schedule["anchor"] != UPLOAD_ANCHOR and anchor_dt < now and not options.allow_past:
        raise UploadRefused(
            f"anchor {lock.schedule['anchor']} is in the past; pass --allow-past to upload anyway"
        )

    # Before any write, and only when there is something to apply: an API that
    # would silently drop the values must refuse the whole upload, not half-do it.
    pref_update_supported: bool | None = None
    if any(_crawler_updates(c) for c in lock.crawlers):
        probe_name = str(lock.crawlers[0]["name"])
        first = by_name[probe_name].get("configuration") or {}
        # A live crawler can come back without one (an interrupted create, an
        # API that did not load the relation): a refusal names what is missing,
        # where indexing `['id']` would only report a KeyError.
        if "id" not in first:
            raise UploadRefused(f"crawler '{probe_name}' has no configuration to update")
        pref_update_supported = supports_preference_update(session, first)
        if not pref_update_supported:
            raise UploadRefused(NO_PREFERENCE_SYNC)

    live_queues = _paginate(session, f"experiments/{experiment_id}/queues-unprocessed") + _paginate(
        session, f"experiments/{experiment_id}/queues-processed"
    )
    existing: dict[int, set[tuple[str, str]]] = defaultdict(set)
    for q in live_queues:
        existing[int(q["crawler_id"])].add(
            (_wall(str(q.get("start_at") or "")), str(q.get("name")))
        )

    return _RunsContext(
        experiment_id=experiment_id,
        live=live,
        by_name=by_name,
        crawlers=crawlers,
        anchor_iso=format_iso8601(anchor_dt),
        existing=existing,
        base_sha=base_sha,
        label=f"upload {len([p for p in live_prefs if p.get('name') == 'pmspec.lock_sha256']) + 1}",
        pref_update_supported=pref_update_supported,
    )


def _crawler_updates(crawler: dict[str, Any]) -> dict[str, Any]:
    return dict((crawler.get("updates") or {}).get("preferences") or {})


def plan_runs_upload(lock_text: str, *, session: Any, options: UploadOptions) -> dict[str, Any]:
    """What a runs upload would do. Every refusal raises; nothing is written."""
    lock = _load_for_upload(lock_text, options)
    if not lock.is_runs:
        raise UploadRefused("not a runs lock: plan_runs_upload describes a `kind: runs` lock")
    ctx = _prepare_runs(lock, session=session, options=options)
    total = sum(len(c["queues"]) for c in ctx.crawlers)
    skip = sum(len(_partition(ctx, c)[1]) for c in ctx.crawlers)
    return {
        "kind": "runs",
        "experiment_id": ctx.experiment_id,
        "crawlers": len(ctx.crawlers),
        "queues": total - skip,
        "queues_in_lock": total,
        "would_skip": skip,
        "updates": sum(1 for c in ctx.crawlers if _crawler_updates(c)),
        "base_ok": True,
        "preference_update_supported": bool(ctx.pref_update_supported),
        "anchor": ctx.anchor_iso,
    }


def _partition(
    ctx: _RunsContext, crawler: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """This crawler's queues split into (to post, already live).

    The one place the skip predicate lives. A dry run reports what an apply
    would do, so the two must compute "already live" the same way or the plan
    is fiction; sharing the function is what makes that true by construction.
    """
    crawler_id = int(ctx.by_name[str(crawler["name"])]["id"])
    live = ctx.existing.get(crawler_id, set())
    post: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for q in crawler["queues"]:
        target = skipped if (_wall(str(q["start_at"])), str(q["name"])) in live else post
        target.append(q)
    return post, skipped


def _apply_crawler(
    ctx: _RunsContext, crawler: dict[str, Any], *, session: Any
) -> tuple[int, int, dict[str, list[Any]]]:
    """One crawler's queues, then its updates. Returns (posted, skipped, record)."""
    name = str(crawler["name"])
    live_crawler = ctx.by_name[name]
    crawler_id = int(live_crawler["id"])
    post, skipped = _partition(ctx, crawler)
    if post:
        session.post_file(
            f"crawlers/{crawler_id}/upload",
            "json",
            "queues.json",
            json.dumps(post, indent=2, ensure_ascii=False),
            "application/json",
        )

    record: dict[str, list[Any]] = {}
    wanted = _crawler_updates(crawler)
    if wanted:
        config = live_crawler.get("configuration") or {}
        if "id" not in config:
            raise UploadRefused(f"crawler '{name}' has no configuration to update")
        old = {str(p.get("name")): p.get("value") for p in (config.get("preferences") or [])}
        session.put(
            f"configurations/{config['id']}",
            {
                "name": config.get("name"),
                "description": config.get("description"),
                "active": config.get("active", 1),
                "preferences": [{"name": k, "value": v} for k, v in wanted.items()],
            },
        )
        record = {f"preferences.{k}": [old.get(k), v] for k, v in wanted.items()}
    return len(post), len(skipped), record


def _upload_runs(
    lock: Lock, lock_text: str, *, session: Any, options: UploadOptions
) -> UploadResult:
    ctx = _prepare_runs(lock, session=session, options=options)

    posted = skipped = 0
    updates: dict[str, dict[str, list[Any]]] = {}
    for crawler in ctx.crawlers:
        name = str(crawler["name"])
        try:
            added, left, record = _apply_crawler(ctx, crawler, session=session)
        except Exception as e:
            # Same exception, same type (the CLI's ApiError handler still
            # matches) -- with the crawler it died on in the message, because
            # "which bots got their queues" is the only thing a re-run needs to
            # know, and a re-run is safe by construction.
            e.args = (f"crawler {name}: {e}",) + tuple(e.args[1:])
            raise
        posted += added
        skipped += left
        if record:
            updates[name] = record

    now = options.now or datetime.now(API_TIMEZONE)
    lock_sha = sha256_text(lock_text)
    uploaded_at = format_iso8601(now.replace(microsecond=0))
    uploaded_by = options.uploaded_by or f"{getpass.getuser()}@{socket.gethostname()}"
    preferences = provenance_preferences(
        lock,
        lock_sha256=lock_sha,
        anchor_iso=ctx.anchor_iso,
        mode=options.mode,
        uploaded_at=uploaded_at,
        uploaded_by=uploaded_by,
        label=ctx.label,
    )
    preferences.append({"name": "pmspec.kind", "description": ctx.label, "value": "runs"})
    preferences.append(
        {"name": "pmspec.base_lock_sha256", "description": ctx.label, "value": ctx.base_sha}
    )
    if updates:
        blob = json.dumps(updates, sort_keys=True)
        if len(blob) > _PREF_MAX:
            # The lock is the record; provenance only has to point at it.
            blob = f"<{len(updates)} crawlers updated; see lock>"
        preferences.append({"name": "pmspec.updates", "description": ctx.label, "value": blob})

    session.put(
        f"experiments/{ctx.experiment_id}",
        {
            "name": ctx.live["name"],
            "user_id": ctx.live["user_id"],
            "description": ctx.live.get("description"),
            "contact": ctx.live.get("contact"),
            "preferences": preferences,
        },
    )

    total_jobs = sum(len(q["jobs"]) for c in lock.crawlers for q in c["queues"])
    record_line = (
        f"{uploaded_at} exp={ctx.experiment_id} mode={options.mode} lock={lock_sha[:12]} "
        f"commit={lock.provenance.git_commit or 'none'} api={session.base_url} "
        f"kind=runs base={ctx.base_sha[:12]} queues={posted} skipped={skipped}"
    )
    return UploadResult(
        experiment_id=ctx.experiment_id,
        experiment_name=ctx.live["name"],
        crawler_count=len(lock.crawlers),
        total_jobs=total_jobs,
        lock_sha256=lock_sha,
        anchor=ctx.anchor_iso,
        mode=options.mode,
        runner_user_id=None,  # nothing is PUT for it: the experiment keeps its runner
        record_line=record_line,
        kind="runs",
        queue_count=posted,
        skipped_existing=skipped,
        updates_applied=len(updates),
    )
