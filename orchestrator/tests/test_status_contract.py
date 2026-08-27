"""The amended decision-function contract, pinned.

This file exists because the *first* version of this contract was wrong in a way
that every layer inherited, and it was wrong in the direction that destroys an
alerting system: it treated ``selection_mode == "random"`` as proof that a
treatment arm had silently become a control arm.

It is not. A genuine control crawler (``pi = psi = kappa = tau = 0``) requests no
LLM filter, so it selects at random and reports five of its six filters as
``skipped`` on every perfectly healthy invocation. Measured on the production box
on 2026-08-27: 295 of 358 real lambda payloads (82%) carry no rating column at
all, and 236 of the 243 bot configurations in the live database are control-arm.
Gating on ``selection_mode`` would have marked ~82% of all ``lambda_select`` jobs
as ``warning`` and pinned a ``severity=critical`` alert permanently on.

So the rules these tests enforce, at every layer that reads a decision:

* ``degraded``       -> "a human should look at this decision"
* ``treatment_lost`` -> "this session's data is scientifically void" (alert here)
* ``selection_mode`` -> a neutral description. Never an alarm on its own.
* ``"skipped"``      -> not requested. Never a failure.
* no report at all   -> unknown. Never degraded.
"""

import logging

import pytest

from orchestrator import status
from orchestrator.debug.job_debugger import JobDebugger, JobDebugReport, JobMetadata
from orchestrator.lambda_service import server

# ---------------------------------------------------------------------------
# Payloads. These mirror what the decision function actually emits.
# ---------------------------------------------------------------------------

#: What 82% of this box's traffic looks like: a control bot, running perfectly.
#: Nothing was requested, so nothing was applied, so the pick was random.
CONTROL_ARM = {
    "xpath": "//a[@id='video-title']",
    "selected": {"title": "Climate explained", "id": "vid123"},
    "degraded": False,
    "treatment_lost": False,
    "selection_mode": "random",
    "filters": {
        "already_watched": "ok",
        "mainstream_media": "skipped",
        "children_entertainment": "skipped",
        "similar": "skipped",
        "political": "skipped",
        "fringe": "skipped",
    },
    "llm": {"available": True, "error_type": None, "error": None},
    "warnings": [],
}

#: A treated bot whose only requested treatment failed: it produced control-arm
#: data. This is the one condition worth waking someone for.
TREATED_AND_LOST = {
    "xpath": "//a[@id='video-title']",
    "selected": {"title": "Some video", "id": "vid999"},
    "degraded": True,
    "treatment_lost": True,
    "selection_mode": "random",
    "filters": {
        "already_watched": "ok",
        "mainstream_media": "failed: KeyError: 'channel'",
        "children_entertainment": "skipped",
        "similar": "skipped",
        "political": "skipped",
        "fringe": "skipped",
    },
    "llm": {"available": False, "error_type": "authentication", "error": "bad api key"},
    "warnings": ["Aversion to mainstream media channels ignored"],
}

#: A treated bot that lost one of several treatments: degraded, but the LLM
#: still rated the recommendations, so the arm is not a control arm.
TREATED_AND_DEGRADED = {
    "xpath": "//a[@id='video-title']",
    "selected": {"title": "Some video", "id": "vid888"},
    "degraded": True,
    "treatment_lost": False,
    "selection_mode": "llm",
    "filters": {
        "already_watched": "ok",
        "mainstream_media": "failed: OSError: csv gone",
        "children_entertainment": "skipped",
        "similar": "skipped",
        "political": "ok",
        "fringe": "skipped",
    },
    "llm": {"available": True, "error_type": None, "error": None},
    "warnings": ["Aversion to mainstream media channels ignored"],
}

#: What the decision function *currently deployed* returns: no report at all.
#: The HAR archive is full of these, and most of them are control-arm.
LEGACY_CONTROL = {
    "xpath": "//a[@id='video-title']",
    "selected": {"title": "Climate explained", "id": "vid123"},
    "warnings": [],
}


