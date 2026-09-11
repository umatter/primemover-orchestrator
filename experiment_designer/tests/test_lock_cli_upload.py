from pathlib import Path

import pytest
from click.testing import CliRunner

from experiment_designer import cli as cli_module
from experiment_designer.cli import cli
from experiment_designer.lock.api import ApiError
from experiment_designer.lock.io import dump_lock
from experiment_designer.lock.upload import UploadResult

from .test_lock_io import make_lock


@pytest.fixture
def lock_file(tmp_path: Path) -> Path:
    p = tmp_path / "study.lock.yaml"
    p.write_text(dump_lock(make_lock()))
    return p


@pytest.fixture
def secrets_file(tmp_path: Path) -> Path:
    p = tmp_path / "secrets.toml"
    p.write_text('[oxylabs.a]\nusername = "u"\npassword = "testpass"\n')
    p.chmod(0o600)
    return p


@pytest.fixture
def fake_upload(monkeypatch: pytest.MonkeyPatch):
    calls = {}

    def _upload_lock(lock_text, *, session, store, options):
        calls["options"] = options
        calls["has_store"] = store is not None
        return UploadResult(
            181,
            "Study",
            1,
            1,
            "f" * 64,
            "2026-10-01T10:00:00+02:00",
            options.mode,
            None,
            "2026-09-11T10:00:00+02:00 exp=181 mode=real lock=ffff commit=a2024c9 api=x",
        )

    class _Verify:
        def __init__(self, ok):
            self.ok = ok
            self.bound = ok
            self.differences = []

        def render(self):
            return "BOUND" if self.ok else "UNBOUND"

    monkeypatch.setattr(cli_module, "upload_lock", _upload_lock)
    monkeypatch.setattr(
        cli_module, "verify_lock", lambda *a, **k: _Verify(calls.get("verify_ok", True))
    )
    monkeypatch.setattr(cli_module, "_session_from_env", lambda: object())
    return calls


def test_upload_requires_mode(lock_file):
    out = CliRunner().invoke(cli, ["upload", str(lock_file)])
    assert out.exit_code != 0 and "--mode" in out.output


def test_upload_dry_run_checks_secrets_and_does_not_upload(lock_file, secrets_file, fake_upload):
    out = CliRunner().invoke(
        cli,
        ["upload", str(lock_file), "--mode", "real", "--dry-run", "--secrets", str(secrets_file)],
    )
    assert out.exit_code == 0, out.output
    assert "oxylabs.a.username" in out.output and "Dry run" in out.output
    assert "options" not in fake_upload


def test_upload_dry_run_reports_missing_secret(lock_file, tmp_path, fake_upload):
    empty = tmp_path / "e.toml"
    empty.write_text("")
    empty.chmod(0o600)
    out = CliRunner().invoke(
        cli, ["upload", str(lock_file), "--mode", "real", "--dry-run", "--secrets", str(empty)]
    )
    assert out.exit_code == 1 and "MISSING" in out.output and "testpass" not in out.output


def test_upload_writes_record_and_verifies(lock_file, secrets_file, fake_upload, tmp_path):
    record = tmp_path / "ledger.txt"
    out = CliRunner().invoke(
        cli,
        [
            "upload",
            str(lock_file),
            "--mode",
            "test",
            "--secrets",
            str(secrets_file),
            "--record",
            str(record),
        ],
    )
    assert out.exit_code == 0, out.output
    assert fake_upload["options"].mode == "test" and fake_upload["has_store"]
    assert record.read_text().strip().endswith("api=x")
    assert "BOUND" in out.output and "exp=181" in out.output and "api=x" in out.output


def test_upload_exits_1_when_post_upload_verify_fails(lock_file, secrets_file, fake_upload):
    fake_upload["verify_ok"] = False
    out = CliRunner().invoke(
        cli, ["upload", str(lock_file), "--mode", "test", "--secrets", str(secrets_file)]
    )
    assert out.exit_code == 1 and "UNBOUND" in out.output and "exp=181" in out.output


