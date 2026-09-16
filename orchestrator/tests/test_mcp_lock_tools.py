"""The lock-aware MCP surface: compile with pools passed as content, validate a
lock, upload a lock (dry-run by default, local-only), verify."""

import json
from typing import Any

import pytest

from orchestrator.mcp_server import server as s
from pmspec.lock.io import dump_lock, load_lock, sha256_text
from pmspec.lock.model import LOCK_FORMAT_VERSION
from pmspec.tests.test_lock_io import make_lock
from pmspec.tests.test_lock_runs import FLEET_SPEC, RUNS_SPEC
from pmspec.tests.test_lock_runs_upload import RunsSession, make_runs_lock
from pmspec.tests.test_timing import JITTER_SPEC

SPEC = """
version: "1.0"
experiment:
  name: MCP
data_pools:
  outlets:
    type: csv
    source: outlets.csv
crawlers:
  - name: bot
    session:
      start: "+1m"
      workflow:
        - visit: https://example.com
"""
FILES = {"outlets.csv": "domain,lean\nnyt.com,L\n"}


@pytest.fixture(autouse=True)
def local_api(monkeypatch: pytest.MonkeyPatch, mock_settings):
    monkeypatch.setattr(s, "get_settings", lambda: mock_settings)


@pytest.fixture(autouse=True)
def no_real_secrets_file(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """No test in this file may read the developer's real secrets file.

    A dry-run upload_experiment loads SecretStore when the lock has
    placeholders; without this, that read falls through to
    ~/.config/primemover/secrets.toml on whatever machine runs the suite.
    Tests that care about a specific secrets-file state override this env var
    themselves.
    """
    monkeypatch.setenv("PMSPEC_SECRETS_FILE", str(tmp_path / "no-secrets.toml"))


def test_compile_with_files_returns_a_lock():
    out = json.loads(s.compile_experiment_spec(SPEC, files=FILES, seed=5))
    assert "lock" in out, out
    assert (
        out["lock"].startswith(f"pmspec_lock: {LOCK_FORMAT_VERSION}")
        and out["seed"] == 5
        and out["crawler_count"] == 1
    )


def test_compile_records_no_commit_and_a_placeholder_path(monkeypatch, tmp_path):
    """An MCP compile comes from no checkout, whatever directory the temp dir
    lands in -- even one inside a git repository."""
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t"]
        + ["commit", "-q", "--allow-empty", "-m", "x"],
        check=True,
    )
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile

    tempfile.tempdir = None  # re-read TMPDIR
    try:
        out = json.loads(s.compile_experiment_spec(SPEC, files=FILES, seed=5))
    finally:
        tempfile.tempdir = None
    assert "git_commit: null" in out["lock"] and "spec_path: <mcp>" in out["lock"]


def test_compile_records_client_supplied_provenance():
    """The spec lives on the caller's machine; the caller knows its commit and path.
    Passed in, they are recorded verbatim so a `real` upload of the lock is possible."""
    out = json.loads(
        s.compile_experiment_spec(
            SPEC,
            files=FILES,
            seed=5,
            git_commit="a2024c9",
            spec_path="design/experiment_spec.yaml",
        )
    )
    assert "git_commit: a2024c9" in out["lock"]
    assert "spec_path: design/experiment_spec.yaml" in out["lock"]
    assert out["git_commit"] == "a2024c9"


def test_compile_refuses_a_malformed_client_commit():
    out = json.loads(s.compile_experiment_spec(SPEC, files=FILES, seed=5, git_commit="not a sha"))
    assert "errors" in out and any("git_commit" in e for e in out["errors"])


def test_compile_records_pool_sha256_matching_a_disk_compile_of_the_same_bytes(tmp_path):
    """`files=` writes the pool to a temp dir before compiling (`_materialise_files`), so
    the sha256 recorded in provenance must be taken from those bytes on disk -- and must
    equal what `pmspec compile` produces for a spec_dir holding the identical file."""
    import hashlib

    out = json.loads(s.compile_experiment_spec(SPEC, files=FILES, seed=5))
    expected = hashlib.sha256(FILES["outlets.csv"].encode()).hexdigest()
    assert f"outlets: {expected}" in out["lock"]

    from pmspec.lock.build import compile_spec

    (tmp_path / "outlets.csv").write_text(FILES["outlets.csv"])
    result = compile_spec(SPEC, base_dir=tmp_path, spec_path="spec.yaml", seed=5)
    assert result.lock is not None, result.errors
    assert result.lock.provenance.pools == {"outlets": expected}


