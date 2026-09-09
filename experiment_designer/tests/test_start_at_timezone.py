"""What a spec's `start` becomes, and why the API's zone is the only one that matters.

The API does not honour the offset it is sent. It keeps the wall-clock digits
and discards the zone. Probed against the live API on 2026-09-09, every case:

    sent 2030-01-15T05:00:00+01:00  -> stored 2030-01-15 05:00:00
    sent 2030-01-15T04:00:00Z       -> stored 2030-01-15 04:00:00   (1h early)
    sent 2030-01-15T05:00:00        -> stored 2030-01-15 05:00:00
    sent 2030-07-15T05:00:00+02:00  -> stored 2030-07-15 05:00:00
    sent 2030-07-15T03:00:00Z       -> stored 2030-07-15 03:00:00   (2h early)
    sent 2030-07-15T05:00:00-05:00  -> stored 2030-07-15 05:00:00   (7h early)

Nothing errors. The queue starts at the wrong time, or lands in the past and
has its jobs marked skipped.

This was invisible because every machine that has ever run the DSL sits in
CET/CEST, so the host's wall clock and the API's were the same number.
`datetime.now().astimezone()` was therefore right by coincidence, and would have
scheduled an hour early from London and six from Boston.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from experiment_designer.spec.time_utils import API_TIMEZONE, parse_relative_time


class TestGeneratedTimes:
    def test_now_is_the_apis_wall_clock(self):
        emitted = parse_relative_time("now")
        expected = datetime.now(API_TIMEZONE).strftime("%Y-%m-%dT%H:%M")

        assert emitted.startswith(expected)

    def test_the_offset_is_the_apis_current_one(self):
        # +02:00 in summer, +01:00 in winter -- whichever it is right now.
        offset = datetime.now(API_TIMEZONE).strftime("%z")

        assert parse_relative_time("now").endswith(offset[:3] + ":" + offset[3:])

    def test_a_relative_offset_lands_on_the_same_clock(self):
        emitted = parse_relative_time("+1h")
        assert emitted.endswith(parse_relative_time("now")[-6:])


class TestAbsoluteTimes:
    """An offset means an instant; no offset means wall clock as written."""

    @pytest.mark.parametrize(
        ("sent", "expected"),
        [
            # Z is a real instant and must survive as one.
            ("2030-07-15T03:00:00Z", "2030-07-15T05:00:00+02:00"),
            ("2030-01-15T04:00:00Z", "2030-01-15T05:00:00+01:00"),
            # Already the API's own clock: unchanged.
            ("2030-07-15T05:00:00+02:00", "2030-07-15T05:00:00+02:00"),
            ("2030-01-15T05:00:00+01:00", "2030-01-15T05:00:00+01:00"),
            # The seven-hour case from the probe.
            ("2030-07-15T05:00:00-05:00", "2030-07-15T12:00:00+02:00"),
        ],
    )
    def test_an_offset_bearing_time_is_converted(self, sent, expected):
        assert parse_relative_time(sent) == expected

    def test_a_naive_time_is_left_exactly_as_written(self):
        # It already means what the API will read it as. Converting it would be
        # inventing an intention the author did not express.
        assert parse_relative_time("2030-07-15T05:00:00") == "2030-07-15T05:00:00"

    def test_dst_is_resolved_per_date_not_once(self):
        summer = parse_relative_time("2030-07-15T12:00:00Z")
        winter = parse_relative_time("2030-01-15T12:00:00Z")

        assert summer.endswith("+02:00")
        assert winter.endswith("+01:00")

    def test_something_unparseable_is_passed_through_untouched(self):
        # A convenience should not refuse to schedule; let the API validate.
        assert parse_relative_time("not-a-timestamp") == "not-a-timestamp"


class TestTheZoneIsNamedOnce:
    def test_the_e2e_and_the_dsl_agree_by_construction(self):
        # They used to declare it separately, which is how two clients of one
        # API end up disagreeing about its timezone.
        from orchestrator.e2e import runner

        assert runner.API_TIMEZONE is API_TIMEZONE