def test_upload_keeps_record_when_verify_raises(
    lock_file, secrets_file, fake_upload, tmp_path, monkeypatch
):
    record = tmp_path / "ledger.txt"

    def _raise(*a, **k):
        raise ApiError("GET x: HTTP 500", status=500, detail="boom")

    monkeypatch.setattr(cli_module, "verify_lock", _raise)
    out = CliRunner().invoke(
        cli,
        [
            "upload",
            str(lock_file),
            "--mode",
            "test",
            "--secrets",
            str(secrets_file),
            "--record",
            str(record),
        ],
    )
    assert out.exit_code == 1
    assert "Verify failed after upload" in out.output
    assert record.read_text().strip().endswith("api=x")


def test_upload_compiles_a_spec_first_and_says_to_commit(tmp_path, secrets_file, fake_upload):
    spec = tmp_path / "s.yaml"
    spec.write_text(
        "version: '1.0'\nexperiment: {name: S}\ncrawlers:\n  - name: b\n    session:\n      start: '+1m'\n      workflow:\n        - wait: 1s\n"
    )
    out = CliRunner().invoke(
        cli, ["upload", str(spec), "--mode", "test", "--seed", "1", "--secrets", str(secrets_file)]
    )
    assert out.exit_code == 0, out.output
    assert (tmp_path / "s.lock.yaml").exists() and "commit" in out.output.lower()


def test_verify_command(lock_file, fake_upload):
    out = CliRunner().invoke(cli, ["verify", "7", str(lock_file)])
    assert out.exit_code == 0 and "BOUND" in out.output
    fake_upload["verify_ok"] = False
    out = CliRunner().invoke(cli, ["verify", "7", str(lock_file)])
    assert out.exit_code == 1


def test_secrets_check(lock_file, secrets_file, tmp_path):
    out = CliRunner().invoke(
        cli, ["secrets", "check", str(lock_file), "--secrets", str(secrets_file)]
    )
    assert (
        out.exit_code == 0 and "oxylabs.a.password" in out.output and "testpass" not in out.output
    )
    (tmp_path / "secrets.toml").write_text("[oxylabs.a]\nusername = 'u'\n")
    out = CliRunner().invoke(
        cli, ["secrets", "check", str(lock_file), "--secrets", str(secrets_file)]
    )
    assert out.exit_code == 1 and "MISSING" in out.output


def test_secrets_check_rejects_a_corrupt_lock(tmp_path):
    bad = tmp_path / "bad.lock.yaml"
    bad.write_text("pmspec_lock: 1\nprovenance: {}\n")
    out = CliRunner().invoke(cli, ["secrets", "check", str(bad)])
    assert out.exit_code == 1
    assert "Lock invalid" in out.output
    assert "Traceback" not in out.output


def test_upload_reports_an_anchor_error(lock_file, secrets_file, fake_upload, monkeypatch):
    from experiment_designer.lock.anchor import AnchorError

    def refusing(lock_text, *, session, store, options):
        raise AnchorError("cannot parse start time 'x'")

    monkeypatch.setattr(cli_module, "upload_lock", refusing)
    out = CliRunner().invoke(
        cli, ["upload", str(lock_file), "--mode", "test", "--secrets", str(secrets_file)]
    )
    assert out.exit_code == 1 and "Refused: cannot parse start time" in out.output


def test_generate_reports_an_anchor_error(tmp_path, monkeypatch):
    from experiment_designer.lock.anchor import AnchorError

    lock = make_lock(schedule={"anchor": "not-a-time"})
    p = tmp_path / "bad-anchor.lock.yaml"
    p.write_text(dump_lock(lock))
    out = CliRunner().invoke(cli, ["generate", str(p)])
    assert out.exit_code == 1, out.output
    assert "cannot parse start time" in out.output
    assert not isinstance(out.exception, AnchorError)
