"""Chrome-only dev mode: `orchestrator up --dev`, and what doctor says about it.

The point of the mode is that a developer with no Multilogin account, no proxy
and no S3 credentials can still drive a bot. Two things have to hold for that,
and neither is visible from reading the flag:

1. The flag must reach the java command line as an *added* Spring profile. A
   profile that is substituted for the configured one silently drops whatever
   else the deployment configured; a profile that never arrives leaves the
   runner signing in to Multilogin and exiting at boot.
2. Whatever `status` and `doctor` say about the mode must describe the process
   that is actually running, not the configuration a later invocation happens to
   read. Those are separate processes; the only honest link is what was recorded
   at launch.

The missing-credentials check is here for the symptom it replaces: without it
the failure surfaces as "Runner process exited unexpectedly" and the
authentication error is only in runner-stdout.log or the journal.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from orchestrator.cli import _multilogin_password_state, cli
from orchestrator.components.manager import ComponentManager
from orchestrator.components.runner import RunnerComponent

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(result) -> str:
    """Output with styling removed (rich inserts escapes inside tokens)."""
    return _ANSI.sub("", result.output)


def flat(result) -> str:
    """Output with styling removed and every whitespace run collapsed.

    rich hard-wraps to the terminal width, so a message asserted on as one
    string is split across lines at a width nobody chose. Collapsing keeps the
    assertions about what was said rather than where it wrapped.
    """
    return " ".join(plain(result).split())


@contextmanager
def _no_api():
    """Keep doctor's canary check off the network in a unit test."""
    with (
        patch("orchestrator.clients.api.APIClient", MagicMock()),
        patch("orchestrator.canary.find_canary", return_value=None),
    ):
        yield


@pytest.fixture
def runner_component(tmp_path: Path) -> RunnerComponent:
    component = RunnerComponent(path=tmp_path, chrome_dev=True)
    component.PID_FILE = tmp_path / "runner.pid"
    return component


class TestActiveProfiles:
    def test_chrome_dev_is_added_to_the_configured_profile_not_swapped_in(self):
        component = RunnerComponent(path=Path("/tmp"), spring_profile="local", chrome_dev=True)

        assert component.active_profiles == "local,chrome-dev"

    def test_a_non_default_configured_profile_survives(self):
        component = RunnerComponent(
            path=Path("/tmp"), spring_profile="integration", chrome_dev=True
        )

        assert component.active_profiles == "integration,chrome-dev"

    def test_without_the_flag_nothing_changes(self):
        component = RunnerComponent(path=Path("/tmp"), spring_profile="local")

        assert component.active_profiles == "local"

    def test_an_already_configured_chrome_dev_is_not_repeated(self):
        component = RunnerComponent(
            path=Path("/tmp"), spring_profile="local,chrome-dev", chrome_dev=True
        )

        assert component.active_profiles == "local,chrome-dev"


class TestTheProfileReachesTheJavaCommand:
    """The flag is worthless if it does not end up on the command line."""

    def _start(self, component: RunnerComponent):
        process = MagicMock()
        process.pid = 4321
        with (
            patch.object(component, "validate", return_value=(True, [])),
            patch.object(component, "_resolve_pid", return_value=(None, None)),
            patch.object(component, "_cleanup_orphaned_browsers", return_value=0),
            patch.object(component, "_wait_for_startup", return_value=True),
            patch("orchestrator.components.runner.subprocess.Popen", return_value=process) as popen,
        ):
            health = component.start()
        return health, popen.call_args.args[0]

    def test_up_dev_launches_the_runner_with_chrome_dev(self, runner_component):
        health, cmd = self._start(runner_component)

        assert "--spring.profiles.active=local,chrome-dev" in cmd
        assert health.details["profile"] == "local,chrome-dev"
        assert health.details["chrome_dev"] is True

    def test_the_default_launch_is_untouched(self, tmp_path):
        component = RunnerComponent(path=tmp_path)
        component.PID_FILE = tmp_path / "runner.pid"

        health, cmd = self._start(component)

        assert "--spring.profiles.active=local" in cmd
        assert "--spring.profiles.active=local,chrome-dev" not in cmd
        assert health.details["chrome_dev"] is False


