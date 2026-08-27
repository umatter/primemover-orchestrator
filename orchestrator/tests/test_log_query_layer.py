"""Behavioural tests for the log query layer: LogQL construction, correlation-id
matching, the lambda degradation signal, and the promtail contract they all rest on.

Every bug covered here was silent. None of them raised, none of them logged; each
one turned a question ("what went wrong?") into the answer "nothing", which is the
worst possible failure mode for a debugging tool. So the assertions are on the
exact query text and the exact entries produced, not on "it returned something".

Nothing here touches the network: ``LokiClient._build_query`` and the helpers
around it are pure functions, and the aggregator is exercised through
``_lambda_entries``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from orchestrator import status
from orchestrator.clients.loki import (
    ANY_COMPONENT_MATCHER,
    API_REQUEST_PLUMBING_MESSAGES,
    LogEntry,
    LokiClient,
    api_request_plumbing_exclusion,
    build_label_selector,
    correlation_line_filters,
    level_label_matcher,
    logql_regex_filter,
    range_query_params,
    time_range_bounds,
)
from orchestrator.logs.aggregator import (
    LOG_LEVEL_SEVERITY,
    MAX_LAMBDA_WARNING_ENTRIES,
    LogAggregator,
    level_at_least,
)
from orchestrator.tracing.trace_builder import (
    SpanStatus,
    TraceBuilder,
    parse_api_timestamp,
    span_bounds,
)

PROMTAIL_CONFIG = (
    Path(__file__).resolve().parents[2] / "infrastructure" / "promtail" / "promtail-config.yaml"
)

# Real lines, copied from the live index on 2026-08-27.
RUNNER_LINE = (
    "2026-08-27T08:19:24.425+02:00 correlationId=cr-250|jl-844, crawler.id=250, "
    "crawler.name=YouTube Bot, joblist.id=844  WARN 3546872 --- [primemover-runner] "
    "[executor-3] primemover.service.JobService : media outlet urls are missing"
)
RUNNER_LINE_CANONICAL = (
    "2026-08-27T08:19:24.425+02:00 correlationId=exp-42|cr-250|q-844|j-3944, "
    "crawler.id=250, crawler.name=YouTube Bot, joblist.id=844, job.id=3944  WARN 3546872 "
    "--- [primemover-runner] [executor-3] primemover.service.JobService : job data not written"
)
LARAVEL_LINE = (
    "[2026-08-27 11:11:09] production.ERROR: Password reset email could not be sent "
    '{"experiment_id":null,"crawler_id":249,"queue_id":null,"job_id":null,'
    '"request_id":"abc","correlation_id":"cr-249"}'
)
# The observer's own footprint: `GET /api/v1/jobs/3943` makes the API's throttle
# middleware log this, with job_id 3943 stamped on it by AddCorrelationContext.
LARAVEL_THROTTLE_LINE = (
    "[2026-08-27 13:35:06] production.DEBUG: 1649254282: Throttling request with IP "
    '{"experiment_id":null,"crawler_id":null,"queue_id":null,"job_id":3943,'
    '"request_id":"dfee5a0d-9254-4d76-9585-ade411db94a0","correlation_id":"j-3943",'
    '"ip":"172.19.0.1","maxAttempts":"1000","decayMinutes":"1","prefix":""}'
)
# A genuine API line about the same kind of entity, which must survive the filter.
LARAVEL_JOB_REPORT_LINE = (
    "[2026-08-27 13:03:30] production.WARNING: runner reported degraded job "
    '{"experiment_id":null,"crawler_id":null,"queue_id":101,"job_id":3943,'
    '"request_id":"b9629243-09d3-472f-9a98-c382b9979d44","correlation_id":"j-3943",'
    '"status_code":"warning","status_message":"job data not written"}'
)


# =============================================================================
# Level matching
# =============================================================================


class TestLevelMatching:
    def test_level_is_a_label_matcher_not_a_line_filter(self):
        """The level is never *in* the line in a ``level=`` form.

        Laravel writes ``production.ERROR:`` and the runner writes
        ``... ERROR 3546872 ---``. The old line filter
        ``|~ "(?i)(level|lvl)[=:]*\\s*error"`` therefore matched zero rows in either
        component -- ``orchestrator logs errors`` found nothing, ever.
        """
        assert level_label_matcher("error") is not None
        assert level_label_matcher("error").startswith("level=~")
        assert not re.search(r"(?i)(level|lvl)[=:]*\s*error", RUNNER_LINE.replace("WARN", "ERROR"))
        assert not re.search(r"(?i)(level|lvl)[=:]*\s*error", LARAVEL_LINE)

    @pytest.mark.parametrize("severe", ["ALERT", "CRITICAL", "EMERGENCY", "alert", "critical"])
    def test_error_also_matches_the_levels_above_error(self, severe):
        """Laravel's three most severe levels were the three it could not see.

        PSR-3 puts ALERT, CRITICAL and EMERGENCY *above* ERROR, and promtail labels
        them verbatim -- ALERT is in the live index right now. A plain
        ``level=~"(?i)error"`` matches none of them, so ``orchestrator logs errors``
        answered "nothing" for exactly the lines that most needed answering.
        """
        matcher = level_label_matcher("error")
        assert matcher is not None
        body = matcher[len('level=~"') : -1]
        # Loki anchors a label regex, so the whole value has to match.
        assert re.fullmatch(body, severe), f"{severe} is invisible to a level=error query"

    def test_error_does_not_swallow_warning_or_info(self):
        """Widening ``error`` must not turn it into "everything"."""
        body = level_label_matcher("error")[len('level=~"') : -1]
        for benign in ("WARN", "warning", "INFO", "DEBUG", "notice"):
            assert not re.fullmatch(body, benign)

    def test_warning_also_matches_the_runners_warn(self):
        matcher = level_label_matcher("warning")
        assert matcher == 'level=~"(?i)warn(ing)?"'
        # Both spellings exist in the live index at once.
        pattern = re.compile("(?i)warn(ing)?")
        assert pattern.fullmatch("WARN") and pattern.fullmatch("warning")

    @pytest.mark.parametrize("given", ["warn", "WARNING", " Warn "])
    def test_warn_and_warning_are_the_same_level(self, given):
        assert level_label_matcher(given) == 'level=~"(?i)warn(ing)?"'

    def test_matching_is_case_insensitive_because_history_is_mixed(self):
        """Promtail lowercases new data; everything already indexed does not.

        The live index holds ALERT, DEBUG, INFO, error, ERROR, Error, WARN, WARNING,
        Warning and warn simultaneously, so a case-sensitive matcher silently halves
        any result set that spans the config change.
        """
        matcher = level_label_matcher("error")
        assert matcher is not None and "(?i)" in matcher
        pattern = re.compile("(?i)error")
        assert all(pattern.fullmatch(v) for v in ("ERROR", "error", "Error"))

    @pytest.mark.parametrize("given", [None, "", "all"])
    def test_no_level_means_no_matcher(self, given):
        assert level_label_matcher(given) is None

    @pytest.mark.parametrize("given", ['error"} |~ "', "err-or", "1"])
    def test_a_non_alphabetic_level_is_dropped_rather_than_interpolated(self, given):
        assert level_label_matcher(given) is None


# =============================================================================
# Stream selector
# =============================================================================


class TestLabelSelector:
    def test_empty_selector_is_replaced_because_loki_rejects_it(self):
        """``{}`` is a 400, not "all streams".

        Loki refuses a selector in which every matcher can match the empty string.
        LogAggregator swallowed that 400 into ``sources["loki_error"]``, so an
        unscoped query looked like a clean run with no logs.
        """
        assert build_label_selector([]) == "{" + ANY_COMPONENT_MATCHER + "}"
        assert build_label_selector([]) == '{component=~".+"}'

    def test_matchers_are_joined(self):
        assert (
            build_label_selector(['component="api"', 'level=~"(?i)error"'])
            == '{component="api",level=~"(?i)error"}'
        )


# =============================================================================
# Correlation-id filters
# =============================================================================


class TestCorrelationLineFilters:
    @staticmethod
    def _to_python_regex(logql_filter: str) -> re.Pattern[str]:
        """Undo the LogQL string escaping so the regex can be run here.

        LogQL literals use Go escapes, so a word boundary has to be written ``\\\\b``
        in the query text. Reversing that is what lets these tests assert the filter
        matches a real log line rather than merely looks plausible.
        """
        body = logql_filter[len('|~ "') : -1]
        return re.compile(body.replace('\\"', '"').replace("\\\\", "\\"))

    def test_json_stage_is_gone(self):
        """``| json`` dropped every entry: the lines are not JSON documents."""
        filters = correlation_line_filters(crawler_id=250)
        assert filters and all("| json" not in f for f in filters)

    def test_matches_the_runners_historical_correlation_id(self):
        pattern = self._to_python_regex(correlation_line_filters(crawler_id=250)[0])
        assert pattern.search(RUNNER_LINE)

    def test_matches_the_runners_canonical_correlation_id(self):
        """The runner is moving to ``exp-|cr-|q-|j-`` while old rows keep ``jl-``."""
        for kwargs in (
            {"experiment_id": 42},
            {"crawler_id": 250},
            {"queue_id": 844},
            {"job_id": 3944},
        ):
            pattern = self._to_python_regex(correlation_line_filters(**kwargs)[0])
            assert pattern.search(RUNNER_LINE_CANONICAL), kwargs

    def test_queue_filter_accepts_both_q_and_jl(self):
        pattern = self._to_python_regex(correlation_line_filters(queue_id=844)[0])
        assert pattern.search("correlationId=cr-250|jl-844,")
        assert pattern.search("correlationId=exp-42|cr-250|q-844|j-1,")

    def test_matches_the_laravel_json_context(self):
        """Laravel appends ``{"crawler_id":249,...}`` to an otherwise plain line."""
        pattern = self._to_python_regex(correlation_line_filters(crawler_id=249)[0])
        assert pattern.search(LARAVEL_LINE)

    def test_job_filter_does_not_match_a_joblist_id(self):
        """``j-`` must not match ``jl-``, or every queue looks like a job."""
        pattern = self._to_python_regex(correlation_line_filters(job_id=844)[0])
        assert not pattern.search("correlationId=cr-250|jl-844,")

    def test_ids_are_word_bounded(self):
        """``cr-25`` must not match ``cr-250``."""
        pattern = self._to_python_regex(correlation_line_filters(crawler_id=25)[0])
        assert not pattern.search(RUNNER_LINE)
        assert pattern.search("correlationId=cr-25|jl-1,")

    def test_each_id_adds_one_anded_filter_plus_the_plumbing_exclusion(self):
        filters = correlation_line_filters(experiment_id=1, crawler_id=2, queue_id=3, job_id=4)
        positive = [f for f in filters if f.startswith("|~")]
        negative = [f for f in filters if f.startswith("!~")]
        assert len(positive) == 4
        assert len(negative) == 1

    def test_no_ids_means_no_filters(self):
        assert correlation_line_filters() == []

    @pytest.mark.parametrize("given", ["7", 7.0, True, None])
    def test_non_integer_ids_are_ignored_rather_than_interpolated(self, given):
        assert correlation_line_filters(crawler_id=given) == []

    def test_quotes_and_backslashes_are_escaped_for_the_logql_literal(self):
        assert logql_regex_filter(r'a\b"c') == '|~ "a\\\\b\\"c"'


class TestObserverEffect:
    """The correlation filter used to match the debugging tool's own footprint.

    ``AddCorrelationContext`` stamps the ids it reads off the route onto every line
    logged while serving that request, so ``GET /api/v1/jobs/3943`` -- which is how
    ``trace job`` and ``debug job`` fetch the job -- made the API's throttle
    middleware emit a line carrying ``job_id: 3943``. The tool then reported that
    line as one of the job's related logs, and since every observation produced two
    more of them, the "related logs" count and the log-derived duration grew every
    single time the job was looked at.

    Measured against live Loki on 2026-08-27 for job 3943 over 7d: 40 matching
    entries, 40 of them this one message, 0 of them about the job.
    """

    @staticmethod
    def _to_python_regex(logql_filter: str) -> re.Pattern[str]:
        body = logql_filter[len('!~ "') : -1]
        return re.compile(body.replace('\\"', '"').replace("\\\\", "\\"))

    def test_the_throttle_line_matches_the_bare_correlation_filter(self):
        """Sanity check on the premise: without the exclusion it really does match."""
        positive = correlation_line_filters(job_id=3943)[0]
        pattern = TestCorrelationLineFilters._to_python_regex(positive)
        assert pattern.search(LARAVEL_THROTTLE_LINE)

    def test_the_exclusion_drops_the_throttle_line(self):
        pattern = self._to_python_regex(api_request_plumbing_exclusion())
        assert pattern.search(LARAVEL_THROTTLE_LINE)

    def test_the_exclusion_keeps_genuine_job_lines(self):
        """``runner reported degraded job`` is the whole reason not to drop
        ``component="api"`` wholesale: six such lines in a week, against 4,994
        throttle lines, and they are the ones a job trace exists to show."""
        pattern = self._to_python_regex(api_request_plumbing_exclusion())
        assert not pattern.search(LARAVEL_JOB_REPORT_LINE)
        assert not pattern.search(RUNNER_LINE)
        assert not pattern.search(RUNNER_LINE_CANONICAL)

    def test_the_exclusion_is_attached_to_every_id_filtered_query(self):
        for kwargs in (
            {"experiment_id": 1},
            {"crawler_id": 2},
            {"queue_id": 3},
            {"job_id": 4},
        ):
            filters = correlation_line_filters(**kwargs)
            assert filters[-1].startswith("!~"), kwargs

    def test_an_unfiltered_query_is_left_alone(self):
        """Without an id there is no route-stamped correlation to be fooled by, and
        ``logs query -c api`` must still be able to show the throttle lines."""
        assert correlation_line_filters() == []

    def test_the_exclusion_never_escapes_a_space(self):
        """``re.escape`` writes ``Throttling\\ request``; ``\\ `` is a hard parse
        error in Go's RE2, which would 400 the whole query rather than filter it."""
        assert "\\ " not in api_request_plumbing_exclusion()

    def test_the_deny_list_stays_small_and_named(self):
        """A deny-list that grows without a criterion is how this rots. The rule is
        in the constant's comment: emitted on every request, says nothing about the
        entity."""
        assert len(API_REQUEST_PLUMBING_MESSAGES) <= 4
        for message in API_REQUEST_PLUMBING_MESSAGES:
            assert message and message.strip() == message


