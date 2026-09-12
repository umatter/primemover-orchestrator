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
