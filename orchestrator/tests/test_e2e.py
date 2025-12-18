"""
End-to-end tests for PrimeMover orchestrator.

These tests verify that all components are running and can communicate.
Run after `orchestrator setup` or `orchestrator up` completes.

Run with:
    pytest orchestrator/tests/test_e2e.py -v --e2e

Or via CLI:
    orchestrator test

Note: These tests require live infrastructure (API, monitoring stack).
      They do NOT require Multilogin X (browser automation tests are skipped).
"""

import os
from datetime import datetime, timedelta

import pytest

# Skip all tests in this module unless --e2e flag is passed
pytestmark = pytest.mark.skipif(
    not os.environ.get("E2E_TESTS", "").lower() in ("1", "true", "yes"),
    reason="E2E tests require --e2e flag or E2E_TESTS=1 env var",
)


class TestAPIStack:
    """Tests for the API Docker stack (Laravel, MySQL, Redis)."""

    def test_api_health_endpoint(self):
        """Test API responds to health check."""
        import httpx

        from orchestrator.config import get_settings

        settings = get_settings()
        base_url = settings.pm_api_base_url.rstrip("/api/v1")

        with httpx.Client(timeout=10) as client:
            # Try health endpoint or just root
            response = client.get(f"{base_url}/health")
            if response.status_code == 404:
                # Fall back to root
                response = client.get(base_url)

            assert response.status_code in (200, 302), f"API not responding: {response.status_code}"

    def test_api_authentication(self):
        """Test API authentication works."""
        from orchestrator.clients.api import APIClient

        client = APIClient()
        assert client.is_authenticated(), "API authentication failed"
        client.close()

    def test_api_can_list_experiments(self):
        """Test API can list experiments."""
        from orchestrator.clients.api import APIClient

        client = APIClient()
        experiments = client.list_experiments()

        # Should return a list (may be empty)
        assert isinstance(experiments, list), "Expected list of experiments"
        client.close()

    def test_database_has_seed_data(self):
        """Test database has been seeded with default user."""
        from orchestrator.clients.api import APIClient

        client = APIClient()

        # If we can authenticate, the user exists
        assert client.is_authenticated(), "Seed data not present (default user missing)"
        client.close()

    def test_api_can_create_experiment(self):
        """Test API can create an experiment."""
        from orchestrator.clients.api import APIClient

        client = APIClient()

        # Create a test experiment
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        experiment_data = {
            "name": f"E2E Test Experiment {timestamp}",
            "description": "Created by e2e test - safe to delete",
            "active": False,
        }

        result = client.create_experiment(experiment_data)
        assert result is not None, "Failed to create experiment"
        assert "id" in result, "Created experiment has no ID"

        # Clean up - delete the experiment
        try:
            client.delete_experiment(result["id"])
        except Exception:
            pass  # Best effort cleanup

        client.close()


class TestMonitoringStack:
    """Tests for the monitoring stack (Grafana, Loki, Prometheus)."""

    def test_grafana_accessible(self):
        """Test Grafana is accessible."""
        import httpx

        from orchestrator.config import get_settings

        settings = get_settings()

        with httpx.Client(timeout=10) as client:
            response = client.get(f"{settings.grafana_url}/api/health")
            assert response.status_code == 200, f"Grafana not healthy: {response.status_code}"

    def test_loki_accessible(self):
        """Test Loki is accessible and ready."""
        from orchestrator.clients.loki import LokiClient

        client = LokiClient()
        assert client.is_healthy(), "Loki is not healthy"
        client.close()

    def test_loki_can_query(self):
        """Test Loki can accept queries."""
        from orchestrator.clients.loki import LokiClient

        client = LokiClient()

        # Query for any logs in last hour (may be empty, but should not error)
        result = client.query_logs(time_range="1h", limit=10)
        assert result is not None, "Loki query returned None"
        assert hasattr(result, "entries"), "Loki result has no entries attribute"

        client.close()

    def test_prometheus_accessible(self):
        """Test Prometheus is accessible."""
        import httpx

        from orchestrator.config import get_settings

        settings = get_settings()

        with httpx.Client(timeout=10) as client:
            response = client.get(f"{settings.prometheus_url}/-/healthy")
            assert response.status_code == 200, f"Prometheus not healthy: {response.status_code}"


class TestLogAggregation:
    """Tests for log aggregation functionality."""

    def test_log_aggregator_initializes(self):
        """Test LogAggregator can be created."""
        from orchestrator.logs.aggregator import LogAggregator

        aggregator = LogAggregator()
        assert aggregator is not None
        aggregator.close()

    def test_log_aggregator_query(self):
        """Test LogAggregator can query logs."""
        from orchestrator.logs.aggregator import LogAggregator

        aggregator = LogAggregator()
        result = aggregator.query(time_range="1h", limit=10)

        assert result is not None
        assert hasattr(result, "entries")
        assert hasattr(result, "sources")

        aggregator.close()

    def test_log_paths_exist(self):
        """Test configured log paths exist."""
        from pathlib import Path

        from orchestrator.config import get_settings

        settings = get_settings()

        # API log path
        api_log_path = Path(settings.pm_api_log_path)
        assert api_log_path.exists(), f"API log path does not exist: {api_log_path}"

        # Runner log path (may not exist if runner hasn't run)
        runner_log_path = Path(settings.pm_runner_log_path)
        # Just check parent exists
        assert runner_log_path.parent.exists(), f"Runner log parent does not exist: {runner_log_path.parent}"


