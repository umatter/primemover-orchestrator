"""
RunnerComponent must recognise a runner owned by primemover-runner.service.

On the production VM the runner is started by systemd, which never writes the
PID file. Before this, every PID-file-only check reported STOPPED there — and
`orchestrator up` would have swept the live runner's browsers as orphans and
launched a second runner beside it.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator.components.base import ComponentStatus
from orchestrator.components.runner import RunnerComponent


@pytest.fixture
def runner(tmp_path: Path) -> RunnerComponent:
    component = RunnerComponent(path=tmp_path)
    component.PID_FILE = tmp_path / "runner.pid"
    return component


def _systemctl_result(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["systemctl"], returncode=0, stdout=stdout)


class TestSystemdPid:
    def test_reads_main_pid_when_process_is_live(self, runner):
        with (
            patch("orchestrator.components.runner.shutil.which", return_value="/bin/systemctl"),
            patch(
                "orchestrator.components.runner.subprocess.run",
                return_value=_systemctl_result("4242\n"),
            ) as run,
            patch.object(runner, "_is_process_running", return_value=True),
        ):
            assert runner._systemd_pid() == 4242
        assert run.call_args.args[0][-1] == RunnerComponent.SYSTEMD_UNIT

    def test_zero_main_pid_means_inactive(self, runner):
        with (
            patch("orchestrator.components.runner.shutil.which", return_value="/bin/systemctl"),
            patch(
                "orchestrator.components.runner.subprocess.run",
                return_value=_systemctl_result("0\n"),
            ),
        ):
            assert runner._systemd_pid() is None

    def test_no_systemctl_is_not_an_error(self, runner):
        with patch("orchestrator.components.runner.shutil.which", return_value=None):
            assert runner._systemd_pid() is None

    def test_systemctl_failure_is_swallowed(self, runner):
        with (
            patch("orchestrator.components.runner.shutil.which", return_value="/bin/systemctl"),
            patch(
                "orchestrator.components.runner.subprocess.run",
                side_effect=subprocess.TimeoutExpired("systemctl", 5),
            ),
        ):
            assert runner._systemd_pid() is None

    def test_dead_main_pid_is_ignored(self, runner):
        with (
            patch("orchestrator.components.runner.shutil.which", return_value="/bin/systemctl"),
            patch(
                "orchestrator.components.runner.subprocess.run",
                return_value=_systemctl_result("4242"),
            ),
            patch.object(runner, "_is_process_running", return_value=False),
        ):
            assert runner._systemd_pid() is None


class TestStatusUnderSystemd:
    def test_status_running_without_pid_file(self, runner):
        with (
            patch.object(runner, "_systemd_pid", return_value=4242),
            patch.object(runner, "_get_process_details", return_value={"pid": 4242}),
        ):
            health = runner.status()
        assert health.status == ComponentStatus.RUNNING
        assert "primemover-runner.service" in health.message
        assert health.details["managed_by"] == "systemd"

    def test_status_stopped_when_neither_source_knows_it(self, runner):
        with patch.object(runner, "_systemd_pid", return_value=None):
            health = runner.status()
        assert health.status == ComponentStatus.STOPPED

    def test_pid_file_still_wins(self, runner):
        runner.PID_FILE.write_text("77")
        with (
            patch.object(runner, "_is_process_running", return_value=True),
            patch.object(runner, "_systemd_pid") as systemd_pid,
            patch.object(runner, "_get_process_details", return_value={"pid": 77}),
        ):
            health = runner.status()
        assert health.status == ComponentStatus.RUNNING
        assert health.details.get("managed_by") is None
        systemd_pid.assert_not_called()

    def test_stale_pid_file_does_not_hide_systemd_runner(self, runner):
        # The exact state found on ex63: /tmp/primemover-runner.pid left by an old
        # manual start, while the live runner belongs to the systemd unit.
        runner.PID_FILE.write_text("77")
        with (
            patch.object(runner, "_is_process_running", side_effect=lambda pid: pid == 4242),
            patch.object(runner, "_systemd_pid", return_value=4242),
            patch.object(runner, "_get_process_details", return_value={"pid": 4242}),
        ):
            health = runner.status()
        assert health.status == ComponentStatus.RUNNING
        assert health.details["managed_by"] == "systemd"

    def test_stale_pid_file_alone_is_stopped(self, runner):
        runner.PID_FILE.write_text("77")
        with (
            patch.object(runner, "_is_process_running", return_value=False),
            patch.object(runner, "_systemd_pid", return_value=None),
        ):
            health = runner.status()
        assert health.status == ComponentStatus.STOPPED
        assert health.details["stale_pid"] == 77

    def test_is_running_sees_systemd_runner(self, runner):
        with patch.object(runner, "_systemd_pid", return_value=4242):
            assert runner.is_running() is True
        with patch.object(runner, "_systemd_pid", return_value=None):
            assert runner.is_running() is False


class TestLifecycleUnderSystemd:
    def test_start_does_not_launch_a_second_runner(self, runner):
        with (
            patch.object(runner, "validate", return_value=(True, [])),
            patch.object(runner, "_systemd_pid", return_value=4242),
            patch.object(runner, "_cleanup_orphaned_browsers") as sweep,
            patch("orchestrator.components.runner.subprocess.Popen") as popen,
        ):
            health = runner.start()
        assert health.status == ComponentStatus.RUNNING
        assert health.details["managed_by"] == "systemd"
        sweep.assert_not_called()
        popen.assert_not_called()

    def test_stop_refuses_to_touch_a_systemd_runner(self, runner):
        with (
            patch.object(runner, "_systemd_pid", return_value=4242),
            patch("orchestrator.components.runner.os.kill") as kill,
        ):
            health = runner.stop()
        assert health.status == ComponentStatus.RUNNING
        assert "systemctl stop" in health.message
        kill.assert_not_called()

    def test_stop_with_stale_pid_file_still_defers_to_systemd(self, runner):
        runner.PID_FILE.write_text("77")
        with (
            patch.object(runner, "_is_process_running", return_value=False),
            patch.object(runner, "_systemd_pid", return_value=4242),
            patch("orchestrator.components.runner.os.kill") as kill,
        ):
            health = runner.stop()
        assert health.status == ComponentStatus.RUNNING
        kill.assert_not_called()
        assert runner.PID_FILE.exists()

    def test_orphan_sweep_spares_systemd_runner_browsers(self, runner):
        pgrep = subprocess.CompletedProcess(args=["pgrep"], returncode=0, stdout="9001\n9002\n")
        with (
            patch("orchestrator.components.runner.subprocess.run", return_value=pgrep),
            patch.object(runner, "_resolve_pid", return_value=(4242, "systemd")),
            patch("orchestrator.components.runner.os.kill") as kill,
        ):
            killed = runner._cleanup_orphaned_browsers()
        assert killed == 0
        kill.assert_not_called()
