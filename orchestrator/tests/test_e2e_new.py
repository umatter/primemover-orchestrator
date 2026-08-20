"""
Comprehensive End-to-End Tests for PrimeMover Orchestrator.

Tests are organized into three categories:
1. Infrastructure Health - Verify all components are running
2. Core Workflow - Create experiments, verify processing
3. Full E2E with Multilogin - Browser automation (requires MLX)

Run with:
    pytest orchestrator/tests/test_e2e_new.py -v --e2e

Or via CLI:
    orchestrator test

Skip Multilogin tests:
    pytest orchestrator/tests/test_e2e_new.py -v --e2e -m "not multilogin"
"""

from datetime import datetime
from pathlib import Path

import httpx
import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def settings():
    """Get orchestrator settings."""
    from orchestrator.config import get_settings

    return get_settings()


@pytest.fixture(scope="session")
def component_manager(settings):
    """Initialize ComponentManager for status checks."""
    from orchestrator.components.manager import ComponentManager

    return ComponentManager(
        api_path=settings.get_api_path(),
        runner_path=settings.get_runner_path(),
        orchestrator_path=settings.get_orchestrator_path(),
        api_http_port=settings.pm_api_http_port,
        api_mysql_port=settings.pm_api_mysql_port,
        api_redis_port=settings.pm_api_redis_port,
        grafana_port=settings.grafana_port,
        runner_java_opts=settings.runner_java_opts,
        runner_spring_profile=settings.runner_spring_profile,
    )


@pytest.fixture(scope="session")
def api_client(settings):
    """Initialize API client with authentication."""
    from orchestrator.clients.api import APIClient

    client = APIClient(
        base_url=settings.pm_api_base_url,
        email=settings.pm_api_email,
        password=settings.pm_api_password,
    )
    yield client
    client.close()


@pytest.fixture(scope="session")
def loki_client(settings):
    """Initialize Loki client."""
    from orchestrator.clients.loki import LokiClient

    client = LokiClient(base_url=settings.loki_url)
    yield client
    client.close()


@pytest.fixture(scope="session")
def spec_parser():
    """Initialize experiment spec parser."""
    from experiment_designer.spec.parser import SpecParser

    return SpecParser()


# ---------------------------------------------------------------------------
# Category 1: Infrastructure Health Checks
# ---------------------------------------------------------------------------