# =============================================================================
# Time window
# =============================================================================


class TestTimeWindow:
    def test_start_and_end_are_explicit(self):
        """Loki defaults to the last hour when start/end are omitted.

        Every caller that built its own params dict therefore ignored its
        ``time_range`` argument silently -- a 7d search really searched 1h.
        """
        params = range_query_params("7d")
        span = timedelta(microseconds=(params["end"] - params["start"]) / 1000)
        assert abs(span - timedelta(days=7)) < timedelta(seconds=2)

    def test_explicit_bounds_win(self):
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        end = datetime(2026, 8, 2, tzinfo=timezone.utc)
        assert range_query_params("7d", start=start, end=end) == {
            "start": int(start.timestamp() * 1e9),
            "end": int(end.timestamp() * 1e9),
        }

    def test_bounds_are_utc_aware(self):
        start, end = time_range_bounds("15m")
        assert start.tzinfo is not None and end.tzinfo is not None
        assert abs((end - start) - timedelta(minutes=15)) < timedelta(seconds=1)


# =============================================================================
# LokiClient._build_query
# =============================================================================


class TestBuildQuery:
    @pytest.fixture
    def client(self) -> LokiClient:
        return LokiClient(base_url="http://loki.invalid:3100")

    def test_component_and_level_are_both_labels(self, client):
        query = client._build_query(component="runner", level="error")
        assert query.startswith('{component="runner",level=~"(?i)')
        assert query.endswith('"}')

    def test_level_alone_is_a_valid_query(self, client):
        assert client._build_query(level="error") == "{" + level_label_matcher("error") + "}"

    def test_no_filters_still_produces_a_valid_selector(self, client):
        assert client._build_query() == '{component=~".+"}'

    def test_correlation_ids_become_line_filters(self, client):
        query = client._build_query(component="runner", crawler_id=250, queue_id=844)
        assert query.startswith('{component="runner"}')
        assert query.count("|~") == 2
        assert "| json" not in query

    def test_search_is_regex_escaped(self, client):
        query = client._build_query(search="a.b[c]")
        assert r"a\.b\[c\]" in query

    def test_invalid_component_is_dropped_not_interpolated(self, client):
        assert client._build_query(component='api"} |~ "') == '{component=~".+"}'


