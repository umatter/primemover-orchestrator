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


# ---------------------------------------------------------------------------
# The verdict, persisted on the job row (2026-09-07)
# ---------------------------------------------------------------------------
# The API now stores the decision verdict in four nullable columns on ``jobs``,
# so a consumer can ask "did the treatment apply?" without fetching a MinIO
# object. The rules do not change; the source does. These tests pin that the
# columns are read through the *same* helpers as the payload, and that NULL
# keeps meaning unknown -- which is the property the staged deploy depends on.

#: A treated bot's row after the runner wrote its verdict: the alarm state.
ROW_TREATMENT_LOST = {
    "id": 1,
    "status_code": "warning",
    "status_message": "job finished - treatment lost",
    "decision_degraded": True,
    "decision_treatment_lost": True,
    "decision_selection_mode": "random",
    "decision_report": {
        "filters": TREATED_AND_LOST["filters"],
        "llm": TREATED_AND_LOST["llm"],
        "warnings": TREATED_AND_LOST["warnings"],
        "requested": ["mainstream_media"],
        "applied": [],
    },
}

#: The common case on this box: a control bot, picking at random, perfectly fine.
ROW_CONTROL_ARM = {
    "id": 2,
    "status_code": "success",
    "decision_degraded": False,
    "decision_treatment_lost": False,
    "decision_selection_mode": "random",
    "decision_report": {
        "filters": CONTROL_ARM["filters"],
        "llm": CONTROL_ARM["llm"],
        "warnings": [],
        "requested": [],
        "applied": [],
    },
}

#: A legacy job, a non-lambda job, or a job written by a runner that predates
#: the columns. All three look like this, and all three mean "we cannot tell".
ROW_NO_VERDICT = {"id": 3, "status_code": "success", "status_message": "successfully finished job"}


class TestAbsentVerdictIsUnknownNotClean:
    """The property that makes API -> GUI -> runner a safe deploy order.

    Every intermediate state of that rollout writes nulls. If a null read as
    "clean" the migration itself would silently certify thousands of jobs as
    having applied treatments nobody measured.
    """

    def test_a_row_with_no_decision_columns_is_not_reported(self):
        assert status.job_decision_reported(ROW_NO_VERDICT) is False
        assert status.job_decision_verdict(ROW_NO_VERDICT).unknown is True

    def test_an_unreported_verdict_is_not_labelled_ok(self):
        assert status.job_decision_verdict(ROW_NO_VERDICT).label() == "no report"

    def test_explicit_nulls_read_the_same_as_missing_keys(self):
        row = {
            "id": 4,
            "decision_degraded": None,
            "decision_treatment_lost": None,
            "decision_selection_mode": None,
            "decision_report": None,
        }
        assert status.job_decision_payload(row) == {}
        assert status.job_decision_reported(row) is False

    def test_a_mode_on_its_own_is_still_unknown(self):
        """``selection_mode`` is a descriptor, so it cannot make a verdict present.

        A row carrying only ``"random"`` says how the pick was made and nothing
        at all about whether anything was requested -- the exact confusion the
        contract was amended to remove.
        """
        row = {"id": 5, "decision_selection_mode": "random"}
        assert status.job_decision_reported(row) is False
        assert status.job_decision_treatment_lost(row) is False

    def test_no_record_at_all_is_unknown_not_an_alarm(self):
        assert status.job_decision_reported(None) is False
        assert status.job_decision_degraded(None) is False
        assert status.job_decision_treatment_lost(None) is False


class TestAControlArmRowIsNotAFinding:
    """82% of this box's traffic. If this goes amber, the change is worthless."""

    def test_a_control_row_is_reported_and_clean(self):
        verdict = status.job_decision_verdict(ROW_CONTROL_ARM)
        assert verdict.reported is True
        assert verdict.unknown is False
        assert verdict.degraded is False
        assert verdict.treatment_lost is False
        assert verdict.label() == "ok"

    def test_random_plus_not_degraded_is_not_bad(self):
        assert status.job_decision_degraded(ROW_CONTROL_ARM) is False
        assert status.job_decision_treatment_lost(ROW_CONTROL_ARM) is False
        assert status.job_decision_verdict(ROW_CONTROL_ARM).needs_attention is False

    def test_its_five_skipped_filters_are_not_casualties(self):
        verdict = status.job_decision_verdict(ROW_CONTROL_ARM)
        assert verdict.failed_filters == {}
        assert len(verdict.skipped_filters) == 5


