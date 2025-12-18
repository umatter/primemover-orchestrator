"""
Shared pytest fixtures for orchestrator tests.
"""

import os
from pathlib import Path
from typing import Generator
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
    config.addinivalue_line("markers", "e2e: mark test as end-to-end test (requires live infrastructure)")


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
    """Provide a Click CLI test runner with isolated filesystem."""
    return CliRunner(mix_stderr=False)


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
    env_file.write_text(
        """
ENV=test
PM_API_PATH=./test_api
PM_RUNNER_PATH=./test_runner
PM_API_HTTP_PORT=8090
GRAFANA_PORT=3000
"""
    )
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