# =============================================================================
# LogEntry correlation extraction
# =============================================================================


def _entry(message: str, **kwargs) -> LogEntry:
    return LogEntry(
        timestamp=datetime(2026, 8, 27, tzinfo=timezone.utc),
        component=kwargs.pop("component", "runner"),
        level=kwargs.pop("level", "info"),
        message=message,
        **kwargs,
    )


class TestLogEntryCorrelation:
    def test_runner_mdc_keys_are_read(self):
        """The runner writes ``crawler.id=250``, not ``crawler_id=250``."""
        entry = _entry(RUNNER_LINE)
        assert (entry.crawler_id, entry.queue_id) == (250, 844)

    def test_runner_job_id_mdc_key_is_read(self):
        assert _entry(RUNNER_LINE_CANONICAL).job_id == 3944

    def test_joblist_id_is_not_mistaken_for_job_id(self):
        """``job\\.id`` must not match inside ``joblist.id``."""
        assert _entry("crawler.id=250, joblist.id=844  INFO x : y").job_id is None

    def test_laravel_json_context_is_read(self):
        entry = _entry(LARAVEL_LINE, component="api", level="error")
        assert entry.crawler_id == 249
        assert entry.queue_id is None and entry.job_id is None

    def test_null_context_values_do_not_become_ids(self):
        entry = _entry('{"experiment_id":null,"crawler_id":null,"job_id":null}', component="api")
        assert (entry.experiment_id, entry.crawler_id, entry.job_id) == (None, None, None)

    def test_canonical_correlation_id_fills_every_field(self):
        entry = _entry(RUNNER_LINE_CANONICAL)
        assert (entry.experiment_id, entry.crawler_id, entry.queue_id, entry.job_id) == (
            42,
            250,
            844,
            3944,
        )

    def test_an_experiment_id_no_longer_suppresses_the_other_ids(self):
        """Correlation parsing used to be skipped whenever experiment_id was set."""
        entry = _entry("experiment_id=42 correlationId=cr-7|jl-8")
        assert (entry.experiment_id, entry.crawler_id, entry.queue_id) == (42, 7, 8)

    def test_labels_win_over_the_message(self):
        entry = _entry(RUNNER_LINE, labels={"crawler_id": "999"})
        assert entry.crawler_id == 999


