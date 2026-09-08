"""Tests for the treated-arm e2e check.

The check exists because every other signal in an e2e run answers a different
question. The decision function degrades by *omission*: a filter it cannot
apply is dropped, the remaining recommendations are still plausible, the HTTP
call is still 200 and the job still succeeds. So a bot configured to be treated
can produce control-arm data through a completely green run -- which is exactly
how the mainstream-media arm ran as a second control arm on this VM for as long
as the lambda's cwd was wrong.

The fixtures are the real shapes: FILTERS_TREATED is what the decision function
returns for pi=1, kappa=1, and FILTERS_CONTROL is copied from a passing
--youtube run on 2026-09-08, where five of six filters are 'skipped' and
already_watched is the deployed stub's 'not_implemented'.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.e2e.runner import TEST_TYPES, _treatment_failures

EXPECTED_MODE = "llm"
EXPECTED_FILTERS = ["mainstream_media", "similar"]

FILTERS_TREATED = {
    "fringe": "skipped",
    "similar": "ok",
    "political": "skipped",
    "already_watched": "not_implemented",
    "mainstream_media": "ok",
    "children_entertainment": "ok",
}

# Verbatim from job 34286 of the control run on 2026-09-08.
FILTERS_CONTROL = {
    "fringe": "skipped",
    "similar": "skipped",
    "political": "skipped",
    "already_watched": "not_implemented",
    "mainstream_media": "skipped",
    "children_entertainment": "ok",
}


def a_job(
    *,
    filters: dict[str, str] | None,
    mode: str | None = "llm",
    degraded: bool | None = False,
    lost: bool | None = False,
    job_id: int = 1,
) -> dict:
    """One API job record, shaped the way GET /jobs/{id} returns it."""
    job: dict = {"id": job_id, "type": "lambdasingleselectjob", "status_code": "success"}
    if degraded is not None:
        job["decision_degraded"] = degraded
    if lost is not None:
        job["decision_treatment_lost"] = lost
    if mode is not None:
        job["decision_selection_mode"] = mode
    if filters is not None:
        job["decision_report"] = json.dumps({"filters": filters, "llm": {"available": True}})
    return job


class TestTreatmentApplied:
    def test_a_treated_run_passes(self):
        jobs = [a_job(filters=FILTERS_TREATED, job_id=i) for i in range(3)]

        assert _treatment_failures(jobs, EXPECTED_MODE, EXPECTED_FILTERS) == []

    def test_non_lambda_jobs_alongside_it_are_ignored(self):
        # A YouTube queue is 20 jobs, 3 of which call the lambda. The other 17
        # carry no verdict and must not be read as a missing one.
        jobs = [
            {"id": 90, "type": "waitjob", "status_code": "success"},
            {"id": 91, "type": "visitjob", "status_code": "success"},
            a_job(filters=FILTERS_TREATED, job_id=92),
        ]

        assert _treatment_failures(jobs, EXPECTED_MODE, EXPECTED_FILTERS) == []


class TestSilentDegradation:
    """The failures this check was built to catch -- every one of them green."""

    def test_a_control_run_fails_the_treated_check(self):
        # The whole point: this is a PASSING --youtube run. Its jobs succeeded,
        # its queue succeeded, and it was never treated.
        jobs = [a_job(filters=FILTERS_CONTROL, mode="random")]

        problems = _treatment_failures(jobs, EXPECTED_MODE, EXPECTED_FILTERS)

        assert any("selection_mode is 'random'" in p for p in problems)
        assert any("'mainstream_media' is 'skipped'" in p for p in problems)
        assert any("never requested" in p for p in problems)

    def test_a_corrupted_csv_looks_like_this(self):
        # mainstream_media_channels.csv mangled, or the lambda's cwd wrong: the
        # filter reports failed, the job still succeeds.
        filters = dict(FILTERS_TREATED, mainstream_media="failed: [Errno 2] No such file")
        jobs = [a_job(filters=filters, degraded=True)]

        problems = _treatment_failures(jobs, EXPECTED_MODE, EXPECTED_FILTERS)

        assert any("'mainstream_media' is 'failed" in p for p in problems)
        assert any("degraded" in p for p in problems)

    def test_treatment_lost_is_reported_as_void_not_merely_degraded(self):
        filters = dict(FILTERS_TREATED, mainstream_media="failed: boom", similar="failed: boom")
        jobs = [a_job(filters=filters, degraded=True, lost=True)]

        problems = _treatment_failures(jobs, EXPECTED_MODE, EXPECTED_FILTERS)

        assert any("void" in p for p in problems)

    def test_a_rating_that_never_happened_fails_even_with_filters_ok(self):
        # Both filters applied, but nothing weighted the draw -- the treated bot
        # picked its video exactly the way a control would.
        jobs = [a_job(filters=FILTERS_TREATED, mode="random")]

        problems = _treatment_failures(jobs, EXPECTED_MODE, EXPECTED_FILTERS)

        assert len(problems) == 1
        assert "drew the same way a control would" in problems[0]


class TestUnknownIsNotAPass:
    def test_a_run_with_no_verdict_at_all_fails(self):
        # An un-redeployed runner, or a lambda that predates the report. Nothing
        # was observed, and nothing observed must never read as healthy.
        jobs = [
            {"id": 1, "type": "lambdasingleselectjob", "status_code": "success"},
            {"id": 2, "type": "waitjob", "status_code": "success"},
        ]

        problems = _treatment_failures(jobs, EXPECTED_MODE, EXPECTED_FILTERS)

        assert len(problems) == 1
        assert "unknown is not a pass" in problems[0]

    def test_an_empty_run_fails_rather_than_vacuously_passing(self):
        assert _treatment_failures([], EXPECTED_MODE, EXPECTED_FILTERS) != []

    def test_a_mode_only_row_is_still_unknown(self):
        # selection_mode is a descriptor, not a verdict: a row carrying only a
        # mode cannot say whether anything degraded, so it is not "reported".
        jobs = [a_job(filters=None, mode="llm", degraded=None, lost=None)]

        problems = _treatment_failures(jobs, EXPECTED_MODE, EXPECTED_FILTERS)

        assert any("unknown is not a pass" in p for p in problems)


class TestWiring:
    def test_the_control_youtube_type_requests_no_treatment_check(self):
        # Otherwise every --youtube run would start failing on selection_mode,
        # which is 'random' precisely because that scenario is correct.
        assert "expect_filters_applied" not in TEST_TYPES["youtube"]

    @pytest.mark.parametrize("key", ["expect_selection_mode", "expect_filters_applied"])
    def test_the_treated_type_declares_what_it_expects(self, key):
        assert key in TEST_TYPES["youtube_treatment"]

    def test_the_treated_scenario_actually_asks_for_those_treatments(self):
        # The check is only meaningful if the uploaded scenario requests the
        # filters it verifies: pi -> mainstream_media, kappa -> similar.
        with open(TEST_TYPES["youtube_treatment"]["template"]) as handle:
            scenario = json.load(handle)

        params = scenario[0]["configuration"]["params"]

        assert params["pi"] == 1, "pi=0 would never request the mainstream_media filter"
        assert params["kappa"] == 1, "kappa=0 would leave selection_mode at 'random'"

    def test_the_control_scenario_stays_a_control(self):
        with open(TEST_TYPES["youtube"]["template"]) as handle:
            scenario = json.load(handle)

        params = scenario[0]["configuration"]["params"]

        assert [params[p] for p in ("pi", "psi", "kappa", "tau")] == [0, 0, 0, 0]
