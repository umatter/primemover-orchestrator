import json

import pytest

from orchestrator.mcp_server import server as s


@pytest.fixture(autouse=True)
def local_api(monkeypatch, mock_settings):
    monkeypatch.setattr(s, "get_settings", lambda: mock_settings)


def test_experiment_status_tool(monkeypatch):
    from experiment_designer.lock.status import ExperimentStatus, ProvenanceUpload

    st = ExperimentStatus(
        experiment_id=181,
        name="Study",
        runner_user_id=None,
        uploads=[ProvenanceUpload("upload 1", {"mode": "test", "lock_sha256": "a" * 64})],
        health={"bad_jobs": 0, "treatment_lost_jobs": 0},
        verify=None,
    )
    monkeypatch.setattr(s, "experiment_status", lambda *a, **k: st)
    out = json.loads(s.experiment_status_tool(181))
    assert out["ok"] is True and out["uploads"][0]["label"] == "upload 1" and "render" in out


def test_diff_locks_tool(monkeypatch):
    from experiment_designer.lock.diff import LockDiff

    d = LockDiff(
        provenance_a={"spec_path": "a.yaml", "git_commit": "a2024c9"},
        provenance_b={"spec_path": "a.yaml", "git_commit": "a2024c9"},
        anchor_a="upload",
        anchor_b="upload",
    )
    monkeypatch.setattr(s, "diff_locks", lambda a, b: d)
    out = json.loads(s.diff_locks_tool("LOCK A TEXT", "LOCK B TEXT"))
    assert out["identical"] is True
    assert out["differences"] == [] and out["only_in_a"] == [] and out["only_in_b"] == []
    assert "identical" in out["render"]


def test_diff_locks_tool_invalid_lock(monkeypatch):
    from experiment_designer.lock.io import LockError

    def _raise(a, b):
        raise LockError(["pmspec_lock: expected 1, got 99"])

    monkeypatch.setattr(s, "diff_locks", _raise)
    out = json.loads(s.diff_locks_tool("bad", "bad"))
    assert out["error"] == "invalid lock" and "pmspec_lock" in out["errors"][0]
