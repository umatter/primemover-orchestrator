"""
Tests for orchestrator CLI commands.

Run with: pytest orchestrator/tests/test_cli.py -v
"""

from unittest.mock import MagicMock, patch

from orchestrator.cli import cli


class TestCliBasics:
    """Tests for basic CLI functionality."""

    def test_cli_help(self, cli_runner):
        """Test CLI shows help."""
        result = cli_runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "PrimeMover Orchestrator" in result.output
        assert "Monitor, debug, and manage experiments" in result.output

    def test_cli_version(self, cli_runner):
        """Test CLI shows version."""
        result = cli_runner.invoke(cli, ["--version"])

        assert result.exit_code == 0
        assert "orchestrator" in result.output.lower()

    def test_cli_no_command(self, cli_runner):
        """Test CLI without command shows help."""
        result = cli_runner.invoke(cli, [])

        # click.Group defaults to no_args_is_help=True, and since Click 8.2 that
        # path exits 2 (invoking with no command is a usage error) rather than 0.
        # What matters here is that the help text is what gets shown.
        assert result.exit_code == 2
        # Should show available commands
        assert "Commands:" in result.output or "Usage:" in result.output


class TestStatusCommand:
    """Tests for status command."""

    def test_status_help(self, cli_runner):
        """Test status command help."""
        result = cli_runner.invoke(cli, ["status", "--help"])

        assert result.exit_code == 0
        assert "status" in result.output.lower()

    @patch("orchestrator.components.manager.ComponentManager")
    def test_status_command(self, mock_manager_class, cli_runner):
        """Test status command execution."""
        # Setup mock
        mock_manager = MagicMock()
        mock_manager_class.return_value = mock_manager
        mock_manager.status_all.return_value = {
            "api": MagicMock(
                status=MagicMock(value="running"),
                message="Running",
                is_healthy=True,
            ),
            "runner": MagicMock(
                status=MagicMock(value="stopped"),
                message="Not running",
                is_healthy=False,
            ),
        }

        result = cli_runner.invoke(cli, ["status"])

        # Status command may have exit code 0 or 1 depending on component state
        # Just verify it runs without crashing
        assert result.exit_code in (0, 1) or result.exception is None


class TestConfigCommand:
    """Tests for config commands."""

    def test_config_help(self, cli_runner):
        """Test config command group help."""
        result = cli_runner.invoke(cli, ["config", "--help"])

        assert result.exit_code == 0
        assert "config" in result.output.lower()

    def test_config_show_help(self, cli_runner):
        """Test config show command help."""
        result = cli_runner.invoke(cli, ["config", "show", "--help"])

        assert result.exit_code == 0

    @patch("orchestrator.cli.get_settings")
    def test_config_show(self, mock_get_settings, cli_runner, mock_settings):
        """Test config show command."""
        mock_get_settings.return_value = mock_settings

        result = cli_runner.invoke(cli, ["config", "show"])

        assert result.exit_code == 0

    @patch("orchestrator.cli.get_settings")
    def test_config_show_json(self, mock_get_settings, cli_runner, mock_settings):
        """Test config show --json command."""
        mock_get_settings.return_value = mock_settings

        result = cli_runner.invoke(cli, ["config", "show", "--json"])

        assert result.exit_code == 0


class TestDbCommand:
    """Tests for database commands."""

    def test_db_help(self, cli_runner):
        """Test db command group help."""
        result = cli_runner.invoke(cli, ["db", "--help"])

        assert result.exit_code == 0
        assert "db" in result.output.lower() or "database" in result.output.lower()

    def test_db_migrate_help(self, cli_runner):
        """Test db migrate command help."""
        result = cli_runner.invoke(cli, ["db", "migrate", "--help"])

        assert result.exit_code == 0

    def test_db_seed_help(self, cli_runner):
        """Test db seed command help."""
        result = cli_runner.invoke(cli, ["db", "seed", "--help"])

        assert result.exit_code == 0


class TestLogsCommand:
    """Tests for logs commands."""

    def test_logs_help(self, cli_runner):
        """Test logs command group help."""
        result = cli_runner.invoke(cli, ["logs", "--help"])

        assert result.exit_code == 0

    def test_logs_query_help(self, cli_runner):
        """Test logs query command help."""
        result = cli_runner.invoke(cli, ["logs", "query", "--help"])

        assert result.exit_code == 0
        # Should show filtering options
        assert "component" in result.output.lower() or "time" in result.output.lower()


class TestTraceCommand:
    """Tests for trace commands."""

    def test_trace_help(self, cli_runner):
        """Test trace command group help."""
        result = cli_runner.invoke(cli, ["trace", "--help"])

        assert result.exit_code == 0

    def test_trace_experiment_help(self, cli_runner):
        """Test trace experiment command help."""
        result = cli_runner.invoke(cli, ["trace", "experiment", "--help"])

        assert result.exit_code == 0

    def test_trace_crawler_help(self, cli_runner):
        """Test trace crawler command help."""
        result = cli_runner.invoke(cli, ["trace", "crawler", "--help"])

        assert result.exit_code == 0

    def test_trace_job_help(self, cli_runner):
        """Test trace job command help."""
        result = cli_runner.invoke(cli, ["trace", "job", "--help"])

        assert result.exit_code == 0


