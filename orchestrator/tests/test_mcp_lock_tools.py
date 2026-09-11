"""The lock-aware MCP surface: compile with pools passed as content, validate a
lock, upload a lock (dry-run by default, local-only), verify."""

import json
from typing import Any

import pytest

from experiment_designer.lock.io import dump_lock
from experiment_designer.tests.test_lock_io import make_lock
from orchestrator.mcp_server import server as s

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


def test_compile_with_files_returns_a_lock():
    out = json.loads(s.compile_experiment_spec(SPEC, files=FILES, seed=5))
    assert "lock" in out, out
    assert (
        out["lock"].startswith("pmspec_lock: 1") and out["seed"] == 5 and out["crawler_count"] == 1
    )


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
    assert calls == []


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
        from experiment_designer.lock.upload import UploadResult

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


def test_verify_experiment_tool(monkeypatch: pytest.MonkeyPatch):
    from experiment_designer.lock.verify import Difference, VerifyResult

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
