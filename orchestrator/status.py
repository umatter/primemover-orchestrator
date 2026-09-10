"""Canonical job/queue status vocabulary for the PrimeMover ecosystem.

This module is the single source of truth for the status contract between the
runner (which writes) and the API, GUI and orchestrator (which read). It exists
because five divergent status vocabularies had grown up across the codebase --
``e2e/job_status.py``, ``e2e/runner.py``, ``tracing/trace_builder.py``,
``debug/job_debugger.py`` and ``cli.py`` each classified failure differently, and
some of them read a ``status`` field the API has never sent.

The wire contract is documented in ``docs/integration-contract.md``; keep the two
in sync.

Wire format
-----------
The runner reports job state with ``PUT /api/v1/runner/jobs/{id}`` and queue
state with ``PUT /api/v1/runner/queues/{id}``, sending:

``status_code``
    One of the values in :class:`JobStatus` / :class:`QueueStatus`. The field is
    named ``status_code`` -- there is no ``status`` field on the wire.
``status_message``
    Human-readable outcome. Either the bare state text (``"job started"``) or,
    when the runner has a diagnostic to attach, ``"<state text> - <detail>"``
    where the detail is a single-line, truncated cause.

``warning`` is a terminal state introduced 2026-08-27: the job ran to completion
but something the experiment cares about degraded -- job data could not be
written, or a filter the experiment asked for did not apply. It was previously
reported as ``success``, which made silent treatment-arm degradation invisible.

The decision-function contract, and the mistake it encodes against
--------------------------------------------------------------------
The ``lambda_*`` helpers at the bottom of this module are the executable copy of
the decision-function half of that contract. Three fields, three different
questions, and they must not be conflated:

``degraded``
    A *requested* filter failed to apply, or selection failed outright. "A
    human should look at this decision."
``selection_mode`` (``"llm"`` / ``"random"``)
    A **neutral description** of how the video was chosen. ``"random"`` means no
    LLM rating informed the pick.
``treatment_lost``
    The bot was configured to be treated and produced control-arm data anyway.
    "This session's data is scientifically void." The only alert-worthy state.

**Never branch on ``selection_mode`` alone.** The first version of this contract
did exactly that -- it treated ``selection_mode == "random"`` as proof that a
treatment arm had silently become a control arm -- and it was wrong for the most
common case there is. A genuine control crawler (``pi = psi = kappa = tau = 0``)
legitimately selects at random, because no LLM filter was ever *requested*.
Measured on the production box on 2026-08-27: **295 of 358 real lambda payloads
(82%) carry no rating column at all**, and **236 of the 243 bot configurations in
the live database are control-arm**. Gating on ``selection_mode`` would therefore
have marked ~82% of all ``lambda_select`` jobs as ``warning`` and held a
``severity=critical`` alert on permanently -- an alarm that is always on is an
alarm nobody reads, which is the precise opposite of what this work is for.

By the same measurement, ``"skipped"`` in the ``filters`` report means *not
requested*, not *failed*: four or five of the six filters are ``skipped`` on
every healthy control invocation. ``"not_implemented"`` is not a failure either -- it
is the deployed function saying honestly that it was given no history to filter
against (until 2026-09-10 it meant the filter was a stub; now it means the caller
sent none). ``_FILTER_OK_STATES`` states the whole rule and is the copy the API's
``DecisionVerdict`` and the runner's ``LambdaSelectJobResult`` are written
against; :func:`lambda_failed_filters` is the only correct way to count
casualties.

Finally, a payload carrying **no** degradation report is a **legacy** payload
from a decision function deployed before the report existed. Absence reads as
*unknown* -- never as degraded, and never as clean.
:func:`lambda_report_present` and :class:`LambdaVerdict` keep "clean" and "we
cannot tell" apart. That shape is now the rare one: of the 6682 archived lambda
payloads on ex63 (decoded 2026-09-08) 5901 carry the full report, emitted on
every invocation since 2026-08-27 ~15:38; 361 are the report-less
``{present, procedure, selected, timestamps, warnings, xpath}`` shape, the
newest of them from 2026-08-27 08:18; 419 are zero-byte objects; and one holds
only the lambda input. The last two are a third and fourth way of saying
*unknown*.

The same verdict is now **persisted on the job row** (``decision_degraded``,
``decision_treatment_lost``, ``decision_selection_mode``, ``decision_report``),
so a consumer no longer has to fetch a MinIO object to ask whether a treatment
applied. The ``job_decision_*`` helpers read those columns, and they answer by
rebuilding the payload and handing it to the ``lambda_*`` helpers above -- one
implementation of the rules, two sources. A NULL column is *unknown*, never
clean; that is what makes every *version* pairing of the
API-then-GUI-then-runner rollout safe. It does not make the API's own two halves
safe: on ex63 the API source is bind-mounted, so its code is live the moment it
lands on disk while ``php artisan migrate`` is a separate later command, and new
code on an un-migrated schema 500s every crawler and queue read -- 107 minutes
of it, 2026-09-07 22:44 to 2026-09-08 00:31. Migration first, then code. See
``docs/integration-contract.md`` section 0.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "JobStatus",
    "QueueStatus",
    "STATUS_ALIASES",
    "SEVERITY",
    "normalize",
    "status_of",
    "message_of",
    "started_at_of",
    "finished_at_of",
    "is_success",
    "is_warning",
    "is_failure",
    "is_skipped",
    "is_running",
    "is_terminal",
    "is_bad",
    "SEVERITY_SORT_RANK",
    "UNKNOWN_SEVERITY_SORT_RANK",
    "severity_rank",
    "DECISION_DEGRADED_FIELD",
    "DECISION_TREATMENT_LOST_FIELD",
    "DECISION_SELECTION_MODE_FIELD",
    "DECISION_REPORT_FIELD",
    "DECISION_FIELDS",
    "job_decision_payload",
    "job_decision_verdict",
    "job_decision_reported",
    "job_decision_degraded",
    "job_decision_treatment_lost",
    "LAMBDA_FILTER_NAMES",
    "LambdaVerdict",
    "lambda_warnings",
    "lambda_report_present",
    "lambda_is_degraded",
    "lambda_treatment_lost",
    "lambda_selection_mode",
    "lambda_filters",
    "lambda_failed_filters",
    "lambda_skipped_filters",
    "lambda_error",
    "lambda_verdict",
]


class JobStatus(str, Enum):
    """Terminal and non-terminal states a *job* can report."""

    RUNNING = "running"
    SUCCESS = "success"
    WARNING = "warning"
    FAILURE = "failure"
    SKIPPED = "skipped"


class QueueStatus(str, Enum):
    """States a *queue* (run / joblist) can report.

    ``warning`` was added 2026-08-28 for the same reason it exists on jobs: a run
    that lost work is neither a success nor a total failure. Before it, the
    runner set the joblist result unconditionally, so a queue reported
    ``success`` however many of its jobs had died -- observed live on experiment
    145, where two runs completed **8 of 204 jobs (4%)** and both recorded
    ``status_code = success, status_message = "done"``.
    """

    RUNNING = "running"
    SUCCESS = "success"
    WARNING = "warning"
    FAILURE = "failure"
    SKIPPED = "skipped"


#: Legacy, misspelled and synonymous values that still occur in the database or
#: in older code paths, mapped onto the canonical vocabulary. ``skpped`` is a
#: typo that the runner's ``JoblistState`` emitted for SKIPPED_INACTIVE and
#: SKIPPED_REVIEWED until 2026-08-27; rows written before then persist.
STATUS_ALIASES: dict[str, str] = {
    "skpped": "skipped",
    "failed": "failure",
    "error": "failure",
    "fatal": "failure",
    "completed": "success",
    "done": "success",
    "finished": "success",
    "ok": "success",
    "warn": "warning",
    "processing": "running",
    "inprogress": "running",
    "in_progress": "running",
}

#: Ordering used when a single status has to summarise many children, and for
#: sorting findings worst-first. Higher is worse.
SEVERITY: dict[str, int] = {
    "success": 0,
    "skipped": 1,
    "running": 2,
    "warning": 3,
    "failure": 4,
}

#: Ordering used **for sorting only** -- the rank behind ``sortBy=severity`` on
#: the API's queue/job/crawler list endpoints, and behind any worst-first
#: ordering in the orchestrator or the GUI.
#:
#: It is deliberately **not** :data:`SEVERITY`, and the difference is the whole
#: point: this table has an ``unknown`` rank, inserted between ``running`` and
#: ``warning``. :data:`SEVERITY` has no such entry because it answers a different
#: question -- "which of these child statuses does the parent inherit?" -- and
#: other code depends on its exact numbers, so it must not gain one.
#:
#: Sorting is where the contract's "an unrecognised value is unknown, never
#: success" rule is easiest to break: rank unknown at 0 and every unreported or
#: mis-spelled row hides at the clean end of the list, which is exactly where
#: nobody looks. Ranking it above ``running`` and below ``warning`` says what is
#: true -- worse than anything still in flight, not yet evidence of damage.
#:
#: An empty or missing ``status_code`` (the ``jobs`` table holds thousands of
#: NULLs) is unknown too, and gets the same rank. The API's ``sortBy=severity``
#: must use these six numbers, including that NULL rule.
SEVERITY_SORT_RANK: dict[str, int] = {
    "success": 0,
    "skipped": 1,
    "running": 2,
    "unknown": 3,
    "warning": 4,
    "failure": 5,
}

#: The rank any value not in :data:`SEVERITY_SORT_RANK` sorts at -- including
#: ``None``, ``""`` and a runner typo nobody has an alias for yet.
UNKNOWN_SEVERITY_SORT_RANK: int = SEVERITY_SORT_RANK["unknown"]

_TERMINAL = frozenset({"success", "warning", "failure", "skipped"})


def normalize(value: Any) -> str:
    """Return the canonical lowercase status code for *value*.

    Accepts ``None``, an ``Enum``, or any string in the legacy vocabulary.
    Unknown values are returned lowercased and stripped rather than mapped onto
    a canonical code -- callers should treat an unrecognised status as unknown,
    not as a success.
    """
    if value is None:
        return ""
    if isinstance(value, Enum):
        value = value.value
    text = str(value).strip().lower()
    return STATUS_ALIASES.get(text, text)


def status_of(record: dict[str, Any] | None) -> str:
    """Extract the canonical status from an API job/queue record.

    Reads ``status_code`` (what the API actually sends) and falls back to
    ``status`` for the handful of fixtures and older payloads that use it.
    """
    if not record:
        return ""
    raw = record.get("status_code")
    if raw in (None, ""):
        raw = record.get("status")
    return normalize(raw)


def message_of(record: dict[str, Any] | None) -> str:
    """Extract the human-readable outcome message from an API record."""
    if not record:
        return ""
    for key in ("status_message", "error_message", "message", "error"):
        value = record.get(key)
        if value:
            return str(value)
    return ""


def started_at_of(record: dict[str, Any] | None) -> Any:
    """Return the start timestamp, tolerating the field names in use."""
    if not record:
        return None
    for key in ("started_at", "start_at", "created_at"):
        if record.get(key):
            return record[key]
    return None


def finished_at_of(record: dict[str, Any] | None) -> Any:
    """Return the finish timestamp.

    The API sends ``finished_at``; ``completed_at`` is read only because older
    orchestrator code invented that name and some fixtures still use it.
    """
    if not record:
        return None
    for key in ("finished_at", "completed_at", "ended_at"):
        if record.get(key):
            return record[key]
    return None


def is_success(value: Any) -> bool:
    """True for a clean completion. A ``warning`` is deliberately not success."""
    return normalize(value) == JobStatus.SUCCESS.value


def is_warning(value: Any) -> bool:
    """True when the job completed but something degraded."""
    return normalize(value) == JobStatus.WARNING.value


def is_failure(value: Any) -> bool:
    return normalize(value) == JobStatus.FAILURE.value


def is_skipped(value: Any) -> bool:
    return normalize(value) == JobStatus.SKIPPED.value


def is_running(value: Any) -> bool:
    return normalize(value) == JobStatus.RUNNING.value


def is_terminal(value: Any) -> bool:
    """True when no further state change is expected."""
    return normalize(value) in _TERMINAL


def is_bad(value: Any) -> bool:
    """True for anything a human should look at: a failure or a warning.

    This is the predicate debugging tools should use to decide whether a job is
    worth surfacing. Using :func:`is_failure` alone hides exactly the silent
    degradation the ``warning`` state was introduced to expose.
    """
    return normalize(value) in (JobStatus.FAILURE.value, JobStatus.WARNING.value)


def severity_rank(value: Any) -> int:
    """Sort rank for *value*: higher is worse. See :data:`SEVERITY_SORT_RANK`.

    Aliases are resolved first, so ``failed`` and ``failure`` sort together --
    which is the reason this exists at all. Sorting on the raw string splits the
    256 legacy ``failed`` job rows away from the 1607 ``failure`` ones and puts
    them either side of ``skipped``.

    Anything unrecognised -- ``None``, ``""``, a future spelling -- ranks as
    *unknown*, between ``running`` and ``warning``. Never as success.
    """
    return SEVERITY_SORT_RANK.get(normalize(value), UNKNOWN_SEVERITY_SORT_RANK)


# ---------------------------------------------------------------------------
# Decision-function (lambda) response contract
# ---------------------------------------------------------------------------
# ``select_recommendation`` returns HTTP 200 even when its LLM-driven filters
# have failed, so the status code alone never reveals a degraded decision. These
# helpers read the fields that do -- and, just as importantly, define what must
# *not* be read as a failure. See the module docstring for the measurement that
# forced the distinction.

#: The six filters the decision function reports on, in the order it emits them.
#: Used only to render findings in a stable order; an unknown key is never
#: dropped.
LAMBDA_FILTER_NAMES: tuple[str, ...] = (
    "already_watched",
    "mainstream_media",
    "children_entertainment",
    "similar",
    "political",
    "fringe",
)

#: THE FILTER-STATE RULE, stated once for the whole ecosystem.
#:
#: A filter state is a **casualty only when the decision function says it
#: failed**. It writes exactly one failure spelling -- ``"failed: <ExcClass>:
#: <msg>"`` -- and three non-failure ones:
#:
#: ``"ok"``
#:     requested, and it applied.
#: ``"skipped"``
#:     **not requested.** The normal state of four or five of the six filters
#:     on every healthy control-crawler invocation, and counting it as a failure is the
#:     single most common way to turn this contract back into a false-alarm
#:     generator.
#: ``"not_implemented"``
#:     the filter had nothing to work with, and the decision function says so
#:     rather than lying with ``"ok"``. Since 2026-09-10 this means the caller
#:     supplied no ``already_watched`` history -- an un-redeployed runner, or one
#:     whose history lookup failed -- and NOT that the filter is a stub, which is
#:     what it meant for the first year of its life. Either way nothing went
#:     wrong and nothing was filtered. ``already_watched`` is always-on hygiene,
#:     never a configured treatment, so it is also absent from the function's own
#:     ``requested_treatments`` denominator -- it can neither degrade a decision
#:     nor lose a treatment.
#: ``""`` / absent
#:     nothing was said. Unknown, and unknown is not a finding.
#:
#: Anything else is treated as a failure, so an unrecognised *future* state errs
#: towards being seen rather than towards silence.
#:
#: Measured on ex63 2026-09-08, decoding all 6682 archived lambda payloads:
#: ``already_watched`` read ``not_implemented`` in **every** payload since
#: ``20260902-144403`` (32 of 32), with ``degraded: false`` in 31 of them. Before
#: this set was corrected, that state alone put a "failed filter" warning on
#: every healthy lambdaselect job the deployed function had produced.
#:
#: It reports ``"ok"`` as of 2026-09-10, when the history loop was closed
#: (integration-contract 7.3.1). Both states are still healthy and neither may be
#: counted as a failure -- which is precisely why this set, rather than a
#: hard-coded expectation about one filter, is the rule.
#:
#: The other two implementations of this rule are
#: ``App\Modules\Job\Service\DecisionVerdict::FILTER_OK_STATES`` in
#: primemover_api and ``LambdaSelectJobResult`` in primemover_runner. They must
#: agree with this set; this module is the copy the other two are written
#: against (docs/integration-contract.md 7.3).
_FILTER_OK_STATES = frozenset({"ok", "skipped", "not_implemented", ""})


@dataclass(frozen=True)
class LambdaVerdict:
    """What one decision-function response says about its own health.

    Three questions, deliberately kept apart, because conflating any two of them
    is what the amended contract exists to prevent:

    ``reported``
        Did this payload carry a degradation report at all? ``False`` means
        *unknown* -- a legacy payload from a decision function deployed before
        the report existed. Unknown is never an alarm.
    ``degraded``
        Did at least one *requested* filter fail, or did selection fail
        outright? This is "a human should look at this decision".
    ``treatment_lost``
        Was this bot configured to be treated and nevertheless produced
        control-arm data? This is "this session's data is scientifically void",
        and the only condition worth alerting on.

    ``selection_mode`` is carried along as a neutral descriptor. Nothing in this
    module ever derives an alarm from it alone.
    """

    reported: bool = False
    degraded: bool = False
    treatment_lost: bool = False
    selection_mode: str = ""
    filters: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def unknown(self) -> bool:
        """True when the payload predates the degradation report entirely."""
        return not self.reported

    @property
    def failed_filters(self) -> dict[str, str]:
        """Only filters that actually failed.

        ``"skipped"`` (not requested) and ``"not_implemented"`` (a stub that
        cannot apply) are not failures -- see :data:`_FILTER_OK_STATES`.
        """
        return _failed_filters(self.filters)

    @property
    def skipped_filters(self) -> list[str]:
        """Filters that were never requested -- informational, never a finding."""
        return [
            name
            for name, state in _ordered_filters(self.filters)
            if str(state).strip().lower() == "skipped"
        ]

    @property
    def needs_attention(self) -> bool:
        """A human should look: a hard error or a genuine degradation."""
        return bool(self.error) or self.degraded

    def label(self) -> str:
        """One word for a table cell or a log line."""
        if self.error:
            return "error"
        if self.treatment_lost:
            return "treatment-lost"
        if self.degraded:
            return "degraded"
        if not self.reported:
            return "no report"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "reported": self.reported,
            "degraded": self.degraded,
            "treatment_lost": self.treatment_lost,
            "selection_mode": self.selection_mode,
            "filters": dict(self.filters),
            "failed_filters": self.failed_filters,
            "skipped_filters": self.skipped_filters,
            "warnings": list(self.warnings),
            "error": self.error,
        }


def _ordered_filters(filters: dict[str, Any] | None) -> list[tuple[str, str]]:
    """``(name, state)`` pairs in the contract's order, unknown keys last."""
    if not isinstance(filters, dict):
        return []
    known = [(name, str(filters[name])) for name in LAMBDA_FILTER_NAMES if name in filters]
    extra = [
        (str(name), str(state))
        for name, state in filters.items()
        if name not in LAMBDA_FILTER_NAMES
    ]
    return known + extra


