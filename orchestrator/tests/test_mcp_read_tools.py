"""Behavioural tests for the read/observe MCP tools in ``orchestrator.mcp_server.server``.

These tools are the entire evidence base an LLM has when it is asked "why did run 40
fail?". If one of them silently changes shape -- drops the ``data`` envelope, renames a
field, swallows an outage and reports "no failures" -- the model does not error, it
confidently reasons from wrong data. So the assertions here are on decoded values and
payload shapes, not on "it returned a string".

Hermeticity
-----------
Nothing in this file may touch the network. The read tools reach out through exactly
four seams, and every test closes all of the ones its tool uses:

* ``server._api_request``          -- the authenticated PrimeMover API client
* ``httpx.get`` / ``httpx.post``   -- used directly by ``query_logs``, ``system_status``,
  ``summarize_experiment`` and ``compare_experiments(type="api")``
* ``server.query_logs``            -- the in-process seam for the two tools that
  aggregate it (``get_error_summary``, ``find_failed_executions``)
* ``TraceBuilder._api_get`` and ``TraceBuilder.log_aggregator`` -- see the comment on
  ``TestTraceTools`` for why the seam was chosen there rather than at ``TraceBuilder``
  itself.

``@mcp.tool()`` leaves the decorated function a plain callable, so the tools are called
directly and their JSON string return value is decoded with ``json.loads``.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import yaml

from orchestrator.clients.loki import LogEntry
from orchestrator.logs.aggregator import AggregatedResult
from orchestrator.mcp_server import server as s
from orchestrator.tracing.trace_builder import TraceBuilder

# =============================================================================
# Test doubles
# =============================================================================


class FakeApi:
    """Stands in for ``server._api_request``.

    Routes on the endpoint string so a single instance can serve a tool that calls
    several endpoints (``list_library``), and records every call so the request the tool
    *made* can be asserted alongside the response it produced.
    """

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        default: Any = None,
        errors: dict[str, Exception] | None = None,
        raises: Exception | None = None,
    ):
        self.responses = responses or {}
        self.default = default if default is not None else {"data": []}
        self.errors = errors or {}
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append({"method": method, "endpoint": endpoint, "data": data, "params": params})
        if self.raises is not None:
            raise self.raises
        if endpoint in self.errors:
            raise self.errors[endpoint]
        return self.responses.get(endpoint, self.default)

    @property
    def endpoints(self) -> list[str]:
        return [c["endpoint"] for c in self.calls]


class FakeResponse:
    """Minimal stand-in for an ``httpx.Response``."""

    def __init__(self, status_code: int = 200, payload: Any = None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self) -> Any:
        return self._payload


class FakeHttp:
    """Records ``httpx.get`` / ``httpx.post`` calls and replays canned responses.

    ``handler`` receives the URL and keyword arguments and returns a ``FakeResponse``
    (or raises, to simulate a connection failure).
    """

    def __init__(self, handler: Callable[..., FakeResponse]) -> None:
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.handler(url, **kwargs)

    @property
    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]


class FakeSettings:
    """Only the attributes the read tools actually read."""

    env = "test"
    grafana_url = "http://grafana.test:3000"
    loki_url = "http://loki.test:3100"
    prometheus_url = "http://prom.test:9090"
    tempo_url = "http://tempo.test:3200"
    pm_api_base_url = "http://localhost:8090/api/v1"
    pm_api_email = "tester@example.org"
    pm_api_password = "s3cret"
    lambda_function_name = "bot-decision-yt"


T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_entry(
    offset_s: float = 0,
    component: str = "runner",
    level: str = "info",
    message: str = "hello",
) -> LogEntry:
    return LogEntry(
        timestamp=T0 + timedelta(seconds=offset_s),
        component=component,
        level=level,
        message=message,
    )


def loki_payload(streams: list[tuple[dict[str, str], list[tuple[str, str]]]]) -> dict:
    """Build a Loki ``query_range`` response body."""
    return {
        "data": {
            "result": [
                {"stream": labels, "values": [list(v) for v in values]}
                for labels, values in streams
            ]
        }
    }


@pytest.fixture
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> FakeSettings:
    """Make every ``get_settings()`` call inside server.py return the stub."""
    settings = FakeSettings()
    monkeypatch.setattr(s, "get_settings", lambda: settings)
    return settings


@pytest.fixture(autouse=True)
def block_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt and braces: no test in this module may open a socket.

    The per-test doubles below are the intended seams, but this repo drives live
    research infrastructure -- a read tool that slipped past a double and reached
    localhost:8090 would be querying (and authenticating against) the real API. Blocking
    at the socket layer turns any such slip into a loud failure instead of a live call.
    """

    def _blocked(*args: Any, **kwargs: Any):
        raise AssertionError("test attempted to open a network socket")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly rather than dial out if a tool reaches a seam a test forgot to close."""

    def _boom(*args: Any, **kwargs: Any):
        raise AssertionError(f"test attempted a real HTTP call: {args!r} {kwargs!r}")

    monkeypatch.setattr(httpx, "get", _boom)
    monkeypatch.setattr(httpx, "post", _boom)
    monkeypatch.setattr(httpx, "Client", _boom)
    monkeypatch.setattr(s, "_api_request", _boom)


# =============================================================================
# Listing tools
# =============================================================================


class TestListExperiments:
    def test_returns_the_api_envelope_verbatim(self, monkeypatch, no_network):
        payload = {
            "data": [
                {"id": 1, "name": "Study A", "description": "first", "crawler_count": 3},
                {"id": 2, "name": "Study B", "description": "second", "crawler_count": 0},
            ],
            "meta": {"current_page": 2, "per_page": 5, "total": 7},
        }
        api = FakeApi(responses={"experiments": payload})
        monkeypatch.setattr(s, "_api_request", api)

        out = json.loads(s.list_experiments(page=2, per_page=5))

        # The envelope is passed through untouched -- callers index result["data"].
        assert out == payload
        assert [e["name"] for e in out["data"]] == ["Study A", "Study B"]

    def test_pagination_arguments_reach_the_api(self, monkeypatch, no_network):
        api = FakeApi()
        monkeypatch.setattr(s, "_api_request", api)

        s.list_experiments(page=3, per_page=50)

        assert api.calls == [
            {
                "method": "GET",
                "endpoint": "experiments",
                "data": None,
                "params": {"page": 3, "per_page": 50},
            }
        ]

    def test_defaults_are_page_one_fifteen_per_page(self, monkeypatch, no_network):
        api = FakeApi()
        monkeypatch.setattr(s, "_api_request", api)

        s.list_experiments()

        assert api.calls[0]["params"] == {"page": 1, "per_page": 15}

    def test_empty_page_is_an_empty_list_not_an_error(self, monkeypatch, no_network):
        api = FakeApi(responses={"experiments": {"data": [], "meta": {"total": 0}}})
        monkeypatch.setattr(s, "_api_request", api)

        out = json.loads(s.list_experiments())

        assert out["data"] == []
        assert "error" not in out

    def test_api_failure_becomes_a_json_error(self, monkeypatch, no_network):
        monkeypatch.setattr(
            s, "_api_request", FakeApi(raises=Exception("API request failed: 500 boom"))
        )

        out = json.loads(s.list_experiments())

        # Must be JSON, not a raised exception: an MCP tool that raises gives the caller
        # a protocol error instead of a readable message.
        assert out["error"] == "API request failed: 500 boom"


class TestListAllExperiments:
    def test_uses_the_unpaginated_endpoint(self, monkeypatch, no_network):
        api = FakeApi(
            responses={"experiments/experiments-list": {"data": [{"id": 9, "name": "All"}]}}
        )
        monkeypatch.setattr(s, "_api_request", api)

        out = json.loads(s.list_all_experiments())

        assert api.endpoints == ["experiments/experiments-list"]
        assert api.calls[0]["params"] is None
        assert out["data"][0]["id"] == 9

    def test_api_failure_becomes_a_json_error(self, monkeypatch, no_network):
        monkeypatch.setattr(s, "_api_request", FakeApi(raises=Exception("connection refused")))

        assert json.loads(s.list_all_experiments()) == {"error": "connection refused"}


class TestListCrawlers:
    def test_no_status_lists_all_crawlers(self, monkeypatch, no_network):
        api = FakeApi(responses={"crawlers": {"data": [{"id": 4, "name": "bot"}]}})
        monkeypatch.setattr(s, "_api_request", api)

        out = json.loads(s.list_crawlers())

        assert api.endpoints == ["crawlers"]
        assert out["data"][0]["id"] == 4

    @pytest.mark.parametrize(
        "status",
        ["processed", "unprocessed", "reviewed", "inactive", "testing", "protected"],
    )
    def test_status_selects_the_status_specific_endpoint(self, monkeypatch, no_network, status):
        api = FakeApi()
        monkeypatch.setattr(s, "_api_request", api)

        s.list_crawlers(status=status)

        assert api.endpoints == [f"crawlers-{status}"]

    def test_unknown_status_is_rejected_without_calling_the_api(self, monkeypatch, no_network):
        api = FakeApi()
        monkeypatch.setattr(s, "_api_request", api)

        out = json.loads(s.list_crawlers(status="deleted"))

        assert "Invalid status" in out["error"]
        # The message has to name the alternatives or the model cannot self-correct.
        assert "processed" in out["error"] and "protected" in out["error"]
        assert api.calls == [], "an invalid status must not reach the API"

    def test_api_failure_becomes_a_json_error(self, monkeypatch, no_network):
        monkeypatch.setattr(s, "_api_request", FakeApi(raises=Exception("401 Unauthorized")))

        assert "401" in json.loads(s.list_crawlers())["error"]


class TestListQueues:
    def test_no_status_lists_all_queues(self, monkeypatch, no_network):
        api = FakeApi(responses={"queues": {"data": [{"id": 12, "crawler_id": 4}]}})
        monkeypatch.setattr(s, "_api_request", api)

        out = json.loads(s.list_queues())

        assert api.endpoints == ["queues"]
        assert out["data"][0]["crawler_id"] == 4

    @pytest.mark.parametrize("status", ["processed", "unprocessed", "reviewed", "unreviewed"])
    def test_status_selects_the_status_specific_endpoint(self, monkeypatch, no_network, status):
        api = FakeApi()
        monkeypatch.setattr(s, "_api_request", api)

        s.list_queues(status=status)

        assert api.endpoints == [f"queues-{status}"]

    @pytest.mark.parametrize("status", ["testing", "protected", "inactive"])
    def test_crawler_only_statuses_are_rejected_for_queues(self, monkeypatch, no_network, status):
        """The two vocabularies differ; queues must not silently accept crawler statuses."""
        api = FakeApi()
        monkeypatch.setattr(s, "_api_request", api)

        out = json.loads(s.list_queues(status=status))

        assert "Invalid status" in out["error"]
        assert "unreviewed" in out["error"]
        assert api.calls == []


class TestListLibrary:
    ROWS = {
        "agents": {
            "data": [
                {
                    "id": 1,
                    "name": "mlx-agent",
                    "description": "browser identity",
                    "type": "mlx",
                    "active": True,
                    "api_token": "SHOULD-NOT-LEAK",
                }
            ]
        },
        "proxies": {"data": [{"id": 2, "name": "res-proxy", "type": "http", "active": True}]},
        "configurations": {"data": [{"id": 3, "name": "profile-a", "active": False}]},
    }

    def test_returns_the_three_library_kinds(self, monkeypatch, no_network):
        api = FakeApi(responses=self.ROWS)
        monkeypatch.setattr(s, "_api_request", api)

        out = json.loads(s.list_library())

        assert set(out) == {"agents", "proxies", "configurations"}
        assert api.endpoints == ["agents", "proxies", "configurations"]
        assert all(c["params"] == {"per_page": 100} for c in api.calls)

    def test_only_the_whitelisted_fields_are_projected(self, monkeypatch, no_network):
        monkeypatch.setattr(s, "_api_request", FakeApi(responses=self.ROWS))

        out = json.loads(s.list_library())

        agent = out["agents"][0]
        assert agent == {
            "id": 1,
            "name": "mlx-agent",
            "description": "browser identity",
            "type": "mlx",
            "active": True,
        }
        assert "api_token" not in agent

    def test_missing_fields_are_omitted_rather_than_nulled(self, monkeypatch, no_network):
        monkeypatch.setattr(s, "_api_request", FakeApi(responses=self.ROWS))

        out = json.loads(s.list_library())

        # configurations row has no description/type at all.
        assert out["configurations"][0] == {"id": 3, "name": "profile-a", "active": False}

    def test_one_failing_endpoint_does_not_lose_the_others(self, monkeypatch, no_network):
        api = FakeApi(
            responses=self.ROWS,
            errors={"proxies": Exception("API request failed: 500")},
        )
        monkeypatch.setattr(s, "_api_request", api)

        out = json.loads(s.list_library())

        assert out["proxies"] == {"error": "API request failed: 500"}
        assert out["agents"][0]["id"] == 1
        assert out["configurations"][0]["id"] == 3

    def test_a_bare_list_response_is_tolerated(self, monkeypatch, no_network):
        """Some list endpoints answer with a bare array instead of {"data": [...]}."""
        api = FakeApi(default=[{"id": 5, "name": "bare"}])
        monkeypatch.setattr(s, "_api_request", api)

        out = json.loads(s.list_library())

        assert out["agents"] == [{"id": 5, "name": "bare"}]


# =============================================================================
# Single-entity getters
# =============================================================================


GETTERS = [
    ("get_experiment", "experiments", "experiment_id"),
    ("get_crawler", "crawlers", "crawler_id"),
    ("get_queue", "queues", "queue_id"),
    ("get_job", "jobs", "job_id"),
]


class TestSingleEntityGetters:
    @pytest.mark.parametrize(("tool", "collection", "arg"), GETTERS)
    def test_fetches_the_entity_by_id(self, monkeypatch, no_network, tool, collection, arg):
        api = FakeApi(responses={f"{collection}/42": {"data": {"id": 42, "name": "thing"}}})
        monkeypatch.setattr(s, "_api_request", api)

        out = json.loads(getattr(s, tool)(42))

        assert api.calls[0]["method"] == "GET"
        assert api.endpoints == [f"{collection}/42"]
        # The {"data": ...} envelope is preserved -- callers index out["data"]["id"].
        assert out["data"]["id"] == 42

    @pytest.mark.parametrize(("tool", "collection", "arg"), GETTERS)
    @pytest.mark.parametrize("bad_id", [0, -1, 2147483648])
    def test_out_of_range_ids_are_rejected_before_the_api(
        self, monkeypatch, no_network, tool, collection, arg, bad_id
    ):
        api = FakeApi()
        monkeypatch.setattr(s, "_api_request", api)

        out = json.loads(getattr(s, tool)(bad_id))

        assert out["error"] == f"Validation error: {arg} must be between 1 and 2147483647"
        assert api.calls == []

    @pytest.mark.parametrize(("tool", "collection", "arg"), GETTERS)
    def test_non_integer_ids_are_rejected(self, monkeypatch, no_network, tool, collection, arg):
        api = FakeApi()
        monkeypatch.setattr(s, "_api_request", api)

        out = json.loads(getattr(s, tool)("42"))

        assert out["error"] == f"Validation error: {arg} must be an integer"
        assert api.calls == []

    @pytest.mark.parametrize(("tool", "collection", "arg"), GETTERS)
    def test_none_is_reported_as_required(self, monkeypatch, no_network, tool, collection, arg):
        api = FakeApi()
        monkeypatch.setattr(s, "_api_request", api)

        out = json.loads(getattr(s, tool)(None))

        assert out["error"] == f"{arg} is required"
        assert api.calls == []

    @pytest.mark.parametrize(("tool", "collection", "arg"), GETTERS)
    def test_api_failure_becomes_a_json_error(self, monkeypatch, no_network, tool, collection, arg):
        monkeypatch.setattr(
            s, "_api_request", FakeApi(raises=Exception("API request failed: 404 Not Found"))
        )

        out = json.loads(getattr(s, tool)(999))

        assert "404" in out["error"]
        assert "Validation error" not in out["error"]

    def test_get_job_returns_behaviours_and_status(self, monkeypatch, no_network):
        """The debug skill reads exactly these fields off get_job."""
        api = FakeApi(
            responses={
                "jobs/7": {
                    "data": {
                        "id": 7,
                        "type": "visitjob",
                        "status": "WARNING_JOBDATA",
                        "behaviors": [{"name": "url", "value": "https://youtube.com"}],
                    }
                }
            }
        )
        monkeypatch.setattr(s, "_api_request", api)

        job = json.loads(s.get_job(7))["data"]

        assert job["type"] == "visitjob"
        assert job["status"] == "WARNING_JOBDATA"
        assert job["behaviors"] == [{"name": "url", "value": "https://youtube.com"}]


# =============================================================================
# query_logs
# =============================================================================


class TestQueryLogs:
    @staticmethod
    def _patch_loki(monkeypatch, response: FakeResponse | Exception) -> FakeHttp:
        def handler(url: str, **kwargs: Any):
            if isinstance(response, Exception):
                raise response
            return response

        fake = FakeHttp(handler)
        monkeypatch.setattr(httpx, "get", fake)
        return fake

    def test_maps_loki_streams_to_flat_entries(self, monkeypatch, fake_settings):
        payload = loki_payload(
            [
                (
                    {"component": "runner", "level": "error"},
                    [("1700000000000000000", "job failed"), ("1700000000100000000", "retrying")],
                ),
                ({"component": "api", "level": "info"}, [("1700000000200000000", "ok")]),
            ]
        )
        self._patch_loki(monkeypatch, FakeResponse(200, payload))

        out = json.loads(s.query_logs())

        assert out["count"] == 3
        assert out["entries"][0] == {
            "timestamp": "1700000000000000000",
            "component": "runner",
            "level": "error",
            "message": "job failed",
        }
        assert [e["component"] for e in out["entries"]] == ["runner", "runner", "api"]

    def test_missing_stream_labels_become_unknown(self, monkeypatch, fake_settings):
        self._patch_loki(
            monkeypatch, FakeResponse(200, loki_payload([({}, [("1", "no labels here")])]))
        )

        entry = json.loads(s.query_logs())["entries"][0]

        assert entry["component"] == "unknown"
        assert entry["level"] == "unknown"

    def test_long_messages_are_truncated_to_500_chars(self, monkeypatch, fake_settings):
        self._patch_loki(monkeypatch, FakeResponse(200, loki_payload([({}, [("1", "x" * 600)])])))

        entry = json.loads(s.query_logs())["entries"][0]

        assert len(entry["message"]) == 500

    def test_count_reports_matches_and_entries_are_capped_at_limit(
        self, monkeypatch, fake_settings
    ):
        """``count`` is the number matched, ``entries`` is the truncated page.

        Pinned deliberately: the two do not have to agree, and a reader who assumes
        ``count == len(entries)`` will misreport how much was returned.
        """
        streams = [({}, [(str(i), f"line {i}") for i in range(5)])]
        self._patch_loki(monkeypatch, FakeResponse(200, loki_payload(streams)))

        out = json.loads(s.query_logs(limit=2))

        assert out["count"] == 5
        assert len(out["entries"]) == 2

    def test_empty_result_is_zero_entries_not_an_error(self, monkeypatch, fake_settings):
        self._patch_loki(monkeypatch, FakeResponse(200, loki_payload([])))

        assert json.loads(s.query_logs()) == {"count": 0, "entries": []}

    def test_builds_a_label_selector_from_component_and_level(self, monkeypatch, fake_settings):
        fake = self._patch_loki(monkeypatch, FakeResponse(200, loki_payload([])))

        s.query_logs(component="api", level="error")

        assert (
            fake.calls[0]["params"]["query"]
            == '{component="api",level=~"(?i)(?:error|err|critical|crit|alert|emergency|emerg|fatal)"}'
        )
        assert fake.urls == ["http://loki.test:3100/loki/api/v1/query_range"]

    def test_all_component_and_all_level_still_produce_a_valid_selector(
        self, monkeypatch, fake_settings
    ):
        """``{}`` is not "everything", it is a 400 from Loki.

        Loki rejects a selector whose every matcher can match the empty string, so
        the "all components" case has to be spelled with a real matcher. The old
        ``{}`` made every unscoped query fail, which the CLI reported as no results.
        """
        fake = self._patch_loki(monkeypatch, FakeResponse(200, loki_payload([])))

        s.query_logs(component="all", level="all")

        assert fake.calls[0]["params"]["query"] == '{component=~".+"}'

    def test_search_is_escaped_into_a_line_filter(self, monkeypatch, fake_settings):
        fake = self._patch_loki(monkeypatch, FakeResponse(200, loki_payload([])))

        s.query_logs(search='he said "boom"')

        # Quotes must be backslash-escaped or the LogQL string closes early --
        # this is the injection guard, not cosmetics.
        assert fake.calls[0]["params"]["query"] == '{component=~".+"} |~ "he said \\"boom\\""'

    def test_id_filters_are_line_filters_over_the_correlation_text(
        self, monkeypatch, fake_settings
    ):
        """``| json`` matched nothing: the log lines are not JSON documents.

        A Logback line is ``... correlationId=cr-7|jl-3 ...`` and a Laravel line is
        text with a JSON context appended, so the ``json`` stage dropped every entry
        and every id-filtered query came back empty -- which reads as "no errors".
        """
        fake = self._patch_loki(monkeypatch, FakeResponse(200, loki_payload([])))

        s.query_logs(experiment_id=15, crawler_id=7, queue_id=3, job_id=9)

        query = fake.calls[0]["params"]["query"]
        assert "| json" not in query
        # Both textual forms, and the doubled backslash a LogQL string literal needs
        # for a word boundary.
        assert '|~ "(?:\\\\bexp-15\\\\b|\\"experiment_id\\": ?15\\\\b)"' in query
        assert '|~ "(?:\\\\bcr-7\\\\b|\\"crawler_id\\": ?7\\\\b)"' in query
        # The queue matcher has to accept the runner's historical ``jl-`` spelling
        # as well as the canonical ``q-``.
        assert '|~ "(?:\\\\b(?:q|jl)-3\\\\b|\\"queue_id\\": ?3\\\\b)"' in query
        assert '|~ "(?:\\\\bj-9\\\\b|\\"job_id\\": ?9\\\\b)"' in query

    def test_time_range_is_sent_to_loki_as_an_explicit_window(self, monkeypatch, fake_settings):
        """Without start/end Loki defaults to the last hour.

        That is how ``find_failed_executions(time_range="24h")`` and
        ``get_error_summary(time_range="7d")`` both really searched 1h.
        """
        fake = self._patch_loki(monkeypatch, FakeResponse(200, loki_payload([])))

        s.query_logs(time_range="7d")

        params = fake.calls[0]["params"]
        span_seconds = (params["end"] - params["start"]) / 1e9
        assert abs(span_seconds - 7 * 24 * 3600) < 2

    def test_warning_level_also_matches_the_runners_uppercase_warn(
        self, monkeypatch, fake_settings
    ):
        """Logback writes ``WARN``; the apache/mysql/php scrapes write ``warning``.

        A matcher for the literal word "warning" never matched a single runner line.
        """
        fake = self._patch_loki(monkeypatch, FakeResponse(200, loki_payload([])))

        s.query_logs(component="runner", level="warning")

        assert fake.calls[0]["params"]["query"] == '{component="runner",level=~"(?i)warn(ing)?"}'

    @pytest.mark.parametrize(
        ("kwargs", "fragment"),
        [
            ({"component": "postgres"}, "Invalid component: postgres"),
            ({"time_range": "banana"}, "Invalid time_range format: banana"),
            ({"time_range": "1y"}, "Invalid time_range format: 1y"),
            ({"level": "trace"}, "Invalid level: trace"),
            ({"search": "x" * 501}, "search string too long"),
            ({"experiment_id": 0}, "experiment_id must be between 1"),
            ({"crawler_id": "7"}, "crawler_id must be an integer"),
        ],
    )
    def test_invalid_input_is_rejected_before_any_http_call(
        self, monkeypatch, fake_settings, kwargs, fragment
    ):
        fake = self._patch_loki(monkeypatch, FakeResponse(200, loki_payload([])))

        out = json.loads(s.query_logs(**kwargs))

        assert out["error"].startswith("Validation error: ")
        assert fragment in out["error"]
        assert fake.calls == [], "validation must happen before the query is sent"

    @pytest.mark.parametrize(("given", "expected"), [(0, 1), (-5, 1), (10001, 10000), (250, 250)])
    def test_limit_is_clamped_into_range(self, monkeypatch, fake_settings, given, expected):
        fake = self._patch_loki(monkeypatch, FakeResponse(200, loki_payload([])))

        s.query_logs(limit=given)

        assert fake.calls[0]["params"]["limit"] == expected

    def test_component_and_level_are_case_insensitive(self, monkeypatch, fake_settings):
        fake = self._patch_loki(monkeypatch, FakeResponse(200, loki_payload([])))

        s.query_logs(component="API", level="ERROR", time_range="24H")

        assert (
            fake.calls[0]["params"]["query"]
            == '{component="api",level=~"(?i)(?:error|err|critical|crit|alert|emergency|emerg|fatal)"}'
        )

    def test_non_200_from_loki_reports_the_status_code(self, monkeypatch, fake_settings):
        self._patch_loki(monkeypatch, FakeResponse(503, {}))

        assert json.loads(s.query_logs()) == {"error": "Loki query failed: 503"}

    def test_connection_failure_is_reported_as_json(self, monkeypatch, fake_settings):
        self._patch_loki(monkeypatch, httpx.ConnectError("All connection attempts failed"))

        out = json.loads(s.query_logs())

        assert "All connection attempts failed" in out["error"]


# =============================================================================
# Aggregators over query_logs
# =============================================================================


#: A timestamp inside every default query window, so scanned rows are "recent".
NOW_ISO = datetime.now(timezone.utc).isoformat()


class FakeQueryLogs:
    """Stands in for the module-level ``query_logs`` the aggregating tools call."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return json.dumps(self.payload)