# =============================================================================
# Lambda degradation signal
# =============================================================================


TS = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)


def _lambda_entries(payload: dict) -> list[LogEntry]:
    return LogAggregator._lambda_entries(payload, message="Lambda decision", timestamp=TS, job_id=5)


class TestLambdaDegradation:
    def test_a_clean_decision_is_a_single_info_entry(self):
        entries = _lambda_entries(
            {"selected": {"id": "abc"}, "selection_mode": "llm", "degraded": False, "warnings": []}
        )
        assert len(entries) == 1
        assert entries[0].level == "info"
        assert entries[0].labels["selection_mode"] == "llm"
        assert entries[0].labels["degraded"] == "false"

    def test_treatment_lost_is_reported_as_an_error(self):
        """The one unambiguous alarm: configured to be treated, produced control data.

        Computed at the source as ``treatment_lost`` precisely so no consumer has to
        combine ``degraded`` with ``selection_mode`` and get the two-term gate wrong.
        """
        entries = _lambda_entries(
            {
                "selected": {"id": "abc"},
                "selection_mode": "random",
                "degraded": True,
                "treatment_lost": True,
                "filters": {"political": "failed: TimeoutError: llm"},
            }
        )
        errors = [e for e in entries if e.level == "error"]
        assert len(errors) == 1
        assert "control-arm data" in errors[0].message
        assert errors[0].labels["treatment_lost"] == "true"
        assert entries[0].labels["degraded"] == "true"

    def test_a_healthy_control_crawler_is_not_an_error(self):
        """The design flaw this contract was amended to remove.

        A genuine control crawler (pi=psi=kappa=tau=0) legitimately produces
        ``selection_mode: random``: no LLM filter was ever REQUESTED, so none could
        fail. Gating on the mode alone would have marked ~82% of all lambda_select
        jobs as warnings -- 295 of 358 real payloads on this box carry no rating
        column at all, and 236 of 243 live bot configurations are control-arm --
        and fired a permanent critical alert. That is the exact opposite of the
        change's purpose: it buries the real signal in false ones.
        """
        entries = _lambda_entries(
            {
                "selected": {"id": "abc"},
                "selection_mode": "random",
                "degraded": False,
                "treatment_lost": False,
                "filters": {
                    "already_watched": "ok",
                    "mainstream_media": "skipped",
                    "children_entertainment": "skipped",
                    "similar": "skipped",
                    "political": "skipped",
                    "fringe": "skipped",
                },
            }
        )
        assert len(entries) == 1
        assert entries[0].level == "info"
        assert entries[0].labels["degraded"] == "false"
        assert entries[0].labels["treatment_lost"] == "false"
        # The mode is still recorded -- it is a description, not an accusation.
        assert entries[0].labels["selection_mode"] == "random"

    def test_skipped_filters_are_never_counted_as_failures(self):
        """ "skipped" means NOT REQUESTED. On a control crawler five of the six are
        skipped on every healthy invocation."""
        entries = _lambda_entries(
            {
                "selected": {"id": "abc"},
                "selection_mode": "random",
                "degraded": False,
                "filters": {"already_watched": "ok", "political": "skipped"},
            }
        )
        assert "failed_filters" not in entries[0].labels
        assert entries[0].labels["skipped_filters"] == "political"
        assert not [e for e in entries if e.labels.get("filter")]

    def test_a_missing_report_reads_as_unknown_not_as_clean(self):
        """A payload with no degradation report is a LEGACY payload from the
        currently deployed decision function. Absence must never be rendered as
        "not degraded" -- that is a blind spot reported as a clean bill of health.
        """
        entries = _lambda_entries({"selected": {"id": "abc"}, "selection_mode": "llm"})
        assert entries[0].labels["degraded"] == "unknown"
        assert "no degradation report" in entries[0].message

    def test_selection_mode_alone_never_moves_the_level(self):
        """Same payload, both modes, one health state -> one level."""
        base = {"selected": {"id": "abc"}, "degraded": False, "filters": {"political": "ok"}}
        llm = _lambda_entries({**base, "selection_mode": "llm"})
        random_mode = _lambda_entries({**base, "selection_mode": "random"})
        assert [e.level for e in llm] == [e.level for e in random_mode] == ["info"]

    def test_degraded_downgrades_the_summary_entry_to_warning(self):
        entries = _lambda_entries({"selected": {}, "degraded": True, "selection_mode": "llm"})
        assert entries[0].level == "warning"
        assert "[DEGRADED]" in entries[0].message

    def test_failed_filters_get_one_entry_each(self):
        entries = _lambda_entries(
            {
                "selected": {},
                "degraded": True,
                "selection_mode": "llm",
                "filters": {
                    "already_watched": "ok",
                    "mainstream_media": "failed: KeyError: 'channel'",
                    "political": "skipped",
                    "fringe": "failed: TimeoutError: llm",
                },
            }
        )
        filter_entries = {e.labels.get("filter") for e in entries if e.labels.get("filter")}
        assert filter_entries == {"mainstream_media", "fringe"}
        assert entries[0].labels["failed_filters"] == "fringe,mainstream_media"

    def test_a_hard_lambda_error_is_an_error_entry(self):
        entries = _lambda_entries({"selected": {}, "error": "500 from lambda server"})
        assert any(e.level == "error" and "500 from lambda server" in e.message for e in entries)

    def test_warnings_still_become_entries(self):
        entries = _lambda_entries(
            {"selected": {}, "warnings": ["Aversion to mainstream media channels ignored"]}
        )
        assert any("mainstream media" in e.message for e in entries)

    def test_truncation_is_announced_never_silent(self):
        """The old code sliced ``warnings[:5]`` and said nothing.

        A warning list that is cut short without saying so is indistinguishable
        from a short warning list.
        """
        warnings = [f"warning {i}" for i in range(MAX_LAMBDA_WARNING_ENTRIES + 3)]
        entries = _lambda_entries({"selected": {}, "warnings": warnings})

        truncation = [e for e in entries if e.labels.get("truncated")]
        assert len(truncation) == 1
        assert "3 further lambda warning(s) not shown" in truncation[0].message
        assert str(len(warnings)) in truncation[0].message

    def test_no_truncation_notice_when_nothing_was_dropped(self):
        warnings = [f"warning {i}" for i in range(MAX_LAMBDA_WARNING_ENTRIES)]
        entries = _lambda_entries({"selected": {}, "warnings": warnings})
        assert not [e for e in entries if e.labels.get("truncated")]

    def test_a_payload_predating_the_contract_still_works(self):
        """Old HAR zips have neither ``degraded`` nor ``selection_mode``."""
        entries = _lambda_entries({"selected": {"id": "x"}, "warnings": ["something odd"]})
        # status.lambda_is_degraded falls back to "there were warnings".
        assert status.lambda_is_degraded({"warnings": ["something odd"]}) is True
        assert entries[0].level == "warning"
        assert "selection_mode" not in entries[0].labels

    def test_every_entry_carries_the_job_id(self):
        entries = _lambda_entries(
            {"selected": {}, "selection_mode": "random", "warnings": ["a", "b"]}
        )
        assert {e.job_id for e in entries} == {5}