def _failed_filters(filters: dict[str, Any] | None) -> dict[str, str]:
    """Filters whose state is a genuine failure.

    :data:`_FILTER_OK_STATES` is the rule and carries the reasoning: ``"ok"``,
    ``"skipped"``, ``"not_implemented"`` and the empty state are not casualties.
    Anything else is treated as a failure so an unrecognised future state errs
    towards being seen.
    """
    return {
        name: state
        for name, state in _ordered_filters(filters)
        if state.strip().lower() not in _FILTER_OK_STATES
    }


def lambda_filters(payload: dict[str, Any] | None) -> dict[str, str]:
    """The per-filter report, as strings, in the contract's order."""
    if not payload:
        return {}
    return dict(_ordered_filters(payload.get("filters")))


def lambda_failed_filters(payload: dict[str, Any] | None) -> dict[str, str]:
    """Filters that were requested and did not apply.

    This is the only correct way to count filter casualties: ``"skipped"`` means
    *not requested*, and on a control crawler five of the six filters are
    skipped on every single healthy invocation.
    """
    if not payload:
        return {}
    return _failed_filters(payload.get("filters"))


def lambda_skipped_filters(payload: dict[str, Any] | None) -> list[str]:
    """Filters that were never requested. Informational; never a finding."""
    if not payload:
        return []
    return [
        name
        for name, state in _ordered_filters(payload.get("filters"))
        if state.strip().lower() == "skipped"
    ]


