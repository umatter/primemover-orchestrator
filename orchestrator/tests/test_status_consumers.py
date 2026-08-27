"""Regression tests for the orchestrator's status consumers.

Every test here pins a bug that shipped, and each one existed because the
consumer guessed at a field name or a vocabulary instead of reading the wire:

* ``debug/job_debugger.py`` read ``status`` and ``completed_at``; the API sends
  ``status_code`` and ``finished_at``. The status was therefore always "" and
  the duration always None, and the ``status in ("failed", "error")`` branch had
  never fired.
* ``tracing/trace_builder.py`` had the same ``status`` bug, then fell through to
  a log-text heuristic, so a failed job with no ERROR line in Loki rendered
  green.
* ``LambdaDecision.success`` meant "the output.json parsed", so a payload
  carrying ``{"error": ...}`` was reported as a successful decision.
* ``cli.py``'s ``debug job`` rich renderer -- the default output format --
  referenced four dataclass attributes that do not exist and raised
  AttributeError, while ``--format json`` worked.
* ``e2e/runner.py`` matched "skipped" exactly, so the runner's real "skpped"
  typo never matched and a legitimately skipped job failed the run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orchestrator import status as status_contract
from orchestrator.debug.har_parser import DomainStats, HARRequest, HARSummary
from orchestrator.debug.job_debugger import (
    Artifacts,
    CloudWatchInfo,
    JobDebugger,
    JobDebugReport,
    JobMetadata,
    LogEntry,
)
from orchestrator.tracing.trace_builder import SpanStatus, TraceBuilder

# Verbatim from a live GET /api/v1/jobs/1576, including the offset spelling.
REAL_JOB_PAYLOAD = {
    "id": 1576,
    "type": "lambdasingleselectjob",
    "queue_id": 633,
    "status_code": "success",
    "status_message": "successfully finished job",
    "started_at": "2026-08-21T09:58:32.994+0200",
    "finished_at": "2026-08-21T09:58:47.922+0200",
    "created_at": "2026-08-21T09:54:52+02:00",
}

# The decision-function response shape introduced 2026-08-27.
CLEAN_FILTERS = {
    "already_watched": "ok",
    "mainstream_media": "ok",
    "children_entertainment": "ok",
    "similar": "ok",
    "political": "ok",
    "fringe": "ok",
}

CLEAN_DECISION = {
    "xpath": "//a[@id='video-title']",
    "selected": {"title": "Climate explained", "id": "vid123"},
    "warnings": [],
    "degraded": False,
    "selection_mode": "llm",
    "filters": CLEAN_FILTERS,
    "llm": {"available": True, "error_type": None, "error": None},
}

RANDOM_DECISION = {
    **CLEAN_DECISION,
    "degraded": True,
    "selection_mode": "random",
    "warnings": ["Aversion to mainstream media channels ignored"],
    "filters": {
        **CLEAN_FILTERS,
        "mainstream_media": "failed: KeyError: 'channel'",
        "political": "skipped",
    },
    "llm": {"available": False, "error_type": "AuthenticationError", "error": "bad api key"},
}


def _debugger() -> JobDebugger:
    """A JobDebugger with no live settings behind it."""
    with patch("orchestrator.config.get_settings") as get_settings:
        get_settings.return_value = SimpleNamespace(
            pm_api_base_url="http://localhost:8090/api/v1",
            pm_api_email="test@test.com",
            pm_api_password="testpass",
            pm_runner_har_path="/tmp/does-not-exist",
        )
        return JobDebugger()


class TestJobMetadataReadsTheFieldsTheApiSends:
    def test_status_comes_from_status_code_not_status(self):
        meta = _debugger()._metadata_from_payload(1576, REAL_JOB_PAYLOAD)
        # `status` is not on the wire at all; reading it left this empty and
        # every status-based branch downstream dead.
        assert meta.status == "success"

    def test_duration_comes_from_finished_at_not_completed_at(self):
        meta = _debugger()._metadata_from_payload(1576, REAL_JOB_PAYLOAD)
        assert meta.completed_at is not None
        assert meta.duration_seconds == pytest.approx(14.928, abs=0.001)

    def test_status_message_is_surfaced(self):
        meta = _debugger()._metadata_from_payload(1576, REAL_JOB_PAYLOAD)
        assert meta.status_message == "successfully finished job"

    def test_a_green_jobs_status_message_is_not_promoted_to_an_error(self):
        meta = _debugger()._metadata_from_payload(1576, REAL_JOB_PAYLOAD)
        assert meta.error_message is None

    def test_a_failed_jobs_diagnostic_becomes_the_error_message(self):
        payload = {
            **REAL_JOB_PAYLOAD,
            "status_code": "failure",
            "status_message": "job failed - ElementNotInteractableException on #consent",
        }
        meta = _debugger()._metadata_from_payload(1576, payload)
        assert meta.status == "failure"
        assert meta.error_message is not None
        assert "ElementNotInteractableException" in meta.error_message
        assert meta.is_failure and meta.needs_attention

    def test_the_new_warning_state_needs_attention_without_being_a_failure(self):
        payload = {
            **REAL_JOB_PAYLOAD,
            "status_code": "warning",
            "status_message": "job finished - jobdata not written",
        }
        meta = _debugger()._metadata_from_payload(1576, payload)
        assert meta.status == "warning"
        assert meta.is_warning
        assert not meta.is_failure
        # is_bad, not is_failure, is what "should a human look at this?" means.
        assert meta.needs_attention

    def test_legacy_spellings_are_folded_onto_the_canonical_vocabulary(self):
        assert _debugger()._metadata_from_payload(1, {"status_code": "skpped"}).status == "skipped"
        assert _debugger()._metadata_from_payload(1, {"status_code": "failed"}).status == "failure"

    def test_an_unknown_status_stays_unknown_rather_than_becoming_success(self):
        meta = _debugger()._metadata_from_payload(1, {"status_code": "banana"})
        assert meta.status == "banana"
        assert not meta.is_failure and not meta.is_warning

    def test_negative_duration_is_dropped_rather_than_rendered(self):
        meta = _debugger()._metadata_from_payload(
            1,
            {
                "started_at": "2026-08-21T09:58:47+02:00",
                "finished_at": "2026-08-21T09:58:32+02:00",
            },
        )
        assert meta.duration_seconds is None

    def test_ids_and_names_are_taken_from_nested_relations_when_present(self):
        meta = _debugger()._metadata_from_payload(
            1576,
            {
                **REAL_JOB_PAYLOAD,
                "queue": {"id": 633, "crawler_id": 42},
                "crawler": {"id": 42, "name": "bot_control_01", "experiment_id": 94},
                "experiment": {"id": 94, "name": "Consent study"},
            },
        )
        assert (meta.queue_id, meta.crawler_id, meta.experiment_id) == (633, 42, 94)
        assert meta.crawler_name == "bot_control_01"
        assert meta.experiment_name == "Consent study"

    def test_a_bare_job_payload_leaves_the_names_empty_rather_than_crashing(self):
        # GET /jobs/{id} really does carry neither: the jobs table has only a
        # queue_id. The renderer must cope, not raise.
        meta = _debugger()._metadata_from_payload(1576, REAL_JOB_PAYLOAD)
        assert meta.crawler_name == ""
        assert meta.experiment_name == ""
        assert meta.queue_id == 633


class TestLambdaDecisionMeansTheDecision:
    def test_a_clean_decision_succeeds(self):
        d = JobDebugger.decision_from_payload(CLEAN_DECISION)
        assert d.output_parsed and d.success
        assert not d.degraded and not d.random_selection
        assert d.selection_mode == "llm"
        assert d.failed_filters == {}

    def test_a_payload_carrying_an_error_is_not_a_success(self):
        # This is the bug: `success` meant "the JSON parsed", so this was
        # reported as a successful decision and never flagged.
        d = JobDebugger.decision_from_payload({**CLEAN_DECISION, "error": "boom"})
        assert d.output_parsed is True
        assert d.success is False
        assert d.error == "boom"

    def test_random_selection_is_the_headline_signal(self):
        d = JobDebugger.decision_from_payload(RANDOM_DECISION)
        assert d.random_selection is True
        assert d.degraded is True
        assert d.success is False
        assert d.error is None  # HTTP 200; there is no hard error to find

    def test_failed_filters_lists_only_what_did_not_apply(self):
        d = JobDebugger.decision_from_payload(RANDOM_DECISION)
        # "political" is "skipped" = never requested. Counting a not-requested
        # filter as a failure reported warnings on every clean job.
        assert set(d.failed_filters) == {"mainstream_media"}

    def test_a_pre_contract_payload_with_warnings_is_treated_as_degraded(self):
        # Reports written before the `degraded` flag existed are the common case
        # on disk; warnings are the only witness they carry.
        d = JobDebugger.decision_from_payload(
            {"selected": {"title": "x", "id": "y"}, "warnings": ["fallback selection used"]}
        )
        assert d.degraded is True
        assert d.success is False

    def test_unparseable_output_is_reported_as_such(self, tmp_path: Path):
        broken = tmp_path / "lambda.zip"
        broken.write_bytes(b"not a zip")
        d = _debugger()._extract_lambda_decision(broken)
        assert d.output_parsed is False
        assert d.success is False
        assert d.error


class TestErrorSummarySplitsFailuresFromDegradation:
    def _summarise(self, report: JobDebugReport) -> JobDebugReport:
        _debugger()._generate_error_summary(report)
        return report

    def test_a_failed_job_is_an_error(self):
        report = self._summarise(
            JobDebugReport(
                job_id=1,
                metadata=JobMetadata(
                    job_id=1, status="failure", status_message="job failed - timeout"
                ),
            )
        )
        assert report.has_errors
        assert "failure" in report.error_summary
        assert not report.has_warnings

    def test_a_warning_job_is_a_warning_not_an_error(self):
        report = self._summarise(
            JobDebugReport(
                job_id=1,
                metadata=JobMetadata(
                    job_id=1,
                    status="warning",
                    status_message="job finished - jobdata not written",
                ),
            )
        )
        assert not report.has_errors
        assert report.has_warnings
        assert "jobdata not written" in report.warning_summary

    def test_a_lost_treatment_says_the_arm_became_a_control_arm(self):
        report = self._summarise(
            JobDebugReport(
                job_id=1,
                metadata=JobMetadata(job_id=1, status="success"),
                lambda_decision=JobDebugger.decision_from_payload(RANDOM_DECISION),
            )
        )
        assert not report.has_errors
        assert report.has_warnings
        assert "TREATMENT LOST" in report.warning_summary
        assert "control-arm" in report.warning_summary
        assert "mainstream_media" in report.warning_summary

    def test_a_hard_lambda_error_is_an_error(self):
        report = self._summarise(
            JobDebugReport(
                job_id=1,
                metadata=JobMetadata(job_id=1, status="success"),
                lambda_decision=JobDebugger.decision_from_payload(
                    {**CLEAN_DECISION, "error": "500 from lambda server"}
                ),
            )
        )
        assert report.has_errors
        assert "500 from lambda server" in report.error_summary

    def test_a_clean_job_reports_nothing(self):
        report = self._summarise(
            JobDebugReport(
                job_id=1,
                metadata=JobMetadata(job_id=1, status="success"),
                lambda_decision=JobDebugger.decision_from_payload(CLEAN_DECISION),
            )
        )
        assert not report.has_errors and not report.has_warnings


class FakeAggregator:
    """Stands in for LogAggregator: no Loki, no files, no network."""

    def __init__(self, entries=None, lambda_output=None):
        self.entries = entries or []
        self.lambda_output = lambda_output

    def query(self, **kwargs):
        return SimpleNamespace(entries=list(self.entries), total_count=len(self.entries))

    def get_lambda_output(self, job_id):
        return self.lambda_output

    def close(self):
        pass


def _builder(aggregator: FakeAggregator) -> TraceBuilder:
    with patch("orchestrator.tracing.trace_builder.get_settings") as get_settings:
        get_settings.return_value = SimpleNamespace(
            pm_api_base_url="http://localhost:8090/api/v1",
            pm_api_email="test@test.com",
            pm_api_password="testpass",
        )
        builder = TraceBuilder()
    builder._log_aggregator = aggregator  # type: ignore[assignment]
    return builder


def _log(level: str, message: str):
    return SimpleNamespace(
        timestamp=datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc),
        component="runner",
        level=level,
        message=message,
    )


class TestJobSpanStatus:
    def test_a_failed_job_with_no_error_log_is_still_failed(self):
        # The bug: status was read from a field that is never sent, so this fell
        # through to "there are logs, so it must have succeeded".
        span = _builder(FakeAggregator([_log("info", "started")]))._build_job_span(
            {"id": 700, "status_code": "failure", "status_message": "job failed - timeout"}
        )
        assert span.status is SpanStatus.FAILED

    def test_the_runners_diagnostic_lands_in_the_span(self):
        span = _builder(FakeAggregator())._build_job_span(
            {"id": 700, "status_code": "failure", "status_message": "job failed - timeout"}
        )
        assert span.metadata["status_message"] == "job failed - timeout"
        assert any("timeout" in err for err in span.errors)

    def test_a_warning_job_is_neither_green_nor_red(self):
        span = _builder(FakeAggregator([_log("info", "ok")]))._build_job_span(
            {"id": 700, "status_code": "warning", "status_message": "job finished - no jobdata"}
        )
        assert span.status is SpanStatus.WARNING
        assert any("no jobdata" in err for err in span.errors)

    def test_the_legacy_skpped_typo_is_a_skip_not_a_success(self):
        span = _builder(FakeAggregator([_log("info", "ok")]))._build_job_span(
            {"id": 700, "status_code": "skpped"}
        )
        assert span.status is SpanStatus.SKIPPED

    def test_no_status_still_falls_back_to_the_log_heuristic(self):
        span = _builder(FakeAggregator([_log("error", "boom")]))._build_job_span({"id": 700})
        assert span.status is SpanStatus.FAILED

    def test_no_status_and_no_logs_is_unknown_not_success(self):
        span = _builder(FakeAggregator())._build_job_span({"id": 700})
        assert span.status is SpanStatus.UNKNOWN


class TestLambdaSpanStatus:
    def _span(self, payload):
        return _builder(FakeAggregator())._build_lambda_span(700, payload)

    def test_a_clean_decision_is_green(self):
        assert self._span(CLEAN_DECISION).status is SpanStatus.SUCCESS

    def test_a_lost_treatment_is_a_warning_and_says_so_in_the_name(self):
        span = self._span(RANDOM_DECISION)
        assert span.status is SpanStatus.WARNING
        assert "TREATMENT LOST" in span.name
        assert span.metadata["selection_mode"] == "random"
        assert any("control" in err for err in span.errors)

    def test_a_hard_error_is_a_failure(self):
        span = self._span({**CLEAN_DECISION, "error": "500 from lambda server"})
        assert span.status is SpanStatus.FAILED
        assert any("500" in err for err in span.errors)

    def test_a_degraded_decision_names_the_filters_that_did_not_apply(self):
        span = self._span(
            {
                **CLEAN_DECISION,
                "degraded": True,
                "selection_mode": "llm",
                "filters": {**CLEAN_DECISION["filters"], "similar": "failed: Timeout"},
            }
        )
        assert span.status is SpanStatus.WARNING
        assert any("similar" in err for err in span.errors)


class TestTraceJobEscalatesFromItsLambdaChild:
    def test_a_green_job_with_a_random_decision_is_reported_as_degraded(self):
        # The runner reports "warning" for this itself now, but reports written
        # before 2026-08-27 still say "success" and the payload is the only
        # witness. A green root here is exactly the silent degradation.
        builder = _builder(FakeAggregator(lambda_output=RANDOM_DECISION))
        with patch.object(
            builder, "_api_get", return_value={"data": {"id": 700, "status_code": "success"}}
        ):
            trace = builder.trace_job(700)
        assert trace.root_span is not None
        assert trace.root_span.status is SpanStatus.WARNING

    def test_a_failed_job_is_not_downgraded_to_a_warning_by_its_child(self):
        builder = _builder(FakeAggregator(lambda_output=RANDOM_DECISION))
        with patch.object(
            builder, "_api_get", return_value={"data": {"id": 700, "status_code": "failure"}}
        ):
            trace = builder.trace_job(700)
        assert trace.root_span is not None
        assert trace.root_span.status is SpanStatus.FAILED


def _fully_populated_report() -> JobDebugReport:
    """Everything the rich renderer can touch, populated.

    The only coverage `debug job`'s default renderer had was an import smoke
    test, which is why four references to attributes that do not exist survived
    in it.
    """
    now = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)
    return JobDebugReport(
        job_id=1576,
        metadata=JobMetadata(
            job_id=1576,
            experiment_id=94,
            crawler_id=42,
            queue_id=633,
            experiment_name="Consent study",
            crawler_name="bot_control_01",
            status="warning",
            status_message="job finished - jobdata not written",
            job_type="lambdasingleselectjob",
            started_at=now,
            completed_at=now,
            duration_seconds=14.9,
            error_message=None,
            raw_data=dict(REAL_JOB_PAYLOAD),
        ),
        lambda_decision=JobDebugger.decision_from_payload(RANDOM_DECISION),
        har_summary=HARSummary(
            har_path="/tmp/1576/job.har",
            total_requests=120,
            total_time_ms=9000,
            total_transfer_bytes=1024 * 1024,
            failed_count=1,
            slow_count=1,
            failed_requests=[
                HARRequest(
                    url="https://www.youtube.com/api/x",
                    method="GET",
                    status=503,
                    status_text="Service Unavailable",
                    time_ms=120,
                    response_body="upstream down",
                )
            ],
            slow_requests=[
                HARRequest(
                    url="https://www.youtube.com/watch",
                    method="GET",
                    status=200,
                    status_text="OK",
                    time_ms=8000,
                )
            ],
            domain_stats={"www.youtube.com": DomainStats("www.youtube.com", 120, 1, 9000, 1024)},
        ),
        cloudwatch=CloudWatchInfo(
            available=True,
            request_id="abc-123",
            duration_ms=1200,
            billed_duration_ms=1300,
            init_duration_ms=300,
            memory_used_mb=180,
            memory_limit_mb=512,
            logs=[
                LogEntry(now, "lambda", "info", "selecting", "cloudwatch"),
                LogEntry(now, "lambda", "error", "openai auth failed", "cloudwatch"),
            ],
        ),
        artifacts=Artifacts(
            job_dir=Path("/tmp/1576"),
            har_file=Path("/tmp/1576/job.har"),
            screenshot_files=[Path("/tmp/1576/a.png"), Path("/tmp/1576/b.png")],
            lambda_zip=Path("/tmp/1576/lambda.zip"),
        ),
        logs=[LogEntry(now, "runner", "info", "job started", "loki")],
        error_logs=[],
    )


class TestDebugJobRichRendererDoesNotCrash:
    """The verified AttributeError: `debug job` raised while --format json worked."""

    def _render(self, report, verbose: bool = False) -> str:
        from rich.console import Console

        import orchestrator.cli as cli

        recorder = Console(record=True, width=140, no_color=True)
        with patch.object(cli, "console", recorder):
            cli._print_debug_report(report, verbose=verbose)
        return recorder.export_text()

    def test_a_fully_populated_report_renders(self):
        text = self._render(_fully_populated_report())
        assert "Job Metadata" in text
        assert "Lambda Decision" in text
        assert "CloudWatch Metrics" in text
        assert "Artifacts" in text

    def test_verbose_renders_too(self):
        # The verbose branches touch different attributes (screenshots, slow
        # requests, domain stats) and were never executed by any test.
        text = self._render(_fully_populated_report(), verbose=True)
        assert "Slow Requests" in text
        assert "Domain Breakdown" in text

    def test_the_random_selection_is_impossible_to_miss(self):
        text = self._render(_fully_populated_report())
        assert "RANDOM" in text
        assert "CONTROL" in text

    def test_the_runners_diagnostic_is_shown(self):
        text = self._render(_fully_populated_report())
        assert "jobdata not written" in text

    def test_cloudwatch_errors_come_from_the_error_level_entries(self):
        # There is no `cw.errors` list; referencing one is what raised.
        text = self._render(_fully_populated_report())
        assert "openai auth failed" in text

    def test_screenshots_are_counted_from_screenshot_files(self):
        text = self._render(_fully_populated_report())
        assert "2 files" in text

    def test_a_clean_job_says_no_obvious_issues(self):
        report = JobDebugReport(
            job_id=1,
            metadata=JobMetadata(
                job_id=1, status="success", status_message="successfully finished"
            ),
            lambda_decision=JobDebugger.decision_from_payload(CLEAN_DECISION),
        )
        text = self._render(report)
        # The old summary compared the status against "completed", a value the
        # API never sends, so even a green job was listed as an issue.
        assert "No obvious issues found" in text

    def test_an_empty_report_renders(self):
        text = self._render(JobDebugReport(job_id=1))
        assert "Job metadata not available" in text


class TestE2eVocabulary:
    def test_the_legacy_skpped_typo_no_longer_fails_the_run(self):
        from orchestrator.e2e.runner import _job_ok

        # The runner's JoblistState really does emit "skpped"; matching
        # "skipped" exactly meant a skipped job failed the e2e run.
        assert _job_ok({"status_code": "skpped", "type": "visitjob"}) is True
        assert _job_ok({"status_code": "skipped", "type": "visitjob"}) is True

    def test_a_degraded_job_does_not_fail_the_run_but_is_not_a_success(self):
        from orchestrator.e2e.job_status import job_degraded, job_succeeded
        from orchestrator.e2e.runner import _job_ok

        job = {"status_code": "warning", "type": "lambdasingleselectjob"}
        assert _job_ok(job) is True
        assert job_degraded(job) is True
        assert job_succeeded(job) is False

    def test_a_failed_job_still_fails_the_run(self):
        from orchestrator.e2e.runner import _job_ok

        assert _job_ok({"status_code": "failure", "type": "visitjob"}) is False
        assert _job_ok({"status_code": "failed", "type": "visitjob"}) is False

    def test_best_effort_job_types_still_pass_when_they_fail(self):
        from orchestrator.e2e.runner import _job_ok

        assert _job_ok({"status_code": "failure", "type": "tryclickjob"}) is True

    def test_queue_status_is_canonical_and_never_leaks_none(self):
        from orchestrator.e2e.runner import _queue_status

        assert _queue_status({"status_code": None}) == ""
        assert _queue_status({"status_code": "skpped"}) == "skipped"
        assert _queue_status({"status_code": "failed"}) == "failure"

    def test_progress_counts_a_degraded_job_as_finished(self):
        from orchestrator.e2e.job_status import job_finished

        assert job_finished({"status_code": "warning"}) is True
        assert job_finished({"status_code": "running"}) is False


class TestOneVocabulary:
    """The five consumers must not drift apart again."""

    @pytest.mark.parametrize(
        ("raw", "canonical"),
        [
            ("skpped", "skipped"),
            ("failed", "failure"),
            ("SUCCESS", "success"),
            ("completed", "success"),
            ("warning", "warning"),
        ],
    )
    def test_every_consumer_agrees(self, raw, canonical):
        from orchestrator.e2e.job_status import job_status

        assert status_contract.status_of({"status_code": raw}) == canonical
        assert job_status({"status_code": raw}) == canonical
        assert _debugger()._metadata_from_payload(1, {"status_code": raw}).status == canonical
        assert SpanStatus.from_status_code(raw).value == (
            "failed" if canonical == "failure" else canonical
        )

    def test_is_bad_covers_the_warning_state(self):
        # Using is_failure alone is what hid the degradation this state exists
        # to expose.
        assert status_contract.is_bad("warning")
        assert status_contract.is_bad("failure")
        assert not status_contract.is_bad("success")


class TestReporterStatusStyles:
    """The e2e summary table must not paint a degraded job like a failure.

    `reporter.py` was missed by the 2026-08-27 status change: it styled every
    non-success job red, so a `warning` (ran to completion, degraded) and a
    `skipped` job were visually identical to a hard failure.
    """

    def test_each_canonical_status_has_its_own_style(self):
        from orchestrator.e2e.reporter import _STATUS_STYLES

        assert _STATUS_STYLES["success"] == "green"
        assert _STATUS_STYLES["warning"] == "yellow"
        assert _STATUS_STYLES["failure"] == "red"
        assert _STATUS_STYLES["warning"] != _STATUS_STYLES["failure"]
        assert _STATUS_STYLES["skipped"] != _STATUS_STYLES["failure"]

    def test_unknown_status_stays_red(self):
        from orchestrator.e2e.reporter import _STATUS_STYLES

        assert _STATUS_STYLES.get("bogus", "red") == "red"

    def test_legacy_spellings_resolve_to_a_style(self):
        """The live DB holds 'failed' and 'skpped'; job_status normalises them."""
        from orchestrator.e2e.job_status import job_status
        from orchestrator.e2e.reporter import _STATUS_STYLES

        assert _STATUS_STYLES[job_status({"status_code": "failed"})] == "red"
        assert _STATUS_STYLES[job_status({"status_code": "skpped"})] == "dim"