class TestTreatmentLostOnTheRowIsTheAlarm:
    def test_the_row_reports_the_lost_treatment(self):
        verdict = status.job_decision_verdict(ROW_TREATMENT_LOST)
        assert verdict.treatment_lost is True
        assert verdict.degraded is True
        assert verdict.needs_attention is True
        assert verdict.label() == "treatment-lost"

    def test_the_helpers_agree_with_the_verdict(self):
        assert status.job_decision_treatment_lost(ROW_TREATMENT_LOST) is True
        assert status.job_decision_degraded(ROW_TREATMENT_LOST) is True

    def test_the_failed_filter_is_named(self):
        assert "mainstream_media" in status.job_decision_verdict(ROW_TREATMENT_LOST).failed_filters

    def test_the_row_and_the_payload_give_the_same_verdict(self):
        """One implementation of the rules, two sources; they cannot diverge."""
        from_row = status.job_decision_verdict(ROW_TREATMENT_LOST)
        from_payload = status.lambda_verdict(TREATED_AND_LOST)
        assert from_row.treatment_lost == from_payload.treatment_lost
        assert from_row.degraded == from_payload.degraded
        assert from_row.failed_filters == from_payload.failed_filters


class TestTheReportColumnSurvivesItsTransport:
    def test_a_json_encoded_report_is_parsed(self):
        """MySQL/CSV hand it back as a string; Laravel's cast hands back a dict."""
        import json as _json

        row = dict(ROW_TREATMENT_LOST)
        row["decision_report"] = _json.dumps(row["decision_report"])
        assert status.job_decision_verdict(row).failed_filters == {
            "mainstream_media": "failed: KeyError: 'channel'"
        }

    def test_an_unparseable_report_does_not_raise(self):
        row = dict(ROW_TREATMENT_LOST)
        row["decision_report"] = "{not json"
        verdict = status.job_decision_verdict(row)
        assert verdict.treatment_lost is True
        assert verdict.filters == {}

    def test_a_report_of_the_wrong_type_is_ignored(self):
        row = dict(ROW_CONTROL_ARM)
        row["decision_report"] = ["filters"]
        assert status.job_decision_verdict(row).filters == {}

    def test_the_column_names_are_the_cross_repo_contract(self):
        assert status.DECISION_FIELDS == (
            "decision_degraded",
            "decision_treatment_lost",
            "decision_selection_mode",
            "decision_report",
        )


class TestTheSortRankKeepsUnknownOutOfTheCleanEnd:
    """``sortBy=severity``: unknown sits between ``running`` and ``warning``."""

    def test_the_documented_rank(self):
        assert status.SEVERITY_SORT_RANK == {
            "success": 0,
            "skipped": 1,
            "running": 2,
            "unknown": 3,
            "warning": 4,
            "failure": 5,
        }

    def test_unknown_ranks_between_running_and_warning(self):
        assert (
            status.severity_rank("running")
            < status.severity_rank("unknown")
            < status.severity_rank("warning")
        )

    def test_an_unrecognised_code_sorts_as_unknown_never_as_success(self):
        for value in ("", None, "pending", "cancelled", "sucess"):
            assert status.severity_rank(value) == status.UNKNOWN_SEVERITY_SORT_RANK
            assert status.severity_rank(value) > status.severity_rank("success")

    def test_legacy_spellings_sort_with_their_canonical_form(self):
        """The reason this exists: raw-string sorting splits 256 rows off 1607."""
        assert status.severity_rank("failed") == status.severity_rank("failure")
        assert status.severity_rank("skpped") == status.severity_rank("skipped")
        assert status.severity_rank("warn") == status.severity_rank("warning")

    def test_worst_first_ordering(self):
        codes = ["success", "failed", "running", "skipped", "mystery", "warning"]
        assert sorted(codes, key=status.severity_rank, reverse=True) == [
            "failed",
            "warning",
            "mystery",
            "running",
            "skipped",
            "success",
        ]

    def test_the_inheritance_table_is_left_alone(self):
        """SEVERITY answers a different question and other code pins its numbers."""
        assert "unknown" not in status.SEVERITY
        assert status.SEVERITY == {
            "success": 0,
            "skipped": 1,
            "running": 2,
            "warning": 3,
            "failure": 4,
        }