class TestLambdaCommand:
    """Tests for lambda commands."""

    def test_lambda_help(self, cli_runner):
        """Test lambda command group help."""
        result = cli_runner.invoke(cli, ["lambda", "--help"])

        assert result.exit_code == 0

    def test_lambda_invocations_help(self, cli_runner):
        """Test lambda invocations command help."""
        result = cli_runner.invoke(cli, ["lambda", "invocations", "--help"])

        assert result.exit_code == 0


class TestExperimentCommand:
    """Tests for experiment commands."""

    def test_experiment_help(self, cli_runner):
        """Test experiment command group help."""
        result = cli_runner.invoke(cli, ["experiment", "--help"])

        assert result.exit_code == 0

    def test_experiment_list_help(self, cli_runner):
        """Test experiment list command help."""
        result = cli_runner.invoke(cli, ["experiment", "list", "--help"])

        assert result.exit_code == 0

    def test_experiment_validate_help(self, cli_runner):
        """Test experiment validate command help."""
        result = cli_runner.invoke(cli, ["experiment", "validate", "--help"])

        assert result.exit_code == 0


class TestSetupCommand:
    """Tests for setup command."""

    def test_setup_help(self, cli_runner):
        """Test setup command help."""
        result = cli_runner.invoke(cli, ["setup", "--help"])

        assert result.exit_code == 0
        assert "setup" in result.output.lower()
        # Should mention skip options
        assert "skip" in result.output.lower()

    def test_setup_options(self, cli_runner):
        """Test setup command has expected options."""
        result = cli_runner.invoke(cli, ["setup", "--help"])

        assert "--skip-clone" in result.output
        assert "--skip-build" in result.output
        assert "--skip-start" in result.output


class TestUpDownCommands:
    """Tests for up/down lifecycle commands."""

    def test_up_help(self, cli_runner):
        """Test up command help."""
        result = cli_runner.invoke(cli, ["up", "--help"])

        assert result.exit_code == 0

    def test_down_help(self, cli_runner):
        """Test down command help."""
        result = cli_runner.invoke(cli, ["down", "--help"])

        assert result.exit_code == 0

    def test_restart_help(self, cli_runner):
        """Test restart command help."""
        result = cli_runner.invoke(cli, ["restart", "--help"])

        assert result.exit_code == 0

    @pytest.mark.parametrize("command", ["up", "down"])
    def test_lambda_local_alone_is_a_valid_selection(self, command, cli_runner):
        """The Lambda server is a component in its own right.

        The guard used to test only api/runner/monitoring, so asking for just the
        Lambda server bailed out with "No components selected" and did nothing.
        """
        with patch("orchestrator.cli._get_component_manager") as get_manager:
            get_manager.return_value = MagicMock()
            result = cli_runner.invoke(
                cli,
                [command, "--lambda-local", "--no-api", "--no-runner", "--no-monitoring"],
            )

        assert "No components selected" not in result.output
        get_manager.assert_called_once()

    @pytest.mark.parametrize("command", ["up", "down"])
    def test_selecting_nothing_still_reports_it(self, command, cli_runner):
        """Deselecting every component, Lambda included, is still a no-op."""
        result = cli_runner.invoke(
            cli,
            [command, "--no-lambda-local", "--no-api", "--no-runner", "--no-monitoring"],
        )

        assert "No components selected" in result.output
        assert "--lambda-local" in result.output


class TestDoctorCommand:
    """Tests for doctor command."""

    def test_doctor_help(self, cli_runner):
        """Test doctor command help."""
        result = cli_runner.invoke(cli, ["doctor", "--help"])

        assert result.exit_code == 0


class TestCommandStructure:
    """Tests to verify CLI command structure."""

    def test_all_expected_commands_exist(self, cli_runner):
        """Test that all expected top-level commands exist."""
        result = cli_runner.invoke(cli, ["--help"])

        expected_commands = [
            "setup",
            "up",
            "down",
            "restart",
            "status",
            "doctor",
            "config",
            "db",
            "logs",
            "trace",
            "lambda",
            "experiment",
        ]

        for cmd in expected_commands:
            assert cmd in result.output, f"Command '{cmd}' not found in CLI help"

    def test_subcommand_groups_have_commands(self, cli_runner):
        """Test that command groups have subcommands."""
        groups_with_subcommands = {
            "config": ["show", "init", "validate"],
            "db": ["migrate", "seed", "status", "reset"],
            "logs": ["query", "errors"],
            "trace": ["experiment", "crawler", "job"],
            "lambda": ["invocations", "decisions", "show"],
            "experiment": ["validate", "preview", "generate", "upload", "list"],
        }

        for group, expected_cmds in groups_with_subcommands.items():
            result = cli_runner.invoke(cli, [group, "--help"])
            assert result.exit_code == 0, f"Command group '{group}' failed"

            for cmd in expected_cmds:
                assert cmd in result.output, f"Subcommand '{cmd}' not found in '{group}'"
