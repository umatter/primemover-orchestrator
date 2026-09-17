"""``orchestrator doctor`` must show which decision functions this box hosts.

A name the registry does not list is answered with HTTP 200 and a degraded
verdict -- right for the runner, useless for a person: the run completes, the
job is recorded, and the arm is simply missing. So the list of functions, and
whether each one's directory and package are real, has to be visible *before* a
run, in the place people already look when something is off.

The assertions here deliberately avoid "All checks passed": doctor also checks
sibling repos and credentials that differ between a laptop, CI and the VM. What
is pinned is that a broken entry is named, makes the command report failure, and
carries the journalctl line -- and that a healthy one does none of those things.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator.cli import cli

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(result) -> str:
    """Output with styling removed.

    rich highlights numbers and inserts escape codes *inside* a token, so a
    substring check against the raw output passes or fails depending on whether
    a TTY was detected. That is not a difference in behaviour worth testing.
    """
    return _ANSI.sub("", result.output)


class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _run_doctor(cli_runner, health: dict | None = None):
    """Run doctor with everything but the decision-function section neutralised."""
    which_real = __import__("shutil").which

    def _which(name):
        # Skip the systemd section: it is another test file's subject, and its
        # verdict on this box would leak into these assertions.
        return None if name == "systemctl" else which_real(name)

    with (
        patch("subprocess.run", return_value=_Result("ok\n")),
        patch("shutil.which", side_effect=_which),
        patch("orchestrator.clients.api.APIClient", side_effect=RuntimeError("no API in a test")),
        patch("orchestrator.cli._decision_function_health", return_value=health or {}),
    ):
        return cli_runner.invoke(cli, ["doctor"])


def _package(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "lambda_function.py").write_text("def select_recommendation(p):\n    return {}\n")
    return path


def _registry_file(root: Path, functions: dict[str, Path], default: str) -> Path:
    path = root / "decision-functions.toml"
    body = [f'default = "{default}"\n']
    for name, target in functions.items():
        body.append(f'\n[functions.{name}]\npath = "{target}"\n')
    path.write_text("".join(body))
    return path


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = _registry_file(
        tmp_path,
        {
            "bot-decision-yt": _package(tmp_path, "yt"),
            "bot-decision-news": _package(tmp_path, "news"),
        },
        default="bot-decision-yt",
    )
    monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))
    return path


class TestTheRegistryIsListed:
    def test_every_registered_function_is_named(self, cli_runner, registry, tmp_path):
        out = plain(_run_doctor(cli_runner))

        assert "Checking decision functions" in out
        assert "bot-decision-yt" in out
        assert "bot-decision-news" in out

    def test_the_registry_file_is_named(self, cli_runner, registry):
        # Whitespace-normalised: rich wraps at the console width, and a long
        # path is wrapped mid-token, which says nothing about what is on screen.
        out = "".join(plain(_run_doctor(cli_runner)).split())

        assert str(registry) in out

    def test_the_default_is_marked(self, cli_runner, registry):
        """Which function a headerless request lands on is the thing people get wrong."""
        out = plain(_run_doctor(cli_runner))

        assert "bot-decision-yt (default)" in out
        assert "bot-decision-news (default)" not in out

    def test_a_healthy_entry_is_not_flagged(self, cli_runner, registry):
        out = plain(_run_doctor(cli_runner))

        assert "✗ bot-decision-yt" not in out
        assert "journalctl -u primemover-lambda" not in out


class TestBrokenEntries:
    def test_a_path_that_does_not_exist_is_reported_and_fails(
        self, cli_runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _registry_file(tmp_path, {"ghost": tmp_path / "nowhere"}, default="ghost")
        monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(tmp_path / "decision-functions.toml"))

        out = plain(_run_doctor(cli_runner))

        assert "✗ ghost" in out
        assert "path not found" in out
        assert "Some checks failed" in out

    def test_a_directory_without_lambda_function_is_reported(
        self, cli_runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        hollow = tmp_path / "hollow"
        hollow.mkdir()
        _registry_file(tmp_path, {"hollow": hollow}, default="hollow")
        monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(tmp_path / "decision-functions.toml"))

        out = plain(_run_doctor(cli_runner))

        assert "lambda_function.py not found" in out
        assert "Some checks failed" in out

    def test_a_malformed_registry_is_reported_and_fails(
        self, cli_runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        path = tmp_path / "decision-functions.toml"
        path.write_text("not = valid = toml\n")
        monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))

        out = plain(_run_doctor(cli_runner))

        assert "Some checks failed" in out
        assert "could not read" in out


class TestWhatTheRunningServiceSays:
    def test_an_unimportable_package_carries_the_journalctl_line(self, cli_runner, registry):
        """The path can be perfect and the package still not import."""
        health = {
            "bot-decision-news": {
                "loaded": False,
                "error": "ModuleNotFoundError: No module named 'langdetect'",
            }
        }

        out = plain(_run_doctor(cli_runner, health=health))

        assert "✗ bot-decision-news" in out
        assert "langdetect" in out
        assert "journalctl -u primemover-lambda -n 50" in out
        assert "Some checks failed" in out

    def test_a_hook_reporting_a_problem_is_surfaced(self, cli_runner, registry):
        """An imported package whose own data check fails is a dead treatment arm."""
        health = {
            "bot-decision-yt": {
                "loaded": True,
                "error": None,
                "health": {"ok": False, "problems": ["mainstream_media_channels.csv: bad header"]},
            }
        }

        out = plain(_run_doctor(cli_runner, health=health))

        assert "health() reports" in out
        assert "bad header" in out
        assert "Some checks failed" in out

    def test_a_service_that_is_not_running_is_not_a_failure(self, cli_runner, registry):
        """``{}`` means "could not ask", and doctor still reports the filesystem facts."""
        out = plain(_run_doctor(cli_runner, health={}))

        assert "✗ bot-decision-yt" not in out
        assert "journalctl -u primemover-lambda" not in out


class TestTheFallback:
    def test_without_a_registry_doctor_says_so_and_points_at_the_template(
        self, cli_runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(tmp_path / "absent.toml"))

        out = plain(_run_doctor(cli_runner))

        assert "no registry at" in out
        assert "infrastructure/decision-functions.example.toml" in out

    def test_with_a_registry_a_missing_symlink_is_not_a_failure(
        self, cli_runner, registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The symlink is only the fallback once the registry names the functions."""
        from orchestrator.config.settings import Settings

        monkeypatch.setattr(Settings, "get_orchestrator_path", lambda self: tmp_path / "elsewhere")

        out = plain(_run_doctor(cli_runner))

        assert "decision symlink broken or missing" not in out
        assert "no decision symlink (not needed" in out