# =============================================================================
# Severity ladder (file-parsing fallback)
# =============================================================================


class TestSeverityLadder:
    @pytest.mark.parametrize("severe", ["alert", "critical", "emergency"])
    def test_laravels_worst_levels_outrank_error(self, severe):
        """They used to rank BELOW debug.

        The old table was ``{"debug": 0, "info": 1, "warning": 2, "warn": 2,
        "error": 3}`` and unknown levels fell to its ``.get(..., 0)`` default. So
        ALERT, CRITICAL and EMERGENCY -- the three most severe things Laravel can
        say, and ALERT is in the live index right now -- were filtered out of
        ``logs errors`` and out of every ``--level`` query. Only the worst lines
        were invisible.
        """
        assert LOG_LEVEL_SEVERITY[severe] > LOG_LEVEL_SEVERITY["error"]
        assert level_at_least(severe, "error")
        assert level_at_least(severe.upper(), "error")

    def test_the_ladder_is_ordered(self):
        ordered = [
            "trace",
            "debug",
            "info",
            "notice",
            "warning",
            "error",
            "critical",
            "alert",
            "emergency",
        ]
        ranks = [LOG_LEVEL_SEVERITY[name] for name in ordered]
        assert ranks == sorted(ranks)
        assert len(set(ranks)) == len(ranks)

    @pytest.mark.parametrize(
        "pair",
        [("warn", "warning"), ("err", "error"), ("crit", "critical"), ("emerg", "emergency")],
    )
    def test_synonyms_share_a_rank(self, pair):
        assert LOG_LEVEL_SEVERITY[pair[0]] == LOG_LEVEL_SEVERITY[pair[1]]

    def test_a_less_severe_line_is_still_filtered_out(self):
        assert not level_at_least("info", "error")
        assert not level_at_least("warning", "error")

    def test_an_unrankable_minimum_admits_everything(self):
        """Better to over-show than to silently drop on a level nobody can rank."""
        assert level_at_least("info", "verbose-ish")

    def test_an_unrankable_line_level_is_dropped_when_a_floor_was_asked_for(self):
        assert not level_at_least("", "error")


