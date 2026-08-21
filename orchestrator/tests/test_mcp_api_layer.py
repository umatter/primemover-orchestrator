"""
Hermetic tests for the HTTP/API layer of the orchestrator MCP server.

Covers ``_api_request`` (mocked at the ``httpx`` level -- it is the function under
test, so it is never patched itself) and ``_next_job_slot`` (which is exercised by
patching ``_api_request``, its seam).

No test in this module is allowed to open a socket: the autouse ``block_network``
fixture makes any real connect attempt fail loudly.
"""

import json
import socket
from typing import Any

import httpx
import pytest

from orchestrator.mcp_server import server as s

BASE_URL = "http://localhost:8090/api/v1"
LOGIN_URL = f"{BASE_URL}/login"
TOKEN = "tok-abc123"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

MISSING = object()


# =============================================================================
# Hermeticity guards
# =============================================================================


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if anything in this module tries to reach the network."""

    def _no_connect(self: socket.socket, address: Any) -> None:
        raise AssertionError(f"network access attempted in a hermetic test: connect({address!r})")

    monkeypatch.setattr(socket.socket, "connect", _no_connect)

    def _no_httpx(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("httpx was used without installing a fake client")

    for name in ("Client", "get", "post", "put", "delete", "request"):
        monkeypatch.setattr(httpx, name, _no_httpx)


@pytest.fixture
def api_settings(monkeypatch: pytest.MonkeyPatch, mock_settings: Any) -> Any:
    """Point the server module's settings at the test API base URL/credentials."""
    monkeypatch.setattr(s, "get_settings", lambda: mock_settings)
    return mock_settings


# =============================================================================
# Fake httpx plumbing
# =============================================================================