class TestTheModeIsVisibleAfterwards:
    """`status`/`doctor` run later, in another process, from settings alone."""

    def test_the_launch_is_recorded_for_a_later_invocation(self, runner_component):
        process = MagicMock()
        process.pid = 4321
        with (
            patch.object(runner_component, "validate", return_value=(True, [])),
            patch.object(runner_component, "_resolve_pid", return_value=(None, None)),
            patch.object(runner_component, "_cleanup_orphaned_browsers", return_value=0),
            patch.object(runner_component, "_wait_for_startup", return_value=True),
            patch("orchestrator.components.runner.subprocess.Popen", return_value=process),
        ):
            runner_component.start()

        record = json.loads(runner_component.launch_file.read_text())
        assert record == {"pid": 4321, "profiles": "local,chrome-dev"}

        # A fresh component built from settings (no --dev) must still report the
        # mode the live process is in.
        later = RunnerComponent(path=runner_component.path)
        later.PID_FILE = runner_component.PID_FILE
        with patch.object(later, "_resolve_pid", return_value=(4321, "pidfile")):
            assert later.running_profiles() == "local,chrome-dev"
            assert later._get_process_details(4321)["chrome_dev"] is True

    def test_a_record_from_an_earlier_runner_is_not_believed(self, runner_component):
        runner_component._write_launch_record(111, "local,chrome-dev")

        assert runner_component._launched_profiles(222) is None

    def test_no_record_means_unknown_not_default(self, runner_component):
        """A systemd-owned runner leaves no record; it must not read as chrome-dev."""
        with patch.object(runner_component, "_resolve_pid", return_value=(999, "systemd")):
            assert runner_component.running_profiles() is None

    def test_stopping_clears_the_record(self, runner_component):
        runner_component._write_launch_record(4321, "local,chrome-dev")

        runner_component._remove_pid_file()

        assert not runner_component.launch_file.exists()


class TestUpPassesTheFlagThrough:
    """From the CLI flag to the component, through the manager."""

    def _invoke(self, args):
        with patch("orchestrator.cli._get_component_manager") as get_manager:
            manager = MagicMock()
            manager.start.return_value.overall.value = "running"
            manager.restart.return_value.overall.value = "running"
            get_manager.return_value = manager
            result = CliRunner().invoke(cli, args)
        assert result.exit_code == 0, result.output
        return get_manager.call_args.kwargs, plain(result)

    def test_up_dev_asks_for_a_chrome_dev_manager(self):
        kwargs, output = self._invoke(["up", "--dev"])

        assert kwargs["chrome_dev"] is True
        assert "chrome-dev" in output

    def test_up_without_dev_does_not(self):
        kwargs, _ = self._invoke(["up"])

        assert kwargs["chrome_dev"] is False

    def test_restart_dev_asks_for_a_chrome_dev_manager(self):
        kwargs, output = self._invoke(["restart", "--runner", "--dev"])

        assert kwargs["chrome_dev"] is True
        assert "chrome-dev" in output

    def test_restart_without_dev_does_not(self):
        kwargs, _ = self._invoke(["restart", "--runner"])

        assert kwargs["chrome_dev"] is False

    def test_the_manager_hands_it_to_the_runner_component(self, tmp_path):
        manager = ComponentManager(runner_path=tmp_path, runner_chrome_dev=True)

        assert manager.runner is not None
        assert manager.runner.active_profiles == "local,chrome-dev"

    def test_the_manager_default_is_the_production_path(self, tmp_path):
        manager = ComponentManager(runner_path=tmp_path)

        assert manager.runner is not None
        assert manager.runner.active_profiles == "local"


