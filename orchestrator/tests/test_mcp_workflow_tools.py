import json

import pytest

from orchestrator.mcp_server import server as s
from orchestrator.tests.test_mcp_write_tools import scripted


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


def _lock_text() -> str:
    from experiment_designer.lock.io import dump_lock
    from experiment_designer.tests.test_lock_io import make_lock

    return dump_lock(make_lock())


def test_audit_experiment_tool(monkeypatch):
    from experiment_designer.lock.audit import AuditResult, JobFinding

    result = AuditResult(
        experiment_id=181,
        hours=24,
        sampled=1,
        total=1,
        findings=[
            JobFinding("bot_01", "s1", 1, 900, "pi=1,psi=0,kappa=0,tau=0", "ok", ""),
            JobFinding(
                "bot_01", "s1", 2, 901, "pi=1,psi=0,kappa=0,tau=0", "lost", "treatment_lost"
            ),
        ],
    )
    calls = {}

    def _audit_experiment(session, experiment_id, lock_text, **kwargs):
        calls["experiment_id"] = experiment_id
        calls.update(kwargs)
        return result

    monkeypatch.setattr(s, "audit_experiment", _audit_experiment)
    out = json.loads(s.audit_experiment_tool(181, _lock_text(), hours=6, sample=2, strict=True))

    assert out["experiment_id"] == 181
    assert out["ok"] is False
    assert out["sampled"] == 1 and out["total"] == 1
    assert {"arm": "pi=1,psi=0,kappa=0,tau=0", "outcome": "lost", "n": 1} in out["counts"]
    assert {"arm": "pi=1,psi=0,kappa=0,tau=0", "outcome": "ok", "n": 1} in out["counts"]
    assert len(out["findings"]) == 2
    assert out["findings"][1] == {
        "crawler": "bot_01",
        "queue": "s1",
        "ordinal": 2,
        "job_id": 901,
        "arm": "pi=1,psi=0,kappa=0,tau=0",
        "outcome": "lost",
        "detail": "treatment_lost",
    }
    assert "render" in out
    assert calls["experiment_id"] == 181
    assert calls["hours"] == 6 and calls["sample"] == 2 and calls["strict"] is True


def test_audit_experiment_tool_caps_findings_at_50(monkeypatch):
    from experiment_designer.lock.audit import AuditResult, JobFinding

    findings = [JobFinding("bot", "s1", i, i, "arm", "ok", "") for i in range(60)]
    result = AuditResult(experiment_id=1, hours=24, sampled=1, total=1, findings=findings)
    monkeypatch.setattr(s, "audit_experiment", lambda *a, **k: result)
    out = json.loads(s.audit_experiment_tool(1, _lock_text()))
    assert len(out["findings"]) == 50


def test_audit_experiment_tool_invalid_lock_never_calls_audit_experiment(monkeypatch):
    def _raise(*a, **k):
        raise AssertionError("audit_experiment should not be reached with an invalid lock")

    monkeypatch.setattr(s, "audit_experiment", _raise)
    out = json.loads(s.audit_experiment_tool(181, "not a lock"))
    assert out["error"] == "invalid lock"


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


# =============================================================================
# pm_cleanup_test_experiments
# =============================================================================

_LISTING = [
    {"id": 1, "name": "test-Study-1"},
    {"id": 2, "name": "Real study"},  # not test-prefixed: never fetched
    {"id": 3, "name": "test-looks-like-one"},  # test-prefixed, but pmspec.mode is "real"
]
_DETAIL_1 = {
    "id": 1,
    "name": "test-Study-1",
    "preferences": [{"name": "pmspec.mode", "value": "test"}],
}
_DETAIL_3 = {
    "id": 3,
    "name": "test-looks-like-one",
    "preferences": [{"name": "pmspec.mode", "value": "real"}],
}


def test_cleanup_dry_run_lists_candidates_and_makes_no_delete(monkeypatch):
    # Trailing [] answers the in-flight check (queues-unprocessed) for candidate 1.
    api = scripted(monkeypatch, _LISTING, _DETAIL_1, _DETAIL_3, [])

    out = json.loads(s.pm_cleanup_test_experiments())

    assert out["dry_run"] is True
    assert [c["experiment_id"] for c in out["candidates"]] == [1]
    assert out["candidates"][0]["in_flight"] is False
    assert out["not_candidates"] == []
    assert "confirm=true" in out["to_apply"]
    assert api.writes == []
    # Only test- named experiments are ever fetched -- id 2 never is.
    assert ("GET", "experiments/2") not in api.signature()