def lambda_warnings(payload: dict[str, Any] | None) -> list[str]:
    """Warnings emitted by the decision function for one invocation."""
    if not payload:
        return []
    raw = payload.get("warnings") or []
    if isinstance(raw, str):
        return [raw]
    return [str(item) for item in raw]


def lambda_report_present(payload: dict[str, Any] | None) -> bool:
    """True when the payload carries a degradation report of any version.

    ``False`` means the answer to "did this decision degrade?" is **unknown**,
    not "no". The decision function deployed today emits the report on every
    invocation, so ``False`` now means one of: a payload from before
    2026-08-27 ~15:38 (361 of the 6682 objects in the MinIO archive, decoded
    2026-09-08; newest 2026-08-27 08:18), a zero-byte object (419 more), or
    something that is not a decision payload at all (one object holds only the
    lambda input). Rare is not the same as gone: an archive reader still meets
    all of them, and all of them are unknown.
    """
    if not payload:
        return False
    return any(key in payload for key in ("degraded", "treatment_lost", "filters"))


def lambda_is_degraded(payload: dict[str, Any] | None) -> bool:
    """True when a *requested* filter failed, or selection failed outright.

    Reads the explicit ``degraded`` flag. For payloads that predate it, falls
    back to evidence the decision function itself produced: an explicit
    per-filter ``"failed: ..."`` state, or a non-empty ``warnings`` list (which
    the function only writes when something it tried to do did not work).

    It deliberately does **not** fall back to ``selection_mode == "random"``.
    That fallback used to exist here and was the bug this contract was amended
    to remove: ``random`` is the correct, healthy mode for a control crawler.

    Absence of any report reads as *not degraded* -- i.e. unknown. Use
    :func:`lambda_report_present` (or :func:`lambda_verdict`) when the
    difference between "clean" and "we cannot tell" matters.
    """
    if not payload:
        return False
    if "degraded" in payload:
        return bool(payload["degraded"])
    if bool(payload.get("treatment_lost")):
        # treatment_lost implies degraded; a payload carrying only the newer
        # field is still self-consistent.
        return True
    if lambda_failed_filters(payload):
        return True
    return bool(lambda_warnings(payload))


