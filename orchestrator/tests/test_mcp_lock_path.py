"""Locks by reference: the drop directory, the path alternatives, the sha echo.

A 500-bot lock is megabytes of YAML. Passed as ``lock_content`` it has to travel
through the model's context to reach the tool, so the largest studies -- exactly
the ones whose randomisation record matters most -- cannot be uploaded, verified
or audited over the MCP at all. The lock therefore arrives out of band (scp) and
the tool takes a *path*.

A path argument on a surface reachable with other people's bearer tokens is a
file reader unless it is fenced, so every one of these tests is about the fence:
one directory, nothing above it, symlinks resolved before the check, and a sha256
echoed back so the caller can prove which bytes were used.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.mcp_server import server as s
from pmspec.lock.io import dump_lock
from pmspec.tests.test_lock_io import make_lock

SPEC = """
version: "1.0"
experiment:
  name: MCP path
crawlers:
  - name: bot
    session:
      start: "+1m"
      workflow:
        - visit: https://example.com
"""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def drop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_settings) -> Path:
    """A lock drop directory this test owns, with the MCP pointed at it."""
    d = tmp_path / "locks"
    d.mkdir()
    monkeypatch.setattr(mock_settings, "pmspec_lock_dir", d)
    monkeypatch.setattr(s, "get_settings", lambda: mock_settings)
    return d


@pytest.fixture(autouse=True)
def no_real_secrets_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("PMSPEC_SECRETS_FILE", str(tmp_path / "no-secrets.toml"))


@pytest.fixture
def lock_file(drop: Path) -> Path:
    p = drop / "study.lock.yaml"
    p.write_text(dump_lock(make_lock()))
    return p


# ---------------------------------------------------------------------------
# The drop-directory guard
# ---------------------------------------------------------------------------


class TestResolveDropPath:
    def test_relative_path_resolves_inside_the_drop_dir(self, drop, lock_file):
        assert s._resolve_drop_path("study.lock.yaml") == lock_file.resolve()

    def test_absolute_path_inside_the_drop_dir_is_accepted(self, drop, lock_file):
        assert s._resolve_drop_path(str(lock_file)) == lock_file.resolve()

    def test_nested_relative_path_is_accepted(self, drop):
        sub = drop / "study-a"
        sub.mkdir()
        (sub / "x.lock.yaml").write_text("pmspec_lock: 2\n")
        assert s._resolve_drop_path("study-a/x.lock.yaml") == (sub / "x.lock.yaml").resolve()

    def test_dot_dot_is_refused(self, drop, tmp_path):
        (tmp_path / "secret.yaml").write_text("nope")
        with pytest.raises(s.LockRefError) as e:
            s._resolve_drop_path("../secret.yaml")
        assert ".." in str(e.value)

    def test_absolute_path_outside_the_drop_dir_is_refused(self, drop, tmp_path):
        outside = tmp_path / "secret.yaml"
        outside.write_text("nope")
        with pytest.raises(s.LockRefError) as e:
            s._resolve_drop_path(str(outside))
        assert str(drop) in str(e.value)

    def test_symlink_pointing_outside_is_refused(self, drop, tmp_path):
        outside = tmp_path / "secret.yaml"
        outside.write_text("nope")
        (drop / "sneaky.lock.yaml").symlink_to(outside)
        with pytest.raises(s.LockRefError) as e:
            s._resolve_drop_path("sneaky.lock.yaml")
        assert str(drop) in str(e.value)

    def test_missing_file_error_names_the_directory(self, drop):
        with pytest.raises(s.LockRefError) as e:
            s._resolve_drop_path("absent.lock.yaml")
        message = str(e.value)
        assert "absent.lock.yaml" in message and str(drop) in message

    def test_a_directory_is_not_a_lock(self, drop):
        (drop / "adir").mkdir()
        with pytest.raises(s.LockRefError):
            s._resolve_drop_path("adir")

    def test_default_when_the_setting_is_unset(self, monkeypatch, mock_settings):
        monkeypatch.setattr(mock_settings, "pmspec_lock_dir", None)
        monkeypatch.setattr(s, "get_settings", lambda: mock_settings)
        assert mock_settings.get_pmspec_lock_dir() == Path.home() / ".local/share/pmspec/locks"
        with pytest.raises(s.LockRefError) as e:
            s._resolve_drop_path("absent.lock.yaml")
        assert str(Path.home() / ".local/share/pmspec/locks") in str(e.value)


class TestReadLockRef:
    def test_content_wins_when_only_content_is_given(self, drop):
        assert s._read_lock_ref("pmspec_lock: 2\n", None) == "pmspec_lock: 2\n"

    def test_path_is_read_from_the_drop_dir(self, drop, lock_file):
        assert s._read_lock_ref(None, "study.lock.yaml") == lock_file.read_text()

    def test_both_is_an_error(self, drop, lock_file):
        with pytest.raises(s.LockRefError):
            s._read_lock_ref("pmspec_lock: 2\n", "study.lock.yaml")

    def test_neither_is_an_error_when_required(self, drop):
        with pytest.raises(s.LockRefError):
            s._read_lock_ref(None, None)

    def test_neither_is_allowed_when_optional(self, drop):
        assert s._read_lock_ref(None, None, required=False) is None


# ---------------------------------------------------------------------------
# Per-tool exclusivity, and the sha echo
# ---------------------------------------------------------------------------


class TestUploadExperiment:
    def test_lock_path_uploads_the_file(self, drop, lock_file, monkeypatch):
        out = json.loads(s.upload_experiment(lock_path="study.lock.yaml", mode="test"))
        assert out["success"] is True and out["dry_run"] is True
        assert out["lock_sha256"] == sha(lock_file.read_text())

    def test_lock_content_still_works_positionally(self, drop):
        text = dump_lock(make_lock())
        out = json.loads(s.upload_experiment(text, "test"))
        assert out["success"] is True and out["lock_sha256"] == sha(text)

    def test_neither_is_an_error(self, drop):
        out = json.loads(s.upload_experiment(mode="test"))
        assert out["success"] is False and "lock_path" in out["error"]

    def test_both_is_an_error(self, drop, lock_file):
        out = json.loads(
            s.upload_experiment(dump_lock(make_lock()), "test", lock_path="study.lock.yaml")
        )
        assert out["success"] is False and "lock_path" in out["error"]

    def test_escaping_path_is_an_error_not_an_exception(self, drop):
        out = json.loads(s.upload_experiment(mode="test", lock_path="../../etc/passwd"))
        assert out["success"] is False and ".." in out["error"]

    def test_lock_path_upload_still_hits_the_write_guard(self, drop, lock_file, monkeypatch):
        """A path upload is still an upload: the remote-target refusal must bite."""

        class _S:
            pm_api_base_url = "https://primemover.qamelab.org/api/v1"
            pm_api_email = "x"
            pm_api_password = "y"
            pmspec_lock_dir = drop

            def get_pmspec_lock_dir(self):
                return drop

        monkeypatch.setattr(s, "get_settings", lambda: _S())
        called: list[Any] = []
        monkeypatch.setattr(s, "upload_lock", lambda *a, **k: called.append(a))
        out = json.loads(
            s.upload_experiment(lock_path="study.lock.yaml", mode="test", dry_run=False)
        )
        assert out["success"] is False and "local" in out["error"]
        assert called == []


class TestVerifyExperiment:
    def _patch_verify(self, monkeypatch):
        from pmspec.lock.verify import VerifyResult

        seen: dict[str, Any] = {}

        def fake_verify_lock(lock_text, *, session, experiment_id, deep=False):
            seen["text"] = lock_text
            return VerifyResult(
                experiment_id=experiment_id,
                bound=True,
                local_sha256="f" * 64,
                live_sha256s=["f" * 64],
                anchor="2026-10-01T10:00:00+02:00",
                deep=deep,
                differences=[],
                extra_live_crawlers=[],
            )

        monkeypatch.setattr(s, "verify_lock", fake_verify_lock)
        return seen

    def test_lock_path(self, drop, lock_file, monkeypatch):
        seen = self._patch_verify(monkeypatch)
        out = json.loads(s.verify_experiment(7, lock_path="study.lock.yaml"))
        assert seen["text"] == lock_file.read_text()
        assert out["lock_sha256"] == sha(lock_file.read_text())

    def test_neither_is_an_error(self, drop, monkeypatch):
        self._patch_verify(monkeypatch)
        out = json.loads(s.verify_experiment(7))
        assert "error" in out and "lock_path" in out["error"]

    def test_both_is_an_error(self, drop, lock_file, monkeypatch):
        self._patch_verify(monkeypatch)
        out = json.loads(
            s.verify_experiment(7, dump_lock(make_lock()), lock_path="study.lock.yaml")
        )
        assert "error" in out and "lock_path" in out["error"]


class TestExperimentStatus:
    def _patch_status(self, monkeypatch):
        seen: dict[str, Any] = {}

        class _Status:
            experiment_id = 7
            name = "S"
            runner_user_id = 1
            uploads: list[Any] = []
            health: dict[str, Any] = {}
            ok = True
            bound_to_any_lock = False
            verify = None

            def render(self):
                return "ok"

        def fake_status(session, experiment_id, *, hours, lock_text, deep):
            seen["text"] = lock_text
            return _Status()

        monkeypatch.setattr(s, "experiment_status", fake_status)
        return seen

    def test_no_lock_at_all_is_valid(self, drop, monkeypatch):
        seen = self._patch_status(monkeypatch)
        out = json.loads(s.experiment_status_tool(7))
        assert seen["text"] is None and "error" not in out
        assert out.get("lock_sha256") is None

    def test_lock_path(self, drop, lock_file, monkeypatch):
        seen = self._patch_status(monkeypatch)
        out = json.loads(s.experiment_status_tool(7, lock_path="study.lock.yaml"))
        assert seen["text"] == lock_file.read_text()
        assert out["lock_sha256"] == sha(lock_file.read_text())

    def test_both_is_an_error(self, drop, lock_file, monkeypatch):
        self._patch_status(monkeypatch)
        out = json.loads(s.experiment_status_tool(7, lock_content="x", lock_path="study.lock.yaml"))
        assert "error" in out and "lock_path" in out["error"]


class TestAuditExperiment:
    def _patch_audit(self, monkeypatch):
        seen: dict[str, Any] = {}

        class _Result:
            experiment_id = 7
            sampled = 1
            total = 1
            audited = 1
            findings: list[Any] = []

            def ok(self, strict):
                return True

            def counts(self):
                return {}

            def render(self):
                return "ok"

        def fake_audit(session, experiment_id, lock_text, **kwargs):
            seen["text"] = lock_text
            return _Result()

        monkeypatch.setattr(s, "audit_experiment", fake_audit)
        return seen

    def test_lock_path(self, drop, lock_file, monkeypatch):
        seen = self._patch_audit(monkeypatch)
        out = json.loads(s.audit_experiment_tool(7, lock_path="study.lock.yaml"))
        assert seen["text"] == lock_file.read_text()
        assert out["lock_sha256"] == sha(lock_file.read_text())

    def test_neither_is_an_error(self, drop, monkeypatch):
        self._patch_audit(monkeypatch)
        out = json.loads(s.audit_experiment_tool(7))
        assert "error" in out and "lock_path" in out["error"]

    def test_both_is_an_error(self, drop, lock_file, monkeypatch):
        self._patch_audit(monkeypatch)
        out = json.loads(
            s.audit_experiment_tool(7, dump_lock(make_lock()), lock_path="study.lock.yaml")
        )
        assert "error" in out and "lock_path" in out["error"]


class TestDiffLocks:
    def test_both_sides_by_path(self, drop):
        a = dump_lock(make_lock())
        (drop / "a.lock.yaml").write_text(a)
        (drop / "b.lock.yaml").write_text(a)
        out = json.loads(s.diff_locks_tool(lock_a_path="a.lock.yaml", lock_b_path="b.lock.yaml"))
        assert out["identical"] is True
        assert out["lock_a_sha256"] == sha(a) and out["lock_b_sha256"] == sha(a)

    def test_one_side_by_path_one_by_content(self, drop):
        a = dump_lock(make_lock())
        (drop / "a.lock.yaml").write_text(a)
        out = json.loads(s.diff_locks_tool(lock_b=a, lock_a_path="a.lock.yaml"))
        assert out["identical"] is True

    def test_missing_side_is_an_error(self, drop):
        a = dump_lock(make_lock())
        out = json.loads(s.diff_locks_tool(lock_a=a))
        assert "error" in out and "lock_b" in out["error"]

    def test_both_forms_on_one_side_is_an_error(self, drop):
        a = dump_lock(make_lock())
        (drop / "a.lock.yaml").write_text(a)
        out = json.loads(s.diff_locks_tool(lock_a=a, lock_b=a, lock_a_path="a.lock.yaml"))
        assert "error" in out and "lock_a" in out["error"]


class TestFilePaths:
    """compile/validate take a runs spec's base fleet lock -- itself a lock -- so
    they need the same out-of-band route."""

    RUNS_POOL = "domain,lean\nnyt.com,L\n"

    def test_compile_reads_a_file_by_path(self, drop):
        spec = SPEC.replace(
            "crawlers:",
            "data_pools:\n  outlets:\n    type: csv\n    source: outlets.csv\ncrawlers:",
        )
        (drop / "outlets.csv").write_text(self.RUNS_POOL)
        out = json.loads(
            s.compile_experiment_spec(spec, file_paths={"outlets.csv": "outlets.csv"}, seed=5)
        )
        assert "lock" in out, out
        assert out["file_sha256"] == {"outlets.csv": sha(self.RUNS_POOL)}

    def test_files_and_file_paths_merge(self, drop):
        (drop / "outlets.csv").write_text(self.RUNS_POOL)
        out = json.loads(
            s.validate_experiment_spec(
                SPEC,
                files={"other.csv": "a,b\n1,2\n"},
                file_paths={"outlets.csv": "outlets.csv"},
            )
        )
        assert set(out["file_sha256"]) == {"other.csv", "outlets.csv"}

    def test_a_key_in_both_is_an_error(self, drop):
        (drop / "outlets.csv").write_text(self.RUNS_POOL)
        out = json.loads(
            s.compile_experiment_spec(
                SPEC, files={"outlets.csv": "x"}, file_paths={"outlets.csv": "outlets.csv"}, seed=5
            )
        )
        assert out["errors"] and "outlets.csv" in out["errors"][0]

    def test_an_escaping_file_path_is_an_error(self, drop):
        out = json.loads(
            s.validate_experiment_spec(SPEC, file_paths={"outlets.csv": "../outlets.csv"})
        )
        assert "errors" in out and ".." in out["errors"][0]


class TestDocstringsMentionLockPath:
    """The docstrings are the model-facing documentation: a model that cannot see
    lock_path in them will keep pasting 5 MB of YAML into a tool call."""

    @pytest.mark.parametrize(
        "tool",
        ["upload_experiment", "verify_experiment", "audit_experiment_tool", "diff_locks_tool"],
    )
    def test_mentions_lock_path(self, tool):
        doc = getattr(s, tool).__doc__ or ""
        assert "_path" in doc and "200 KB" in doc
