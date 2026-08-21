"""Reading a job's status and duration the way the API actually reports them.

Shared by the e2e runner and reporter because they disagreed. The runner read
``status_code`` and treated ``success`` as a pass; the reporter read ``status``
and compared against ``completed``. Since the API sends neither of those to the
reporter's satisfaction, every row of the summary table rendered as a red
``unknown`` even on a fully passing run -- which is worse than no table, because
it reads as "the test passed but every job is in an unknown state".

The shape, taken from a live ``GET /api/v1/jobs/{id}``:

    status_code   "success"                        (there is no `status` field)
    started_at    "2026-08-21T09:58:32.994+0200"   (offset WITHOUT a colon)
    finished_at   "2026-08-21T09:58:47.922+0200"
    created_at    "2026-08-21T09:54:52+02:00"      (offset WITH a colon)

There is no ``duration`` field at all, which is why the reporter's
``job.get("duration")`` was always falsy and every duration rendered as "-".
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# "success" is what this API returns; the other two are accepted because the
# runner has always accepted them and something upstream may still send them.
JOB_SUCCESS_STATUSES = ("completed", "done", "success")

# Matches a trailing +HHMM/-HHMM offset so it can be rewritten as +HH:MM.
# datetime.fromisoformat only learned to parse the colon-less form in 3.11, and
# this project supports 3.10 -- so `started_at` would raise there while working
# fine on a developer's newer interpreter.
_OFFSET_WITHOUT_COLON = re.compile(r"([+-]\d{2})(\d{2})$")


def job_status(job: dict[str, Any]) -> str:
    """The job's status, or ``"unknown"`` if the payload carries neither field."""
    return job.get("status_code") or job.get("status") or "unknown"


def job_succeeded(job: dict[str, Any]) -> bool:
    """Whether the job finished successfully, matched case-insensitively."""
    return job_status(job).lower() in JOB_SUCCESS_STATUSES


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

    started = _parse_timestamp(job.get("started_at"))
    finished = _parse_timestamp(job.get("finished_at"))
    if started is None or finished is None:
        return None

    seconds = (finished - started).total_seconds()
    # Clocks can disagree, and a negative duration would render as nonsense.
    return seconds if seconds >= 0 else None