def test_compile_records_a_dirty_client_commit_as_dirty():
    out = json.loads(
        s.compile_experiment_spec(SPEC, files=FILES, seed=5, git_commit="a2024c9-dirty")
    )
    assert "git_commit: a2024c9-dirty" in out["lock"]


def test_compile_without_files_reports_the_missing_pool():
    out = json.loads(s.compile_experiment_spec(SPEC, seed=5))
    assert "errors" in out and any("outlets.csv" in e for e in out["errors"])


def test_validate_accepts_files_and_locks():
    ok = json.loads(s.validate_experiment_spec(SPEC, files=FILES))
    assert ok["valid"], ok
    lock = json.loads(s.validate_experiment_spec(dump_lock(make_lock())))
    assert lock["valid"] and lock["kind"] == "lock"
    bad = json.loads(s.validate_experiment_spec("pmspec_lock: 1\nprovenance: {}\n"))
    assert not bad["valid"] and bad["kind"] == "lock"


def test_upload_rejects_a_spec():
    out = json.loads(s.upload_experiment(SPEC, mode="test"))
    assert out["success"] is False and "compile" in out["error"]


def test_upload_dry_run_makes_no_request(monkeypatch: pytest.MonkeyPatch):
    calls: list[Any] = []
    monkeypatch.setattr(
        s, "_api_request", lambda *a, **k: calls.append(a) or {"data": {}}  # type: ignore[func-returns-value]
    )
    out = json.loads(s.upload_experiment(dump_lock(make_lock()), mode="test"))
    assert out["dry_run"] is True and out["success"] is True
    assert out["secrets_needed"] == ["oxylabs.a.password", "oxylabs.a.username"]
    assert out["secrets_store"] == "missing"
    assert calls == []


def test_upload_dry_run_reports_missing_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path):
    secrets_file = tmp_path / "secrets.toml"
    secrets_file.write_text("")
    secrets_file.chmod(0o600)
    monkeypatch.setenv("PMSPEC_SECRETS_FILE", str(secrets_file))
    out = json.loads(s.upload_experiment(dump_lock(make_lock()), mode="test"))
    assert out["secrets_missing"] == ["oxylabs.a.password", "oxylabs.a.username"]
    assert out["secrets_store"] == "ok"


