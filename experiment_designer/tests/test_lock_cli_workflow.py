import pytest
from click.testing import CliRunner

from experiment_designer import cli as cli_module
from experiment_designer.cli import cli
from experiment_designer.lock.cleanup import CleanupCandidate, CleanupError, CleanupResult


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


def test_status_reports_an_unresolvable_anchor_as_a_refusal(monkeypatch, tmp_path):
    from experiment_designer.lock.anchor import AnchorError

    def _raise(*a, **k):
        raise AnchorError("anchor '2020-01-01T00:00:00' is in the past")

    monkeypatch.setattr(cli_module, "_session_from_env", lambda: object())
    monkeypatch.setattr(cli_module, "experiment_status", _raise)
    lock = tmp_path / "s.lock.yaml"
    lock.write_text("pmspec_lock: 1\n")
    out = CliRunner().invoke(cli, ["status", "181", str(lock)])
    assert out.exit_code == 1
    assert "Refused" in out.output and "in the past" in out.output
    assert "Traceback" not in out.output


def _minimal_lock_text() -> str:
    from experiment_designer.lock.io import dump_lock
    from experiment_designer.lock.model import Lock, Provenance

    crawler = {
        "name": "bot",
        "description": "d",
        "configuration": {
            "name": "c",
            "description": "d",
            "active": 1,
            "params": {},
            "preferences": [],
        },
        "proxy": {},
        "agent": {"name": "a", "identification": "mlx", "location": "US-NC", "active": True},
        "queues": [
            {
                "name": "s1",
                "description": "d",
                "start_offset": "+5m",
                "timezone": "Europe/Zurich",
                "timezone_utc_offset": "+02:00",
                "active": True,
                "jobs": [],
            }
        ],
    }
    lock = Lock(
        provenance=Provenance(
            spec_path="s.yaml",
            spec_sha256="0" * 64,
            generator=None,
            git_commit=None,
            pmspec_version="0.1.0",
            seed=1,
        ),
        experiment={"name": "E", "description": "", "runner_user_id": None, "contact": None},
        schedule={"anchor": "2026-01-01T00:00:00+01:00"},
        crawlers=[crawler],
    )
    return dump_lock(lock)


class _Audit:
    def __init__(self, ok_value: bool):
        self.ok_value = ok_value
        self.ok_calls: list[bool] = []

    def ok(self, strict: bool) -> bool:
        self.ok_calls.append(strict)
        return self.ok_value

    def render(self):
        return "AUDIT-RENDER-OK" if self.ok_value else "AUDIT-RENDER-BAD"


@pytest.fixture
def fake_audit(monkeypatch: pytest.MonkeyPatch):
    calls = {}

    def _audit_experiment(
        session, experiment_id, lock_text, *, hours=24, sample=None, strict=False, schemas=None
    ):
        calls["experiment_id"] = experiment_id
        calls["hours"] = hours
        calls["sample"] = sample
        calls["strict"] = strict
        calls["schemas"] = schemas
        return calls.get("result", _Audit(True))

    monkeypatch.setattr(cli_module, "_session_from_env", lambda: object())
    monkeypatch.setattr(cli_module, "audit_experiment", _audit_experiment)
    return calls


def test_audit_prints_render_and_exits_zero_when_ok(fake_audit, tmp_path):
    lock = tmp_path / "e.lock.yaml"
    lock.write_text(_minimal_lock_text())
    out = CliRunner().invoke(cli, ["audit", "181", str(lock)])
    assert out.exit_code == 0, out.output
    assert "AUDIT-RENDER-OK" in out.output
    assert fake_audit["experiment_id"] == 181
    assert fake_audit["hours"] == 24
    assert fake_audit["sample"] is None
    assert fake_audit["strict"] is False
    assert fake_audit["schemas"] == {}


def test_audit_exit_code_follows_ok_false(fake_audit, tmp_path):
    fake_audit["result"] = _Audit(False)
    lock = tmp_path / "e.lock.yaml"
    lock.write_text(_minimal_lock_text())
    out = CliRunner().invoke(cli, ["audit", "181", str(lock)])
    assert out.exit_code == 1
    assert "AUDIT-RENDER-BAD" in out.output


def test_audit_passes_hours_sample_and_strict_through(fake_audit, tmp_path):
    result = _Audit(True)
    fake_audit["result"] = result
    lock = tmp_path / "e.lock.yaml"
    lock.write_text(_minimal_lock_text())
    out = CliRunner().invoke(
        cli, ["audit", "181", str(lock), "--hours", "6", "--sample", "3", "--strict"]
    )
    assert out.exit_code == 0, out.output
    assert fake_audit["hours"] == 6
    assert fake_audit["sample"] == 3
    assert fake_audit["strict"] is True
    assert result.ok_calls == [True]


def test_audit_reports_an_unresolvable_anchor_as_a_refusal(monkeypatch, tmp_path):
    from experiment_designer.lock.anchor import AnchorError

    def _raise(*a, **k):
        raise AnchorError("anchor '2020-01-01T00:00:00' is in the past")

    monkeypatch.setattr(cli_module, "_session_from_env", lambda: object())
    monkeypatch.setattr(cli_module, "audit_experiment", _raise)
    lock = tmp_path / "e.lock.yaml"
    lock.write_text(_minimal_lock_text())
    out = CliRunner().invoke(cli, ["audit", "181", str(lock)])
    assert out.exit_code == 1
    assert "Refused" in out.output and "in the past" in out.output
    assert "Traceback" not in out.output


