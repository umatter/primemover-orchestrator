"""`orchestrator doctor` must surface a failed systemd unit.

This check exists because of a specific failure: ``primemover-health-check`` is
a one-shot that runs at boot, it failed, and it stayed ``failed`` for over a
week without anyone noticing. Nothing was broken about the reporting -- the
state was correct and available the whole time. It was simply only visible via
``systemctl status``, which nobody runs while working.

So the property under test is not "can we read systemd", it is "does a failed
unit reach the surface a person actually looks at, and does it make the command
report failure". A check that finds the problem and then prints it as a passing
run would reproduce the original bug exactly.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestrator.cli import cli

UNITS = {
    "primemover-lambda": ("enabled", "active"),
    "primemover-runner": ("enabled", "active"),
    "primemover-health-check": ("enabled", "active"),
    "vncserver@1": ("enabled", "active"),
    "multilogin-desktop": ("enabled", "active"),
}


def _fake_systemctl(states: dict[str, tuple[str, str]]):
    """Return a subprocess.run double answering systemctl queries from ``states``.

    Anything that is not a systemctl call falls through to a benign success, so
    doctor's other checks (docker, java, ...) still run without blowing up.
    """

    class _Result:
        def __init__(self, stdout: str = "", returncode: int = 0):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    def _run(cmd, *args, **kwargs):
        if not cmd or cmd[0] != "systemctl":
            return _Result("ok\n")
        verb = cmd[1]
        unit = cmd[-1]
        enabled, active = states.get(unit, ("not-found", "inactive"))
        if verb == "is-enabled":
            return _Result(enabled + "\n")
        if verb == "is-active":
            return _Result(active + "\n")
        if verb == "show":
            return _Result("Thu 2026-08-13 22:01:58 CEST\n")
        return _Result("")

    return _run


def _run_doctor(cli_runner, states, systemctl_present=True):
    which_real = __import__("shutil").which

    def _which(name):
        if name == "systemctl":
            return "/usr/bin/systemctl" if systemctl_present else None
        return which_real(name)

    with (
        patch("subprocess.run", side_effect=_fake_systemctl(states)),
        patch("shutil.which", side_effect=_which),
    ):
        return cli_runner.invoke(cli, ["doctor"])


class TestFailedUnitIsSurfaced:
    def test_a_failed_unit_is_named_in_the_output(self, cli_runner):
        states = dict(UNITS, **{"primemover-health-check": ("enabled", "failed")})
        result = _run_doctor(cli_runner, states)

        assert "primemover-health-check" in result.output
        assert "FAILED" in result.output

    def test_a_failed_unit_makes_doctor_report_failure(self, cli_runner):
        """The important half: it must not find the problem and still say OK."""
        states = dict(UNITS, **{"primemover-health-check": ("enabled", "failed")})
        result = _run_doctor(cli_runner, states)

        assert "Some checks failed" in result.output
        assert "All checks passed" not in result.output

    def test_the_failure_carries_a_timestamp(self, cli_runner):
        """A one-shot that failed at boot weeks ago reads like a fresh failure without it."""
        states = dict(UNITS, **{"primemover-health-check": ("enabled", "failed")})
        result = _run_doctor(cli_runner, states)

        assert "2026-08-13" in result.output

    def test_the_failure_tells_you_where_to_look(self, cli_runner):
        states = dict(UNITS, **{"primemover-runner": ("enabled", "failed")})
        result = _run_doctor(cli_runner, states)

        assert "journalctl -u primemover-runner" in result.output

    @pytest.mark.parametrize("unit", sorted(UNITS))
    def test_every_known_unit_is_checked(self, cli_runner, unit):
        """A unit that is never queried can never be reported as failed."""
        states = dict(UNITS, **{unit: ("enabled", "failed")})
        result = _run_doctor(cli_runner, states)

        assert unit in result.output
        assert "Some checks failed" in result.output


class TestHealthyAndAbsentCases:
    def test_all_units_active_is_not_reported_as_a_failure(self, cli_runner):
        result = _run_doctor(cli_runner, UNITS)

        assert "FAILED" not in result.output
        assert "primemover-lambda" in result.output

    def test_units_that_are_not_installed_are_skipped_silently(self, cli_runner):
        """A dev laptop has none of these; that is not an error."""
        result = _run_doctor(cli_runner, {})

        assert "FAILED" not in result.output
        assert "no PrimeMover units installed" in result.output

    def test_a_disabled_unit_is_a_note_not_a_failure(self, cli_runner):
        """multilogin-agent is deliberately disabled on v12; it must not go red."""
        states = dict(UNITS, **{"multilogin-desktop": ("disabled", "inactive")})
        result = _run_doctor(cli_runner, states)

        assert "not enabled" in result.output
        assert "FAILED" not in result.output

    def test_without_systemd_the_section_is_skipped_cleanly(self, cli_runner):
        """Most contributors are not on the VM; doctor must stay usable there."""
        result = _run_doctor(cli_runner, UNITS, systemctl_present=False)

        assert "systemd not present" in result.output
        assert "FAILED" not in result.output