def test_upload_dry_run_reports_no_store(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("PMSPEC_SECRETS_FILE", str(tmp_path / "does-not-exist.toml"))
    out = json.loads(s.upload_experiment(dump_lock(make_lock()), mode="test"))
    assert out["secrets_store"] == "missing"
    assert out["secrets_missing"] == ["oxylabs.a.password", "oxylabs.a.username"]


def test_upload_passes_allow_flags_to_upload_lock(monkeypatch: pytest.MonkeyPatch):
    seen = {}

    def fake_upload_lock(lock_text, *, session, store, options):
        from pmspec.lock.upload import UploadResult

        seen["options"] = options
        return UploadResult(
            181,
            "test-Study-20260911",
            1,
            1,
            "f" * 64,
            "2026-10-01T10:00:00+02:00",
            "test",
            3,
            "rec",
        )

    monkeypatch.setattr(s, "upload_lock", fake_upload_lock)
    monkeypatch.setattr(s, "verify_lock", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(s.SecretStore, "load", classmethod(lambda cls, path=None: None))
    out = json.loads(
        s.upload_experiment(
            dump_lock(make_lock()),
            mode="test",
            dry_run=False,
            allow_dirty=True,
            allow_past=True,
        )
    )
    assert out["success"] is True
    assert seen["options"].allow_dirty is True
    assert seen["options"].allow_past is True


def test_upload_refuses_remote_target(monkeypatch: pytest.MonkeyPatch):
    class _S:
        pm_api_base_url = "https://primemover.qamelab.org/api/v1"
        pm_api_email = "x"
        pm_api_password = "y"

    monkeypatch.setattr(s, "get_settings", lambda: _S())
    out = json.loads(s.upload_experiment(dump_lock(make_lock()), mode="test", dry_run=False))
    assert out["success"] is False and "local" in out["error"]


def test_upload_applies_through_upload_lock(monkeypatch: pytest.MonkeyPatch):
    seen = {}

    def fake_upload_lock(lock_text, *, session, store, options):
        from pmspec.lock.upload import UploadResult

        seen["mode"] = options.mode
        seen["session"] = session
        return UploadResult(
            181,
            "test-Study-20260911",
            1,
            1,
            "f" * 64,
            "2026-10-01T10:00:00+02:00",
            "test",
            3,
            "rec",
        )

    class _Verdict:
        ok = True
        bound = True
        differences: list[Any] = []

        def render(self):
            return "BOUND"

    monkeypatch.setattr(s, "upload_lock", fake_upload_lock)
    monkeypatch.setattr(s, "verify_lock", lambda *a, **k: _Verdict())
    monkeypatch.setattr(s.SecretStore, "load", classmethod(lambda cls, path=None: None))
    out = json.loads(s.upload_experiment(dump_lock(make_lock()), mode="test", dry_run=False))
    assert out["success"] and out["experiment_id"] == 181 and seen["mode"] == "test"
    assert out["verify"]["bound"] is True
    assert hasattr(seen["session"], "post_file") and hasattr(seen["session"], "get_bytes")


def test_upload_returns_experiment_id_when_verify_raises(monkeypatch: pytest.MonkeyPatch):
    """A raised verify_lock must not swallow a successful upload's experiment_id.

    Otherwise the caller sees a bare tool error with no id and the natural next
    move -- retry with dry_run=False -- creates a duplicate experiment.
    """

    def fake_upload_lock(lock_text, *, session, store, options):
        from pmspec.lock.upload import UploadResult

        return UploadResult(
            181,
            "test-Study-20260911",
            1,
            1,
            "f" * 64,
            "2026-10-01T10:00:00+02:00",
            "test",
            3,
            "rec",
        )

    def fake_verify_lock(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(s, "upload_lock", fake_upload_lock)
    monkeypatch.setattr(s, "verify_lock", fake_verify_lock)
    monkeypatch.setattr(s.SecretStore, "load", classmethod(lambda cls, path=None: None))
    out = json.loads(s.upload_experiment(dump_lock(make_lock()), mode="test", dry_run=False))
    assert out["success"] is True
    assert out["experiment_id"] == 181
    assert "boom" in out["verify"]["error"]


def test_verify_experiment_tool(monkeypatch: pytest.MonkeyPatch):
    from pmspec.lock.verify import Difference, VerifyResult

    monkeypatch.setattr(
        s,
        "verify_lock",
        lambda *a, **k: VerifyResult(
            7,
            False,
            "a" * 64,
            ["b" * 64],
            None,
            [Difference("crawlers.bot_01.params.pi", 1.0, 0.0)],
        ),
    )
    out = json.loads(s.verify_experiment(7, dump_lock(make_lock())))
    assert out["bound"] is False and out["ok"] is False
    assert out["differences"] == [
        {"path": "crawlers.bot_01.params.pi", "expected": 1.0, "actual": 0.0}
    ]
    assert out["render"].startswith("UNBOUND")


def test_verify_experiment_redacts_secret_valued_differences(monkeypatch: pytest.MonkeyPatch):
    from pmspec.lock.verify import Difference, VerifyResult

    monkeypatch.setattr(
        s,
        "verify_lock",
        lambda *a, **k: VerifyResult(
            7,
            True,
            "a" * 64,
            ["a" * 64],
            None,
            [Difference("crawlers.bot_01.preferences.token", "${secret:a.b}", "hunter2")],
        ),
    )
    raw = s.verify_experiment(7, dump_lock(make_lock()))
    assert "hunter2" not in raw
    out = json.loads(raw)
    assert out["differences"] == [
        {
            "path": "crawlers.bot_01.preferences.token",
            "expected": "${secret:a.b}",
            "actual": "<redacted>",
        }
    ]


# --- a runs lock over the MCP ---------------------------------------------


def test_runs_lock_without_an_experiment_id_is_an_error(monkeypatch: pytest.MonkeyPatch):
    """A runs lock names no experiment: it appends to the one its base created."""
    session = RunsSession()
    monkeypatch.setattr(s, "_McpSession", lambda: session)
    out = json.loads(s.upload_experiment(dump_lock(make_runs_lock()), mode="real"))
    assert out["success"] is False
    assert "pass -e <id>" in out["error"]
    assert session.calls == []


def test_runs_lock_dry_run_returns_the_plan_and_writes_nothing(monkeypatch: pytest.MonkeyPatch):
    session = RunsSession()
    monkeypatch.setattr(s, "_McpSession", lambda: session)
    out = json.loads(
        s.upload_experiment(dump_lock(make_runs_lock()), mode="real", experiment_id=181)
    )
    assert out["success"] is True and out["dry_run"] is True
    assert out["kind"] == "runs" and out["experiment_id"] == 181
    assert out["crawlers"] == 2 and out["queues"] == 2 and out["queues_in_lock"] == 2
    assert out["would_skip"] == 0 and out["updates"] == 0
    assert out["base_ok"] is True and out["preference_update_supported"] is False
    assert session.experiment_puts == [] and session.crawler_uploads == {}


def test_runs_lock_apply_reports_the_counts_and_verifies_its_queues(
    monkeypatch: pytest.MonkeyPatch,
):
    session = RunsSession()
    monkeypatch.setattr(s, "_McpSession", lambda: session)
    out = json.loads(
        s.upload_experiment(
            dump_lock(make_runs_lock()), mode="real", experiment_id=181, dry_run=False
        )
    )
    assert out["success"] is True and out["kind"] == "runs"
    assert out["queues_posted"] == 2 and out["queues_skipped_existing"] == 0
    assert out["verify"]["ok"] is True and out["verify"]["bound"] is True
    assert out["verify"]["differences"] == []
    assert sorted(session.crawler_uploads) == [10, 11]


def _fleet_lock_text(tmp_path) -> str:
    """A compiled fleet lock, as a runs spec's `runs_for:` target arrives over MCP:
    as text, under the bare filename the spec names."""
    from pmspec.lock.build import compile_spec

    result = compile_spec(
        FLEET_SPEC, base_dir=tmp_path, spec_path="fleet.yaml", seed=7, generator="test"
    )
    assert result.lock is not None, result.errors
    return dump_lock(result.lock)


def test_compile_a_runs_spec_with_the_base_lock_passed_as_a_file(tmp_path):
    """`files=` is how a fleet lock reaches a content-only caller: `_materialise_files`
    writes it under the name `runs_for:` names, so the compile resolves it there."""
    fleet = _fleet_lock_text(tmp_path)
    out = json.loads(
        s.compile_experiment_spec(
            RUNS_SPEC, files={"fleet.lock.yaml": fleet}, seed=11, git_commit="a2024c9"
        )
    )
    assert "lock" in out, out
    assert out["kind"] == "runs"
    assert out["base_lock_sha256"] == sha256_text(fleet)
    lock = load_lock(out["lock"])
    assert lock.is_runs and lock.provenance.base_lock_sha256 == sha256_text(fleet)
    assert sorted(c["name"] for c in lock.crawlers) == ["bot_control", "bot_treated"]


def test_compile_of_a_fleet_spec_reports_the_fleet_kind_and_no_base(tmp_path):
    out = json.loads(s.compile_experiment_spec(SPEC, files=FILES, seed=5))
    assert out["kind"] == "fleet" and out["base_lock_sha256"] is None


def test_compile_of_a_runs_spec_without_its_base_names_the_file_it_cannot_read():
    """The one new failure mode of a content-only runs compile: the caller forgot
    to put the fleet lock in `files=`."""
    out = json.loads(s.compile_experiment_spec(RUNS_SPEC, seed=11))
    assert "lock" not in out
    assert any("runs_for: cannot read fleet.lock.yaml" in e for e in out["errors"]), out


def test_the_upload_docstring_says_a_runs_lock_needs_an_experiment_id():
    """The tool's own description is the only place an MCP caller can learn this
    before the refusal; `experiment_id` defaults to None for the fleet path."""
    doc = s.upload_experiment.__doc__ or ""
    assert "REQUIRED for a `kind: runs` lock" in doc


def test_validate_accepts_a_seeded_jitter_spec():
    """The MCP validator must accept exactly what compile_experiment_spec
    accepts: the start is drawn after conversion, from the lock's seed."""
    out = json.loads(s.validate_experiment_spec(JITTER_SPEC))
    assert out["valid"] is True, out
    assert not any("jitter needs a seed" in e for e in out.get("errors", [])), out
    assert "lock" in json.loads(s.compile_experiment_spec(JITTER_SPEC, seed=7))


def test_convert_spec_to_json_still_refuses_a_jitter():
    """`convert_spec_to_json` records no seed, so a drawn start could never be
    reproduced from its output -- it resolves times, and refuses."""
    out = json.loads(s.convert_spec_to_json(JITTER_SPEC))
    assert any("start_at jitter needs a seed" in e for e in out["errors"]), out