class TestControlArmIsNotAnAlarm:
    """The regression that made this amendment necessary."""

    def test_a_healthy_control_crawler_is_not_degraded(self):
        assert status.lambda_is_degraded(CONTROL_ARM) is False

    def test_a_healthy_control_crawler_has_not_lost_its_treatment(self):
        assert status.lambda_treatment_lost(CONTROL_ARM) is False

    def test_random_selection_is_reported_neutrally(self):
        assert status.lambda_selection_mode(CONTROL_ARM) == "random"

    def test_the_verdict_calls_it_ok(self):
        verdict = status.lambda_verdict(CONTROL_ARM)
        assert verdict.reported is True
        assert verdict.label() == "ok"
        assert verdict.needs_attention is False

    def test_no_filter_is_reported_as_a_casualty(self):
        assert status.lambda_failed_filters(CONTROL_ARM) == {}
        assert status.lambda_skipped_filters(CONTROL_ARM) == [
            "mainstream_media",
            "children_entertainment",
            "similar",
            "political",
            "fringe",
        ]

    def test_the_job_debugger_reports_it_as_a_success(self):
        decision = JobDebugger.decision_from_payload(CONTROL_ARM)
        assert decision.success is True
        assert decision.degraded is False
        assert decision.treatment_lost is False
        assert decision.failed_filters == {}
        # descriptive, and deliberately still available -- just not a verdict
        assert decision.random_selection is True

    def test_the_job_debugger_finds_nothing_to_report(self):
        report = JobDebugReport(
            job_id=1,
            metadata=JobMetadata(job_id=1, status="success"),
            lambda_decision=JobDebugger.decision_from_payload(CONTROL_ARM),
        )
        JobDebugger._generate_error_summary(object.__new__(JobDebugger), report)

        assert report.has_errors is False
        assert report.has_warnings is False, report.warning_summary
        assert report.treatment_lost is False


class TestTreatmentLostIsTheAlarm:
    def test_it_is_degraded_and_lost(self):
        assert status.lambda_is_degraded(TREATED_AND_LOST) is True
        assert status.lambda_treatment_lost(TREATED_AND_LOST) is True

    def test_the_verdict_says_so(self):
        verdict = status.lambda_verdict(TREATED_AND_LOST)
        assert verdict.label() == "treatment-lost"
        assert verdict.needs_attention is True
        assert set(verdict.failed_filters) == {"mainstream_media"}

    def test_a_degraded_arm_that_kept_its_llm_rating_is_not_lost(self):
        """Worth a look, not worth an alert: the LLM still rated the videos."""
        assert status.lambda_is_degraded(TREATED_AND_DEGRADED) is True
        assert status.lambda_treatment_lost(TREATED_AND_DEGRADED) is False
        assert status.lambda_verdict(TREATED_AND_DEGRADED).label() == "degraded"

    def test_the_job_debugger_surfaces_it_first_and_in_words(self):
        report = JobDebugReport(
            job_id=1,
            metadata=JobMetadata(job_id=1, status="warning"),
            lambda_decision=JobDebugger.decision_from_payload(TREATED_AND_LOST),
        )
        JobDebugger._generate_error_summary(object.__new__(JobDebugger), report)

        assert report.treatment_lost is True
        assert report.has_warnings is True
        first_line = report.warning_summary.splitlines()[0]
        assert "TREATMENT LOST" in first_line, report.warning_summary
        # a lost treatment is not a failed job: it ran to completion
        assert report.has_errors is False


