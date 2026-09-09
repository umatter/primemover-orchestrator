"""Saying WHICH degradation a `warning` job represents.

`warning` is one status covering outcomes that are not remotely equivalent: a
void treatment arm and a page that needed one reload both land there. The e2e
reporter used to describe every one of them as "a treatment may have silently
become a control", which was the best available claim before the verdict was
persisted on the job row and is now demonstrably wrong most of the time.

Observed on 2026-09-09, experiment 176: a lambdasingleselectjob came back
`warning` with "watch page had no recommendation container; recovered by
reloading once" and a verdict of degraded=0, treatment_lost=0, mode=llm. The
treatment applied. The run was right to pass, and the reporter was wrong to
imply the arm might have been lost.
"""

from __future__ import annotations

import json

from orchestrator.e2e.job_status import job_degradation_reason

FILTERS_OK = {"mainstream_media": "ok", "similar": "ok", "already_watched": "not_implemented"}


def a_job(*, degraded=None, lost=None, filters=None, mode="llm") -> dict:
    job: dict = {"id": 1, "type": "lambdasingleselectjob", "status_code": "warning"}
    if degraded is not None:
        job["decision_degraded"] = degraded
    if lost is not None:
        job["decision_treatment_lost"] = lost
    if mode is not None:
        job["decision_selection_mode"] = mode
    if filters is not None:
        job["decision_report"] = json.dumps({"filters": filters})
    return job


class TestTheFourAnswers:
    def test_a_lost_treatment_says_the_session_is_void(self):
        reason = job_degradation_reason(
            a_job(degraded=True, lost=True, filters={"mainstream_media": "failed: boom"})
        )

        assert "treatment lost" in reason
        assert "control-arm data" in reason

    def test_a_degraded_decision_says_a_filter_did_not_apply(self):
        reason = job_degradation_reason(
            a_job(degraded=True, lost=False, filters={"mainstream_media": "failed: boom"})
        )

        assert "requested filter did not apply" in reason
        assert "treatment lost" not in reason

    def test_a_clean_verdict_points_away_from_the_decision(self):
        # The experiment-176 case: the runner warned, the decision was fine.
        reason = job_degradation_reason(a_job(degraded=False, lost=False, filters=FILTERS_OK))

        assert "decision itself was clean" in reason
        assert "treatment lost" not in reason
        assert "did not apply" not in reason

    def test_no_verdict_is_unknown_and_says_so(self):
        # A non-lambda job, a pre-migration row, an un-redeployed runner. All
        # three are "cannot tell", and none of them is clean.
        reason = job_degradation_reason({"id": 2, "type": "waitjob", "status_code": "warning"})

        assert "cannot tell" in reason
        assert "clean" not in reason


class TestItNeverGuessesFromSelectionMode:
    def test_a_random_mode_with_a_clean_verdict_is_not_called_a_lost_treatment(self):
        # random is the CORRECT mode for a control crawler, which is most of
        # them. Reading it as degradation is the alarm that would fire forever.
        reason = job_degradation_reason(
            a_job(degraded=False, lost=False, filters=FILTERS_OK, mode="random")
        )

        assert "decision itself was clean" in reason

    def test_a_mode_only_row_is_unknown_not_clean(self):
        # selection_mode alone is a descriptor and cannot make a payload
        # "reported", so this must fall through to the unknown answer.
        reason = job_degradation_reason(
            {"id": 3, "type": "lambdasingleselectjob", "decision_selection_mode": "llm"}
        )

        assert "cannot tell" in reason
