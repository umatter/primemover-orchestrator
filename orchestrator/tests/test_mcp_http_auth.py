"""The HTTP transport must not expose the tool surface without a bearer token.

Every MCP tool -- including the ``pm_`` writes -- is reachable by whoever can
talk to the port; the token is the only per-request check. These tests drive
the ASGI app directly, so no socket is opened (see ``block_network_in_mcp_tests``).
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from orchestrator.mcp_server import server

TOKENS = {
    "ulrich": "correct-horse-battery-staple",  # noqa: S105 - test fixtures, not secrets
    "matthias": "another-perfectly-fine-token",  # noqa: S105
}


# One client for the module: the SDK's StreamableHTTPSessionManager can only be
# started once per FastMCP instance, and the app wraps the module-level `mcp`.
@pytest.fixture(scope="module")
def client():
    with TestClient(server.build_http_app(TOKENS), raise_server_exceptions=True) as c:
        yield c


def _mcp_post(client: TestClient, headers: dict[str, str]):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream", **headers},
    )


def test_health_is_open(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_health_reports_ok_secrets_store(
    client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "secrets.toml"
    p.write_text('[a]\nb = "v"\n')
    p.chmod(0o600)
    monkeypatch.setenv("PMSPEC_SECRETS_FILE", str(p))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "secrets_store": "ok"}


def test_health_reports_missing_secrets_store(
    client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PMSPEC_SECRETS_FILE", str(tmp_path / "nope.toml"))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "secrets_store": "missing"}


def test_health_reports_insecure_secrets_store(
    client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "secrets.toml"
    p.write_text('[a]\nb = "v"\n')
    p.chmod(0o644)
    monkeypatch.setenv("PMSPEC_SECRETS_FILE", str(p))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "secrets_store": "insecure"}


def test_health_reports_invalid_secrets_store(
    client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "secrets.toml"
    p.write_text("this is not [valid toml")
    p.chmod(0o600)
    monkeypatch.setenv("PMSPEC_SECRETS_FILE", str(p))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "secrets_store": "invalid"}


def test_health_never_leaks_the_path_or_a_value(
    client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "secrets.toml"
    p.write_text('[a]\nb = "super-secret-value"\n')
    p.chmod(0o600)
    monkeypatch.setenv("PMSPEC_SECRETS_FILE", str(p))
    r = client.get("/health")
    assert "super-secret-value" not in r.text
    assert str(p) not in r.text


def test_missing_token_is_rejected(client: TestClient) -> None:
    r = _mcp_post(client, {})
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_wrong_token_is_rejected(client: TestClient) -> None:
    assert _mcp_post(client, {"Authorization": "Bearer nope"}).status_code == 401


def test_wrong_scheme_is_rejected(client: TestClient) -> None:
    assert _mcp_post(client, {"Authorization": f"Basic {TOKENS['ulrich']}"}).status_code == 401


@pytest.mark.parametrize("name", sorted(TOKENS))
def test_each_named_token_reaches_the_mcp_layer(client: TestClient, name: str, caplog) -> None:
    # Whatever the MCP transport answers to a bare tools/list without a session,
    # it must not be the gate's 401: the request got through -- under that name.
    with caplog.at_level("INFO", logger="orchestrator.mcp.auth"):
        r = _mcp_post(client, {"Authorization": f"Bearer {TOKENS[name]}"})
    assert r.status_code != 401
    assert f"user={name}" in caplog.text


def test_single_token_string_still_accepted() -> None:
    # Older callers pass one token; it becomes the name "default".
    app = server.build_http_app("a-single-legacy-token-value")
    assert app is not None


def test_no_tokens_is_refused() -> None:
    with pytest.raises(ValueError):
        server.build_http_app({})


def test_tokens_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("MCP_AUTH_TOKENS", raising=False)
    assert server._auth_tokens_from_env() == {}

    monkeypatch.setenv(
        "MCP_AUTH_TOKENS", " ulrich:0123456789abcdef , matthias:fedcba9876543210 ,"
    )  # pragma: allowlist secret
    assert server._auth_tokens_from_env() == {
        "ulrich": "0123456789abcdef",  # pragma: allowlist secret
        "matthias": "fedcba9876543210",  # pragma: allowlist secret
    }

    # The single-token variable still works, as "default", alongside named ones.
    monkeypatch.setenv("MCP_AUTH_TOKEN", "legacy-token-0123456789")
    assert server._auth_tokens_from_env()["default"] == "legacy-token-0123456789"


@pytest.mark.parametrize(
    "value",
    [
        "no-colon-here",
        ":missing-name-0123456789",
        "ulrich:",
        "ulrich:short",
        "ulrich:0123456789abcdef,ulrich:fedcba9876543210",  # pragma: allowlist secret
    ],
)
def test_malformed_token_entries_raise(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("MCP_AUTH_TOKENS", value)
    with pytest.raises(ValueError):
        server._auth_tokens_from_env()