def lambda_treatment_lost(payload: dict[str, Any] | None) -> bool:
    """True when a bot configured to be treated produced control-arm data.

    This is the single alarm condition: the session's data is scientifically
    void because the treatment it was assigned did not happen. The decision
    function computes it at the source (``treatment_lost``) precisely so no
    consumer has to combine two terms and get it wrong.

    For payloads written after ``degraded`` but before ``treatment_lost``, it
    falls back to ``degraded and selection_mode == "random"`` -- both terms,
    never ``selection_mode`` alone. For payloads with no report it is ``False``:
    unknown is not an alarm.
    """
    if not payload:
        return False
    if "treatment_lost" in payload:
        return bool(payload["treatment_lost"])
    if "degraded" in payload:
        return bool(payload["degraded"]) and lambda_selection_mode(payload) == "random"
    return False


def lambda_selection_mode(payload: dict[str, Any] | None) -> str:
    """``"llm"``, ``"random"``, or ``""`` when the payload predates the field.

    A **neutral description** of how the video was picked, not a verdict.
    ``"random"`` means no LLM rating informed the pick, which is exactly what a
    control crawler is supposed to produce. Never branch on this alone -- use
    :func:`lambda_is_degraded` or :func:`lambda_treatment_lost`.
    """
    if not payload:
        return ""
    return str(payload.get("selection_mode") or "").strip().lower()


