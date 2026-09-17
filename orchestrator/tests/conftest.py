"""
Shared pytest fixtures for orchestrator tests.
"""

import os
import sys
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from orchestrator.components.base import ComponentHealth, ComponentStatus
from orchestrator.config.settings import Settings


def pytest_addoption(parser):
    """Add custom command line options."""
    parser.addoption(
        "--e2e",
        action="store_true",
        default=False,
        help="Run end-to-end tests against live infrastructure",
    )


def pytest_configure(config):
    """Add custom markers."""
    config.addinivalue_line(
        "markers", "e2e: mark test as end-to-end test (requires live infrastructure)"
    )
    config.addinivalue_line(
        "markers", "multilogin: mark test as requiring Multilogin X browser automation"
    )


def pytest_collection_modifyitems(config, items):
    """Skip e2e tests unless --e2e flag is passed."""
    if config.getoption("--e2e"):
        # Set environment variable so the skipif decorator passes
        os.environ["E2E_TESTS"] = "1"
        return

    # Skip e2e tests
    skip_e2e = pytest.mark.skip(reason="Need --e2e option to run e2e tests")
    for item in items:
        if "test_e2e" in str(item.fspath):
            item.add_marker(skip_e2e)


@pytest.fixture
def cli_runner() -> CliRunner:
    """Provide a Click CLI test runner."""
    return CliRunner()


@pytest.fixture
def isolated_cli_runner() -> CliRunner:
    """Provide a Click CLI test runner that keeps stdout and stderr separate.

    Click 8.2 removed the ``mix_stderr`` argument and now always captures the
    two streams separately, exposing stderr as ``result.stderr``.
    """
    return CliRunner()


@pytest.fixture
def mock_settings() -> Settings:
    """Provide mock settings with test defaults."""
    return Settings(
        ENV="test",
        PM_API_PATH="/tmp/test_api",
        PM_RUNNER_PATH="/tmp/test_runner",
        PM_GUI_PATH="/tmp/test_gui",
        PM_API_HTTP_PORT=8090,
        PM_API_MYSQL_PORT=3307,
        PM_API_REDIS_PORT=8889,
        GRAFANA_PORT=3000,
        LOKI_PORT=3100,
        PROMETHEUS_PORT=9090,
        PM_API_BASE_URL="http://localhost:8090/api/v1",
        PM_API_EMAIL="test@test.com",
        PM_API_PASSWORD="testpass",
    )


@pytest.fixture
def temp_env_file(tmp_path: Path) -> Generator[Path, None, None]:
    """Create a temporary .env file for testing."""
    env_file = tmp_path / ".env"
    env_file.write_text("""
ENV=test
PM_API_PATH=./test_api
PM_RUNNER_PATH=./test_runner
PM_API_HTTP_PORT=8090
GRAFANA_PORT=3000
""")
    yield env_file


@pytest.fixture
def healthy_component_health() -> ComponentHealth:
    """Provide a healthy component health response."""
    return ComponentHealth(
        status=ComponentStatus.RUNNING,
        message="Component is running",
        details={"uptime": "1h 30m"},
        url="http://localhost:8090",
    )


@pytest.fixture
def stopped_component_health() -> ComponentHealth:
    """Provide a stopped component health response."""
    return ComponentHealth(
        status=ComponentStatus.STOPPED,
        message="Component is not running",
        details={},
    )


@pytest.fixture
def error_component_health() -> ComponentHealth:
    """Provide an error component health response."""
    return ComponentHealth(
        status=ComponentStatus.ERROR,
        message="Failed to start: port already in use",
        details={"error": "Port 8090 is already in use"},
    )


@pytest.fixture
def mock_docker_compose() -> Generator[MagicMock, None, None]:
    """Mock subprocess calls for docker-compose."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        yield mock_run


@pytest.fixture
def mock_httpx_client() -> Generator[MagicMock, None, None]:
    """Mock httpx client for API calls."""
    with patch("httpx.Client") as mock_client:
        client_instance = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=client_instance)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)
        yield client_instance


@pytest.fixture
def sample_log_entries() -> list[dict]:
    """Provide sample log entries for testing."""
    return [
        {
            "timestamp": "2025-01-15T10:30:45.000Z",
            "component": "api",
            "level": "info",
            "message": "Application started",
            "labels": {"environment": "production"},
        },
        {
            "timestamp": "2025-01-15T10:30:46.000Z",
            "component": "runner",
            "level": "error",
            "message": "Job failed: experiment_id=15 job_id=456",
            "labels": {"experiment_id": "15", "job_id": "456"},
        },
        {
            "timestamp": "2025-01-15T10:30:47.000Z",
            "component": "lambda",
            "level": "info",
            "message": "Lambda invocation completed",
            "labels": {"request_id": "abc-123"},
        },
    ]


@pytest.fixture(autouse=True)
def reset_settings_cache() -> Generator[None, None, None]:
    """Reset settings cache before each test."""
    from orchestrator.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def isolate_decision_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let the developer's own decision-function registry decide a test.

    ``DECISION_FUNCTIONS_FILE`` defaults to
    ``~/.config/primemover/decision-functions.toml``, which exists on the VM and
    on any box where somebody set one up. A test that reads it would pass or
    fail depending on whose machine it runs on, and the first symptom would be
    a CI/laptop disagreement nobody can reproduce. Point it at a path that
    cannot exist; a test that wants a registry writes one under ``tmp_path`` and
    sets the variable itself (a later ``setenv`` wins).

    Also drops the server's cached registry, which is module state.
    """
    monkeypatch.setenv("DECISION_FUNCTIONS_FILE", "/nonexistent/decision-functions.toml")

    if "orchestrator.lambda_service.server" in sys.modules:
        # Imported inside the guard: most tests never touch the lambda service,
        # and importing it installs a log handler and pulls in flask.
        from orchestrator.lambda_service import server

        server._registry = None


@pytest.fixture(autouse=True)
def block_network_in_mcp_tests(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make an unmocked MCP test fail loudly instead of hitting the real API.

    Every MCP tool talks to the PrimeMover API, and on a developer or VM box that
    API is genuinely running on :8090 with real experiment data behind it. A test
    that forgets to patch ``_api_request`` would therefore not fail -- it would
    quietly succeed against production, and a write tool test would mutate it.

    All current MCP tests patch correctly; this exists so that stays true. It is
    scoped to ``test_mcp_*`` files on purpose: a blanket block would break the
    ``--e2e`` suite, which is supposed to reach the network.
    """
    if not Path(str(request.node.fspath)).name.startswith("test_mcp_"):
        return

    import socket

    def _blocked(*args: object, **kwargs: object) -> None:
        raise RuntimeError(
            "This MCP test tried to open a network connection. MCP tests must be "
            "hermetic -- patch orchestrator.mcp_server.server._api_request (or the "
            "httpx layer) rather than letting the call reach the real API."
        )

    for attr in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, attr, _blocked, raising=False)
    for attr in ("create_connection", "getaddrinfo", "gethostbyname"):
        monkeypatch.setattr(socket, attr, _blocked, raising=False)