# =============================================================================
# Span duration (trace job)
# =============================================================================


class TestSpanBounds:
    """``trace job 3943`` reported 329.4 MINUTES for a job that ran 0.11 s.

    The span was min/max of the MATCHED LOG TIMESTAMPS, and the matched lines were
    the API's per-request context logging for every observation of that job (see
    TestObserverEffect). So the number grew every time anyone looked -- the more it
    was investigated, the more wrong the answer became.
    """

    # Exactly as the API sends them, from GET /api/v1/jobs/3943 on 2026-08-27.
    JOB_3943 = {
        "id": 3943,
        "status_code": "failure",
        "started_at": "2026-08-27T07:54:02.884+0200",
        "finished_at": "2026-08-27T07:54:02.994+0200",
        "created_at": "2026-08-27T07:09:31+02:00",
    }

    @staticmethod
    def _spread_logs() -> list[LogEntry]:
        """Log lines spread over 5.5 hours, as the observer's own traffic was."""
        base = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
        return [
            LogEntry(
                timestamp=base + timedelta(hours=hours),
                component="api",
                level="debug",
                message="Throttling request with IP",
            )
            for hours in (0, 1.25, 3, 5.5)
        ]

    def test_the_api_record_wins_over_widely_spread_logs(self):
        start, end = span_bounds(self.JOB_3943, self._spread_logs())
        assert (end - start).total_seconds() == pytest.approx(0.110, abs=1e-6)

    def test_the_duration_does_not_grow_when_more_logs_are_matched(self):
        """The defining symptom: observing the job changed its reported duration."""
        first = span_bounds(self.JOB_3943, self._spread_logs()[:1])
        later = span_bounds(self.JOB_3943, self._spread_logs())
        assert first == later

    def test_log_timestamps_are_used_only_when_the_api_has_none(self):
        start, end = span_bounds({"id": 1}, self._spread_logs())
        assert start is not None and end is not None
        assert (end - start) == timedelta(hours=5.5)

    def test_no_api_bounds_and_no_logs_is_no_duration(self):
        assert span_bounds({"id": 1}, []) == (None, None)
        assert span_bounds(None, None) == (None, None)

    def test_a_running_job_has_no_end_rather_than_a_borrowed_one(self):
        """A missing finish is a real answer. Back-filling it from an unrelated log
        line is what produced 329 minutes in the first place."""
        start, end = span_bounds(
            {"started_at": "2026-08-27T07:54:02.884+0200", "status_code": "running"},
            self._spread_logs(),
        )
        assert start is not None and end is None

    def test_the_job_span_uses_the_api_record(self):
        builder = TraceBuilder.__new__(TraceBuilder)

        class _Result:
            entries = TestSpanBounds._spread_logs()

        class _Aggregator:
            def query(self, **kwargs):
                return _Result()

        builder._log_aggregator = _Aggregator()
        span = builder._build_job_span(dict(self.JOB_3943, name="Lambda Select"), job_id=3943)
        assert span.duration_ms == pytest.approx(110.0, abs=0.001)
        assert span.status is SpanStatus.FAILED


class TestApiTimestampParsing:
    @pytest.mark.parametrize(
        "given,expected_offset_hours",
        [
            # Jobs: no colon in the offset. Python only accepted this from 3.11,
            # and this package supports 3.10.
            ("2026-08-27T07:54:02.884+0200", 2),
            # Queues: colon. Same endpoint, same response, different spelling.
            ("2026-08-27T07:09:17+02:00", 2),
            ("2026-06-19T06:36:53.000000Z", 0),
        ],
    )
    def test_every_shape_the_api_actually_sends(self, given, expected_offset_hours):
        parsed = parse_api_timestamp(given)
        assert parsed is not None and parsed.utcoffset() is not None
        assert parsed.utcoffset() == timedelta(hours=expected_offset_hours)

    def test_the_result_is_always_aware(self):
        """Naive would raise on subtraction against Loki's aware timestamps."""
        assert parse_api_timestamp("2026-08-27 07:54:02").tzinfo is not None

    @pytest.mark.parametrize("given", [None, "", "   ", "not a timestamp", 17, []])
    def test_junk_is_none_rather_than_an_exception(self, given):
        assert parse_api_timestamp(given) is None


# =============================================================================
# promtail contract
# =============================================================================


def _render_promtail_template(template: str, value: str) -> str:
    """Evaluate the one Go-template shape this config uses, in Python.

    Deliberately narrow: it understands ``{{ $v := ToLower .Value }}`` plus a
    single ``{{ if eq $v "x" }}a{{ else }}b{{ end }}``, and raises on anything
    else rather than quietly returning a wrong answer. The real rendering is
    promtail's; this exists so the mapping is pinned by a unit test as well.
    """
    match = re.fullmatch(
        r"\{\{ \$(\w+) := ToLower \.Value \}\}"
        r"\{\{ if eq \$\1 \"(?P<needle>[^\"]+)\" \}\}(?P<then>[^{]*)"
        r"\{\{ else \}\}\{\{ \$\1 \}\}\{\{ end \}\}",
        template.strip(),
    )
    if not match:
        raise AssertionError(f"unrecognised level template, update this helper: {template!r}")
    lowered = value.lower()
    return match.group("then") if lowered == match.group("needle") else lowered


@pytest.fixture(scope="module")
def promtail_config() -> dict:
    with open(PROMTAIL_CONFIG) as handle:
        loaded: dict = yaml.safe_load(handle)
    return loaded


def _scrape(config: dict, job_name: str) -> dict:
    return next(s for s in config["scrape_configs"] if s["job_name"] == job_name)


def _level_templates(scrape: dict) -> list[str]:
    return [
        stage["template"]["template"]
        for stage in scrape.get("pipeline_stages") or []
        if "template" in stage and stage["template"].get("source") == "level"
    ]