def lambda_error(payload: dict[str, Any] | None) -> str | None:
    """The hard error reported by the local lambda server, if any."""
    if not payload:
        return None
    error = payload.get("error")
    return str(error) if error else None


def lambda_verdict(payload: dict[str, Any] | None) -> LambdaVerdict:
    """Read one decision-function response into a :class:`LambdaVerdict`.

    Preferred over the individual helpers when a caller needs more than one
    answer, because it keeps "not degraded" and "no report at all" apart in a
    single value.
    """
    return LambdaVerdict(
        reported=lambda_report_present(payload),
        degraded=lambda_is_degraded(payload),
        treatment_lost=lambda_treatment_lost(payload),
        selection_mode=lambda_selection_mode(payload),
        filters=lambda_filters(payload),
        warnings=lambda_warnings(payload),
        error=lambda_error(payload),
    )


# ---------------------------------------------------------------------------
# The decision verdict, persisted on the job row
# ---------------------------------------------------------------------------
# Until this change the verdict above existed only inside a MinIO object and a
# runner log line, so answering "did the treatment apply?" for a job meant
# fetching an artifact -- and, for the 6682 archived ``lambda-jobdata`` objects in
# MinIO (counted 2026-09-07; one more lands with every ``lambdaselect`` job),
# meant nobody ever asked. The API now stores it on ``jobs`` in four columns,
# written by the runner from the same parsed response.
#
# **NULL on any of them means UNKNOWN, never clean.** A legacy job, a non-lambda
# job, and a job run by a runner that predates the change are all indistinguishable
# from each other and all of them are "we cannot tell". That is precisely what
# makes every *version* pairing of the staged deploy safe (API migration -> API
# code -> GUI -> runner jar): each intermediate state produces nulls, and nulls
# render as "no verdict". It says nothing about the API's own two halves --
# code before migration is the one unsafe step, and it is an outage rather than
# a null. See the module docstring and docs/integration-contract.md section 0.

