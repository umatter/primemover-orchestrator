"""``pmspec audit``: did each finished job apply the treatment its lock says it should?

The lock records, per lambda job, a ``strategy`` behaviour plus the treatment
parameters (``pi``/``psi``/``kappa``/``tau``/``beta`` for ``youtube_v1``, or
arbitrary keys for anything else). The live job row, once it finishes, carries
the decision function's own account of what it did: ``decision_report``
(``requested``/``applied``/``filters``), ``decision_treatment_lost`` and
``status_code``. This module walks a lock's jobs, matches each one carrying a
``strategy`` behaviour -- or, with no ``strategy``, each real lambda-select
job (:data:`LAMBDA_SELECT_TYPE`), audited under ``youtube_v1`` with the
crawler's ``configuration.params`` as its treatment, which is how a
params-based study such as the standing treatment canary carries its arm --
to its live counterpart by (crawler name, queue name, job ordinal), and
reports whether the two agree.

Four checks, in order, each producing a distinct outcome the moment it fails
(a later check is never reached once an earlier one has already flagged the
job):

``shape``
    Is the live job at this ordinal the same ``type`` as the lock's? A live
    queue with one extra leading job puts every lock ordinal one job early:
    the lock's lambda job lands on a live ``visitjob`` (which reports nothing)
    while the real lambda job -- possibly treatment-lost -- is never examined.
    Type mismatch -> ``shape``, always a failure. A live row with no ``type``
    at all cannot be checked and is not a ``shape`` finding. A matched queue
    whose live job count differs from the lock's also gets one ``shape``
    finding of its own when the live side has *more* jobs (``lock has N jobs,
    live has M``, no job id) -- an extra job at the *tail* lines every ordinal
    up and would otherwise pass -- and its ordinals are still audited on their
    merits. Fewer live jobs is ``missing`` per ordinal, as before.
``reported``
    Did the job carry a decision verdict at all (:func:`orchestrator.status.
    job_decision_reported`)? ``False`` -> ``unreported`` -- unknown, never
    clean.
``requested``
    Does the lock's expected filter set (:func:`expected_filters`) match what
    the decision function says it actually requested? Mismatch -> ``mismatch``.
    Two filters are **hygiene**, not treatment (:data:`HYGIENE`), and are
    never a mismatch in either direction: ``children_entertainment`` runs for
    *every* crawler (the decision function marks it ``ok`` unconditionally and
    the runner persists it in ``requested``, psi or not), and
    ``already_watched`` is always-on history filtering (``ok`` on every
    post-2026-09-10 payload; the runner's ``requestedFilters()`` already
    leaves it out). So the rule is: every expected filter must be requested,
    and every requested filter must be expected *or hygiene*. A consequence
    worth stating plainly: **``psi`` cannot be verified from the report** --
    the filter's state is ``ok`` for ``psi=0`` and ``psi=1`` alike, and only
    the LLM prompt differs.
``applied``
    Did every requested filter apply, and was the bot not treatment-lost?
    ``decision_treatment_lost`` true -> ``lost``; requested filters not fully
    applied (but not lost) -> ``degraded``; otherwise -> ``ok``.

A job whose live ``status_code`` is a known running state is ``unfinished``
before any of the above are evaluated -- it has not had a chance to report
anything yet. A ``status_code`` that is neither terminal nor running (NULL,
empty, or a value :func:`orchestrator.status.normalize` does not recognise)
is *unknown*, and unknown is ``unreported``, never "still running".
A lock job with no live counterpart -- no live crawler at all, no live queue at
its ordinal, or a shorter live job list -- is ``missing``. ``unfinished`` and
``missing`` are informational, never a failure, regardless of ``--strict``;
``unreported``/``degraded`` fail the run only under ``--strict``.

Only jobs whose lock behaviours include a ``strategy`` key, or whose type is
:data:`LAMBDA_SELECT_TYPE`, are considered -- a plain ``visitjob`` has nothing
to audit and produces no finding, but still occupies its ordinal so a
genuinely missing job downstream is not mistaken for one that was never a
lambda job to begin with.

Queues sharing a name within a crawler are matched in ``start_offset`` order
on the lock side -- which is what the live side's ``start_at`` order
corresponds to -- so authoring order in the spec does not decide which live
queue a lock queue is compared with.

An audit that examines **no** job at all is vacuous: it fails under
``--strict`` and says so in its rendering either way, so a window that
excludes everything, or a lock naming no lambda job, cannot pass as clean.

Read bounding: a live queue's own ``finished_at`` is checked against the
``--hours`` window *before* its jobs are fetched at all, so an old queue costs
no jobs call; a live job's own ``finished_at``/``started_at`` is checked again
as a secondary guard once jobs are in hand. Absence of a timestamp on either
side never excludes anything -- unknown recency is not evidence of staleness.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from orchestrator import status as status_mod

from ..spec.job_registry import JobRegistry
from .anchor import parse_offset
from .io import load_lock
from .model import Lock
from .verify import _paginate

#: The API type of a lambda-select job. A lock job of this type with no
#: ``strategy`` behaviour is audited under ``youtube_v1`` using the crawler's
#: ``configuration.params`` -- the legacy/params-based shape the runner still
#: serves via ``selectionConfiguration``.
LAMBDA_SELECT_TYPE: str = JobRegistry.get_api_type("lambda_select") or "lambdasingleselectjob"

#: The decision function's own table (``lambda_function.py`` ~L996-1004):
#: ``pi``/``psi``/``kappa``/``tau`` are switches (only ``1`` requests the
#: filter); ``beta`` is preference-fringe/-mainstream/off (``0``), so any
#: nonzero value -- including ``-1`` -- requests ``fringe``.
YOUTUBE_V1: dict[str, str] = {
    "pi": "mainstream_media",
    "psi": "children_entertainment",
    "kappa": "similar",
    "tau": "political",
    "beta": "fringe",
}

#: Filters the decision function runs (or reports) regardless of any
#: treatment: ``children_entertainment`` is baseline hygiene on every crawler
#: and ``already_watched`` is always-on history filtering. Their presence in
#: a report's ``requested`` set is never a mismatch, and their absence never
#: is either. See the module docstring: ``psi`` is unverifiable from the
#: report as a direct consequence.
HYGIENE: frozenset[str] = frozenset({"already_watched", "children_entertainment"})

#: Filters the runner's ``requestedFilters()`` leaves out of ``requested``
#: (``LambdaSelectJobResult.NON_TREATMENT_FILTERS``). The derived-from-
#: ``filters`` fallback in :func:`_requested_applied` mirrors it exactly so a
#: backfilled row and a runner-persisted row read the same.
_NON_TREATMENT_FILTERS: frozenset[str] = frozenset({"already_watched"})

#: Values a treatment key's string form must not equal to count as "requested"
#: under the generic (non-youtube_v1) rule.
_EMPTYISH = frozenset({"", "0", "none", "None"})

#: Outcomes worth listing individually in a rendered report.
_OFFENDING = frozenset({"shape", "unreported", "mismatch", "lost", "degraded", "missing"})

#: Outcomes that fail the run regardless of ``--strict``.
_ALWAYS_BAD = frozenset({"shape", "mismatch", "lost"})

#: Outcomes that only fail the run under ``--strict`` -- absence of evidence
#: (``unreported``, ``missing``) and a degraded-but-not-lost arm are all "a
#: human should look", never a hard failure on their own. ``unfinished`` is
#: deliberately excluded even here: a job still running has not had a chance
#: to report anything, in strict mode or not.
_STRICT_BAD = frozenset({"unreported", "degraded", "missing"})


def _truthy(value: Any) -> bool:
    """Is *value* a "requested" treatment value under the generic rule?

    ``False`` and ``0``/``0.0`` are never requested even though their string
    forms (``"False"``, ``"0.0"``) would not otherwise match the empty-ish set
    -- a treatment value can arrive already typed (from a schema-validated
    spec) rather than as the string a lock behaviour normally carries.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value == 0:
        return False
    return str(value).strip() not in _EMPTYISH