class TestGetErrorSummary:
    def test_counts_errors_by_component(self, monkeypatch, no_network):
        entries = [
            {"component": "runner", "level": "error", "message": "chrome died"},
            {"component": "runner", "level": "error", "message": "chrome died"},
            {"component": "api", "level": "error", "message": "500 on /queues"},
        ]
        monkeypatch.setattr(s, "query_logs", FakeQueryLogs({"count": 3, "entries": entries}))

        out = json.loads(s.get_error_summary())

        assert out["total_errors"] == 3
        assert out["by_component"] == {"runner": 2, "api": 1}

    def test_top_messages_are_ranked_by_frequency(self, monkeypatch, no_network):
        entries = (
            [{"component": "runner", "message": "rare"}]
            + [{"component": "runner", "message": "common"} for _ in range(5)]
            + [{"component": "runner", "message": "medium"} for _ in range(3)]
        )
        monkeypatch.setattr(s, "query_logs", FakeQueryLogs({"entries": entries}))

        out = json.loads(s.get_error_summary())

        assert out["top_messages"][0] == {"message": "common", "count": 5}
        assert [m["message"] for m in out["top_messages"]] == ["common", "medium", "rare"]

    def test_top_messages_are_capped_at_ten(self, monkeypatch, no_network):
        entries = [{"component": "api", "message": f"msg-{i}"} for i in range(25)]
        monkeypatch.setattr(s, "query_logs", FakeQueryLogs({"entries": entries}))

        out = json.loads(s.get_error_summary())

        assert out["total_errors"] == 25
        assert len(out["top_messages"]) == 10

    def test_messages_are_grouped_on_their_first_100_chars(self, monkeypatch, no_network):
        prefix = "Job failed: " + "y" * 100
        entries = [
            {"component": "runner", "message": prefix + " tail-A"},
            {"component": "runner", "message": prefix + " tail-B"},
        ]
        monkeypatch.setattr(s, "query_logs", FakeQueryLogs({"entries": entries}))

        out = json.loads(s.get_error_summary())

        # Differing tails collapse into one bucket keyed by the truncated message.
        assert len(out["top_messages"]) == 1
        assert out["top_messages"][0]["count"] == 2
        assert len(out["top_messages"][0]["message"]) == 100

    def test_asks_only_for_error_level_logs(self, monkeypatch, no_network):
        fake = FakeQueryLogs({"entries": []})
        monkeypatch.setattr(s, "query_logs", fake)

        s.get_error_summary(time_range="7d", component="runner")

        assert fake.calls == [
            {"component": "runner", "time_range": "7d", "level": "error", "limit": 500}
        ]

    def test_no_errors_is_an_empty_summary(self, monkeypatch, no_network):
        monkeypatch.setattr(s, "query_logs", FakeQueryLogs({"count": 0, "entries": []}))

        out = json.loads(s.get_error_summary())

        assert out == {"total_errors": 0, "by_component": {}, "top_messages": []}

    def test_a_log_backend_failure_is_propagated_not_masked(self, monkeypatch, no_network):
        """If Loki is down the summary must say so rather than report zero errors."""
        monkeypatch.setattr(s, "query_logs", FakeQueryLogs({"error": "Loki query failed: 503"}))

        out = json.loads(s.get_error_summary())

        assert out == {"error": "Loki query failed: 503"}
        assert "total_errors" not in out