class TestLegacyPayloadsAreUnknownNotDegraded:
    def test_no_report_means_no_report(self):
        assert status.lambda_report_present(LEGACY_CONTROL) is False
        assert status.lambda_verdict(LEGACY_CONTROL).unknown is True
        assert status.lambda_verdict(LEGACY_CONTROL).label() == "no report"

    def test_absence_never_reads_as_degraded(self):
        assert status.lambda_is_degraded(LEGACY_CONTROL) is False
        assert status.lambda_treatment_lost(LEGACY_CONTROL) is False

    def test_a_legacy_random_selection_is_not_an_alarm(self):
        """The exact payload the removed fallback used to condemn."""
        legacy_random = {**LEGACY_CONTROL, "selection_mode": "random"}
        assert status.lambda_is_degraded(legacy_random) is False
        assert status.lambda_treatment_lost(legacy_random) is False

    def test_a_legacy_payloads_own_warnings_are_still_believed(self):
        """The function only warns when something it tried to do did not work."""
        legacy_warned = {**LEGACY_CONTROL, "warnings": ["Preference for similar videos ignored"]}
        assert status.lambda_is_degraded(legacy_warned) is True
        # but it still cannot tell whether the treatment survived
        assert status.lambda_treatment_lost(legacy_warned) is False

    def test_an_empty_or_missing_payload_is_not_an_alarm(self):
        for payload in (None, {}):
            assert status.lambda_is_degraded(payload) is False
            assert status.lambda_treatment_lost(payload) is False
            assert status.lambda_report_present(payload) is False


class TestSkippedIsNotAFailure:
    """``skipped`` means *not requested*. Five of six on every control job."""

    def test_skipped_is_never_a_failed_filter(self):
        payload = {"filters": {"political": "skipped", "similar": "ok"}}
        assert status.lambda_failed_filters(payload) == {}

    def test_only_failed_entries_count(self):
        payload = {
            "filters": {
                "political": "skipped",
                "similar": "ok",
                "fringe": "failed: TimeoutError: openai timed out",
            }
        }
        assert set(status.lambda_failed_filters(payload)) == {"fringe"}

    def test_filters_are_reported_in_the_contract_order(self):
        payload = {"filters": dict.fromkeys(reversed(status.LAMBDA_FILTER_NAMES), "ok")}
        assert list(status.lambda_filters(payload)) == list(status.LAMBDA_FILTER_NAMES)

    def test_the_job_debugger_does_not_count_skipped_filters(self):
        decision = JobDebugger.decision_from_payload(TREATED_AND_LOST)
        assert set(decision.failed_filters) == {"mainstream_media"}
        assert "children_entertainment" in decision.skipped_filters


class TestSelectionModeIsNeverAVerdictOnItsOwn:
    """Flip only ``selection_mode`` and nothing about the verdict may move."""

    @pytest.mark.parametrize("mode", ["llm", "random", "", "LLM", "Random"])
    def test_selection_mode_alone_moves_nothing(self, mode):
        payload = {**CONTROL_ARM, "selection_mode": mode}
        assert status.lambda_is_degraded(payload) is False
        assert status.lambda_treatment_lost(payload) is False

    def test_the_intermediate_contract_needs_both_terms(self):
        """Payloads with ``degraded`` but no ``treatment_lost`` yet."""
        assert status.lambda_treatment_lost({"degraded": True, "selection_mode": "random"}) is True
        assert status.lambda_treatment_lost({"degraded": True, "selection_mode": "llm"}) is False
        assert (
            status.lambda_treatment_lost({"degraded": False, "selection_mode": "random"}) is False
        )

    def test_the_explicit_field_always_wins(self):
        """The source computes it once so no consumer can get the gate wrong."""
        payload = {"degraded": True, "selection_mode": "random", "treatment_lost": False}
        assert status.lambda_treatment_lost(payload) is False


class TestTheRationaleStaysInTheModule:
    """A contract whose reasoning is deleted gets re-broken the same way."""

    def test_the_measurement_is_documented(self):
        doc = status.__doc__ or ""
        assert "295 of 358" in doc
        assert "selection_mode" in doc