def expected_filters(
    strategy: str, treatment: dict[str, Any], schema: dict[str, Any] | None = None
) -> set[str]:
    """The set of decision-function filter names this job's treatment should request.

    ``youtube_v1`` uses the decision function's own fixed table (see
    :data:`YOUTUBE_V1`): each of ``pi``/``psi``/``kappa``/``tau`` requests its
    filter only at exactly ``1`` (rounded, mirroring the decision function's
    own ``int(round(...))``); ``beta`` requests ``fringe`` at any nonzero
    value.

    Any other strategy falls back to a generic rule: every treatment key whose
    value is not "empty" (``""``, ``"0"``, ``"none"``, ``"None"``) is a
    requested filter, named after the key itself -- unless the strategy's own
    schema carries an ``x-requested: {key: filter_name}`` mapping, in which
    case only keys present in that mapping are considered (renamed to the
    mapped filter), and any other key is ignored even if truthy.
    """
    if strategy == "youtube_v1":
        result: set[str] = set()
        for key, name in YOUTUBE_V1.items():
            raw = treatment.get(key, 0)
            try:
                val = int(round(float(raw)))
            except (TypeError, ValueError):
                val = 0
            if key == "beta":
                if val != 0:
                    result.add(name)
            elif val == 1:
                result.add(name)
        return result

    mapping = schema.get("x-requested") if schema else None
    mapping = mapping if isinstance(mapping, dict) else None

    result = set()
    for key, value in treatment.items():
        if not _truthy(value):
            continue
        if mapping is not None:
            if key in mapping:
                result.add(mapping[key])
            # else: not in the mapping -- ignored, even though truthy.
        else:
            result.add(key)
    return result