class TestFindFailedExecutions:
    """The tool now asks the API what actually happened, not the logs.

    The log-only implementation could only ever see runs that logged an ERROR
    line. A run that failed quietly, and every job that completed while silently
    degrading (the new ``warning`` state), were invisible -- which is exactly what
    step 1 of the primemover-debug skill relies on.
    """

    @staticmethod
    def _pages(rows_by_endpoint: dict[str, list[dict[str, Any]]]) -> FakeApi:
        """One page per endpoint, with the ``meta.last_page`` that stops the scan."""
        return FakeApi(
            responses={
                endpoint: {"data": rows, "meta": {"last_page": 1}}
                for endpoint, rows in rows_by_endpoint.items()
            },
            default={"data": [], "meta": {"last_page": 1}},
        )

    @staticmethod
    def _job(job_id: int, code: str, message: str = "", crawler_id: int = 5) -> dict[str, Any]:
        return {
            "id": job_id,
            "name": f"job-{job_id}",
            "type": "lambdasingleselectjob",
            "queue_id": 800,
            "queue": {"id": 800, "crawler_id": crawler_id},
            "status_code": code,
            "status_message": message,
            "started_at": NOW_ISO,
            "finished_at": NOW_ISO,
            "updated_at": NOW_ISO,
        }

    @staticmethod
    def _queue(queue_id: int, code: str, message: str = "", crawler_id: int = 5) -> dict[str, Any]:
        return {
            "id": queue_id,
            "name": f"run-{queue_id}",
            "crawler_id": crawler_id,
            "crawler": {"id": crawler_id, "experiment_id": 31},
            "status_code": code,
            "status_message": message,
            "started_at": NOW_ISO,
            "finished_at": NOW_ISO,
            "updated_at": NOW_ISO,
        }

    def test_reports_api_recorded_failures_and_warnings(self, monkeypatch, no_network):
        api = self._pages(
            {
                "jobs": [
                    self._job(1, "success"),
                    self._job(2, "failure", "job failed - chrome died"),
                    self._job(3, "warning", "job finished - selection fell back to random"),
                    self._job(4, "skipped", "previous task was not executed"),
                ],
                "queues": [self._queue(800, "failed")],
            }
        )
        monkeypatch.setattr(s, "_api_request", api)
        monkeypatch.setattr(s, "query_logs", FakeQueryLogs({"entries": []}))

        out = json.loads(s.find_failed_executions())
        ex = out["executions"]

        # failure + warning are reported; success and skipped are not.
        assert ex["total"] == 3
        assert ex["by_status"] == {"failure": 2, "warning": 1}
        assert [j["id"] for j in ex["jobs"]] == [2, 3]
        assert [q["id"] for q in ex["queues"]] == [800]

    def test_the_warning_state_is_reported_not_swallowed(self, monkeypatch, no_network):
        """The whole reason ``warning`` exists: it used to be reported as success."""
        api = self._pages(
            {"jobs": [self._job(7, "warning", "job finished - job data not written")]}
        )
        monkeypatch.setattr(s, "_api_request", api)
        monkeypatch.setattr(s, "query_logs", FakeQueryLogs({"entries": []}))

        out = json.loads(s.find_failed_executions())

        job = out["executions"]["jobs"][0]
        assert job["status_code"] == "warning"
        assert job["status_message"] == "job finished - job data not written"

    def test_legacy_status_spellings_are_normalised(self, monkeypatch, no_network):
        """``failed`` (queues) and ``skpped`` (the runner's typo) are real DB values."""
        api = self._pages({"queues": [self._queue(801, "failed"), self._queue(802, "skpped")]})
        monkeypatch.setattr(s, "_api_request", api)
        monkeypatch.setattr(s, "query_logs", FakeQueryLogs({"entries": []}))

        out = json.loads(s.find_failed_executions())
        ex = out["executions"]

        assert ex["by_status"] == {"failure": 1}
        assert [q["id"] for q in ex["queues"]] == [801]

    def test_failures_are_ordered_worst_first(self, monkeypatch, no_network):
        api = self._pages(
            {"jobs": [self._job(1, "warning"), self._job(2, "failure"), self._job(3, "warning")]}
        )
        monkeypatch.setattr(s, "_api_request", api)
        monkeypatch.setattr(s, "query_logs", FakeQueryLogs({"entries": []}))

        out = json.loads(s.find_failed_executions())

        assert [j["status_code"] for j in out["executions"]["jobs"]] == [
            "failure",
            "warning",
            "warning",
        ]

    def test_rows_outside_the_window_are_excluded(self, monkeypatch, no_network):
        stale = self._job(9, "failure")
        stale["started_at"] = stale["finished_at"] = "2020-01-01T00:00:00+00:00"
        # A bulk UPDATE bumps updated_at on rows that last ran years ago; judging the
        # window on it pulled those into a 24h query.
        stale["updated_at"] = NOW_ISO
        api = self._pages({"jobs": [self._job(1, "failure"), stale]})
        monkeypatch.setattr(s, "_api_request", api)
        monkeypatch.setattr(s, "query_logs", FakeQueryLogs({"entries": []}))

        out = json.loads(s.find_failed_executions(time_range="24h"))

        assert [j["id"] for j in out["executions"]["jobs"]] == [1]

    def test_experiment_filter_uses_the_experiments_crawlers(self, monkeypatch, no_network):
        api = FakeApi(
            responses={
                "experiments/31": {"data": {"id": 31, "crawlers": [{"id": 5}]}},
                "jobs": {
                    "data": [
                        self._job(1, "failure", crawler_id=5),
                        self._job(2, "failure", crawler_id=99),
                    ],
                    "meta": {"last_page": 1},
                },
            },
            default={"data": [], "meta": {"last_page": 1}},
        )
        monkeypatch.setattr(s, "_api_request", api)
        monkeypatch.setattr(s, "query_logs", FakeQueryLogs({"entries": []}))

        out = json.loads(s.find_failed_executions(experiment_id=31))

        assert [j["id"] for j in out["executions"]["jobs"]] == [1]

    def test_no_failures_is_an_empty_result_not_an_error(self, monkeypatch, no_network):
        monkeypatch.setattr(s, "_api_request", self._pages({}))
        monkeypatch.setattr(s, "query_logs", FakeQueryLogs({"count": 0, "entries": []}))

        out = json.loads(s.find_failed_executions())

        assert out["executions"]["total"] == 0
        assert out["executions"]["by_status"] == {}
        assert out["log_signal"]["total_errors"] == 0

    def test_an_api_outage_is_surfaced_not_reported_as_clean(self, monkeypatch, no_network):
        monkeypatch.setattr(s, "_api_request", FakeApi(raises=RuntimeError("API down")))
        monkeypatch.setattr(s, "query_logs", FakeQueryLogs({"entries": []}))

        out = json.loads(s.find_failed_executions())

        assert "API down" in out["executions"]["error"]
        assert out["executions"]["total"] is None

    def test_a_log_backend_failure_is_surfaced_in_its_own_section(self, monkeypatch, no_network):
        """A Loki outage must not read as "no errors" -- and must not hide the API result."""
        monkeypatch.setattr(s, "_api_request", self._pages({"jobs": [self._job(1, "failure")]}))
        monkeypatch.setattr(s, "query_logs", FakeQueryLogs({"error": "Loki query failed: 503"}))

        out = json.loads(s.find_failed_executions())

        assert out["log_signal"]["error"] == "Loki query failed: 503"
        assert out["executions"]["total"] == 1

    def test_the_log_signal_is_labelled_secondary(self, monkeypatch, no_network):
        monkeypatch.setattr(s, "_api_request", self._pages({}))
        monkeypatch.setattr(
            s,
            "query_logs",
            FakeQueryLogs({"entries": [{"component": "runner", "message": "boom"}]}),
        )

        out = json.loads(s.find_failed_executions())
        log_signal = out["log_signal"]

        assert "SECONDARY" in log_signal["note"]
        assert log_signal["by_component"] == {"runner": 1}

    def test_filters_are_forwarded_to_the_log_query(self, monkeypatch, no_network):
        fake = FakeQueryLogs({"entries": []})
        monkeypatch.setattr(s, "_api_request", self._pages({}))
        monkeypatch.setattr(s, "query_logs", fake)

        s.find_failed_executions(time_range="7d", experiment_id=31)

        assert fake.calls == [
            {"level": "error", "time_range": "7d", "experiment_id": 31, "limit": 200}
        ]

    def test_logs_can_be_skipped_entirely(self, monkeypatch, no_network):
        fake = FakeQueryLogs({"entries": []})
        monkeypatch.setattr(s, "_api_request", self._pages({}))
        monkeypatch.setattr(s, "query_logs", fake)

        out = json.loads(s.find_failed_executions(include_logs=False))

        assert "log_signal" not in out
        assert fake.calls == []


