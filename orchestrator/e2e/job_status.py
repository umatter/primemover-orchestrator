"""Reading a job's status and duration the way the API actually reports them.

Shared by the e2e runner and reporter because they disagreed. The runner read
``status_code`` and treated ``success`` as a pass; the reporter read ``status``
and compared against ``completed``. Since the API sends neither of those to the
reporter's satisfaction, every row of the summary table rendered as a red
``unknown`` even on a fully passing run -- which is worse than no table, because
it reads as "the test passed but every job is in an unknown state".

The vocabulary itself now lives in :mod:`orchestrator.status`; this module is
the thin e2e-facing view of it, so that ``skpped``/``failed``/``completed`` are
folded onto the canonical codes in exactly one place.

The shape, taken from a live ``GET /api/v1/jobs/{id}``:

    status_code    "success"                        (there is no `status` field)
    status_message "successfully finished job"      (the runner's diagnostic)
    started_at     "2026-08-21T09:58:32.994+0200"   (offset WITHOUT a colon)
    finished_at    "2026-08-21T09:58:47.922+0200"
    created_at     "2026-08-21T09:54:52+02:00"      (offset WITH a colon)

There is no ``duration`` field at all, which is why the reporter's
``job.get("duration")`` was always falsy and every duration rendered as "-".
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from orchestrator import status as status_contract

# Retained for callers that predate orchestrator.status. The canonical mapping
# is STATUS_ALIASES there; this tuple is only the historical spelling list.
JOB_SUCCESS_STATUSES = ("completed", "done", "success")

# Matches a trailing +HHMM/-HHMM offset so it can be rewritten as +HH:MM.
# datetime.fromisoformat only learned to parse the colon-less form in 3.11, and
# this project supports 3.10 -- so `started_at` would raise there while working
# fine on a developer's newer interpreter.
_OFFSET_WITHOUT_COLON = re.compile(r"([+-]\d{2})(\d{2})$")


def job_status(job: dict[str, Any]) -> str:
    """The job's canonical status, or ``"unknown"`` if it carries neither field.

    Legacy spellings are folded onto the canonical vocabulary, so a row written
    as ``skpped`` reads back as ``skipped`` and one written as ``failed`` reads
    back as ``failure``.
    """
    return status_contract.status_of(job) or "unknown"


def job_status_message(job: dict[str, Any]) -> str:
    """The runner's diagnostic for this job, or ``""``.

    Since 2026-08-27 ``status_message`` may carry a real cause after the state
    text (``"<state text> - <detail>"``), so it is the field worth showing next
    to a failure or a warning.
    """
    return status_contract.message_of(job)


def job_succeeded(job: dict[str, Any]) -> bool:
    """Whether the job finished cleanly. A ``warning`` is deliberately not one."""
    return status_contract.is_success(job_status(job))


def job_degraded(job: dict[str, Any]) -> bool:
    """Whether the runner marked the job ``warning``: it ran to completion, and
    something the experiment cares about did not happen. Terminal, not a failure.

    This is a *status* predicate and says nothing about which of the possible
    somethings it was -- job data unwritten, a filter that would not apply, or a
    page that had to be reloaded. :func:`job_degradation_reason` reads the
    persisted verdict to tell those apart.

    The docstring used to offer "a RANDOM instead of LLM-driven video selection"
    as an example, which is exactly the reading the status contract forbids:
    ``selection_mode`` is a descriptor, and ``random`` is the CORRECT answer for
    a control crawler, which is most of them.
    """
    return status_contract.is_warning(job_status(job))


def job_degradation_reason(job: dict[str, Any]) -> str:
    """Why a ``warning`` job is flagged, read off its persisted decision verdict.

    A ``warning`` says only "look at this". It is worth saying WHICH thing,
    because the same status covers outcomes that are not remotely equivalent --
    a void treatment arm and a page that needed one reload both land here -- and
    a reporter that describes every one of them as a possibly-lost treatment
    cries wolf on the majority while under-stating the minority that matters.

    Four answers, and the fourth is not the third:

    * treatment lost -- the bot was configured to be treated and produced
      control-arm data. The session is void.
    * decision degraded -- a requested filter did not apply, but something else
      requested did, so there is partial treatment signal.
    * decision clean -- the verdict says the decision was fine, so the warning
      came from somewhere else in the job (job data, a page reload).
    * no verdict recorded -- a non-lambda job, a pre-migration row, or an
      un-redeployed runner. **Unknown, never clean.**
    """
    if status_contract.job_decision_treatment_lost(job):
        return "treatment lost — the bot was treated and produced control-arm data"

    if status_contract.job_decision_degraded(job):
        return "a requested filter did not apply"

    if status_contract.job_decision_reported(job):
        return "the decision itself was clean; the warning came from elsewhere in the job"

    return "no decision verdict recorded — cannot tell whether a treatment applied"


def job_skipped(job: dict[str, Any]) -> bool:
    """Whether the runner skipped the job (includes the legacy ``skpped``)."""
    return status_contract.is_skipped(job_status(job))


def job_finished(job: dict[str, Any]) -> bool:
    """Whether the job reached a terminal state, degraded or not.

    Used for progress counting: a warning job is done and must not leave the
    "3/4 jobs completed" counter stuck one short forever.
    """
    return job_succeeded(job) or job_degraded(job)


def job_needs_attention(job: dict[str, Any]) -> bool:
    """Whether a human should look at this job: a failure *or* a warning."""
    return status_contract.is_bad(job_status(job))


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse one API timestamp, tolerating both offset spellings.

    Returns None rather than raising: a summary table is not worth failing a
    green e2e run over.
    """
    if not isinstance(value, str) or not value:
        return None

    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = _OFFSET_WITHOUT_COLON.sub(r"\1:\2", text)

    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def job_duration_seconds(job: dict[str, Any]) -> float | None:
    """Seconds between ``started_at`` and ``finished_at``, or None.

    Prefers an explicit ``duration`` if one ever appears, so this keeps working
    if the API grows the field later.
    """
    explicit = job.get("duration")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return float(explicit)

    # ``started_at`` strictly, not status.started_at_of(): that helper also
    # accepts ``start_at`` (the *scheduled* start) and ``created_at``, either of
    # which would silently fold the wait for runner pickup into the duration.
    # ``finished_at_of`` is safe -- every name it accepts means "finished".
    started = _parse_timestamp(job.get("started_at"))
    finished = _parse_timestamp(status_contract.finished_at_of(job))
    if started is None or finished is None:
        return None

    seconds = (finished - started).total_seconds()
    # Clocks can disagree, and a negative duration would render as nonsense.
    return seconds if seconds >= 0 else None
