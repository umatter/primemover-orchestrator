"""The HTTP transport must not expose the tool surface without a bearer token.

Every MCP tool -- including the ``pm_`` writes -- is reachable by whoever can
talk to the port; the token is the only per-request check. These tests drive
the ASGI app directly, so no socket is opened (see ``block_network_in_mcp_tests``).
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from orchestrator.mcp_server import server

TOKEN = "correct-horse-battery-staple"  # noqa: S105 - test fixture, not a secret


# One client for the module: the SDK's StreamableHTTPSessionManager can only be
# started once per FastMCP instance, and the app wraps the module-level `mcp`.
@pytest.fixture(scope="module")
def client():
    with TestClient(server.build_http_app(TOKEN), raise_server_exceptions=True) as c:
        yield c


def _mcp_post(client: TestClient, headers: dict[str, str]):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream", **headers},
    )


def test_health_is_open(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_missing_token_is_rejected(client: TestClient) -> None:
    r = _mcp_post(client, {})
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_wrong_token_is_rejected(client: TestClient) -> None:
    assert _mcp_post(client, {"Authorization": "Bearer nope"}).status_code == 401


def test_wrong_scheme_is_rejected(client: TestClient) -> None:
    assert _mcp_post(client, {"Authorization": f"Basic {TOKEN}"}).status_code == 401


def test_correct_token_reaches_the_mcp_layer(client: TestClient) -> None:
    # Whatever the MCP transport answers to a bare tools/list without a session,
    # it must not be the gate's 401: the request got through.
    r = _mcp_post(client, {"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code != 401


def test_http_mode_refuses_to_start_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    assert server._auth_token_from_env() is None
    monkeypatch.setenv("MCP_AUTH_TOKEN", "   ")
    assert server._auth_token_from_env() is None
    monkeypatch.setenv("MCP_AUTH_TOKEN", " abc ")
    assert server._auth_token_from_env() == "abc"