class TestMultiloginPasswordState:
    """Presence only — the value must never leave this function."""

    def test_a_missing_file_is_not_set(self, tmp_path):
        assert _multilogin_password_state(tmp_path / "secrets.properties") == "missing-file"

    def test_a_blank_value_is_not_set(self, tmp_path):
        secrets = tmp_path / "secrets.properties"
        secrets.write_text("multilogin.apiUser=someone@example.com\nmultilogin.apiPassword=\n")

        assert _multilogin_password_state(secrets) == "blank"

    def test_a_missing_key_is_not_set(self, tmp_path):
        secrets = tmp_path / "secrets.properties"
        secrets.write_text("awsS3.endpoint=https://s3.example.org\n")

        assert _multilogin_password_state(secrets) == "missing-key"

    def test_a_commented_out_key_does_not_count(self, tmp_path):
        secrets = tmp_path / "secrets.properties"
        secrets.write_text("# multilogin.apiPassword=old-value\n")

        assert _multilogin_password_state(secrets) == "missing-key"

    def test_a_filled_in_value_is_set(self, tmp_path):
        secrets = tmp_path / "secrets.properties"
        secrets.write_text("multilogin.apiPassword=  hunter2  \n")

        assert _multilogin_password_state(secrets) == "set"


class TestDoctorReportsTheCredentials:
    def _doctor(self, tmp_path: Path):
        from orchestrator.config.settings import Settings

        settings = Settings(
            ENV="test",
            PM_RUNNER_PATH=str(tmp_path / "primemover_runner"),
            PM_API_PATH=str(tmp_path / "primemover_api"),
        )
        with (
            patch("orchestrator.cli.get_settings", return_value=settings),
            # No live runner: the launch record of whatever this box is running
            # must not leak into the assertion.
            patch.object(RunnerComponent, "running_profiles", return_value=None),
            _no_api(),
        ):
            return flat(CliRunner().invoke(cli, ["doctor"]))

    def _write_secrets(self, tmp_path: Path, body: str) -> None:
        config = tmp_path / "primemover_runner" / "primemover-runner" / "config"
        config.mkdir(parents=True)
        (config / "secrets.properties").write_text(body)

    def test_a_missing_secrets_file_is_reported(self, tmp_path):
        output = self._doctor(tmp_path)

        assert "Multilogin credentials: not set" in output
        assert "orchestrator up --dev" in output
        assert "the default runner exits at startup" in output

    def test_a_blank_password_is_reported(self, tmp_path):
        self._write_secrets(tmp_path, "multilogin.apiPassword=\n")

        output = self._doctor(tmp_path)

        assert "Multilogin credentials: not set" in output
        assert "fill in multilogin.apiPassword" in output

    def test_a_set_password_is_reported_without_its_value(self, tmp_path):
        self._write_secrets(tmp_path, "multilogin.apiPassword=hunter2\n")

        output = self._doctor(tmp_path)

        assert "Multilogin credentials: set" in output
        assert "not set" not in output.split("Multilogin credentials")[1][:80]
        assert "hunter2" not in output

    def test_a_live_chrome_dev_runner_is_named(self, tmp_path):
        from orchestrator.config.settings import Settings

        settings = Settings(ENV="test", PM_RUNNER_PATH=str(tmp_path / "primemover_runner"))
        with (
            patch("orchestrator.cli.get_settings", return_value=settings),
            patch.object(RunnerComponent, "running_profiles", return_value="local,chrome-dev"),
            _no_api(),
        ):
            output = flat(CliRunner().invoke(cli, ["doctor"]))

        assert "runner mode: chrome-dev" in output


class TestSetupWording:
    def test_multilogin_is_not_called_optional(self):
        """It is not optional with the defaults: the runner exits without it."""
        source = Path("orchestrator/cli.py").read_text()
        # The claim that cost the time: "optional, for production browser profiles".
        assert "optional, for production browser profiles" not in source

    def test_the_setup_check_names_the_way_out(self):
        source = Path("orchestrator/cli.py").read_text()
        multilogin_check = source.split("# Check Multilogin X")[1][:1200]

        assert "required for the default" in multilogin_check
        assert "orchestrator up --dev" in multilogin_check