def _scrapes_labelling_a_parsed_level(config: dict) -> list[dict]:
    """Scrapes that derive a ``level`` label from the log line.

    Excludes the two that pin a constant label (mysql-slow, mysql-general): there
    is nothing to normalise there, and both already write a canonical value.
    """
    found = []
    for scrape in config["scrape_configs"]:
        stages = scrape.get("pipeline_stages") or []
        labels_level = any("labels" in s and "level" in (s["labels"] or {}) for s in stages)
        if labels_level:
            found.append(scrape)
    return found


class TestPromtailContract:
    @pytest.mark.parametrize("job_name", ["primemover-api", "primemover-runner"])
    def test_level_label_is_lowercased(self, promtail_config, job_name):
        templates = _level_templates(_scrape(promtail_config, job_name))
        assert any(
            "ToLower" in t for t in templates
        ), f"{job_name} must lowercase its level label like the apache/mysql/php scrapes"

    def test_every_scrape_that_labels_a_level_normalises_it(self, promtail_config):
        """One value per level, across the whole file.

        Logback says WARN, PHP-FPM says WARNING, Apache says warn, MySQL says
        Warning. Lowercasing alone left the runner labelling its warnings ``warn``
        while every other scrape said ``warning`` -- the same disagreement in a new
        spelling, which is the exact class of bug this file's history is made of.
        """
        for scrape in _scrapes_labelling_a_parsed_level(promtail_config):
            templates = _level_templates(scrape)
            assert templates, f"{scrape['job_name']} labels a level without normalising it"
            assert any(
                "warning" in t for t in templates
            ), f"{scrape['job_name']} does not fold warn -> warning"

    def test_the_normalising_template_maps_warn_to_warning(self, promtail_config):
        """Assert on the rendered semantics, not on the template text.

        Verified against promtail 3.4.1 itself with ``-dry-run`` on 2026-08-27:
        a Logback ``WARN`` line came out labelled ``level="warning"``, matching the
        API's ``WARNING``, and ``CRITICAL`` came out ``critical``.
        """
        template = _level_templates(_scrape(promtail_config, "primemover-runner"))[0]
        for value in ("WARN", "warn", "Warn"):
            assert _render_promtail_template(template, value) == "warning"
        for value, expected in (
            ("ERROR", "error"),
            ("INFO", "info"),
            ("CRITICAL", "critical"),
            ("WARNING", "warning"),
        ):
            assert _render_promtail_template(template, value) == expected

    def test_the_normalised_value_is_the_one_queries_ask_for(self, promtail_config):
        """The label promtail writes and the label the query layer looks for have
        to be the same string, or this fix is only half done."""
        template = _level_templates(_scrape(promtail_config, "primemover-runner"))[0]
        written = _render_promtail_template(template, "WARN")
        matcher = level_label_matcher("warning")
        body = matcher[len('level=~"') : -1]
        assert re.fullmatch(body, written)

    def test_a_level_template_writes_back_into_level(self, promtail_config):
        """A template stage stores its result in ``source``. ``source: level_char``
        therefore produced no ``level`` at all, and the labels stage after it
        silently labelled nothing -- redis lines carried no level for as long as
        that stage has existed."""
        for scrape in _scrapes_labelling_a_parsed_level(promtail_config):
            for stage in scrape.get("pipeline_stages") or []:
                if "template" in stage:
                    assert stage["template"]["source"] == "level", scrape["job_name"]

    def test_runner_extracts_job_id_from_the_mdc(self, promtail_config):
        stages = _scrape(promtail_config, "primemover-runner")["pipeline_stages"]
        expressions = [s["regex"]["expression"] for s in stages if "regex" in s]
        assert any("job_id" in e for e in expressions)

    def test_the_runner_job_id_regex_cannot_match_joblist_id(self, promtail_config):
        stages = _scrape(promtail_config, "primemover-runner")["pipeline_stages"]
        expression = next(
            s["regex"]["expression"]
            for s in stages
            if "regex" in s and "job_id" in s["regex"]["expression"]
        )
        pattern = re.compile(expression.replace("(?P<job_id>", "("))
        assert not pattern.search("joblist.id=844")
        assert pattern.search("job.id=3944").group(1) == "3944"

    @pytest.mark.parametrize("job_name", ["primemover-api", "primemover-runner"])
    def test_correlation_ids_never_become_labels(self, promtail_config, job_name):
        """job_id as a Loki label is a cardinality bomb: one stream per job.

        The ids stay in the extracted map; filtering happens in the query layer as
        a line filter over the correlation text.
        """
        stages = _scrape(promtail_config, job_name)["pipeline_stages"]
        labelled: set[str] = set()
        for stage in stages:
            if "labels" in stage:
                labelled.update(stage["labels"] or {})
        assert labelled & {"experiment_id", "crawler_id", "queue_id", "job_id"} == set()

    def test_the_runner_level_regex_still_survives_a_longer_mdc(self, promtail_config):
        """The MDC sits between the timestamp and the level, and just grew.

        This regex has already been wrong once, leaving every runner line with no
        level label at all -- so it is re-checked against the new MDC content.
        """
        stages = _scrape(promtail_config, "primemover-runner")["pipeline_stages"]
        expression = next(
            s["regex"]["expression"]
            for s in stages
            if "regex" in s
            and "source" not in s["regex"]
            and "timestamp" in s["regex"]["expression"]
        )
        match = re.match(expression, RUNNER_LINE_CANONICAL)
        assert match is not None
        assert match.group("level") == "WARN"
        assert "job.id=3944" in match.group("mdc")

    def test_the_runner_timestamp_keeps_its_offset(self, promtail_config):
        """Parsing the local time with a zone-less layout got every line rejected
        as "timestamp too new"; the offset must stay in both regex and layout."""
        stages = _scrape(promtail_config, "primemover-runner")["pipeline_stages"]
        timestamp_stage = next(s["timestamp"] for s in stages if "timestamp" in s)
        assert timestamp_stage["format"].endswith("-07:00")

    def test_the_api_timestamp_stays_in_zurich(self, promtail_config):
        """The API logs in CET; Loki is UTC. Dropping this makes every API line
        land two hours off."""
        stages = _scrape(promtail_config, "primemover-api")["pipeline_stages"]
        timestamp_stage = next(s["timestamp"] for s in stages if "timestamp" in s)
        assert timestamp_stage["location"] == "Europe/Zurich"