class TestRunnerSetup:
    """Tests for Runner setup (not execution - requires Multilogin)."""

    def test_runner_jar_exists(self):
        """Test Runner JAR file exists."""
        from pathlib import Path

        from orchestrator.config import get_settings

        settings = get_settings()
        runner_path = Path(settings.pm_runner_path)

        # Find JAR file
        jar_files = list(runner_path.glob("target/*.jar"))
        assert len(jar_files) > 0, f"No JAR files found in {runner_path}/target/"

    def test_runner_config_exists(self):
        """Test Runner configuration exists."""
        from pathlib import Path

        from orchestrator.config import get_settings

        settings = get_settings()
        runner_path = Path(settings.pm_runner_path)

        # Check for application properties
        config_paths = [
            runner_path / "src/main/resources/application.properties",
            runner_path / "src/main/resources/application.yml",
            runner_path / "src/main/resources/application-local.properties",
        ]

        config_exists = any(p.exists() for p in config_paths)
        assert config_exists, "Runner configuration not found"


class TestComponentManager:
    """Tests for ComponentManager functionality."""

    def test_status_all_components(self):
        """Test getting status of all components."""
        from orchestrator.components.manager import ComponentManager

        manager = ComponentManager()
        status = manager.status_all()

        assert "api" in status, "API component missing from status"
        assert "monitoring" in status, "Monitoring component missing from status"

    def test_api_component_running(self):
        """Test API component reports as running."""
        from orchestrator.components.manager import ComponentManager

        manager = ComponentManager()
        status = manager.status_all()

        api_status = status.get("api")
        assert api_status is not None, "API status is None"
        assert api_status.is_healthy, f"API is not healthy: {api_status.message}"

    def test_monitoring_component_running(self):
        """Test Monitoring component reports as running."""
        from orchestrator.components.manager import ComponentManager

        manager = ComponentManager()
        status = manager.status_all()

        monitoring_status = status.get("monitoring")
        assert monitoring_status is not None, "Monitoring status is None"
        assert monitoring_status.is_healthy, f"Monitoring is not healthy: {monitoring_status.message}"


class TestDebugFunctionality:
    """Tests for debug module functionality."""

    def test_har_parser_import(self):
        """Test HAR parser can be imported."""
        from orchestrator.debug import HARParser, HARSummary

        parser = HARParser()
        assert parser is not None

    def test_job_debugger_import(self):
        """Test JobDebugger can be imported."""
        from orchestrator.debug import JobDebugger

        debugger = JobDebugger()
        assert debugger is not None


class TestExperimentDesigner:
    """Tests for Experiment Designer functionality."""

    def test_spec_parser_import(self):
        """Test spec parser can be imported."""
        from experiment_designer.spec.parser import SpecParser

        parser = SpecParser()
        assert parser is not None

    def test_validate_sample_spec(self, tmp_path):
        """Test validating a minimal spec."""
        from experiment_designer.spec.parser import SpecParser
        from experiment_designer.spec.validator import SpecValidator

        # Create minimal valid spec
        spec_content = """
version: "1.0"
experiment:
  name: "E2E Test"
crawlers:
  - name: "test_crawler"
    session:
      start: now
      workflow:
        - visit: https://example.com
        - wait: 5s
"""
        spec_file = tmp_path / "test_spec.yaml"
        spec_file.write_text(spec_content)

        parser = SpecParser()
        spec = parser.parse_file(spec_file)

        validator = SpecValidator()
        errors = validator.validate(spec)

        assert len(errors) == 0, f"Spec validation errors: {errors}"


class TestCLICommands:
    """Tests that CLI commands work against live system."""

    def test_status_command(self):
        """Test status command runs successfully."""
        from click.testing import CliRunner

        from orchestrator.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["status"])

        # Should complete (may exit 0 or 1 depending on runner status)
        assert result.exit_code in (0, 1), f"Status command crashed: {result.output}"
        assert "api" in result.output.lower() or "API" in result.output

    def test_config_show_command(self):
        """Test config show command runs successfully."""
        from click.testing import CliRunner

        from orchestrator.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["config", "show"])

        assert result.exit_code == 0, f"Config show failed: {result.output}"

    def test_logs_query_command(self):
        """Test logs query command runs successfully."""
        from click.testing import CliRunner

        from orchestrator.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["logs", "query", "-t", "15m", "-n", "5"])

        # Should complete without error (may have no logs)
        assert result.exit_code == 0, f"Logs query failed: {result.output}"

    def test_experiment_list_command(self):
        """Test experiment list command runs successfully."""
        from click.testing import CliRunner

        from orchestrator.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["experiment", "list"])

        assert result.exit_code == 0, f"Experiment list failed: {result.output}"


# =============================================================================
# Fixtures for E2E tests
# =============================================================================


@pytest.fixture(scope="module")
def api_client():
    """Provide an authenticated API client for the test module."""
    from orchestrator.clients.api import APIClient

    client = APIClient()
    yield client
    client.close()


@pytest.fixture(scope="module")
def loki_client():
    """Provide a Loki client for the test module."""
    from orchestrator.clients.loki import LokiClient

    client = LokiClient()
    yield client
    client.close()