def requested_matches(expected: set[str], requested: set[str]) -> bool:
    """Does the report's ``requested`` set agree with the lock's expectation?

    Every expected filter must have been requested, and every requested filter
    must be expected -- except the :data:`HYGIENE` filters, which the decision
    function requests on its own for every crawler and which therefore carry
    no information about the treatment either way.
    """
    return not (expected - requested) and not ((requested - expected) - HYGIENE)


@dataclass
class JobFinding:
    crawler: str
    queue: str
    ordinal: int
    job_id: int | None
    arm: str
    outcome: str
    detail: str


@dataclass
class AuditResult:
    experiment_id: int
    hours: int
    sampled: int
    total: int
    findings: list[JobFinding] = field(default_factory=list)

    def counts(self) -> dict[tuple[str, str], int]:
        """``(arm, outcome) -> n`` over every finding."""
        out: dict[tuple[str, str], int] = {}
        for f in self.findings:
            key = (f.arm, f.outcome)
            out[key] = out.get(key, 0) + 1
        return out

    @property
    def audited(self) -> int:
        """How many jobs were actually examined."""
        return len(self.findings)

    def ok(self, strict: bool) -> bool:
        """No ``shape``/``mismatch``/``lost`` ever passes; ``--strict``
        additionally refuses ``unreported``/``degraded``/``missing`` -- and an
        audit that examined no job at all, which is vacuous, not clean.
        ``unfinished`` never fails the run, strict or not."""
        if strict and self.audited == 0:
            return False
        bad = set(_ALWAYS_BAD)
        if strict:
            bad |= _STRICT_BAD
        return not any(f.outcome in bad for f in self.findings)

    def render(self) -> str:
        lines = [
            f"audit: experiment {self.experiment_id} -- {self.sampled}/{self.total} crawler(s) "
            f"sampled, {self.hours}h window"
        ]
        counts = self.counts()
        if counts:
            lines.append("counts (arm, outcome -> n):")
            for (arm, outcome), n in sorted(counts.items()):
                lines.append(f"  {arm}  {outcome}: {n}")
        else:
            lines.append(
                "!! no jobs audited -- nothing in the lock matched a finished job inside the "
                "window; this says nothing about the treatment (fails under --strict)"
            )
        offending = [f for f in self.findings if f.outcome in _OFFENDING]
        if offending:
            shown = offending[:10]
            lines.append(f"first {len(shown)} of {len(offending)} offending job(s):")
            for f in shown:
                jid = f.job_id if f.job_id is not None else "-"
                lines.append(
                    f"  job {jid} [{f.crawler}/{f.queue}#{f.ordinal}] {f.outcome}: {f.detail}"
                )
        return "\n".join(lines)