# =============================================================================
# MCP API token caching
# =============================================================================


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "{}"

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Counts logins so the storm is measured, not assumed."""

    def __init__(self, token: str = "tok-1", accept: set[str] | None = None):
        self.logins = 0
        self.gets = 0
        self.token = token
        self.accept = accept if accept is not None else {token}

    def post(self, url, **kwargs):
        assert url.endswith("/login")
        self.logins += 1
        return _FakeResponse(200, {"access_token": self.token})

    def get(self, url, headers=None, params=None):
        self.gets += 1
        presented = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        if presented not in self.accept:
            return _FakeResponse(401)
        return _FakeResponse(200, {"data": {"id": 3943}})


class TestMcpTokenCache:
    """``_api_request`` used to POST /login before every single call.

    One ``trace experiment`` or ``pm_compose_run`` is dozens of API calls, so a
    single MCP tool call meant dozens of logins -- each burning a bcrypt
    verification and writing two more ``Throttling request with IP`` lines into the
    log stream that ``trace job`` then has to filter back out. The login storm was
    polluting the evidence it was being used to fetch.

    Measured against the live API on 2026-08-27: 10 calls to ``GET jobs/3943``
    produced 10 logins before, 1 after.
    """

    @pytest.fixture
    def api(self, monkeypatch):
        from orchestrator.mcp_server import server as mcp_server

        mcp_server._clear_token_cache()

        class _Settings:
            pm_api_base_url = "http://localhost:8090/api/v1"
            pm_api_email = "admin@primemover.example"
            pm_api_password = "testpass"

        # One instance, as ``get_settings``' lru_cache hands out in a live server.
        settings = _Settings()
        monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
        yield mcp_server
        mcp_server._clear_token_cache()

    def _install(self, monkeypatch, mcp_server, client):
        import contextlib

        @contextlib.contextmanager
        def _fake_client(*args, **kwargs):
            yield client

        import httpx

        monkeypatch.setattr(httpx, "Client", _fake_client)

    def test_one_login_serves_many_calls(self, api, monkeypatch):
        client = _FakeClient()
        self._install(monkeypatch, api, client)
        for _ in range(10):
            api._api_request("GET", "jobs/3943")
        assert client.gets == 10
        assert client.logins == 1

    def test_a_401_refreshes_the_token_once(self, api, monkeypatch):
        """A token revoked or expired server-side must self-heal, not poison every
        tool until the process restarts."""
        client = _FakeClient(token="tok-good", accept={"tok-good"})
        self._install(monkeypatch, api, client)
        api._api_request("GET", "jobs/3943")
        assert client.logins == 1

        # Poison the cache exactly as a server-side revocation would.
        settings, _token, deadline = api._TOKEN_CACHE
        api._TOKEN_CACHE = (settings, "tok-stale", deadline)

        body = api._api_request("GET", "jobs/3943")
        assert body["data"]["id"] == 3943
        assert client.logins == 2

    def test_a_persistent_401_does_not_become_a_login_storm(self, api, monkeypatch):
        """Retrying past one refresh would turn a wrong password into the very
        storm this cache exists to prevent."""
        client = _FakeClient(token="tok-rejected", accept=set())
        self._install(monkeypatch, api, client)
        with pytest.raises(Exception, match="401"):
            api._api_request("GET", "jobs/3943")
        assert client.logins == 2
        assert client.gets == 2

    def test_an_expired_cache_entry_logs_in_again(self, api, monkeypatch):
        client = _FakeClient()
        self._install(monkeypatch, api, client)
        api._api_request("GET", "jobs/3943")
        settings, token, _deadline = api._TOKEN_CACHE
        api._TOKEN_CACHE = (settings, token, 0.0)  # deadline in the past
        api._api_request("GET", "jobs/3943")
        assert client.logins == 2

    def test_a_failed_login_leaves_no_cached_token(self, api, monkeypatch):
        class _Rejecting(_FakeClient):
            def post(self, url, **kwargs):
                self.logins += 1
                return _FakeResponse(403)

        client = _Rejecting()
        self._install(monkeypatch, api, client)
        with pytest.raises(Exception, match="authentication failed"):
            api._api_request("GET", "jobs/3943")
        assert api._TOKEN_CACHE is None

    def test_a_new_settings_instance_gets_a_new_token(self, api, monkeypatch):
        """The token is derived from the credentials in Settings, so its lifetime is
        that object's. Reloading config must not replay a token minted from
        credentials that are no longer in force."""
        client = _FakeClient()
        self._install(monkeypatch, api, client)
        api._api_request("GET", "jobs/3943")

        class _OtherSettings:
            pm_api_base_url = "http://localhost:8090/api/v1"
            pm_api_email = "admin@primemover.example"
            pm_api_password = "rotated"

        other = _OtherSettings()
        monkeypatch.setattr(api, "get_settings", lambda: other)
        api._api_request("GET", "jobs/3943")
        assert client.logins == 2
