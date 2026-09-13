"""No MCP read tool may return a proxy credential.

The API serialises the proxies table verbatim -- ``password`` included -- on
``experiments/{id}``, ``crawlers/{id}``, the crawler lists, the library and
inside every trace's ``entity_data``. The MCP is the boundary where that JSON
leaves the box for an LLM and, over HTTP, for other people; it is scrubbed
here, once, for every tool that hands API rows back."""

import json

import pytest

from orchestrator.mcp_server import server as s

ROW = {
    "id": 7,
    "name": "Study",
    "crawlers": [
        {
            "id": 30,
            "name": "bot",
            "proxy": {"id": 3, "hostname": "h", "port": 1, "username": "u", "password": "hunter2"},
            "agent": {
                "multilogin_profile": {
                    "parameters": {"fingerprint": {"proxy": {"password": "hunter2"}}}
                }
            },
        }
    ],
}


@pytest.fixture(autouse=True)
def local_api(monkeypatch, mock_settings):
    monkeypatch.setattr(s, "get_settings", lambda: mock_settings)


def test_scrub_replaces_every_password_key_at_any_depth():
    out = s._scrub_secrets(ROW)
    assert out["crawlers"][0]["proxy"]["password"] == "<redacted>"
    assert (
        out["crawlers"][0]["agent"]["multilogin_profile"]["parameters"]["fingerprint"]["proxy"][
            "password"
        ]
        == "<redacted>"
    )
    assert (
        out["crawlers"][0]["proxy"]["username"] == "u"
    )  # not a secret: it carries geo/session targeting
    assert ROW["crawlers"][0]["proxy"]["password"] == "hunter2"  # input untouched


@pytest.mark.parametrize(
    "tool,args",
    [
        ("get_experiment", (7,)),
        ("get_crawler", (30,)),
        ("list_crawlers", ()),
    ],
)
def test_read_tools_never_return_a_password(monkeypatch, tool, args):
    monkeypatch.setattr(s, "_api_request", lambda *a, **k: {"data": ROW})
    raw = getattr(s, tool)(*args)
    assert "hunter2" not in raw, tool
    assert "<redacted>" in raw


def test_list_library_projects_proxies_to_safe_fields(monkeypatch):
    """The library never carried the password: it projects id/name/description/type/active."""
    monkeypatch.setattr(s, "_api_request", lambda *a, **k: {"data": [ROW["crawlers"][0]["proxy"]]})
    raw = s.list_library()
    assert "hunter2" not in raw and "password" not in raw


def test_trace_experiment_never_returns_a_password(monkeypatch):
    class _Trace:
        def to_dict(self):
            return {"entity_data": ROW, "root_span": None}

        def to_json(self):
            return json.dumps(self.to_dict())

    class _Builder:
        def trace_experiment(self, *a, **k):
            return _Trace()

        def trace_crawler(self, *a, **k):
            return _Trace()

        def trace_job(self, *a, **k):
            return _Trace()

        def close(self):
            pass

    monkeypatch.setattr(s, "TraceBuilder", _Builder)
    for call in (
        lambda: s.trace_experiment(7),
        lambda: s.trace_crawler(30),
        lambda: s.trace_job(1),
    ):
        raw = call()
        assert "hunter2" not in raw and "<redacted>" in raw