def _treatment_arm(treatment: dict[str, Any] | None) -> str:
    """The arm a *job's own treatment* names, for a crawler that has no
    configuration to read one from (a runs lock's).

    ``reliability/lean`` when the job asks for both, else the switches it
    actually carries -- only those, since a runs lock says nothing about the
    ones it leaves out. Nothing to go on is ``-``, never a control arm: the
    bot's arm is in the fleet lock this one was compiled against.
    """
    treatment = treatment or {}
    if "reliability" in treatment and "lean" in treatment:
        return f"{treatment['reliability']}/{treatment['lean']}"
    named = [f"{k}={treatment[k]}" for k in ("pi", "psi", "kappa", "tau") if k in treatment]
    return ",".join(named) if named else "-"


def _arm_label(crawler: dict[str, Any], treatment: dict[str, Any] | None = None) -> str:
    """``reliability/lean`` from the lock crawler's own preferences when both
    are set, else ``pi=...,psi=...,kappa=...,tau=...`` from its params.

    A runs lock's crawler carries no configuration at all, so there the arm
    comes from the job's treatment instead (:func:`_treatment_arm`)."""
    config = crawler.get("configuration")
    if config is None:
        return _treatment_arm(treatment)
    prefs = {p.get("name"): p.get("value") for p in config.get("preferences", [])}
    if "reliability" in prefs and "lean" in prefs:
        return f"{prefs['reliability']}/{prefs['lean']}"
    params = config.get("params") or {}
    return "pi={},psi={},kappa={},tau={}".format(
        params.get("pi", 0), params.get("psi", 0), params.get("kappa", 0), params.get("tau", 0)
    )


def _behaviors(job: dict[str, Any]) -> dict[str, Any]:
    return {b["name"]: b.get("value") for b in job.get("behaviors", [])}


def lock_strategies(lock: Lock) -> set[str]:
    """Every distinct ``strategy`` behaviour value named across the lock's jobs.

    The lock itself carries no schemas (see the module docstring), so a caller
    building ``schemas=`` for :func:`audit_experiment` -- to resolve ``x-requested``
    for a non-``youtube_v1`` strategy -- first needs to know which strategy names
    to look up at all. Resolve each one the way ``pmspec validate`` resolves a
    spec's own ``strategy_schemas``: via
    ``pmspec.spec.validator.SpecValidator._strategy_schema``, which
    honours an inline/path ``strategy_schemas`` entry (only ever present on a
    spec, never a lock), then ``PMSPEC_STRATEGY_SCHEMA_DIRS``, then the sibling
    ``bot-decision-yt`` checkout.
    """
    strategies: set[str] = set()
    for crawler in lock.crawlers:
        for queue in crawler.get("queues", []):
            for job in queue.get("jobs", []):
                strategy = _behaviors(job).get("strategy")
                if strategy:
                    strategies.add(str(strategy))
    return strategies