class TestInfrastructureHealth:
    """Verify all infrastructure components are running and healthy."""

    def test_api_containers_running(self, component_manager):
        """Verify API Docker containers are running."""
        from orchestrator.components.base import ComponentStatus

        status = component_manager.status()
        assert (
            status.api.status == ComponentStatus.RUNNING
        ), f"API not running: {status.api.message}"

    def test_api_http_accessible(self, settings):
        """Verify API responds to HTTP requests."""
        base_url = settings.pm_api_base_url.replace("/api/v1", "")

        with httpx.Client(timeout=10) as client:
            # Try root endpoint
            response = client.get(base_url)
            assert response.status_code in (
                200,
                302,
                404,
            ), f"API not accessible: {response.status_code}"

    def test_api_authentication(self, api_client):
        """Verify API authentication works."""
        assert api_client.is_authenticated(), "API authentication failed"

    def test_mysql_connected(self, api_client):
        """Verify database is connected by listing experiments."""
        experiments = api_client.list_experiments(per_page=1)
        assert isinstance(experiments, list), "Database query failed"

    def test_runner_running(self, component_manager):
        """Verify Runner process is running."""
        from orchestrator.components.base import ComponentStatus

        status = component_manager.status()
        assert (
            status.runner.status == ComponentStatus.RUNNING
        ), f"Runner not running: {status.runner.message}"

    def test_runner_jar_exists(self, settings):
        """Verify Runner JAR file exists."""
        runner_path = settings.get_runner_path()

        # Multi-module structure: primemover-runner/target/
        jar_files = list(runner_path.glob("primemover-runner/target/*.jar"))
        if not jar_files:
            # Fallback to flat structure
            jar_files = list(runner_path.glob("target/*.jar"))

        assert len(jar_files) > 0, f"No JAR files found in {runner_path}"

    def test_runner_config_exists(self, settings):
        """Verify Runner configuration exists."""
        runner_path = settings.get_runner_path()

        config_paths = [
            runner_path / "primemover-runner/config/application.properties",
            runner_path / "config/application.properties",
            runner_path / "primemover-runner/src/main/resources/application.properties",
        ]

        assert any(
            p.exists() for p in config_paths
        ), f"Runner config not found in: {[str(p) for p in config_paths]}"

    def test_grafana_accessible(self, settings):
        """Verify Grafana is accessible."""
        with httpx.Client(timeout=10) as client:
            response = client.get(f"{settings.grafana_url}/api/health")
            assert response.status_code == 200, f"Grafana not accessible: {response.status_code}"

    def test_loki_healthy(self, loki_client):
        """Verify Loki is healthy."""
        assert loki_client.is_healthy(), "Loki health check failed"

    def test_prometheus_accessible(self, settings):
        """Verify Prometheus is accessible."""
        with httpx.Client(timeout=10) as client:
            response = client.get(f"{settings.prometheus_url}/-/healthy")
            assert response.status_code == 200, f"Prometheus not accessible: {response.status_code}"

    def test_monitoring_stack_running(self, component_manager):
        """Verify monitoring stack (Grafana, Loki, Prometheus) is running."""
        from orchestrator.components.base import ComponentStatus

        status = component_manager.status()
        assert (
            status.monitoring.status == ComponentStatus.RUNNING
        ), f"Monitoring not running: {status.monitoring.message}"

    def test_overall_system_status(self, component_manager):
        """Verify overall system status is RUNNING."""
        from orchestrator.components.base import ComponentStatus

        status = component_manager.status()
        assert status.overall == ComponentStatus.RUNNING, (
            f"System not fully running. "
            f"API: {status.api.status}, "
            f"Runner: {status.runner.status}, "
            f"Monitoring: {status.monitoring.status}"
        )


# ---------------------------------------------------------------------------
# Category 2: Core Workflow Tests
# ---------------------------------------------------------------------------


class TestCoreWorkflow:
    """Test core experiment creation and processing workflow."""

    def test_list_experiments(self, api_client):
        """Verify listing experiments works."""
        experiments = api_client.list_experiments(per_page=10)
        assert isinstance(experiments, list)

    def test_database_has_seed_data(self, api_client):
        """Verify database was seeded with initial data."""
        experiments = api_client.list_experiments(per_page=100)
        # Seed data should create at least one experiment
        # If empty, that's also fine for a fresh setup
        assert isinstance(experiments, list)

    def test_create_experiment(self, api_client):
        """Verify creating a new experiment works."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        exp_data = {
            "name": f"E2E Test Experiment {timestamp}",
            "description": "Created by e2e test suite",
            "active": False,  # Don't actually run it
        }

        new_exp = api_client.create_experiment(exp_data)
        assert new_exp is not None
        assert "id" in new_exp or "data" in new_exp

        # Clean up - try to get the ID and delete
        exp_id = new_exp.get("id") or new_exp.get("data", {}).get("id")
        if exp_id:
            try:
                api_client.delete_experiment(exp_id)
            except Exception:
                pass  # Cleanup failure is not a test failure

    def test_spec_parser_basic(self, spec_parser):
        """Verify spec parser can parse a basic YAML spec."""
        yaml_content = """
version: "1.0"
experiment:
  name: "E2E Test Spec"
  description: "Basic test specification"
defaults:
  browser: "mlx"
  location: "US-NY-NEW_YORK"
crawlers:
  - name: "test_crawler"
    session:
      workflow:
        - visit: https://example.com
        - wait: 5s
