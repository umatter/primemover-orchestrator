"""orchestrator experiment upload is a thin wrapper over experiment_designer.lock."""

from pathlib import Path

import pytest

from experiment_designer.lock.io import dump_lock
from experiment_designer.lock.upload import UploadResult
from experiment_designer.tests.test_lock_io import make_lock
from orchestrator.cli import cli


@pytest.fixture
def lock_file(tmp_path: Path) -> Path:
    p = tmp_path / "s.lock.yaml"
    p.write_text(dump_lock(make_lock()))
    return p


def test_upload_delegates_and_uses_settings_credentials(
    cli_runner, lock_file, monkeypatch, mock_settings
):
    seen = {}

    def fake_upload_lock(lock_text, *, session, store, options):
        seen["base_url"] = session.base_url
        seen["mode"] = options.mode
        return UploadResult(9, "n", 1, 1, "f" * 64, "a", options.mode, None, "rec")

    class _V:
        ok = True

        def render(self):
            return "BOUND"

    class _FakeStore:
        """Stands in for a fully-configured SecretStore: nothing reported missing.

        `make_lock()`'s proxy carries real ${secret:...} placeholders, so a bare
        `None` store (as if no secrets file existed) would make the CLI's own
        pre-upload secrets gate report both as MISSING and exit 1 before
        `upload_lock` is ever reached -- correct behaviour (it mirrors
        `pmspec upload`'s refusal to send unresolved placeholders), but not what
        this test is exercising. A stand-in store keeps that gate green without
        depending on a real secrets file on the machine running the suite.
        """

        def missing(self, keys):
            return []

    # `upload_lock`/`verify_lock` are imported lazily inside experiment_upload
    # (module-level imports of experiment_designer.lock.upload cost ~150ms and
    # would slow every `orchestrator` invocation), so they are patched at
    # their source module rather than on orchestrator.cli.
    monkeypatch.setattr("experiment_designer.lock.upload.upload_lock", fake_upload_lock)
    monkeypatch.setattr("experiment_designer.lock.verify.verify_lock", lambda *a, **k: _V())
    monkeypatch.setattr("orchestrator.cli.get_settings", lambda: mock_settings)
    monkeypatch.setattr(
        "experiment_designer.lock.secrets.SecretStore.load",
        classmethod(lambda cls, p=None: _FakeStore()),
    )
    out = cli_runner.invoke(cli, ["experiment", "upload", str(lock_file), "--mode", "test"])
    assert out.exit_code == 0, out.output
    assert seen == {"base_url": "http://localhost:8090/api/v1", "mode": "test"}
    assert "rec" in out.output


def test_upload_requires_mode(cli_runner, lock_file):
    out = cli_runner.invoke(cli, ["experiment", "upload", str(lock_file)])
    assert out.exit_code != 0 and "--mode" in out.output