# =============================================================================
# system_status
# =============================================================================


class TestSystemStatus:
    @staticmethod
    def _patch_health(monkeypatch, handler) -> FakeHttp:
        fake = FakeHttp(handler)
        monkeypatch.setattr(httpx, "get", fake)
        return fake

    def test_probes_every_component_health_endpoint(self, monkeypatch, fake_settings):
        fake = self._patch_health(monkeypatch, lambda url, **kw: FakeResponse(200))

        out = json.loads(s.system_status())

        assert fake.urls == [
            "http://grafana.test:3000/api/health",
            "http://loki.test:3100/ready",
            "http://prom.test:9090/-/healthy",
            "http://tempo.test:3200/ready",
            "http://localhost:8090/api/v1/crawlers",
        ]
        assert [c["name"] for c in out["components"]] == [
            "grafana",
            "loki",
            "prometheus",
            "tempo",
            "primemover_api",
        ]

    def test_all_healthy(self, monkeypatch, fake_settings):
        self._patch_health(
            monkeypatch,
            lambda url, **kw: FakeResponse(200) if "/api/v1/" in url else FakeResponse(204),
        )

        out = json.loads(s.system_status())

        assert out["overall_status"] == "healthy"
        assert out["healthy_count"] == 5
        assert all(c["status"] == "healthy" for c in out["components"])
        assert out["components"][0]["code"] == 204

    def test_a_4xx_component_is_unhealthy_and_degrades_the_system(self, monkeypatch, fake_settings):
        def handler(url: str, **kwargs: Any):
            return FakeResponse(503) if "loki" in url else FakeResponse(200)

        self._patch_health(monkeypatch, handler)

        out = json.loads(s.system_status())

        loki = next(c for c in out["components"] if c["name"] == "loki")
        assert loki == {"name": "loki", "status": "unhealthy", "code": 503}
        assert out["overall_status"] == "degraded"
        assert out["healthy_count"] == 4

    def test_connection_refused_is_reported_as_down(self, monkeypatch, fake_settings):
        def handler(url: str, **kwargs: Any):
            if "tempo" in url:
                raise httpx.ConnectError("nope")
            return FakeResponse(200)

        self._patch_health(monkeypatch, handler)

        out = json.loads(s.system_status())

        tempo = next(c for c in out["components"] if c["name"] == "tempo")
        assert tempo == {"name": "tempo", "status": "down", "error": "Connection refused"}
        assert "code" not in tempo

    def test_timeout_is_reported_as_down(self, monkeypatch, fake_settings):
        def handler(url: str, **kwargs: Any):
            if "grafana" in url:
                raise httpx.TimeoutException("slow")
            return FakeResponse(200)

        self._patch_health(monkeypatch, handler)

        grafana = json.loads(s.system_status())["components"][0]

        assert grafana == {"name": "grafana", "status": "down", "error": "Timeout"}

    def test_unexpected_exceptions_are_reported_as_error(self, monkeypatch, fake_settings):
        self._patch_health(
            monkeypatch, lambda url, **kw: (_ for _ in ()).throw(ValueError("weird"))
        )

        out = json.loads(s.system_status())

        assert out["components"][0]["status"] == "error"
        assert out["components"][0]["error"] == "weird"
        assert out["overall_status"] == "down"

    def test_everything_down_is_overall_down(self, monkeypatch, fake_settings):
        self._patch_health(
            monkeypatch, lambda url, **kw: (_ for _ in ()).throw(httpx.ConnectError("x"))
        )

        out = json.loads(s.system_status())

        assert out["overall_status"] == "down"
        assert out["healthy_count"] == 0

    def test_api_probe_asks_for_json_so_laravel_answers_401_not_a_login_redirect(
        self, monkeypatch, fake_settings
    ):
        """Without ``Accept: application/json`` Laravel redirects an unauthenticated
        request to a ``login`` route this API does not define, and the probe sees a
        404. The header is what makes the 401 observable."""
        fake = self._patch_health(monkeypatch, lambda url, **kw: FakeResponse(200))

        s.system_status()

        api_call = fake.calls[-1]
        assert api_call["url"] == "http://localhost:8090/api/v1/crawlers"
        assert api_call["headers"] == {"Accept": "application/json"}

    def test_api_401_is_healthy_because_every_route_requires_auth(self, monkeypatch, fake_settings):
        """Regression: the old probe hit the non-existent ``/api/health`` and called
        anything >= 400 unhealthy, so a fully working API reported ``unhealthy``."""

        def handler(url: str, **kwargs: Any):
            return FakeResponse(401) if "/api/v1/" in url else FakeResponse(200)

        self._patch_health(monkeypatch, handler)

        out = json.loads(s.system_status())

        api = next(c for c in out["components"] if c["name"] == "primemover_api")
        assert api["status"] == "healthy"
        assert api["code"] == 401
        assert "401" in api["note"]
        assert out["overall_status"] == "healthy"
        assert out["healthy_count"] == 5

    def test_api_404_is_still_unhealthy(self, monkeypatch, fake_settings):
        """A 404 means the route did not resolve -- the app is not serving the API."""

        def handler(url: str, **kwargs: Any):
            return FakeResponse(404) if "/api/v1/" in url else FakeResponse(200)

        self._patch_health(monkeypatch, handler)

        out = json.loads(s.system_status())

        api = next(c for c in out["components"] if c["name"] == "primemover_api")
        assert api == {"name": "primemover_api", "status": "unhealthy", "code": 404}
        assert out["overall_status"] == "degraded"

    def test_trailing_slashes_in_configured_urls_do_not_double_up(self, monkeypatch, fake_settings):
        fake_settings.grafana_url = "http://grafana.test:3000/"
        fake = self._patch_health(monkeypatch, lambda url, **kw: FakeResponse(200))

        s.system_status()

        assert fake.urls[0] == "http://grafana.test:3000/api/health"


