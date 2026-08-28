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
requested*, not *failed*: five of the six filters are ``skipped`` on every
healthy control invocation. :func:`lambda_failed_filters` is the only correct way
to count casualties.

Finally, a payload carrying **no** degradation report is a legacy payload from
the decision function currently deployed, and the HAR archive is full of them.
Absence reads as *unknown* -- never as degraded. :func:`lambda_report_present`
and :class:`LambdaVerdict` keep "clean" and "we cannot tell" apart.
"""

from __future__ import annotations

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

#: Filter states that are *not* failures. ``"skipped"`` means the filter was
#: never requested -- the normal state of five of the six filters on a control
#: crawler -- and counting it as a failure is the single most common way to turn
#: this contract back into a false-alarm generator.
_FILTER_OK_STATES = frozenset({"ok", "skipped", ""})


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
        """Only filters that actually failed. ``"skipped"`` is not a failure."""
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

    The wire values are ``"ok"``, ``"skipped"`` and ``"failed: <ExcClass>:
    <msg>"``. Anything that is not ``ok``/``skipped``/empty is treated as a
    failure so an unrecognised future state errs towards being seen.
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
    not "no". Payloads written by the decision function currently deployed to
    production have no report at all, and there are years of them in the HAR
    archive.
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