class TestTheLambdaServerGatesTheSameWay:
    """The contract has to hold where the log lines are actually written."""

    @pytest.fixture
    def client(self, monkeypatch: pytest.MonkeyPatch):
        holder: dict = {"response": CONTROL_ARM}

        monkeypatch.setattr(server, "_select_recommendation", lambda payload: holder["response"])
        monkeypatch.setattr(server, "_missing_correlation_id_logged", False)
        server.app.config["TESTING"] = False
        return server.app.test_client(), holder

    @staticmethod
    def _at(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
        return [r.getMessage() for r in caplog.records if r.levelno == level]

    def test_a_control_crawler_produces_no_error_and_no_warning(
        self, client, caplog: pytest.LogCaptureFixture
    ):
        test_client, holder = client
        holder["response"] = CONTROL_ARM
        caplog.set_level(logging.DEBUG)

        test_client.post("/invoke", json={"job_id": "exp-1|cr-1|q-1|j-1"})

        assert self._at(caplog, logging.ERROR) == []
        assert self._at(caplog, logging.WARNING) == []

    def test_a_lost_treatment_produces_an_error(self, client, caplog: pytest.LogCaptureFixture):
        test_client, holder = client
        holder["response"] = TREATED_AND_LOST
        caplog.set_level(logging.DEBUG)

        test_client.post("/invoke", json={"job_id": "exp-1|cr-1|q-1|j-2"})

        assert any("TREATMENT LOST" in m for m in self._at(caplog, logging.ERROR))

    def test_a_degradation_produces_a_warning_but_no_error(
        self, client, caplog: pytest.LogCaptureFixture
    ):
        test_client, holder = client
        holder["response"] = TREATED_AND_DEGRADED
        caplog.set_level(logging.DEBUG)

        test_client.post("/invoke", json={"job_id": "exp-1|cr-1|q-1|j-3"})

        assert any("DEGRADED" in m for m in self._at(caplog, logging.WARNING))
        assert self._at(caplog, logging.ERROR) == []

    def test_a_legacy_payload_produces_neither(self, client, caplog: pytest.LogCaptureFixture):
        test_client, holder = client
        holder["response"] = LEGACY_CONTROL
        caplog.set_level(logging.DEBUG)

        test_client.post("/invoke", json={"job_id": "exp-1|cr-1|q-1|j-4"})

        assert self._at(caplog, logging.ERROR) == []
        assert self._at(caplog, logging.WARNING) == []


class TestTheDiagnosticIsLabelledByTheActualStatus:
    """A ``warning`` job's diagnostic is not an "Error", and not a second copy.

    ``JobMetadata.error_message`` is the runner's ``status_message`` promoted
    whenever the job went wrong, so a panel that renders both prints the same
    sentence twice -- the second time under the label "Error", which for a
    ``warning`` job is also the wrong word. These properties give a renderer the
    label and the duplicate check instead of making it guess.
    """

    @staticmethod
    def _meta(status: str, message: str) -> JobMetadata:
        return JobDebugger._metadata_from_payload(
            object.__new__(JobDebugger),
            1,
            {"id": 1, "status_code": status, "status_message": message},
        )

    def test_a_warning_jobs_diagnostic_is_not_called_an_error(self):
        meta = self._meta("warning", "job finished - jobdata not written")
        assert meta.diagnostic_label == "Degradation"
        assert meta.diagnostic == "job finished - jobdata not written"

    def test_a_failed_jobs_diagnostic_is_called_an_error(self):
        meta = self._meta("failure", "job failed - ElementNotInteractableException")
        assert meta.diagnostic_label == "Error"

    def test_the_promoted_diagnostic_is_flagged_as_a_duplicate(self):
        """It is verbatim the Status Message row, so a panel must not repeat it."""
        meta = self._meta("warning", "job finished - jobdata not written")
        assert meta.diagnostic_is_duplicate is True

    def test_a_distinct_api_error_is_not_a_duplicate(self):
        meta = JobDebugger._metadata_from_payload(
            object.__new__(JobDebugger),
            1,
            {
                "id": 1,
                "status_code": "failure",
                "status_message": "job failed - see error",
                "error_message": "SessionNotCreatedException: profile 42 refused",
            },
        )
        assert meta.diagnostic_is_duplicate is False
        assert meta.diagnostic_label == "Error"

    def test_a_green_job_has_no_diagnostic_to_label(self):
        meta = self._meta("success", "successfully finished job")
        assert meta.error_message is None
        assert meta.diagnostic_label == "Detail"
        assert meta.diagnostic_is_duplicate is False