def test_cleanup_confirm_against_remote_target_is_refused(monkeypatch):
    class _Remote:
        pm_api_base_url = "https://pm-api.example.org/api/v1"

    monkeypatch.setattr(s, "get_settings", lambda: _Remote())
    api = scripted(monkeypatch, _LISTING, _DETAIL_1, _DETAIL_3, [])

    out = json.loads(s.pm_cleanup_test_experiments(confirm=True))

    assert "refusing to mutate" in out["error"]
    assert api.writes == []


def test_cleanup_confirm_local_deletes_only_candidates(monkeypatch):
    api = scripted(monkeypatch, _LISTING, _DETAIL_1, _DETAIL_3, [], {})

    out = json.loads(s.pm_cleanup_test_experiments(confirm=True))

    assert out["deleted"] == [1]
    assert out["skipped_in_flight"] == []
    assert api.signature()[-1] == ("DELETE", "experiments/1/destroy")
    assert all(endpoint != "experiments/3/destroy" for _, endpoint in api.signature())


def test_cleanup_in_flight_candidate_is_skipped_unless_forced(monkeypatch):
    # The in-flight check for candidate 1 returns a non-empty list this time.
    api = scripted(monkeypatch, _LISTING, _DETAIL_1, _DETAIL_3, [{"id": 900}])

    out = json.loads(s.pm_cleanup_test_experiments(confirm=True))

    assert out["deleted"] == [] and out["skipped_in_flight"] == [1]
    assert ("DELETE", "experiments/1/destroy") not in api.signature()


def test_cleanup_force_deletes_an_in_flight_candidate(monkeypatch):
    scripted(monkeypatch, _LISTING, _DETAIL_1, _DETAIL_3, [{"id": 900}], {})

    out = json.loads(s.pm_cleanup_test_experiments(confirm=True, force=True))

    assert out["deleted"] == [1] and out["skipped_in_flight"] == []


def test_cleanup_experiment_ids_scopes_to_those_ids_only(monkeypatch):
    # id 3 is listed but is not a candidate (pmspec.mode == "real").
    scripted(monkeypatch, _LISTING, _DETAIL_1, _DETAIL_3, [])

    out = json.loads(s.pm_cleanup_test_experiments(experiment_ids=[1, 3]))

    assert out["dry_run"] is True
    assert [c["experiment_id"] for c in out["candidates"]] == [1]
    assert out["not_candidates"] == [3]


def test_cleanup_experiment_ids_confirm_deletes_only_those_ids(monkeypatch):
    api = scripted(monkeypatch, _LISTING, _DETAIL_1, _DETAIL_3, [], {})

    out = json.loads(s.pm_cleanup_test_experiments(confirm=True, experiment_ids=[1, 3]))

    assert out["deleted"] == [1]
    assert out["not_candidates"] == [3]
    assert all(endpoint != "experiments/3/destroy" for _, endpoint in api.signature())


def test_experiment_status_tool_cannot_leak_a_secret_valued_difference(monkeypatch):
    """The lock side of a verify difference is a placeholder; the live side is the
    resolved credential. The JSON the tool returns must never carry it."""
    from experiment_designer.lock.io import dump_lock
    from experiment_designer.lock.status import experiment_status
    from experiment_designer.tests.test_lock_io import make_lock
    from experiment_designer.tests.test_lock_verify import FakeSession

    lock = make_lock()
    lock.crawlers[0]["configuration"]["preferences"].append(
        {"name": "token", "value": "${secret:a.b}"}
    )
    text = dump_lock(lock)

    class LeakySession(FakeSession):
        def _crawler(self, cid, name, config_id):
            c = super()._crawler(cid, name, config_id)
            c["configuration"]["preferences"].append({"name": "token", "value": "hunter2"})
            return c

        def get(self, path, params=None):
            if path == "experiments/7/health":
                return {"treatment_lost_jobs": 0}
            return super().get(path, params)

    # A real VerifyResult built through verify_lock: the redaction happens there,
    # not in the tool -- so the tool has nothing to forget.
    st = experiment_status(LeakySession(text), 7, lock_text=text)
    monkeypatch.setattr(s, "experiment_status", lambda *a, **k: st)
    raw = s.experiment_status_tool(7, lock_content=text)
    assert "hunter2" not in raw
    out = json.loads(raw)
    assert out["ok"] is False
    assert {
        "path": "crawlers.bot_01.preferences.token",
        "expected": "${secret:a.b}",
        "actual": "<redacted>",
    } in out["verify"]["differences"]