def _job_treatment(
    crawler: dict[str, Any], job: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    """``(strategy, treatment)`` for a lock job worth auditing, else ``None``.

    A ``strategy`` behaviour wins, with the job's other behaviours as the
    treatment. Without one, a job of :data:`LAMBDA_SELECT_TYPE` is a
    params-based YouTube job: the runner hands the decision function
    ``configuration.params`` via ``selectionConfiguration``, so it is audited
    under ``youtube_v1`` with those params (``pi``/``psi``/``kappa``/``tau``/
    ``beta``) as the treatment. Anything else has nothing to audit.

    The params fallback needs a ``configuration``, which a runs lock's crawler
    has none of -- its bot was configured by the fleet lock it appends to. A
    strategy-less lambda job there is not audited rather than audited against
    an assumed control arm, which would report every treated bot as a mismatch.
    """
    behaviors = _behaviors(job)
    strategy = behaviors.get("strategy")
    if strategy is not None:
        return str(strategy), {k: v for k, v in behaviors.items() if k != "strategy"}
    config = crawler.get("configuration")
    if config is not None and job.get("type") == LAMBDA_SELECT_TYPE:
        params = config.get("params") or {}
        return "youtube_v1", {k: params.get(k, 0) for k in YOUTUBE_V1}
    return None


def _offset_seconds(queue: dict[str, Any]) -> float:
    try:
        return parse_offset(str(queue.get("start_offset") or "+0s")).total_seconds()
    except ValueError:
        return 0.0


def _wall(value: Any) -> str:
    return str(value or "").replace(" ", "T")[:19]


def _sort_queues(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same ordering ``verify.live_shape`` uses, so ordinals line up when a
    crawler has more than one queue sharing a name."""
    return sorted(rows, key=lambda q: (_wall(q.get("start_at")), q.get("name", "")))


def _live_jobs(session: Any, queue_id: int) -> list[dict[str, Any]]:
    """Every job in a queue, fully paginated.

    A bare ``session.get("jobs", params={"filter[queue]": ...})`` with no
    ``per_page`` relies on the endpoint's own default page size -- 15 on the
    real API, sorted newest-id-first -- which silently truncates any queue
    with more jobs than that to its highest-id ones. Routing through
    :func:`verify._paginate` requests the same ``per_page`` the queue listings
    already use and keeps paging until a short page, so every ordinal the lock
    expects actually has a live job to compare against.
    """
    rows = _paginate(session, "jobs", params={"filter[queue]": str(queue_id)})
    return sorted(rows, key=lambda j: (j.get("order") or 0, j.get("id") or 0))


def _decision_report(job: dict[str, Any]) -> dict[str, Any]:
    """The raw ``decision_report`` dict off a live job row.

    ``job_decision_payload`` (``orchestrator.status``) rebuilds only
    ``filters``/``llm``/``warnings`` from this column -- deliberately, since
    that is all a decision-function *payload* carries. ``requested``/
    ``applied`` are extra columns the API persists alongside it
    (``docs/integration-contract.md`` / ``status.py`` ~L718-760), so reading
    them means going to the column directly rather than through that helper.
    """
    raw = job.get(status_mod.DECISION_REPORT_FIELD)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _requested_applied(report: dict[str, Any]) -> tuple[set[str], set[str]] | None:
    """``(requested, applied)`` filter-name sets from a raw ``decision_report``.

    Most reports carry ``requested``/``applied`` directly. Every backfilled
    row and the decision function's own payload do not -- that shape only ever
    has ``degraded``/``treatment_lost``/``selection_mode``/``filters``/``llm``
    -- so those are derived from ``filters`` exactly as the runner derives its
    own summary lists: a filter in state ``"ok"`` was both requested and
    applied; one in a ``"failed: ..."`` state was requested but did not apply;
    ``"skipped"``/``"not_implemented"`` (and any other state) mean it was never
    requested at all; and ``already_watched`` is dropped from both sets,
    exactly as the runner's ``NON_TREATMENT_FILTERS`` drops it.

    Returns ``None`` when there is no evidence to derive anything from --
    neither ``requested``/``applied`` nor ``filters`` -- so a caller never
    compares against a manufactured empty set and calls that a ``mismatch``.
    """
    if "requested" in report or "applied" in report:
        return set(report.get("requested") or []), set(report.get("applied") or [])
    filters = report.get("filters")
    if not isinstance(filters, dict):
        return None
    requested: set[str] = set()
    applied: set[str] = set()
    for name, state in filters.items():
        if str(name).strip().lower() in _NON_TREATMENT_FILTERS:
            continue
        state_l = str(state).strip().lower()
        if state_l == "ok":
            requested.add(name)
            applied.add(name)
        elif state_l.startswith("failed"):
            requested.add(name)
    return requested, applied


_TZ_OFFSET_RE = re.compile(r"([+-]\d{2})(\d{2})$")


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    text = _TZ_OFFSET_RE.sub(r"\1:\2", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def audit_experiment(
    session: Any,
    experiment_id: int,
    lock_text: str,
    *,
    hours: int = 24,
    sample: int | None = None,
    strict: bool = False,
    schemas: dict[str, dict[str, Any]] | None = None,
) -> AuditResult:
    """Audit every lambda job the lock describes against what actually ran.

    ``strict`` affects only :meth:`AuditResult.ok`; every finding is always
    collected, so a non-strict caller still sees ``unreported``/``degraded``/
    ``missing``/``unfinished`` rows -- they just do not fail the run.
    """
    lock = load_lock(lock_text)
    schemas = schemas or {}

    crawler_names = sorted(c["name"] for c in lock.crawlers)
    total = len(crawler_names)
    if sample is not None and sample < total:
        chosen = set(random.Random(experiment_id).sample(crawler_names, sample))
    else:
        chosen = set(crawler_names)
    sampled = len(chosen)

    exp = session.get(f"experiments/{experiment_id}")
    live_by_name = {c["name"]: c for c in (exp.get("crawlers") or [])}

    queues = _paginate(session, f"experiments/{experiment_id}/queues-unprocessed") + _paginate(
        session, f"experiments/{experiment_id}/queues-processed"
    )
    live_queues_by_crawler: dict[int, list[dict[str, Any]]] = {}
    for q in queues:
        live_queues_by_crawler.setdefault(int(q["crawler_id"]), []).append(q)
    for cid, rows in live_queues_by_crawler.items():
        live_queues_by_crawler[cid] = _sort_queues(rows)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    findings: list[JobFinding] = []

    for crawler in lock.crawlers:
        name = crawler["name"]
        if name not in chosen:
            continue
        # A fleet crawler's arm is a property of the bot; a runs crawler has no
        # configuration, so its arm is whatever each job's treatment asks for.
        arm = _arm_label(crawler)
        live_crawler = live_by_name.get(name)
        if live_crawler is None:
            # Wrong experiment id, or a partial upload that never created this
            # crawler: every strategy job it should have run is missing, not
            # silently skipped.
            for lock_queue in crawler.get("queues", []):
                qname = lock_queue.get("name", "")
                for ordinal, job in enumerate(lock_queue.get("jobs", [])):
                    resolved = _job_treatment(crawler, job)
                    if resolved is None:
                        continue
                    findings.append(
                        JobFinding(
                            name,
                            qname,
                            ordinal,
                            None,
                            _arm_label(crawler, resolved[1]),
                            "missing",
                            "no live crawler",
                        )
                    )
            continue
        live_cid = int(live_crawler["id"])
        live_by_qname: dict[str, list[dict[str, Any]]] = {}
        for q in live_queues_by_crawler.get(live_cid, []):
            live_by_qname.setdefault(q.get("name", ""), []).append(q)
        qname_position: dict[str, int] = {}

        # Same-name queues: the live side is sorted by start_at, so the lock
        # side is sorted by offset, which is what that start order corresponds
        # to -- otherwise the authoring order in the spec decides which live
        # queue each lock queue is compared with.
        lock_queues = sorted(
            crawler.get("queues", []), key=lambda q: (_offset_seconds(q), q.get("name", ""))
        )
        for lock_queue in lock_queues:
            qname = lock_queue.get("name", "")
            position = qname_position.get(qname, 0)
            qname_position[qname] = position + 1
            candidates = live_by_qname.get(qname, [])
            lock_jobs = lock_queue.get("jobs", [])

            if position >= len(candidates):
                for ordinal, job in enumerate(lock_jobs):
                    resolved = _job_treatment(crawler, job)
                    if resolved is None:
                        continue
                    findings.append(
                        JobFinding(
                            name,
                            qname,
                            ordinal,
                            None,
                            _arm_label(crawler, resolved[1]),
                            "missing",
                            "no live queue",
                        )
                    )
                continue

            live_queue = candidates[position]

            # Read bound: a queue's own finished_at is the evidence its jobs
            # ran recently at all. Checked before the jobs call so an old
            # queue never costs one. Missing entirely (still running, or an
            # older API shape without the column) never excludes it -- only a
            # timestamp that positively predates the window does.
            queue_occurred = _parse_dt(status_mod.finished_at_of(live_queue))
            if queue_occurred is not None and queue_occurred < cutoff:
                continue

            live_jobs = _live_jobs(session, int(live_queue["id"]))

            # More live jobs than the lock is a drifted queue even when every
            # lock ordinal happens to line up (the extra job is at the tail):
            # one queue-level shape finding, then the ordinals are still
            # audited on their own merits. Fewer live jobs is already caught
            # per ordinal as `missing` (strict-only by design) and is not
            # escalated here.
            if len(live_jobs) > len(lock_jobs):
                findings.append(
                    JobFinding(
                        name,
                        qname,
                        0,
                        None,
                        arm,
                        "shape",
                        f"lock has {len(lock_jobs)} jobs, live has {len(live_jobs)}",
                    )
                )

            for ordinal, job in enumerate(lock_jobs):
                resolved = _job_treatment(crawler, job)
                if resolved is None:
                    continue
                strategy, treatment = resolved
                arm = _arm_label(crawler, treatment)

                if ordinal >= len(live_jobs):
                    findings.append(
                        JobFinding(name, qname, ordinal, None, arm, "missing", "no live job")
                    )
                    continue

                live_job = live_jobs[ordinal]
                job_id = live_job.get("id")

                # Secondary guard: the job's own record, in case it disagrees
                # with its queue's finished_at (or the queue lacked one).
                job_occurred = _parse_dt(
                    status_mod.finished_at_of(live_job) or status_mod.started_at_of(live_job)
                )
                if job_occurred is not None and job_occurred < cutoff:
                    continue

                # Shape first: a job of the wrong type at this ordinal means
                # the live queue drifted from the lock and nothing below --
                # including a clean "unreported" on a visitjob -- can be
                # trusted. A live row with no type cannot be checked.
                lock_type = job.get("type")
                live_type = live_job.get("type")
                if lock_type and live_type and str(live_type) != str(lock_type):
                    findings.append(
                        JobFinding(
                            name,
                            qname,
                            ordinal,
                            job_id,
                            arm,
                            "shape",
                            f"lock {lock_type} vs live {live_type} at ordinal {ordinal}",
                        )
                    )
                    continue

                code = status_mod.normalize(live_job.get("status_code"))
                if status_mod.is_running(code):
                    findings.append(
                        JobFinding(
                            name, qname, ordinal, job_id, arm, "unfinished", f"status_code={code}"
                        )
                    )
                    continue
                if not status_mod.is_terminal(code):
                    # NULL, empty, or a value the vocabulary does not know:
                    # unknown, and unknown is never "still running".
                    findings.append(
                        JobFinding(
                            name,
                            qname,
                            ordinal,
                            job_id,
                            arm,
                            "unreported",
                            f"status_code={code!r} is not a known state",
                        )
                    )
                    continue

                if not status_mod.job_decision_reported(live_job):
                    findings.append(
                        JobFinding(
                            name, qname, ordinal, job_id, arm, "unreported", "no decision_report"
                        )
                    )
                    continue

                report = _decision_report(live_job)
                derived = _requested_applied(report)
                if derived is None:
                    findings.append(
                        JobFinding(
                            name,
                            qname,
                            ordinal,
                            job_id,
                            arm,
                            "unreported",
                            "decision_report has neither requested/applied nor filters",
                        )
                    )
                    continue
                requested, applied = derived

                schema = schemas.get(strategy)
                expected = expected_filters(strategy, treatment, schema)

                if not requested_matches(expected, requested):
                    findings.append(
                        JobFinding(
                            name,
                            qname,
                            ordinal,
                            job_id,
                            arm,
                            "mismatch",
                            f"expected {sorted(expected)}, requested {sorted(requested)}",
                        )
                    )
                    continue

                verdict = status_mod.job_decision_verdict(live_job)

                if verdict.treatment_lost:
                    detail = "treatment_lost"
                    if verdict.degraded:
                        detail += "; degraded"
                    findings.append(JobFinding(name, qname, ordinal, job_id, arm, "lost", detail))
                    continue

                if not requested <= applied:
                    detail = f"not applied: {sorted(requested - applied)}"
                    if verdict.degraded:
                        detail += "; degraded"
                    findings.append(
                        JobFinding(name, qname, ordinal, job_id, arm, "degraded", detail)
                    )
                    continue

                detail = "degraded" if verdict.degraded else ""
                findings.append(JobFinding(name, qname, ordinal, job_id, arm, "ok", detail))

    return AuditResult(
        experiment_id=experiment_id, hours=hours, sampled=sampled, total=total, findings=findings
    )