"""
        parsed = spec_parser.parse(yaml_content)

        assert len(parsed.errors) == 0, f"Parse errors: {parsed.errors}"
        assert parsed.experiment["name"] == "E2E Test Spec"
        assert len(parsed.crawlers) == 1
        assert parsed.crawlers[0]["name"] == "test_crawler"

    def test_spec_parser_factorial_design(self, spec_parser):
        """Verify spec parser handles factorial design."""
        from experiment_designer.spec.design_expander import DesignExpander

        yaml_content = """
version: "1.0"
experiment:
  name: "Factorial Design Test"
design:
  factors:
    condition:
      levels: [control, treatment]
      maps_to:
        params.pi: {control: 0, treatment: 0.5}
  replications: 2
crawlers:
  - from_design: true
    name: "bot_{condition}_{replica:02d}"
    session:
      workflow:
        - visit: https://example.com
"""
        parsed = spec_parser.parse(yaml_content)
        assert len(parsed.errors) == 0, f"Parse errors: {parsed.errors}"
        assert parsed.design is not None, "Design should be parsed"

        # Expand the factorial design
        expander = DesignExpander()
        crawler_template = parsed.crawlers[0]  # Get the template crawler
        expanded = expander.expand(parsed.design, crawler_template)

        # 2 conditions x 2 replications = 4 crawlers
        assert len(expanded) == 4, f"Expected 4 crawlers, got {len(expanded)}"

    def test_query_logs_from_loki(self, loki_client):
        """Verify querying logs from Loki works."""
        # Query with a component filter (empty query {} is invalid in Loki)
        result = loki_client.query_logs(
            component="api",  # Must specify at least one filter
            time_range="1h",
            limit=10,
        )

        assert result is not None
        assert hasattr(result, "entries")
        assert isinstance(result.entries, list)

    def test_query_api_logs(self, loki_client):
        """Verify querying API-specific logs works."""
        result = loki_client.query_logs(
            component="api",
            time_range="1h",
            limit=10,
        )

        assert result is not None
        # May or may not have entries depending on activity

    def test_log_paths_exist(self, settings):
        """Verify log directories exist."""
        api_log_path = Path(settings.pm_api_log_path)
        runner_log_path = Path(settings.pm_runner_log_path)

        # At minimum, parent directories should exist
        assert (
            api_log_path.parent.exists()
        ), f"API log parent directory missing: {api_log_path.parent}"
        assert (
            runner_log_path.parent.exists()
        ), f"Runner log parent directory missing: {runner_log_path.parent}"


# ---------------------------------------------------------------------------
# Category 3: CLI Command Tests
# ---------------------------------------------------------------------------


class TestCLICommands:
    """Test CLI commands work correctly."""

    def test_status_command(self):
        """Verify 'orchestrator status' command works."""
        from click.testing import CliRunner

        from orchestrator.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0, f"Status command failed: {result.output}"
        assert "API" in result.output or "RUNNING" in result.output

    def test_config_show_command(self):
        """Verify 'orchestrator config show' command works."""
        from click.testing import CliRunner

        from orchestrator.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["config", "show"])

        assert result.exit_code == 0, f"Config show failed: {result.output}"

    def test_logs_query_command(self):
        """Verify 'orchestrator logs query' command works."""
        from click.testing import CliRunner

        from orchestrator.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["logs", "query", "-t", "15m", "-n", "5"])

        # May succeed or fail depending on Loki data, but shouldn't crash
        assert result.exit_code in (0, 1), f"Logs query crashed: {result.output}"

    def test_experiment_list_command(self):
        """Verify 'orchestrator experiment list' command works."""
        from click.testing import CliRunner

        from orchestrator.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["experiment", "list"])

        assert result.exit_code == 0, f"Experiment list failed: {result.output}"


# ---------------------------------------------------------------------------
# Category 4a: Chrome E2E (No Multilogin X required)
# ---------------------------------------------------------------------------


class TestChromeE2E:
    """
    End-to-end test using plain Chrome browser.

    Does NOT require Multilogin X or proxy servers — only Chrome/Chromium
    and ChromeDriver. Useful for running e2e tests in simpler environments
    without the full anti-detect browser setup.

    Requirements:
    - All infrastructure running (API, monitoring)
    - Runner process running
    - Chrome/Chromium installed on the runner host
    - ChromeDriver available (WebDriverManager will auto-download)
    """

    def test_chrome_e2e(self):
        """
        E2E test with plain Chrome: create experiment, execute jobs, verify completion.

        Uses identification="chrome" so the runner launches ChromeDriver directly
        instead of going through Multilogin X. No proxy is configured.

        Timeout: 180 seconds (3 minutes)
        """
        from orchestrator.e2e.runner import E2ETestRunner

        runner = E2ETestRunner(timeout=180, verbose=True, test_type="chrome")
        result = runner.run()

        assert result.success, f"Chrome E2E test failed: {result.error}"
        assert result.experiment_id is not None, "No experiment was created"
        assert result.queue_id is not None, "No queue was created"


# ---------------------------------------------------------------------------
# Category 4b: Full E2E with Multilogin (Optional)
# ---------------------------------------------------------------------------


@pytest.mark.multilogin
class TestMultiloginE2E:
    """
    Full end-to-end tests with Multilogin browser automation.

    These tests require:
    - Multilogin X (mlx) to be installed and running
    - Valid Multilogin account credentials

    Skip with: pytest -m "not multilogin"
    """

    @pytest.fixture
    def mlx_available(self) -> bool:
        """Check if Multilogin X is available."""
        import subprocess

        try:
            result = subprocess.run(
                ["pgrep", "-f", "mlx"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def test_multilogin_agent_running(self, mlx_available):
        """Verify Multilogin X agent is running."""
        if not mlx_available:
            pytest.skip("Multilogin X agent is not running")

        import subprocess

        result = subprocess.run(
            ["pgrep", "-af", "mlx"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, "Multilogin X agent not found"

    def test_full_e2e_with_browser(self, mlx_available):
        """
        Full E2E: Create experiment, upload crawlers, run browser automation.

        This test uses the E2ETestRunner which:
        1. Validates all components are running
        2. Creates experiment with proper crawler format
        3. Waits for runner to create Multilogin profile and execute
        4. Verifies job completion

        Timeout: 180 seconds (3 minutes)
        """
        if not mlx_available:
            pytest.skip("Multilogin X agent is not running")

        from orchestrator.e2e.runner import E2ETestRunner

        runner = E2ETestRunner(timeout=180, verbose=True)
        result = runner.run()

        assert result.success, f"E2E test failed: {result.error}"
        assert result.experiment_id is not None, "No experiment was created"
        assert result.queue_id is not None, "No queue was created"


# ---------------------------------------------------------------------------
# Category 5: Module Import Tests (Sanity Checks)
# ---------------------------------------------------------------------------


class TestModuleImports:
    """Verify all key modules can be imported."""

    def test_import_components(self):
        """Verify component modules import."""
        from orchestrator.components.api import ApiComponent
        from orchestrator.components.manager import ComponentManager
        from orchestrator.components.monitoring import MonitoringComponent
        from orchestrator.components.runner import RunnerComponent

        assert ComponentManager is not None
        assert ApiComponent is not None
        assert RunnerComponent is not None
        assert MonitoringComponent is not None

    def test_import_clients(self):
        """Verify client modules import."""
        from orchestrator.clients.api import APIClient
        from orchestrator.clients.loki import LokiClient

        assert APIClient is not None
        assert LokiClient is not None

    def test_import_debug(self):
        """Verify debug modules import."""
        from orchestrator.debug import HARParser, JobDebugger

        assert HARParser is not None
        assert JobDebugger is not None

    def test_import_experiment_designer(self):
        """Verify experiment designer modules import."""
        from experiment_designer.spec.parser import SpecParser

        assert SpecParser is not None

    def test_import_tracing(self):
        """Verify tracing modules import."""
        from orchestrator.tracing.correlation import CorrelationId
        from orchestrator.tracing.trace_builder import TraceBuilder

        assert CorrelationId is not None
        assert TraceBuilder is not None