#: ``jobs.decision_degraded`` -- boolean, nullable. Mirrors the payload's
#: ``degraded``: a *requested* filter failed, or selection failed outright.
DECISION_DEGRADED_FIELD = "decision_degraded"

#: ``jobs.decision_treatment_lost`` -- boolean, nullable. Mirrors the payload's
#: ``treatment_lost``: the bot was configured to be treated and produced
#: control-arm data. The alarm.
DECISION_TREATMENT_LOST_FIELD = "decision_treatment_lost"

#: ``jobs.decision_selection_mode`` -- varchar(32), nullable. ``"llm"`` /
#: ``"random"``. A neutral descriptor, and the one column no consumer may branch
#: on by itself.
DECISION_SELECTION_MODE_FIELD = "decision_selection_mode"

#: ``jobs.decision_report`` -- JSON, nullable:
#: ``{"filters": {...}, "llm": {...}, "warnings": [...], "applied": [...],
#: "requested": [...]}``.
DECISION_REPORT_FIELD = "decision_report"

#: The four columns, in migration order. Anything projecting the job row for a
#: verdict should select exactly these.
DECISION_FIELDS: tuple[str, ...] = (
    DECISION_DEGRADED_FIELD,
    DECISION_TREATMENT_LOST_FIELD,
    DECISION_SELECTION_MODE_FIELD,
    DECISION_REPORT_FIELD,
)