# =============================================================================
# Trace tools
# =============================================================================


class FakeAggregator:
    """Stands in for ``LogAggregator`` inside TraceBuilder.

    Returns entries keyed by which correlation id the caller filtered on, so an
    experiment-level query and a job-level query can yield different logs.
    """

    def __init__(self, by_key: dict[tuple[str, int], list[LogEntry]] | None = None):
        self.by_key = by_key or {}
        self.lambda_output: dict | None = None
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def query(self, **kwargs: Any) -> AggregatedResult:
        self.calls.append(kwargs)
        entries: list[LogEntry] = []
        for key in ("experiment_id", "crawler_id", "job_id"):
            value = kwargs.get(key)
            if value is not None:
                entries = self.by_key.get((key, value), [])
                break
        return AggregatedResult(
            entries=entries, sources={}, total_count=len(entries), query_params=kwargs
        )

    def get_lambda_output(self, job_id: int) -> dict | None:
        return self.lambda_output

    def close(self) -> None:
        self.closed = True


class FakeApiGet:
    """Stands in for ``TraceBuilder._api_get`` (the builder's only HTTP seam to the API)."""

    def __init__(self, responses: dict[str, Any], raises: Exception | None = None):
        self.responses = responses
        self.raises = raises
        self.calls: list[str] = []

    def __call__(self, endpoint: str, retry_on_401: bool = True) -> Any:
        self.calls.append(endpoint)
        if self.raises is not None:
            raise self.raises
        return self.responses.get(endpoint)


