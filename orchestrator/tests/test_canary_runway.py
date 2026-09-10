"""How much runway the standing treatment canary has left.

The canary is a fixed list of daily sessions, not a recurring job -- `start_at`
already schedules work, so no cron is involved. The consequence is that it RUNS
OUT, and when the last session has gone the canary simply stops producing
evidence. Silence from a canary reads as health, which is the one failure a
canary must not have, so `orchestrator doctor` warns while there is still time
to top it up.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from experiment_designer.spec.time_utils import API_TIMEZONE
from orchestrator.canary import (
    RUNWAY_WARNING_DAYS,
    CanaryRunway,
    find_canary,
    parse_start,
    runway_from_queues,
)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=API_TIMEZONE)


def a_queue(day_offset: int, processed: int = 0) -> dict:
    start = NOW + timedelta(days=day_offset)
    return {"start_at": start.strftime("%Y-%m-%d %H:%M:%S"), "processed": processed}


class TestRunway:
    def test_counts_only_unprocessed_sessions(self):
        queues = [a_queue(-3, processed=1), a_queue(1), a_queue(2)]

        remaining, _, _ = runway_from_queues(queues, now=NOW)

        assert remaining == 2

    def test_days_left_is_measured_to_the_last_session_not_the_next(self):
        # Not to the next one: the question is when the canary goes silent.
        queues = [a_queue(1), a_queue(20)]

        _, last, days = runway_from_queues(queues, now=NOW)

        assert last.day == 30
        assert days == 20

    def test_an_exhausted_canary_has_nothing_left(self):
        queues = [a_queue(-2, processed=1), a_queue(-1, processed=1)]

        remaining, last, days = runway_from_queues(queues, now=NOW)

        assert (remaining, last, days) == (0, None, None)

    def test_an_overdue_session_still_counts_as_remaining(self):
        # It is unprocessed and its start has passed: the runner is not picking
        # it up. That is a different problem, and silently discounting it here
        # would turn one problem into no problem at all.
        queues = [a_queue(-1), a_queue(3)]

        remaining, _, days = runway_from_queues(queues, now=NOW)

        assert remaining == 2
        assert days == 3


class TestNeedsAttention:
    @pytest.mark.parametrize("days", [0, 1, RUNWAY_WARNING_DAYS])
    def test_inside_the_window_needs_attention(self, days):
        assert CanaryRunway(missing=False, remaining=days or 1, days_left=days).needs_attention

    def test_outside_the_window_does_not(self):
        runway = CanaryRunway(missing=False, remaining=28, days_left=RUNWAY_WARNING_DAYS + 1)

        assert runway.needs_attention is False

    def test_an_exhausted_canary_always_needs_attention(self):
        assert CanaryRunway(missing=False, remaining=0).needs_attention

    def test_an_overdue_canary_needs_attention(self):
        # Every session in the past and unprocessed.
        assert CanaryRunway(missing=False, remaining=3, days_left=-2).needs_attention

    def test_no_canary_at_all_is_not_a_problem(self):
        # Most deployments do not run one; doctor must not nag a laptop about
        # infrastructure only ex63 has.
        assert CanaryRunway(missing=True).needs_attention is False


class TestParseStart:
    def test_a_naive_string_is_read_in_the_apis_zone(self):
        # The column is a varchar holding wall clock. Reading it as UTC or as the
        # host's zone would shift the runway by an hour or six.
        parsed = parse_start("2026-10-07 05:00:00")

        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == NOW.utcoffset()

    def test_an_offset_bearing_string_keeps_its_instant(self):
        assert parse_start("2026-10-07T05:00:00+02:00").hour == 5

    @pytest.mark.parametrize("value", [None, "", "   ", "not a date", 42])
    def test_junk_is_none_rather_than_an_exception(self, value):
        assert parse_start(value) is None


class TestFindCanary:
    class _Client:
        def __init__(self, experiments, crawlers, queues):
            self._experiments, self._crawlers, self._queues = experiments, crawlers, queues

        def list_experiments(self, per_page=15):
            return self._experiments

        def get_experiment(self, experiment_id):
            return {
                "id": experiment_id,
                "name": f"exp {experiment_id}",
                "crawlers": self._crawlers.get(experiment_id, []),
            }

        def list_queues_for_crawler(self, crawler_id):
            return self._queues.get(crawler_id, [])

    def test_it_matches_on_the_crawler_name_not_the_experiment_name(self):
        # The experiment name is typed by hand at upload time and will eventually
        # be typed differently; the crawler name is set by the generator.
        client = self._Client(
            [{"id": 5}],
            {5: [{"id": 50, "name": "treatment-canary-2026-09-10"}]},
            {50: [a_queue(3)]},
        )

        runway = find_canary(client)

        assert runway.missing is False
        assert runway.experiment_id == 5

    def test_it_prefers_the_newest_canary(self):
        # A freshly topped-up canary must be found before the exhausted one it
        # replaces, or doctor keeps warning about the old one for ever.
        client = self._Client(
            [{"id": 5}, {"id": 9}],
            {
                5: [{"id": 50, "name": "treatment-canary-2026-08-01"}],
                9: [{"id": 90, "name": "treatment-canary-2026-09-10"}],
            },
            {50: [], 90: [a_queue(20)]},
        )

        assert find_canary(client).experiment_id == 9

    def test_a_deployment_without_one_reports_missing_not_broken(self):
        client = self._Client([{"id": 1}], {1: [{"id": 10, "name": "some-study-bot"}]}, {})

        runway = find_canary(client)

        assert runway.missing is True
        assert runway.needs_attention is False

    def test_an_unreachable_api_is_reported_not_raised(self):
        class _Broken:
            def list_experiments(self, per_page=15):
                raise ConnectionError("refused")

        runway = find_canary(_Broken())

        assert runway.missing is True
        assert runway.problems and "refused" in runway.problems[0]