def _decision_report_of(record: dict[str, Any] | None) -> dict[str, Any]:
    """The ``decision_report`` column as a dict.

    Tolerates the two shapes it arrives in: already decoded (Laravel's ``json``
    cast, the MCP tools) or still a JSON string (a raw DB row, a CSV export).
    Anything unparseable is treated as absent rather than raising -- a corrupt
    report must not make a job unreadable.
    """
    if not record:
        return {}
    raw = record.get(DECISION_REPORT_FIELD)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return raw if isinstance(raw, dict) else {}


def job_decision_payload(record: dict[str, Any] | None) -> dict[str, Any]:
    """Rebuild a decision-function payload from the persisted job columns.

    The point of this function is that there is exactly **one** implementation of
    the verdict rules in this module. The columns are reassembled into the shape
    the wire payload has, and every question is then answered by the same
    ``lambda_*`` helpers that read a payload straight out of MinIO -- so a job row
    and the HAR object it came from can never disagree about what happened.

    A NULL column contributes **no key**, which is what keeps "unknown" distinct
    from "false": :func:`lambda_report_present` looks for ``degraded`` /
    ``treatment_lost`` / ``filters``, so a row with all four columns NULL yields
    ``{}`` and reads as unknown.

    ``decision_selection_mode`` alone is deliberately *not* enough to make a
    payload "present". It is a descriptor, not a verdict; a row carrying only a
    mode still cannot say whether anything degraded.
    """
    if not record:
        return {}
    payload: dict[str, Any] = {}

    degraded = record.get(DECISION_DEGRADED_FIELD)
    if degraded is not None:
        payload["degraded"] = bool(degraded)

    treatment_lost = record.get(DECISION_TREATMENT_LOST_FIELD)
    if treatment_lost is not None:
        payload["treatment_lost"] = bool(treatment_lost)

    mode = record.get(DECISION_SELECTION_MODE_FIELD)
    if mode:
        payload["selection_mode"] = str(mode)

    report = _decision_report_of(record)
    for key in ("filters", "llm", "warnings"):
        if key in report:
            payload[key] = report[key]

    return payload


