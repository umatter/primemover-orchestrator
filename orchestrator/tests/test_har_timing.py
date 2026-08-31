"""A duration that is not a duration must not be summed.

`orchestrator debug job 13451` reported ``Total Time -18246271267ms`` -- about
minus fifteen days. The cause is in the recorder, not here: a request that never
received a response (``"status": 0``) gets its ``wait`` timing computed against
an unset response time, so the entry carries ``"time": -1303303604`` and
``timings.wait: -1303304090``. On that job 14 of 53 entries looked like that.

The parser summed them blindly. Clamping them to zero would be just as wrong in
the other direction -- it would report a request nobody timed as instantaneous,
and drag every average down. They are excluded from totals and counted, so the
report can say what it does not know.
"""

import json

import pytest

from orchestrator.debug.har_parser import HARParser


def _entry(url, status, time_ms, wait, size=100):
    return {
        "startedDateTime": "2026-08-28T22:01:02.815+00:00",
        "time": time_ms,
        "request": {"url": url, "method": "GET", "headers": []},
        "response": {
            "status": status,
            "statusText": "",
            "headers": [],
            "content": {"size": size},
        },
        "timings": {
            "blocked": -1,
            "dns": -1,
            "connect": 486,
            "send": 0,
            "wait": wait,
            "receive": 0,
        },
    }


# Shaped after the real file: some good entries, some never-completed ones.
HAR = {
    "log": {
        "version": "1.2",
        "creator": {"name": "BrowserUp", "version": "x"},
        "entries": [
            _entry("https://www.youtube.com/a", 200, 187, 187),
            _entry("https://www.youtube.com/b", 200, 213, 213),
            # The production symptom: no response, absurd negative duration.
            _entry("https://i.ytimg.com", 0, -1303303604, -1303304090),
            _entry("https://i.ytimg.com", 0, -1303307892, -1303307892),
            # The HAR spec's own "not available" sentinel.
            _entry("https://fonts.gstatic.com/x", 200, -1, -1),
        ],
    }
}


@pytest.fixture
def summary(tmp_path):
    path = tmp_path / "trafic.har"
    path.write_text(json.dumps(HAR))
    return HARParser().parse_file(path)


class TestUntimedRequestsAreExcluded:
    def test_total_time_is_not_negative(self, summary):
        assert summary.total_time_ms >= 0, "a negative total is never a real duration"

    def test_total_time_is_the_sum_of_the_measurable_ones(self, summary):
        assert summary.total_time_ms == 187 + 213

    def test_every_request_is_still_counted(self, summary):
        # Excluding them from the timing total must not hide them entirely.
        assert summary.total_requests == 5

    def test_untimed_requests_are_reported(self, summary):
        # Two absurd negatives plus one spec -1.
        assert summary.untimed_count == 3

    def test_an_untimed_request_is_not_slow(self, summary):
        # time_ms > 5000 was never true for a negative, but the intent is that
        # "unknown" is not a performance verdict either way.
        assert summary.slow_count == 0


class TestAveragesUseTheMeasuredRequests:
    def test_domain_average_ignores_untimed_requests(self, summary):
        yt = summary.domain_stats["www.youtube.com"]
        assert yt.avg_time_ms == pytest.approx((187 + 213) / 2)

    def test_a_domain_with_no_usable_timing_averages_zero_not_negative(self, summary):
        img = summary.domain_stats["i.ytimg.com"]
        assert img.request_count == 2
        assert img.timed_count == 0
        # Dividing a negative total by 2 would have produced a negative average.
        assert img.avg_time_ms == 0
        assert img.total_time_ms == 0


class TestSerialization:
    def test_untimed_count_survives_to_dict(self, summary):
        assert summary.to_dict()["untimed_count"] == 3