class TestTraceTools:
    """Traces are assembled by ``TraceBuilder``; the tools are thin wrappers.

    Seam choice: rather than replacing ``TraceBuilder`` wholesale (which would test only
    that the tool calls a method and returns its ``to_json()``), the two *outbound* seams
    of the real builder are patched -- ``_api_get`` (HTTP to the PrimeMover API) and the
    ``log_aggregator`` property (Loki/files/HAR/CloudWatch). Everything else, including
    span assembly and status derivation, is the real code path, so these tests exercise
    the JSON an LLM actually receives while nothing leaves the process.
    """

    EXPERIMENT = {
        "data": {
            "id": 42,
            "name": "Chicago Study",
            "description": "pi sweep",
            "active": True,
            "crawlers": [
                {
                    "id": 7,
                    "name": "bot_control_01",
                    "experiment_id": 42,
                    "active": True,
                    "testing": False,
                    "queues": [
                        {
                            "id": 70,
                            "jobs": [
                                {
                                    "id": 700,
                                    "name": "Open YouTube",
                                    "type": "visitjob",
                                    "status": "completed",
                                    "behaviors": [{"name": "url", "value": "https://youtube.com"}],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    }

    @pytest.fixture
    def builder_seams(self, monkeypatch):
        """Patch the builder's API and log seams; hand back both doubles."""
        aggregator = FakeAggregator()
        api = FakeApiGet({})
        monkeypatch.setattr(TraceBuilder, "_api_get", api)
        monkeypatch.setattr(TraceBuilder, "log_aggregator", property(lambda self, a=aggregator: a))
        return api, aggregator

    def test_trace_experiment_shape(self, monkeypatch, builder_seams):
        api, aggregator = builder_seams
        api.responses["experiments/42"] = self.EXPERIMENT
        aggregator.by_key[("experiment_id", 42)] = [
            make_entry(0, "api", "info", "experiment started"),
            make_entry(30, "runner", "error", "chrome renderer died"),
        ]

        out = json.loads(s.trace_experiment(42))

        assert api.calls == ["experiments/42"]
        assert out["entity_type"] == "experiment"
        assert out["entity_id"] == 42
        assert out["entity_data"]["name"] == "Chicago Study"
        assert out["root_span"]["name"] == "Chicago Study"
        assert out["root_span"]["component"] == "api"
        assert out["root_span"]["metadata"] == {"description": "pi sweep", "active": True}

    def test_trace_experiment_summary_counts_logs_and_errors(self, monkeypatch, builder_seams):
        api, aggregator = builder_seams
        api.responses["experiments/42"] = self.EXPERIMENT
        aggregator.by_key[("experiment_id", 42)] = [
            make_entry(0, "api", "info", "started"),
            make_entry(30, "runner", "error", "chrome renderer died"),
        ]

        out = json.loads(s.trace_experiment(42, include_crawlers=False))

        assert out["summary"]["total_logs"] == 2
        assert out["summary"]["error_count"] == 1
        assert out["summary"]["duration_ms"] == 30_000
        assert out["summary"]["first_event"] == T0.isoformat()
        # One error is enough to mark the whole experiment failed.
        assert out["root_span"]["status"] == "failed"
        assert out["root_span"]["errors"] == ["chrome renderer died"]
        assert out["root_span"]["log_count"] == 2

    def test_trace_experiment_with_no_logs_is_unknown_not_success(self, monkeypatch, builder_seams):
        api, _ = builder_seams
        api.responses["experiments/42"] = self.EXPERIMENT

        out = json.loads(s.trace_experiment(42, include_crawlers=False))

        assert out["root_span"]["status"] == "unknown"
        assert out["summary"]["total_logs"] == 0
        assert out["summary"]["duration_ms"] is None

    def test_include_crawlers_toggles_child_spans(self, monkeypatch, builder_seams):
        api, _ = builder_seams
        api.responses["experiments/42"] = self.EXPERIMENT

        with_kids = json.loads(s.trace_experiment(42, include_crawlers=True))
        without = json.loads(s.trace_experiment(42, include_crawlers=False))

        assert [c["name"] for c in with_kids["root_span"]["children"]] == ["bot_control_01"]
        assert with_kids["root_span"]["children"][0]["crawler_id"] == 7
        assert with_kids["root_span"]["children"][0]["component"] == "runner"
        assert with_kids["summary"]["total_spans"] == 2
        assert without["root_span"]["children"] == []
        assert without["summary"]["total_spans"] == 1

    def test_include_jobs_adds_job_spans_under_the_crawler(self, monkeypatch, builder_seams):
        api, _ = builder_seams
        api.responses["experiments/42"] = self.EXPERIMENT

        out = json.loads(s.trace_experiment(42, include_crawlers=True, include_jobs=True))

        job = out["root_span"]["children"][0]["children"][0]
        assert job["name"] == "Open YouTube"
        assert job["job_id"] == 700
        assert job["queue_id"] == 70
        assert job["metadata"]["type"] == "visitjob"
        # Job status wins over log-derived status.
        assert job["status"] == "success"

    def test_trace_crawler_shape(self, monkeypatch, builder_seams):
        api, aggregator = builder_seams
        api.responses["crawlers/7"] = {"data": self.EXPERIMENT["data"]["crawlers"][0]}
        aggregator.by_key[("crawler_id", 7)] = [make_entry(0, "runner", "error", "proxy timeout")]

        out = json.loads(s.trace_crawler(7, include_jobs=False))

        assert api.calls == ["crawlers/7"]
        assert out["entity_type"] == "crawler"
        assert out["entity_id"] == 7
        assert out["root_span"]["name"] == "bot_control_01"
        assert out["root_span"]["status"] == "failed"
        assert out["summary"]["error_count"] == 1
        assert out["summary"]["total_spans"] == 1

    def test_trace_crawler_include_jobs_counts_child_spans(self, monkeypatch, builder_seams):
        api, _ = builder_seams
        api.responses["crawlers/7"] = {"data": self.EXPERIMENT["data"]["crawlers"][0]}

        out = json.loads(s.trace_crawler(7, include_jobs=True))

        assert out["summary"]["total_spans"] == 2
        assert out["root_span"]["children"][0]["name"] == "Open YouTube"

    def test_trace_job_shape(self, monkeypatch, builder_seams):
        api, aggregator = builder_seams
        api.responses["jobs/700"] = {
            "data": {
                "id": 700,
                "name": "Open YouTube",
                "type": "visitjob",
                "status": "failed",
                "behaviors": [{"name": "url", "value": "https://youtube.com"}],
            }
        }
        aggregator.by_key[("job_id", 700)] = [make_entry(0, "runner", "error", "timeout")]

        out = json.loads(s.trace_job(700))

        assert out["entity_type"] == "job"
        assert out["entity_id"] == 700
        assert out["root_span"]["job_id"] == 700
        assert out["root_span"]["status"] == "failed"
        assert out["root_span"]["metadata"]["behaviors"] == [
            {"name": "url", "value": "https://youtube.com"}
        ]
        assert out["summary"]["error_count"] == 1

    def test_trace_job_appends_a_lambda_span_when_har_output_exists(
        self, monkeypatch, builder_seams
    ):
        api, aggregator = builder_seams
        api.responses["jobs/700"] = {"data": {"id": 700, "name": "Select video"}}
        aggregator.lambda_output = {
            "selected": {"title": "Climate explained", "id": "vid123"},
            "xpath": "//a[@id='video-title']",
            "warnings": ["error: fallback selection used"],
        }

        out = json.loads(s.trace_job(700))

        lambda_span = out["root_span"]["children"][0]
        assert lambda_span["name"] == "Lambda Decision"
        assert lambda_span["component"] == "lambda"
        assert lambda_span["metadata"]["selected_video"] == "Climate explained"
        assert lambda_span["metadata"]["selected_id"] == "vid123"
        # A decision that emitted warnings is DEGRADED, not failed: it answered
        # HTTP 200 and returned a video, but not the one the arm asked for.
        # (This used to assert "failed", from a heuristic that looked for the
        # substring "error" anywhere in a warning string. "failed" is now
        # reserved for a hard `error` in the payload -- see
        # orchestrator/status.py and test_status_consumers.py.)
        assert lambda_span["status"] == "warning"
        assert out["summary"]["total_spans"] == 2

    def test_trace_job_without_lambda_output_has_no_children(self, monkeypatch, builder_seams):
        api, _ = builder_seams
        api.responses["jobs/700"] = {"data": {"id": 700, "name": "Wait"}}

        out = json.loads(s.trace_job(700))

        assert out["root_span"]["children"] == []
        assert out["summary"]["total_spans"] == 1

    def test_missing_entity_still_produces_a_usable_trace(self, monkeypatch, builder_seams):
        """A 404 from the API makes ``_api_get`` return None; the tool must not crash."""
        api, _ = builder_seams  # no responses registered -> None

        out = json.loads(s.trace_job(999))

        assert out["entity_data"] == {}
        assert out["root_span"]["name"] == "Job 999"
        assert out["root_span"]["status"] == "unknown"

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("trace_experiment", (0,)),
            ("trace_crawler", (0,)),
            ("trace_job", (0,)),
        ],
    )
    def test_invalid_id_is_rejected_before_the_builder_runs(
        self, monkeypatch, builder_seams, tool, args
    ):
        api, _ = builder_seams

        out = json.loads(getattr(s, tool)(*args))

        assert out["error"].startswith("Validation error: ")
        assert "must be between 1 and 2147483647" in out["error"]
        assert api.calls == []

    @pytest.mark.parametrize("tool", ["trace_experiment", "trace_crawler", "trace_job"])
    def test_invalid_time_range_is_rejected_before_the_builder_runs(
        self, monkeypatch, builder_seams, tool
    ):
        api, _ = builder_seams

        out = json.loads(getattr(s, tool)(7, time_range="forever"))

        assert "Invalid time_range format: forever" in out["error"]
        assert api.calls == []

    @pytest.mark.parametrize("tool", ["trace_experiment", "trace_crawler", "trace_job"])
    def test_builder_failure_becomes_json_and_still_closes_the_builder(
        self, monkeypatch, builder_seams, tool
    ):
        api, _ = builder_seams
        api.raises = Exception("API authentication failed (401)")

        closed: list[bool] = []
        real_close = TraceBuilder.close
        monkeypatch.setattr(
            TraceBuilder,
            "close",
            lambda self: (closed.append(True), real_close(self))[1],
        )

        out = json.loads(getattr(s, tool)(7))

        assert out == {"error": "API authentication failed (401)"}
        assert closed == [True], "the builder must be closed even when tracing fails"


# =============================================================================
# Job type registry tools
# =============================================================================


class TestListJobTypes:
    def test_lists_the_registry_with_stable_field_names(self, no_network):
        out = json.loads(s.list_job_types())

        assert "job_types" in out
        by_name = {j["yaml_name"]: j for j in out["job_types"]}
        assert "visit" in by_name
        assert by_name["visit"]["api_type"] == "visitjob"
        assert by_name["visit"]["description"]
        assert set(by_name["visit"]) == {
            "yaml_name",
            "api_type",
            "description",
            "required_behaviors",
            "optional_behaviors",
        }

    def test_required_and_optional_behaviors_are_split(self, no_network):
        out = json.loads(s.list_job_types())
        visit = next(j for j in out["job_types"] if j["yaml_name"] == "visit")

        assert visit["required_behaviors"] == ["url"]
        assert "captchaMode" in visit["optional_behaviors"]
        assert "url" not in visit["optional_behaviors"]

    def test_yaml_names_are_unique(self, no_network):
        names = [j["yaml_name"] for j in json.loads(s.list_job_types())["job_types"]]

        assert len(names) == len(set(names))
        assert len(names) >= 10

    def test_needs_no_api_or_network(self, no_network):
        """Pinned because the tool is the model's offline reference for workflow syntax."""
        assert json.loads(s.list_job_types())["job_types"]


class TestGetJobTypeSchema:
    def test_returns_the_full_schema_for_a_known_type(self, no_network):
        out = json.loads(s.get_job_type_schema("visit"))

        assert out["yaml_name"] == "visit"
        assert out["api_type"] == "visitjob"
        assert out["display_name"] == "Visit URL"
        url = next(b for b in out["behaviors"] if b["name"] == "url")
        assert url["required"] is True
        assert url["default"] is None
        assert url["description"]

    def test_optional_behaviours_carry_their_default(self, no_network):
        out = json.loads(s.get_job_type_schema("click"))

        selection = next(b for b in out["behaviors"] if b["name"] == "selectionType")
        assert selection["required"] is False
        assert selection["default"] == "XPATH"

    def test_unknown_type_lists_the_available_ones(self, no_network):
        out = json.loads(s.get_job_type_schema("teleport"))

        assert out["error"] == "Unknown job type: teleport"
        # available_types lists both names, "yaml_name (api_type)".
        joined = " ".join(out["available_types"])
        assert "visit (visitjob)" in joined
        assert "wait (waitjob)" in joined

    def test_api_type_is_accepted_as_a_lookup_key(self, no_network):
        """Lookup accepts either the YAML name or the API type, so a caller holding
        the write-tool name (e.g. 'visitjob') still resolves."""
        by_api = json.loads(s.get_job_type_schema("visitjob"))
        by_yaml = json.loads(s.get_job_type_schema("visit"))

        assert "error" not in by_api
        assert by_api["yaml_name"] == "visit"
        assert by_api["api_type"] == "visitjob"
        assert by_api == by_yaml


class TestGetSpecTemplate:
    @pytest.mark.parametrize("template_type", ["default", "google"])
    def test_templates_are_parseable_yaml_with_the_expected_skeleton(
        self, no_network, template_type
    ):
        out = json.loads(s.get_spec_template(template_type))

        assert out["type"] == template_type
        spec = yaml.safe_load(out["template"])
        assert spec["version"] == "1.0"
        assert "name" in spec["experiment"]
        assert isinstance(spec["crawlers"], list) and spec["crawlers"]

    def test_youtube_template_is_valid_and_uses_switch_params(self, no_network):
        """The youtube template must parse, validate, and demonstrate correct 0/1
        switch params.

        Formerly this pinned two bugs: an over-indented ``template:`` under
        ``repeat`` that made the document unparseable, and a
        ``params.pi: {left: -0.5, center: 0, right: 0.5}`` mapping that rounds to
        three identical control arms. Both are fixed; this now guards against
        regressing either.
        """
        from experiment_designer.spec.validator import SpecValidator

        template = json.loads(s.get_spec_template("youtube"))["template"]
        spec = yaml.safe_load(template)  # must not raise

        result = SpecValidator().validate(spec)
        assert result.valid, result.errors

        # Any params.pi mapping in the template must be a 0/1 switch, never fractional.
        for factor in spec["design"]["factors"].values():
            pi_map = factor.get("maps_to", {}).get("params.pi")
            if pi_map:
                assert set(pi_map.values()) <= {0, 1}

    def test_the_three_templates_are_actually_different(self, no_network):
        bodies = {
            t: json.loads(s.get_spec_template(t))["template"]
            for t in ("default", "youtube", "google")
        }

        assert len(set(bodies.values())) == 3
        assert "google" in bodies["google"]
        assert "youtube" in bodies["youtube"].lower()

    def test_google_template_defines_the_template_it_references(self, no_network):
        """A template whose workflow references a missing block would not convert."""
        spec = yaml.safe_load(json.loads(s.get_spec_template("google"))["template"])

        defined = set(spec.get("templates", {}))
        referenced = {
            step["template"]
            for crawler in spec["crawlers"]
            for step in crawler["session"]["workflow"]
            if isinstance(step, dict) and "template" in step
        }
        assert referenced <= defined


class TestDocResources:
    """The doc:// resources give an MCP-only agent the design knowledge."""

    def test_authored_resources_carry_the_key_semantics(self, no_network):
        tp = s.doc_treatment_params()
        assert "int(round(x)) == 1" in tp
        assert "0.5" in tp and "switch" in tp.lower()
        cap = s.doc_capacity()
        assert "CET" in cap and "start_at" in cap

    def test_file_backed_resources_return_the_docs(self, no_network):
        assert "experiment" in s.doc_experiment_designer().lower()
        # §7.3 degradation contract lives in the integration contract
        assert "treatment_lost" in s.doc_integration_contract()

    def test_unknown_template_type_silently_returns_the_default(self, no_network):
        """Documents current behaviour, which is mildly misleading.

        The body is the default template but ``type`` echoes what was asked for, so a
        caller that requests 'tiktok' gets the default spec labelled 'tiktok' with no
        indication the request was not honoured.
        """
        out = json.loads(s.get_spec_template("tiktok"))

        assert out["type"] == "tiktok"
        assert out["template"] == json.loads(s.get_spec_template("default"))["template"]
        assert "error" not in out


# =============================================================================
# Summary / comparison tools
# =============================================================================


API_EXPERIMENT = {
    "data": {
        "id": 42,
        "name": "Chicago Study",
        "description": "pi sweep",
        "crawlers": [
            {
                "id": 1,
                "name": "bot_control_01",
                "active": True,
                "configuration": {"params": {"pi": 0, "alpha": 1}},
                "queues": [
                    {
                        "id": 10,
                        "status": "processed",
                        "jobs": [{"id": 100, "type": "visitjob"}, {"id": 101, "type": "waitjob"}],
                    }
                ],
            },
            {
                "id": 2,
                "name": "bot_treat_01",
                "active": False,
                "configuration": {"params": {"pi": 0.5, "alpha": 1}},
                "queues": [
                    {"id": 11, "status": "unprocessed", "jobs": [{"id": 102, "type": "visitjob"}]}
                ],
            },
        ],
    }
}


def patch_api_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    login_status: int = 200,
    fetch_status: int = 200,
    payload: Any = None,
    token: str = "tok-123",
) -> tuple[FakeHttp, FakeHttp]:
    """Close the raw httpx seam used by summarize_experiment / compare_experiments."""
    post = FakeHttp(lambda url, **kw: FakeResponse(login_status, {"access_token": token}))
    get = FakeHttp(
        lambda url, **kw: FakeResponse(fetch_status, payload if payload is not None else {})
    )
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)
    return post, get


class TestSummarizeExperiment:
    def test_summarizes_api_data(self, monkeypatch, fake_settings):
        patch_api_httpx(monkeypatch, payload=API_EXPERIMENT)

        out = json.loads(s.summarize_experiment(42))

        assert out["success"] is True
        summary = out["summary"]
        assert summary["metadata"]["name"] == "Chicago Study"
        assert summary["metadata"]["experiment_id"] == 42
        assert summary["metadata"]["source_type"] == "api_response"
        assert summary["summary"]["crawler_count"] == 2
        assert summary["summary"]["total_jobs"] == 3
        assert summary["summary"]["jobs_by_type"] == {"visitjob": 2, "waitjob": 1}
        assert summary["statistics"]["active_crawler_count"] == 1
        assert "Chicago Study" in out["text"]

    def test_authenticates_then_fetches_with_the_bearer_token(self, monkeypatch, fake_settings):
        post, get = patch_api_httpx(monkeypatch, payload=API_EXPERIMENT, token="tok-abc")

        s.summarize_experiment(42)

        assert post.urls == ["http://localhost:8090/api/v1/login"]
        assert post.calls[0]["json"] == {
            "email": "tester@example.org",
            "password": "s3cret",
        }
        assert get.urls == ["http://localhost:8090/api/v1/experiments/42"]
        assert get.calls[0]["headers"] == {"Authorization": "Bearer tok-abc"}

    def test_auth_failure_stops_before_fetching(self, monkeypatch, fake_settings):
        _, get = patch_api_httpx(monkeypatch, login_status=401, payload=API_EXPERIMENT)

        out = json.loads(s.summarize_experiment(42))

        assert out == {"success": False, "error": "API authentication failed"}
        assert get.calls == [], "must not fetch the experiment without a token"

    def test_fetch_failure_reports_the_status_code(self, monkeypatch, fake_settings):
        patch_api_httpx(monkeypatch, fetch_status=404)

        out = json.loads(s.summarize_experiment(9999))

        assert out == {"success": False, "error": "Failed to fetch experiment: 404"}

    def test_connection_failure_is_reported_as_json(self, monkeypatch, fake_settings):
        def boom(url: str, **kwargs: Any):
            raise httpx.ConnectError("All connection attempts failed")

        monkeypatch.setattr(httpx, "post", boom)

        out = json.loads(s.summarize_experiment(42))

        assert out["success"] is False
        assert "All connection attempts failed" in out["error"]

    def test_unwrapped_response_body_is_accepted(self, monkeypatch, fake_settings):
        """The API sometimes answers without the {"data": ...} envelope."""
        patch_api_httpx(monkeypatch, payload=API_EXPERIMENT["data"])

        out = json.loads(s.summarize_experiment(42))

        assert out["success"] is True
        assert out["summary"]["summary"]["crawler_count"] == 2


YAML_A = """version: "1.0"
experiment:
  name: "Study A"
  description: "first"
crawlers:
  - name: "bot_one"
    configuration:
      params:
        pi: 0
        alpha: 1
    session:
      workflow:
        - visit: https://example.com
        - wait: 5s
"""

YAML_B = """version: "1.0"
experiment:
  name: "Study B"
  description: "second"
crawlers:
  - name: "bot_one"
    configuration:
      params:
        pi: 0
        alpha: 1
    session:
      workflow:
        - visit: https://example.com
        - wait: 5s
  - name: "bot_two"
    configuration:
      params:
        pi: 0.5
        alpha: 1
    session:
      workflow:
        - visit: https://example.org
"""


class TestCompareExperiments:
    def test_identical_specs_compare_equal(self, no_network):
        out = json.loads(s.compare_experiments(YAML_A, YAML_A))

        assert out["success"] is True
        assert out["identical"] is True
        assert out["differences"] == []
        assert out["crawler_count_diff"] == 0
        assert out["job_count_diff"] == 0
        assert out["crawlers_only_in_a"] == []
        assert out["crawlers_only_in_b"] == []

    def test_differences_are_itemised_by_category(self, no_network):
        out = json.loads(s.compare_experiments(YAML_A, YAML_B))

        assert out["identical"] is False
        assert out["crawler_count_diff"] == 1
        assert out["job_count_diff"] == 1
        name_diff = next(
            d for d in out["differences"] if d["category"] == "metadata" and d["field"] == "name"
        )
        assert name_diff["value_a"] == "Study A"
        assert name_diff["value_b"] == "Study B"
        assert out["crawlers_only_in_b"] == ["bot_two"]
        assert out["crawlers_only_in_a"] == []

    def test_both_full_summaries_are_returned(self, no_network):
        out = json.loads(s.compare_experiments(YAML_A, YAML_B))

        assert out["experiment1"]["summary"]["crawler_count"] == 1
        assert out["experiment2"]["summary"]["crawler_count"] == 2
        assert out["experiment2"]["metadata"]["name"] == "Study B"

    def test_yaml_can_be_compared_against_api_data(self, monkeypatch, fake_settings):
        post, get = patch_api_httpx(monkeypatch, payload=API_EXPERIMENT)

        out = json.loads(s.compare_experiments(YAML_A, "42", type1="yaml", type2="api"))

        assert out["success"] is True
        assert get.urls == ["http://localhost:8090/api/v1/experiments/42"]
        assert out["experiment2"]["metadata"]["name"] == "Chicago Study"
        # Diffs are b - a: the YAML spec has one crawler, the API experiment has two.
        assert out["crawler_count_diff"] == 1
        assert out["job_count_diff"] == 1

    def test_api_side_failure_is_reported_as_json(self, monkeypatch, fake_settings):
        patch_api_httpx(monkeypatch, fetch_status=500)

        out = json.loads(s.compare_experiments("42", YAML_A, type1="api", type2="yaml"))

        assert out["success"] is False
        assert out["error"] == "Failed to fetch experiment 42"

    def test_api_auth_failure_is_reported_as_json(self, monkeypatch, fake_settings):
        patch_api_httpx(monkeypatch, login_status=401)

        out = json.loads(s.compare_experiments("42", "43", type1="api", type2="api"))

        assert out == {"success": False, "error": "API authentication failed"}

    @pytest.mark.parametrize(
        ("type1", "type2", "bad"), [("bogus", "yaml", "bogus"), ("yaml", "xml", "xml")]
    )
    def test_unknown_source_types_are_rejected(self, no_network, type1, type2, bad):
        out = json.loads(s.compare_experiments(YAML_A, YAML_A, type1=type1, type2=type2))

        assert out["success"] is False
        assert out["error"] == f"Unknown source type: {bad}. Use 'yaml', 'json', or 'api'"

    def test_unparseable_yaml_still_reports_success(self, no_network):
        """Documents a real gap rather than endorsing it.

        ``SummaryGenerator`` collects parse problems into ``validation.errors`` instead of
        raising, and ``compare_experiments`` never inspects them. Comparing garbage against
        a near-empty spec therefore comes back ``success: true, identical: true`` -- the
        only trace of the failure is inside ``experiment1.validation``.
        """
        out = json.loads(s.compare_experiments("::: not yaml [", 'version: "1.0"'))

        assert out["success"] is True
        assert out["identical"] is True
        assert out["experiment1"]["validation"]["is_valid"] is False
        assert out["experiment1"]["validation"]["errors"]