def job_decision_verdict(record: dict[str, Any] | None) -> LambdaVerdict:
    """Read the persisted verdict off an API job record.

    Returns the same :class:`LambdaVerdict` a raw payload would, so callers do
    not care which source the verdict came from. ``verdict.unknown`` is ``True``
    for every job the columns say nothing about.
    """
    return lambda_verdict(job_decision_payload(record))


def job_decision_reported(record: dict[str, Any] | None) -> bool:
    """Did this job record carry a decision verdict at all?

    ``False`` is the answer for a legacy job, a non-lambda job, and any job
    written by a runner that predates the columns. All three are **unknown**, and
    a renderer must say so rather than showing them clean.
    """
    return lambda_report_present(job_decision_payload(record))


def job_decision_degraded(record: dict[str, Any] | None) -> bool:
    """Did a *requested* filter fail on this job's decision? "Look at this."

    ``False`` also means "no verdict recorded" -- a boolean cannot carry three
    states. Pair it with :func:`job_decision_reported`, or use
    :func:`job_decision_verdict`, whenever clean and unknown must be told apart.
    """
    return lambda_is_degraded(job_decision_payload(record))


def job_decision_treatment_lost(record: dict[str, Any] | None) -> bool:
    """Did this job's bot produce control-arm data despite being treated?

    The one alarm condition, read straight from the column the runner copied it
    into. Never derived here from ``decision_selection_mode``.
    """
    return lambda_treatment_lost(job_decision_payload(record))
