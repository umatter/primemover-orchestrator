"""Tests for reading job status and duration out of API payloads.

The fixtures below are copied verbatim from a live ``GET /api/v1/jobs/1576``
during a passing YouTube e2e run, because every bug these cover came from
guessing the field names rather than looking at them.
"""

from __future__ import annotations

import pytest

from orchestrator.e2e.job_status import (
    job_duration_seconds,
    job_status,
    job_succeeded,
)

# Verbatim from the API, including the offset spelling.
REAL_JOB = {
    "id": 1576,
    "type": "lambdasingleselectjob",
    "status_code": "success",
    "status_message": "successfully finished job",
    "started_at": "2026-08-21T09:58:32.994+0200",
    "finished_at": "2026-08-21T09:58:47.922+0200",
    "created_at": "2026-08-21T09:54:52+02:00",
}


class TestJobStatus:
    def test_reads_status_code_which_is_the_field_the_api_sends(self):
        assert job_status(REAL_JOB) == "success"

    def test_success_is_a_pass(self):
        # The reporter used to compare against "completed", so a real passing
        # job rendered red.
        assert job_succeeded(REAL_JOB) is True

    @pytest.mark.parametrize("value", ["success", "completed", "done", "SUCCESS", "Done"])
    def test_accepted_success_values_including_case(self, value):
        assert job_succeeded({"status_code": value}) is True

    @pytest.mark.parametrize("value", ["failed", "error", "running", "unknown", ""])
    def test_non_success_values(self, value):
        assert job_succeeded({"status_code": value}) is False

    def test_falls_back_to_status_when_status_code_absent(self):
        assert job_status({"status": "success"}) == "success"
        assert job_succeeded({"status": "success"}) is True

    def test_missing_both_fields_is_unknown_not_a_crash(self):
        assert job_status({}) == "unknown"
        assert job_succeeded({}) is False


class TestJobDuration:
    def test_computed_from_start_and_finish(self):
        # 09:58:32.994 -> 09:58:47.922
        assert job_duration_seconds(REAL_JOB) == pytest.approx(14.928, abs=0.001)

    def test_offset_without_a_colon_parses(self):
        """The regression that only shows up on Python 3.10.

        datetime.fromisoformat learned the colon-less +HHMM form in 3.11, and
        started_at/finished_at use exactly that. Parsed naively this works on a
        developer's 3.12 and raises in CI on 3.10.
        """
        assert job_duration_seconds(
            {
                "started_at": "2026-08-21T09:58:32.994+0200",
                "finished_at": "2026-08-21T09:58:47.922+0200",
            }
        ) == pytest.approx(14.928, abs=0.001)

    def test_offset_with_a_colon_also_parses(self):
        assert job_duration_seconds(
            {
                "started_at": "2026-08-21T09:58:32+02:00",
                "finished_at": "2026-08-21T09:58:47+02:00",
            }
        ) == pytest.approx(15.0)

    def test_zulu_suffix_parses(self):
        assert job_duration_seconds(
            {"started_at": "2026-08-21T07:58:32Z", "finished_at": "2026-08-21T07:58:47Z"}
        ) == pytest.approx(15.0)

    def test_offsets_that_differ_are_honoured(self):
        # Same instant expressed in two zones -> zero, not an hour.
        assert job_duration_seconds(
            {
                "started_at": "2026-08-21T09:58:32+02:00",
                "finished_at": "2026-08-21T08:58:32+01:00",
            }
        ) == pytest.approx(0.0)

    def test_explicit_duration_wins_if_the_api_ever_sends_one(self):
        assert job_duration_seconds({**REAL_JOB, "duration": 42.0}) == 42.0

    @pytest.mark.parametrize(
        "job",
        [
            {},
            {"started_at": "2026-08-21T09:58:32.994+0200"},  # no finish
            {"finished_at": "2026-08-21T09:58:47.922+0200"},  # no start
            {"started_at": None, "finished_at": None},
            {"started_at": "not a timestamp", "finished_at": "also not"},
            {"started_at": "", "finished_at": ""},
        ],
    )
    def test_unusable_input_returns_none_rather_than_raising(self, job):
        # A summary table is not worth failing a green e2e run over.
        assert job_duration_seconds(job) is None

    def test_negative_duration_is_rejected(self):
        assert (
            job_duration_seconds(
                {
                    "started_at": "2026-08-21T09:58:47+02:00",
                    "finished_at": "2026-08-21T09:58:32+02:00",
                }
            )
            is None
        )


class TestReporterRendersRealJobs:
    """The end the bug was actually visible from."""

    def test_summary_table_shows_success_and_a_duration(self):
        from rich.console import Console

        from orchestrator.e2e.reporter import E2EReporter

        reporter = E2EReporter()
        # Swap in a recording console so the rendered table can be asserted on.
        reporter.console = Console(record=True, width=120, no_color=True)

        result = type(
            "R",
            (),
            {
                "success": True,
                "duration": 301.9,
                "error": None,
                "jobs": [REAL_JOB],
                "experiment_id": 94,
            },
        )()
        reporter.print_results(result)
        text = reporter.console.export_text()

        assert "success" in text
        assert "unknown" not in text
        assert "14.9s" in text
