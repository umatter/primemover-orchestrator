"""orchestrator experiment upload is a thin wrapper over pmspec.lock."""

from pathlib import Path

import pytest

from orchestrator.cli import cli
from pmspec.lock.api import ApiError
from pmspec.lock.io import dump_lock
from pmspec.lock.model import LOCK_FORMAT_VERSION
from pmspec.lock.upload import UploadResult
from pmspec.tests.test_lock_io import make_lock


@pytest.fixture
def lock_file(tmp_path: Path) -> Path:
    p = tmp_path / "s.lock.yaml"
    p.write_text(dump_lock(make_lock()))
    return p


class _FakeStore:
    """Stands in for a fully-configured SecretStore: nothing reported missing.

    `make_lock()`'s proxy carries real ${secret:...} placeholders, so a bare
    `None` store (as if no secrets file existed) would make the CLI's own
    pre-upload secrets gate report both as MISSING and exit 1 before
    `upload_lock` is ever reached -- correct behaviour (it mirrors
    `pmspec upload`'s refusal to send unresolved placeholders), but not what
    these tests are exercising. A stand-in store keeps that gate green
    without depending on a real secrets file on the machine running the
    suite.
    """

    def missing(self, keys):
        return []


@pytest.fixture
def patched_upload(monkeypatch, mock_settings):
    """Patch settings, the secrets store, and a successful `upload_lock`.

    `upload_lock`/`verify_lock` are imported lazily inside `experiment_upload`
    (module-level imports of pmspec.lock.upload cost ~150ms and
    would slow every `orchestrator` invocation), so they are patched at their
    source module rather than on orchestrator.cli.
    """
    seen = {}

    def fake_upload_lock(lock_text, *, session, store, options):
        seen["base_url"] = session.base_url
        seen["mode"] = options.mode
        return UploadResult(9, "n", 1, 1, "f" * 64, "a", options.mode, None, "rec")

    monkeypatch.setattr("pmspec.lock.upload.upload_lock", fake_upload_lock)
    monkeypatch.setattr("orchestrator.cli.get_settings", lambda: mock_settings)
    monkeypatch.setattr(
        "pmspec.lock.secrets.SecretStore.load",
        classmethod(lambda cls, p=None: _FakeStore()),
    )
    return seen


def test_upload_delegates_and_uses_settings_credentials(
    cli_runner, lock_file, monkeypatch, patched_upload
):
    class _V:
        ok = True

        def render(self):
            return "BOUND"

    monkeypatch.setattr("pmspec.lock.verify.verify_lock", lambda *a, **k: _V())
    out = cli_runner.invoke(cli, ["experiment", "upload", str(lock_file), "--mode", "test"])
    assert out.exit_code == 0, out.output
    assert patched_upload == {"base_url": "http://localhost:8090/api/v1", "mode": "test"}
    assert "rec" in out.output


def test_upload_requires_mode(cli_runner, lock_file):
    out = cli_runner.invoke(cli, ["experiment", "upload", str(lock_file)])
    assert out.exit_code != 0 and "--mode" in out.output


def test_upload_keeps_record_when_verify_raises(
    cli_runner, lock_file, tmp_path, monkeypatch, patched_upload
):
    """A post-upload verify failure must not swallow the record of a server-side
    success: the record line is written and printed *before* verify runs."""

    def raising_verify_lock(*a, **k):
        raise ApiError("GET x: HTTP 500", status=500, detail="boom")

    monkeypatch.setattr("pmspec.lock.verify.verify_lock", raising_verify_lock)
    record_path = tmp_path / "record.txt"
    out = cli_runner.invoke(
        cli,
        [
            "experiment",
            "upload",
            str(lock_file),
            "--mode",
            "test",
            "--record",
            str(record_path),
        ],
    )
    assert out.exit_code == 1
    assert "Verify failed after upload" in out.output
    assert "rec" in record_path.read_text()


def test_upload_exits_1_when_verify_not_ok(cli_runner, lock_file, monkeypatch, patched_upload):
    class _V:
        ok = False

        def render(self):
            return "UNBOUND"

    monkeypatch.setattr("pmspec.lock.verify.verify_lock", lambda *a, **k: _V())
    out = cli_runner.invoke(cli, ["experiment", "upload", str(lock_file), "--mode", "test"])
    assert out.exit_code == 1
    assert "UNBOUND" in out.output
    assert "rec" in out.output


def test_upload_reports_an_invalid_lock_instead_of_a_traceback(
    cli_runner, tmp_path, patched_upload
):
    bad = tmp_path / "bad.lock.yaml"
    bad.write_text(
        dump_lock(make_lock()).replace(f"pmspec_lock: {LOCK_FORMAT_VERSION}", "pmspec_lock: 99")
    )
    out = cli_runner.invoke(cli, ["experiment", "upload", str(bad), "--mode", "test"])
    assert out.exit_code == 1, out.output
    assert "Lock invalid" in out.output and "pmspec_lock" in out.output
    assert out.exception is None or isinstance(out.exception, SystemExit)


def test_upload_reports_an_anchor_error(cli_runner, lock_file, monkeypatch, patched_upload):
    from pmspec.lock.anchor import AnchorError

    def refusing(lock_text, *, session, store, options):
        raise AnchorError("cannot parse start time 'x'")

    monkeypatch.setattr("pmspec.lock.upload.upload_lock", refusing)
    out = cli_runner.invoke(cli, ["experiment", "upload", str(lock_file), "--mode", "test"])
    assert out.exit_code == 1 and "cannot parse start time" in out.output