class FakeResponse:
    """Stands in for ``httpx.Response`` for the attributes ``_api_request`` touches."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = MISSING,
        text: str | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self._json_error = json_error
        if text is not None:
            self.text = text
        elif json_data is not MISSING:
            self.text = json.dumps(json_data)
        else:
            self.text = ""

    def json(self) -> Any:
        if self._json_error is not None:
            raise self._json_error
        if self._json_data is MISSING:
            raise ValueError("no JSON body")
        return self._json_data


class FakeClient:
    """Records every request and answers it from the test-supplied handler."""

    def __init__(self, record: dict[str, Any], handler: Any) -> None:
        self._record = record
        self._handler = handler

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        self._record["closed"] = True
        return False

    def _call(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self._record["calls"].append({"method": method, "url": url, "kwargs": kwargs})
        return self._handler(method, url, kwargs)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._call("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._call("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._call("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._call("DELETE", url, **kwargs)


def install_fake_httpx(monkeypatch: pytest.MonkeyPatch, handler: Any) -> dict[str, Any]:
    """Replace ``httpx.Client`` with a recording fake; return the shared record."""
    record: dict[str, Any] = {"calls": [], "closed": False, "clients": 0, "init_kwargs": []}

    def factory(**kwargs: Any) -> FakeClient:
        record["clients"] += 1
        record["init_kwargs"].append(kwargs)
        return FakeClient(record, handler)

    monkeypatch.setattr(httpx, "Client", factory)
    return record


def scripted(*responses: Any, login: Any = None) -> Any:
    """Handler answering ``/login`` with `login`, then `responses` in order.

    Any element may be an ``Exception`` instance, which is raised instead of returned.
    """
    queue = list(responses)

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        if url == LOGIN_URL:
            reply = login if login is not None else FakeResponse(200, {"access_token": TOKEN})
        elif queue:
            reply = queue.pop(0)
        else:
            raise AssertionError(f"unscripted request: {method} {url}")
        if isinstance(reply, Exception):
            raise reply
        return reply

    return handler


# =============================================================================
# _api_request -- authentication
# =============================================================================


def test_login_posts_credentials_to_login_endpoint(
    monkeypatch: pytest.MonkeyPatch, api_settings: Any
) -> None:
    record = install_fake_httpx(monkeypatch, scripted(FakeResponse(200, {"data": []})))

    s._api_request("GET", "experiments")

    login = record["calls"][0]
    assert login["method"] == "POST"
    assert login["url"] == LOGIN_URL  # /login, NOT /auth/login
    assert login["kwargs"]["json"] == {"email": "test@test.com", "password": "testpass"}
    assert login["kwargs"]["headers"] == {"Accept": "application/json"}


def test_token_from_login_is_attached_to_the_request(
    monkeypatch: pytest.MonkeyPatch, api_settings: Any
) -> None:
    handler = scripted(
        FakeResponse(200, {"data": []}),
        login=FakeResponse(200, {"access_token": "sk-live-9f8e7d"}),
    )
    record = install_fake_httpx(monkeypatch, handler)

    s._api_request("GET", "experiments")

    assert record["calls"][1]["kwargs"]["headers"] == {
        "Authorization": "Bearer sk-live-9f8e7d",
        "Accept": "application/json",
    }


def test_auth_non_200_raises_and_skips_the_request(
    monkeypatch: pytest.MonkeyPatch, api_settings: Any
) -> None:
    record = install_fake_httpx(monkeypatch, scripted(login=FakeResponse(401, {"message": "nope"})))

    with pytest.raises(Exception, match=r"^API authentication failed \(401\)$"):
        s._api_request("GET", "experiments")

    assert len(record["calls"]) == 1  # login only; the real request never went out
    assert record["closed"] is True


def test_auth_without_access_token_raises(
    monkeypatch: pytest.MonkeyPatch, api_settings: Any
) -> None:
    record = install_fake_httpx(
        monkeypatch, scripted(login=FakeResponse(200, {"token_type": "Bearer"}))
    )

    with pytest.raises(Exception, match=r"^API authentication returned no access_token$"):
        s._api_request("GET", "experiments")

    assert len(record["calls"]) == 1


def test_every_call_re_authenticates(monkeypatch: pytest.MonkeyPatch, api_settings: Any) -> None:
    """The token is per-call: two _api_request calls perform two logins (no caching)."""
    handler = scripted(FakeResponse(200, {"data": 1}), FakeResponse(200, {"data": 2}))
    record = install_fake_httpx(monkeypatch, handler)

    assert s._api_request("GET", "experiments") == {"data": 1}
    assert s._api_request("GET", "crawlers") == {"data": 2}

    assert record["clients"] == 2
    assert [c["url"] for c in record["calls"]] == [
        LOGIN_URL,
        f"{BASE_URL}/experiments",
        LOGIN_URL,
        f"{BASE_URL}/crawlers",
    ]


def test_client_is_constructed_with_a_30s_timeout(
    monkeypatch: pytest.MonkeyPatch, api_settings: Any
) -> None:
    record = install_fake_httpx(monkeypatch, scripted(FakeResponse(200, {"data": []})))

    s._api_request("GET", "experiments")

    assert record["init_kwargs"] == [{"timeout": 30.0}]


# =============================================================================
# _api_request -- verbs, params and payloads
# =============================================================================


def test_get_passes_params_and_returns_parsed_body(
    monkeypatch: pytest.MonkeyPatch, api_settings: Any
) -> None:
    body = {"data": [{"id": 3, "name": "exp"}], "meta": {"total": 1}}
    record = install_fake_httpx(monkeypatch, scripted(FakeResponse(200, body)))

    result = s._api_request("GET", "experiments", params={"page": 2, "per_page": 50})

    assert result == body
    call = record["calls"][1]
    assert call["method"] == "GET"
    assert call["url"] == f"{BASE_URL}/experiments"
    assert call["kwargs"]["params"] == {"page": 2, "per_page": 50}
    assert call["kwargs"]["headers"] == AUTH_HEADERS


def test_get_without_params_sends_none(monkeypatch: pytest.MonkeyPatch, api_settings: Any) -> None:
    record = install_fake_httpx(monkeypatch, scripted(FakeResponse(200, {"data": {}})))

    s._api_request("GET", "experiments/7")

    assert record["calls"][1]["kwargs"]["params"] is None


def test_post_sends_data_as_json_body(monkeypatch: pytest.MonkeyPatch, api_settings: Any) -> None:
    created = {"data": {"id": 42, "name": "My Experiment"}}
    record = install_fake_httpx(monkeypatch, scripted(FakeResponse(201, created)))
    payload = {"name": "My Experiment", "description": "d", "active": 1}

    result = s._api_request("POST", "experiments", data=payload)

    assert result == created
    call = record["calls"][1]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE_URL}/experiments"
    assert call["kwargs"]["json"] == payload
    assert "params" not in call["kwargs"]  # POST never forwards params


def test_post_ignores_params(monkeypatch: pytest.MonkeyPatch, api_settings: Any) -> None:
    """Documented gap: params passed to a POST are silently dropped."""
    record = install_fake_httpx(monkeypatch, scripted(FakeResponse(200, {"data": {}})))

    s._api_request("POST", "experiments", data={"a": 1}, params={"dropped": "yes"})

    assert record["calls"][1]["kwargs"] == {"headers": AUTH_HEADERS, "json": {"a": 1}}


def test_put_sends_data_as_json_body(monkeypatch: pytest.MonkeyPatch, api_settings: Any) -> None:
    record = install_fake_httpx(monkeypatch, scripted(FakeResponse(200, {"data": {"id": 5}})))

    result = s._api_request("PUT", "queues/5", data={"active": 1})

    assert result == {"data": {"id": 5}}
    call = record["calls"][1]
    assert call["method"] == "PUT"
    assert call["url"] == f"{BASE_URL}/queues/5"
    assert call["kwargs"]["json"] == {"active": 1}


def test_delete_forwards_params_and_drops_data(
    monkeypatch: pytest.MonkeyPatch, api_settings: Any
) -> None:
    """DELETE mirrors GET: it forwards `params` and never sends `data` as a body."""
    record = install_fake_httpx(monkeypatch, scripted(FakeResponse(204)))

    result = s._api_request("DELETE", "jobs/9", data={"reason": "cleanup"}, params={"force": 1})

    assert result == {"status": 204}
    call = record["calls"][1]
    assert call["method"] == "DELETE"
    assert call["url"] == f"{BASE_URL}/jobs/9"
    assert call["kwargs"] == {"headers": AUTH_HEADERS, "params": {"force": 1}}


def test_unsupported_method_raises(monkeypatch: pytest.MonkeyPatch, api_settings: Any) -> None:
    record = install_fake_httpx(monkeypatch, scripted())

    with pytest.raises(Exception, match=r"^Unsupported method: PATCH$"):
        s._api_request("PATCH", "experiments/1", data={"name": "x"})

    # The login round trip already happened before the method was validated.
    assert [c["url"] for c in record["calls"]] == [LOGIN_URL]
    assert record["closed"] is True


def test_endpoint_is_joined_with_a_single_slash(
    monkeypatch: pytest.MonkeyPatch, api_settings: Any
) -> None:
    record = install_fake_httpx(monkeypatch, scripted(FakeResponse(200, {"data": {}})))

    s._api_request("GET", "experiments/12/crawlers")

    assert record["calls"][1]["url"] == "http://localhost:8090/api/v1/experiments/12/crawlers"


# =============================================================================
# _api_request -- failure and edge-case bodies
# =============================================================================


def test_error_status_includes_status_and_body(
    monkeypatch: pytest.MonkeyPatch, api_settings: Any
) -> None:
    body = {"message": "The given data was invalid.", "errors": {"name": ["required"]}}
    record = install_fake_httpx(monkeypatch, scripted(FakeResponse(422, body)))

    with pytest.raises(Exception) as excinfo:
        s._api_request("POST", "experiments", data={})

    message = str(excinfo.value)
    assert message.startswith("API request failed: 422 ")
    assert "The given data was invalid." in message
    assert record["closed"] is True


def test_error_body_is_truncated_to_400_chars(
    monkeypatch: pytest.MonkeyPatch, api_settings: Any
) -> None:
    long_body = "A" * 399 + "B" + "C" * 50
    install_fake_httpx(monkeypatch, scripted(FakeResponse(500, text=long_body)))

    with pytest.raises(Exception) as excinfo:
        s._api_request("GET", "experiments")

    message = str(excinfo.value)
    assert message == "API request failed: 500 " + "A" * 399 + "B"
    assert "C" not in message


def test_204_returns_status_marker(monkeypatch: pytest.MonkeyPatch, api_settings: Any) -> None:
    install_fake_httpx(monkeypatch, scripted(FakeResponse(204, text="ignored")))

    assert s._api_request("DELETE", "jobs/9") == {"status": 204}


def test_empty_200_body_returns_status_marker(
    monkeypatch: pytest.MonkeyPatch, api_settings: Any
) -> None:
    install_fake_httpx(monkeypatch, scripted(FakeResponse(200, text="")))

    assert s._api_request("GET", "experiments") == {"status": 200}


def test_malformed_json_body_propagates(monkeypatch: pytest.MonkeyPatch, api_settings: Any) -> None:
    """A 200 whose body is not JSON surfaces the decode error, not a wrapped API error."""
    decode_error = json.JSONDecodeError("Expecting value", "<html>oops</html>", 0)
    record = install_fake_httpx(
        monkeypatch, scripted(FakeResponse(200, text="<html>oops</html>", json_error=decode_error))
    )

    with pytest.raises(json.JSONDecodeError):
        s._api_request("GET", "experiments")

    assert record["closed"] is True


def test_malformed_auth_body_propagates(monkeypatch: pytest.MonkeyPatch, api_settings: Any) -> None:
    decode_error = json.JSONDecodeError("Expecting value", "not json", 0)
    login = FakeResponse(200, text="not json", json_error=decode_error)
    record = install_fake_httpx(monkeypatch, scripted(login=login))

    with pytest.raises(json.JSONDecodeError):
        s._api_request("GET", "experiments")

    assert len(record["calls"]) == 1


def test_network_error_on_login_propagates(
    monkeypatch: pytest.MonkeyPatch, api_settings: Any
) -> None:
    record = install_fake_httpx(
        monkeypatch, scripted(login=httpx.ConnectError("[Errno 111] Connection refused"))
    )

    with pytest.raises(httpx.ConnectError, match="Connection refused"):
        s._api_request("GET", "experiments")

    assert record["closed"] is True  # the context manager still closed the client


def test_network_error_on_request_propagates(
    monkeypatch: pytest.MonkeyPatch, api_settings: Any
) -> None:
    record = install_fake_httpx(monkeypatch, scripted(httpx.ReadTimeout("timed out")))

    with pytest.raises(httpx.ReadTimeout, match="timed out"):
        s._api_request("GET", "experiments")

    assert record["closed"] is True


def test_base_url_comes_from_settings(monkeypatch: pytest.MonkeyPatch, mock_settings: Any) -> None:
    """A non-default base URL is honoured (read tools may target a remote API)."""
    mock_settings.pm_api_base_url = "https://api.example.org/api/v1"
    monkeypatch.setattr(s, "get_settings", lambda: mock_settings)

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        if url == "https://api.example.org/api/v1/login":
            return FakeResponse(200, {"access_token": TOKEN})
        return FakeResponse(200, {"data": []})

    record = install_fake_httpx(monkeypatch, handler)

    s._api_request("GET", "experiments")

    assert [c["url"] for c in record["calls"]] == [
        "https://api.example.org/api/v1/login",
        "https://api.example.org/api/v1/experiments",
    ]


# =============================================================================
# _next_job_slot
# =============================================================================


def fake_api(result: Any) -> Any:
    """_api_request stand-in returning `result`; records the call it received."""
    calls: list[dict[str, Any]] = []

    def _fake(method: str, endpoint: str, **kwargs: Any) -> Any:
        calls.append({"method": method, "endpoint": endpoint, "kwargs": kwargs})
        if isinstance(result, Exception):
            raise result
        return result

    _fake.calls = calls  # type: ignore[attr-defined]
    return _fake


def test_next_job_slot_queries_jobs_filtered_by_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_api({"data": []})
    monkeypatch.setattr(s, "_api_request", fake)

    s._next_job_slot(42)

    assert fake.calls == [
        {
            "method": "GET",
            "endpoint": "jobs",
            "kwargs": {"params": {"filter[queue]": "42"}},  # id stringified for the filter
        }
    ]


def test_next_job_slot_empty_queue_starts_at_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s, "_api_request", fake_api({"data": []}))

    assert s._next_job_slot(42) == (1, None)


def test_next_job_slot_single_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s, "_api_request", fake_api({"data": [{"id": 7, "order": 3}]}))

    assert s._next_job_slot(42) == (4, 7)


def test_next_job_slot_picks_highest_order_regardless_of_row_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"id": 1, "order": 1},
        {"id": 9, "order": 5},
        {"id": 4, "order": 3},
    ]
    monkeypatch.setattr(s, "_api_request", fake_api({"data": rows}))

    assert s._next_job_slot(42) == (6, 9)


def test_next_job_slot_accepts_a_bare_list_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s, "_api_request", fake_api([{"id": 2, "order": 1}, {"id": 8, "order": 2}]))

    assert s._next_job_slot(42) == (3, 8)


def test_next_job_slot_dict_without_data_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The {'status': 204} shape _api_request returns for empty bodies means 'no jobs'."""
    monkeypatch.setattr(s, "_api_request", fake_api({"status": 204}))

    assert s._next_job_slot(42) == (1, None)


def test_next_job_slot_falls_back_to_row_count_when_order_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows without an `order` fall back to len(rows) + 1; previous id is still the last row."""
    monkeypatch.setattr(s, "_api_request", fake_api({"data": [{"id": 2}, {"id": 3}]}))

    assert s._next_job_slot(42) == (3, 3)


def test_next_job_slot_treats_order_zero_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`order or len(rows)` is falsy-sensitive: a job at order 0 yields 2, not 1."""
    monkeypatch.setattr(s, "_api_request", fake_api({"data": [{"id": 5, "order": 0}]}))

    assert s._next_job_slot(42) == (2, 5)


def test_next_job_slot_swallows_api_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """An API failure is indistinguishable from an empty queue -- returns the first slot."""
    monkeypatch.setattr(s, "_api_request", fake_api(Exception("API request failed: 500")))

    assert s._next_job_slot(42) == (1, None)


def test_next_job_slot_swallows_malformed_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-dict rows blow up in the sort key and are absorbed by the bare except."""
    monkeypatch.setattr(s, "_api_request", fake_api({"data": ["not-a-dict"]}))

    assert s._next_job_slot(42) == (1, None)
