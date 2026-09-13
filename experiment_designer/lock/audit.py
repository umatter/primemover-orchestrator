"""``pmspec audit``: did each finished job apply the treatment its lock says it should?

The lock records, per lambda job, a ``strategy`` behaviour plus the treatment
parameters (``pi``/``psi``/``kappa``/``tau``/``beta`` for ``youtube_v1``, or
arbitrary keys for anything else). The live job row, once it finishes, carries
the decision function's own account of what it did: ``decision_report``
(``requested``/``applied``/``filters``), ``decision_treatment_lost`` and
``status_code``. This module walks a lock's jobs, matches each one carrying a
``strategy`` behaviour to its live counterpart by (crawler name, queue name,
job ordinal), and reports whether the two agree.

Three checks, in order, each producing a distinct outcome the moment it fails
(a later check is never reached once an earlier one has already flagged the
job):

``reported``
    Did the job carry a decision verdict at all (:func:`orchestrator.status.
    job_decision_reported`)? ``False`` -> ``unreported`` -- unknown, never
    clean.
``requested``
    Does the lock's expected filter set (:func:`expected_filters`) match what
    the decision function says it actually requested? Mismatch -> ``mismatch``.
``applied``
    Did every requested filter apply, and was the bot not treatment-lost?
    ``decision_treatment_lost`` true -> ``lost``; requested filters not fully
    applied (but not lost) -> ``degraded``; otherwise -> ``ok``.

A job whose live ``status_code`` is not terminal is ``unfinished`` before any
of the above are evaluated -- it has not had a chance to report anything yet.
A lock job with no live counterpart at its ordinal (a shorter live queue, or no
live queue at all for that name) is ``missing``. Both are informational, not
failures, unless the caller asks for ``--strict``.

Only jobs whose lock behaviours include a ``strategy`` key are considered --
a plain ``visitjob`` has nothing to audit and produces no finding, but still
occupies its ordinal so a genuinely missing job downstream is not mistaken for
one that was never a lambda job to begin with.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from orchestrator import status as status_mod

from .io import load_lock
from .verify import _paginate

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

#: Values a treatment key's string form must not equal to count as "requested"
#: under the generic (non-youtube_v1) rule.
_EMPTYISH = frozenset({"", "0", "none", "None"})

#: Outcomes worth listing individually in a rendered report.
_OFFENDING = frozenset({"unreported", "mismatch", "lost", "degraded", "missing"})

#: Outcomes that fail the run regardless of ``--strict``.
_ALWAYS_BAD = frozenset({"mismatch", "lost"})

#: Outcomes that only fail the run under ``--strict`` -- absence of evidence
#: (``unreported``, ``missing``, ``unfinished``) and a degraded-but-not-lost
#: arm are all "a human should look", never a hard failure on their own.
_STRICT_BAD = frozenset({"unreported", "degraded", "missing", "unfinished"})


def _truthy(value: Any) -> bool:
    """Is *value* a "requested" treatment value under the generic rule?"""
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

    def ok(self, strict: bool) -> bool:
        """No ``mismatch``/``lost`` ever passes; ``--strict`` additionally
        refuses ``unreported``/``degraded``/``missing``/``unfinished``."""
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
            lines.append("no jobs to audit")
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


def _arm_label(crawler: dict[str, Any]) -> str:
    """``reliability/lean`` from the lock crawler's own preferences when both
    are set, else ``pi=...,psi=...,kappa=...,tau=...`` from its params."""
    config = crawler.get("configuration") or {}
    prefs = {p.get("name"): p.get("value") for p in config.get("preferences", [])}
    if "reliability" in prefs and "lean" in prefs:
        return f"{prefs['reliability']}/{prefs['lean']}"
    params = config.get("params") or {}
    return "pi={},psi={},kappa={},tau={}".format(
        params.get("pi", 0), params.get("psi", 0), params.get("kappa", 0), params.get("tau", 0)
    )


def _behaviors(job: dict[str, Any]) -> dict[str, Any]:
    return {b["name"]: b.get("value") for b in job.get("behaviors", [])}


def _wall(value: Any) -> str:
    return str(value or "").replace(" ", "T")[:19]


def _sort_queues(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same ordering ``verify.live_shape`` uses, so ordinals line up when a
    crawler has more than one queue sharing a name."""
    return sorted(rows, key=lambda q: (_wall(q.get("start_at")), q.get("name", "")))


def _live_jobs(session: Any, queue_id: int) -> list[dict[str, Any]]:
    body = session.get("jobs", params={"filter[queue]": str(queue_id)})
    rows = body.get("data", body) if isinstance(body, dict) else body
    rows = list(rows or [])
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
        live_crawler = live_by_name.get(name)
        if live_crawler is None:
            continue
        arm = _arm_label(crawler)
        live_cid = int(live_crawler["id"])
        live_by_qname: dict[str, list[dict[str, Any]]] = {}
        for q in live_queues_by_crawler.get(live_cid, []):
            live_by_qname.setdefault(q.get("name", ""), []).append(q)
        qname_position: dict[str, int] = {}

        for lock_queue in crawler.get("queues", []):
            qname = lock_queue.get("name", "")
            position = qname_position.get(qname, 0)
            qname_position[qname] = position + 1
            candidates = live_by_qname.get(qname, [])
            lock_jobs = lock_queue.get("jobs", [])

            if position >= len(candidates):
                for ordinal, job in enumerate(lock_jobs):
                    if "strategy" not in _behaviors(job):
                        continue
                    findings.append(
                        JobFinding(name, qname, ordinal, None, arm, "missing", "no live queue")
                    )
                continue

            live_queue = candidates[position]
            live_jobs = _live_jobs(session, int(live_queue["id"]))

            for ordinal, job in enumerate(lock_jobs):
                behaviors = _behaviors(job)
                strategy = behaviors.get("strategy")
                if strategy is None:
                    continue

                if ordinal >= len(live_jobs):
                    findings.append(
                        JobFinding(name, qname, ordinal, None, arm, "missing", "no live job")
                    )
                    continue

                live_job = live_jobs[ordinal]
                job_id = live_job.get("id")

                occurred = _parse_dt(
                    status_mod.finished_at_of(live_job) or status_mod.started_at_of(live_job)
                )
                if occurred is not None and occurred < cutoff:
                    # This job's own record shows it ran outside the audit
                    # window; skip it entirely rather than judging it stale.
                    continue

                code = status_mod.normalize(live_job.get("status_code"))
                if not status_mod.is_terminal(code):
                    findings.append(
                        JobFinding(
                            name, qname, ordinal, job_id, arm, "unfinished", f"status_code={code}"
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

                treatment = {k: v for k, v in behaviors.items() if k != "strategy"}
                schema = schemas.get(strategy)
                expected = expected_filters(strategy, treatment, schema)
                report = _decision_report(live_job)
                requested = set(report.get("requested") or [])
                applied = set(report.get("applied") or [])

                if expected != requested:
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

                if status_mod.job_decision_treatment_lost(live_job):
                    findings.append(
                        JobFinding(name, qname, ordinal, job_id, arm, "lost", "treatment_lost")
                    )
                    continue

                if not requested <= applied:
                    findings.append(
                        JobFinding(
                            name,
                            qname,
                            ordinal,
                            job_id,
                            arm,
                            "degraded",
                            f"not applied: {sorted(requested - applied)}",
                        )
                    )
                    continue

                findings.append(JobFinding(name, qname, ordinal, job_id, arm, "ok", ""))

    return AuditResult(
        experiment_id=experiment_id, hours=hours, sampled=sampled, total=total, findings=findings
    )
