import pytest
from click.testing import CliRunner

from experiment_designer import cli as cli_module
from experiment_designer.cli import cli


class _Status:
    def __init__(self, ok: bool):
        self._ok = ok
        self.ok = ok

    def render(self):
        return "STATUS-RENDER-OK" if self._ok else "STATUS-RENDER-BAD"


@pytest.fixture
def fake_status(monkeypatch: pytest.MonkeyPatch):
    calls = {}

    def _experiment_status(session, experiment_id, *, hours=24, lock_text=None, deep=False):
        calls["experiment_id"] = experiment_id
        calls["hours"] = hours
        calls["lock_text"] = lock_text
        calls["deep"] = deep
        return calls.get("result", _Status(True))

    monkeypatch.setattr(cli_module, "_session_from_env", lambda: object())
    monkeypatch.setattr(cli_module, "experiment_status", _experiment_status)
    return calls


def test_status_prints_render_and_exits_zero_when_ok(fake_status):
    out = CliRunner().invoke(cli, ["status", "181"])
    assert out.exit_code == 0, out.output
    assert "STATUS-RENDER-OK" in out.output
    assert fake_status["experiment_id"] == 181 and fake_status["hours"] == 24
    assert fake_status["lock_text"] is None and fake_status["deep"] is False


def test_status_exit_code_follows_ok(fake_status):
    fake_status["result"] = _Status(False)
    out = CliRunner().invoke(cli, ["status", "181"])
    assert out.exit_code == 1
    assert "STATUS-RENDER-BAD" in out.output


def test_status_passes_hours_and_deep(fake_status):
    out = CliRunner().invoke(cli, ["status", "181", "--hours", "6", "--deep"])
    assert out.exit_code == 0, out.output
    assert fake_status["hours"] == 6 and fake_status["deep"] is True


class _Diff:
    def __init__(self, identical: bool):
        self.identical = identical

    def render(self):
        return "DIFF-IDENTICAL" if self.identical else "DIFF-DIFFERENT"


@pytest.fixture
def fake_diff(monkeypatch: pytest.MonkeyPatch):
    calls = {}

    def _diff_locks(text_a, text_b, *, deep=True):
        calls["text_a"] = text_a
        calls["text_b"] = text_b
        calls["deep"] = deep
        return calls.get("result", _Diff(True))

    monkeypatch.setattr(cli_module, "diff_locks", _diff_locks)
    return calls


def test_diff_prints_render_and_exits_zero_when_identical(fake_diff, tmp_path):
    lock_a = tmp_path / "a.lock.yaml"
    lock_b = tmp_path / "b.lock.yaml"
    lock_a.write_text("A")
    lock_b.write_text("B")
    out = CliRunner().invoke(cli, ["diff", str(lock_a), str(lock_b)])
    assert out.exit_code == 0, out.output
    assert "DIFF-IDENTICAL" in out.output
    assert fake_diff["text_a"] == "A" and fake_diff["text_b"] == "B"


def test_diff_exit_code_follows_identical(fake_diff, tmp_path):
    fake_diff["result"] = _Diff(False)
    lock_a = tmp_path / "a.lock.yaml"
    lock_b = tmp_path / "b.lock.yaml"
    lock_a.write_text("A")
    lock_b.write_text("B")
    out = CliRunner().invoke(cli, ["diff", str(lock_a), str(lock_b)])
    assert out.exit_code == 1
    assert "DIFF-DIFFERENT" in out.output