def test_audit_reports_an_invalid_lock_before_ever_calling_audit_experiment(monkeypatch, tmp_path):
    def _raise(*a, **k):
        raise AssertionError("audit_experiment should not be reached with an invalid lock")

    monkeypatch.setattr(cli_module, "_session_from_env", lambda: object())
    monkeypatch.setattr(cli_module, "audit_experiment", _raise)
    lock = tmp_path / "bad.lock.yaml"
    lock.write_text("pmspec_lock: 1\nprovenance: {}\n")
    out = CliRunner().invoke(cli, ["audit", "181", str(lock)])
    assert out.exit_code == 1
    assert "Lock invalid" in out.output
    assert "Traceback" not in out.output


def test_verify_reports_an_unresolvable_anchor_as_a_refusal(monkeypatch, tmp_path):
    from experiment_designer.lock.anchor import AnchorError

    def _raise(*a, **k):
        raise AnchorError("anchor '2020-01-01T00:00:00' is in the past")

    monkeypatch.setattr(cli_module, "_session_from_env", lambda: object())
    monkeypatch.setattr(cli_module, "verify_lock", _raise)
    lock = tmp_path / "s.lock.yaml"
    lock.write_text("pmspec_lock: 1\n")
    out = CliRunner().invoke(cli, ["verify", "181", str(lock)])
    assert out.exit_code == 1
    assert "Refused" in out.output and "in the past" in out.output
    assert "Traceback" not in out.output


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


@pytest.fixture
def fake_cleanup(monkeypatch: pytest.MonkeyPatch):
    calls = {"deleted_ids": None}
    candidates = [
        CleanupCandidate(
            experiment_id=1, name="test-Study-1", uploaded_at="2026-09-01", mode="test"
        )
    ]

    def _find(session, *, older_than=None, now=None):
        calls["older_than"] = older_than
        return calls.get("candidates", candidates)

    def _delete(session, ids, *, candidates=None, force=False):
        calls["deleted_ids"] = ids
        calls["force"] = force
        calls["candidates_arg"] = candidates
        return calls.get("deleted_result", CleanupResult(deleted=ids, skipped_in_flight=[]))

    monkeypatch.setattr(cli_module, "_session_from_env", lambda: object())
    monkeypatch.setattr(cli_module, "find_test_experiments", _find)
    monkeypatch.setattr(cli_module, "delete_experiments", _delete)
    return calls


def test_cleanup_dry_run_never_deletes(fake_cleanup):
    out = CliRunner().invoke(cli, ["cleanup", "--dry-run"])
    assert out.exit_code == 0, out.output
    assert "test-Study-1" in out.output
    assert fake_cleanup["deleted_ids"] is None


def test_cleanup_lists_an_in_flight_column(fake_cleanup):
    fake_cleanup["candidates"] = [
        CleanupCandidate(
            experiment_id=2,
            name="test-Study-2",
            uploaded_at="2026-09-01",
            mode="test",
            in_flight=True,
        )
    ]
    out = CliRunner().invoke(cli, ["cleanup", "--dry-run"])
    assert out.exit_code == 0, out.output
    assert "in-flight" in out.output
    assert "yes" in out.output


def test_cleanup_without_yes_prompts_and_a_no_answer_deletes_nothing(fake_cleanup):
    out = CliRunner().invoke(cli, ["cleanup"], input="n\n")
    assert out.exit_code == 0, out.output
    assert "Delete 1 test experiment(s)?" in out.output
    assert fake_cleanup["deleted_ids"] is None


def test_cleanup_with_yes_deletes_without_prompting(fake_cleanup):
    out = CliRunner().invoke(cli, ["cleanup", "--yes"])
    assert out.exit_code == 0, out.output
    assert fake_cleanup["deleted_ids"] == [1]
    assert fake_cleanup["force"] is False


def test_cleanup_force_flag_is_forwarded(fake_cleanup):
    out = CliRunner().invoke(cli, ["cleanup", "--yes", "--force"])
    assert out.exit_code == 0, out.output
    assert fake_cleanup["force"] is True


def test_cleanup_prompt_warns_how_many_will_be_skipped(fake_cleanup):
    fake_cleanup["candidates"] = [
        CleanupCandidate(
            experiment_id=1, name="test-a", uploaded_at=None, mode="test", in_flight=True
        ),
        CleanupCandidate(
            experiment_id=2, name="test-b", uploaded_at=None, mode="test", in_flight=False
        ),
    ]
    out = CliRunner().invoke(cli, ["cleanup"], input="n\n")
    assert out.exit_code == 0, out.output
    assert "1 in-flight experiment(s) will be skipped" in out.output
    assert "Delete 1 test experiment(s)?" in out.output


def test_cleanup_reports_cleanup_error_and_exits_nonzero(monkeypatch, fake_cleanup):
    def _delete(session, ids, *, candidates=None, force=False):
        raise CleanupError([], 1, RuntimeError("boom"))

    monkeypatch.setattr(cli_module, "delete_experiments", _delete)
    out = CliRunner().invoke(cli, ["cleanup", "--yes"])
    assert out.exit_code == 1
    assert "boom" in out.output
