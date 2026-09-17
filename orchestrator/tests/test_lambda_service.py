"""Tests for the local Lambda server's cwd gate, data-file gate and invoke logging.

Every one of these covers a failure that is *invisible* in production without
it: the decision function answers HTTP 200 whether its filters applied or not,
and the CSV behind the mainstream-media treatment arm degrades through a
swallowed ``except`` rather than raising. So "the request succeeded" has never
been evidence that the experiment got what it asked for.
"""

import logging
import os
from pathlib import Path

import pytest

from orchestrator.lambda_service import server

# A healthy fixture, mirroring bot-decision-yt/tests/test_mainstream_media_csv.py.
# Too few rows for check_parsed()'s sanity band, which is why the tests below
# that use it assert on check_csv()-level findings.
_HEALTHY_CSV = (
    "channel,verified\n" "BBC News,1\n" "The News & Observer,0\n" "WAFF 48 News &amp; Weather,1\n"
)

# The documented spreadsheet round-trip damage: header renamed, trailing empty
# column, ';' inside HTML entities replaced by ','.
_CORRUPT_CSV = "gi,verified,\nBBC News,1,\n'WAFF 48 News &amp, Weather',1,\n"

REAL_DECISION_DIR = Path(server.__file__).resolve().parent / "decision"
_HAS_SIBLING_REPO = (REAL_DECISION_DIR / "lambda_function.py").exists() and (
    REAL_DECISION_DIR.resolve().parent / "tests" / "test_mainstream_media_csv.py"
).exists()


@pytest.fixture(autouse=True)
def restore_cwd():
    """Several code paths chdir on purpose; never leak that into other tests."""
    original = os.getcwd()
    yield
    os.chdir(original)


@pytest.fixture(autouse=True)
def reset_cwd_announced(monkeypatch: pytest.MonkeyPatch):
    """The 'logged the cwd once' flag is module state; keep tests independent."""
    monkeypatch.setattr(server, "_cwd_announced", False)


@pytest.fixture(autouse=True)
def reset_missing_id_flag(monkeypatch: pytest.MonkeyPatch):
    """Same for the 'caller sent no correlation id' once-per-process flag."""
    monkeypatch.setattr(server, "_missing_correlation_id_logged", False)


@pytest.fixture
def client():
    app = server.app
    app.config["TESTING"] = False
    return app.test_client()


@pytest.fixture
def fake_decision(monkeypatch: pytest.MonkeyPatch):
    """Install a stub decision function and let each test set its response."""
    holder: dict[str, object] = {"response": {"xpath": "//a[1]", "selection_mode": "llm"}}
    calls: list[str] = []

    def _fake(payload: str):
        calls.append(payload)
        return holder["response"]

    # Patch the dispatch seam, not a cached callable: which function runs is now
    # a lookup, and a test that bypassed the lookup could not see it break.
    monkeypatch.setattr(server, "get_decision_function", lambda name=None: _fake)
    holder["calls"] = calls
    return holder


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


def _tagged(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    """(correlation_id, message) for every record that carries an id."""
    out = []
    for record in caplog.records:
        cid = getattr(record, "correlation_id", None)
        if cid:
            out.append((str(cid), record.getMessage()))
    return out


def _decision_dir(tmp_path: Path, csv: str | None = _HEALTHY_CSV) -> Path:
    """A stand-in decision directory: lambda_function.py plus (optionally) the CSV."""
    (tmp_path / "lambda_function.py").write_text("def select_recommendation(x): return {}\n")
    if csv is not None:
        (tmp_path / "mainstream_media_channels.csv").write_text(csv)
    return tmp_path


# ---------------------------------------------------------------------------
# /invoke - degradation logging
# ---------------------------------------------------------------------------


def test_degraded_response_logs_warning_naming_failed_filters(
    client, fake_decision, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)
    fake_decision["response"] = {
        "xpath": "//a[1]",
        "degraded": True,
        "selection_mode": "llm",
        "filters": {
            "already_watched": "ok",
            "political": "failed: TimeoutError: openai timed out",
            "similar": "skipped",
        },
        "warnings": ["Political filter not applied"],
    }

    response = client.post("/invoke", json={"job_id": "j-1"})

    assert response.status_code == 200
    warnings = _messages(caplog, logging.WARNING)
    assert any("DEGRADED" in m for m in warnings), warnings
    assert any("political" in m for m in warnings), warnings
    # the filter that worked must not be named as a casualty
    assert not any("'already_watched'" in m for m in warnings), warnings


def test_degraded_response_still_returns_200_with_body_unchanged(client, fake_decision):
    """The runner decides what to do with a degraded decision; we never fail the call."""
    payload = {
        "xpath": "//a[3]",
        "degraded": True,
        "selection_mode": "random",
        "filters": {"political": "failed: RuntimeError: boom"},
        "warnings": ["nothing applied"],
    }
    fake_decision["response"] = payload

    response = client.post("/invoke", json={"videos": []})

    assert response.status_code == 200
    assert response.get_json() == payload


def test_treatment_lost_logs_error(client, fake_decision, caplog: pytest.LogCaptureFixture):
    """The one alarm: a treated bot produced control-arm data."""
    caplog.set_level(logging.DEBUG)
    fake_decision["response"] = {
        "xpath": "//a[7]",
        "degraded": True,
        "treatment_lost": True,
        "selection_mode": "random",
        "filters": {"political": "failed: RuntimeError: no llm", "similar": "failed: RuntimeError"},
        "warnings": ["LLM unavailable"],
        "llm": {"available": False, "error_type": "AuthenticationError", "error": "401"},
    }

    client.post("/invoke", json={"job_id": "j-2"})

    errors = _messages(caplog, logging.ERROR)
    assert any("TREATMENT LOST" in m for m in errors), errors
    assert any("control-arm data" in m for m in errors), errors
    # the cause belongs in the same line, not in a second log the reader has to find
    assert any("llm_unavailable=AuthenticationError" in m for m in errors), errors
    # and a degraded decision is still a WARNING naming the casualty
    assert any("political" in m for m in _messages(caplog, logging.WARNING))


def test_healthy_control_crawler_logs_no_error_despite_random_selection(
    client, fake_decision, caplog: pytest.LogCaptureFixture
):
    """The regression this contract was amended to prevent.

    A control crawler (pi=psi=kappa=tau=0) requests no LLM filter, so it
    legitimately selects at random with five of six filters ``skipped``. 82% of
    real payloads on this box look like this. Gating the alarm on
    ``selection_mode`` alone fired an ERROR on every one of them.
    """
    caplog.set_level(logging.DEBUG)
    fake_decision["response"] = {
        "xpath": "//a[2]",
        "degraded": False,
        "treatment_lost": False,
        "selection_mode": "random",
        "filters": {
            "already_watched": "ok",
            "mainstream_media": "skipped",
            "children_entertainment": "skipped",
            "similar": "skipped",
            "political": "skipped",
            "fringe": "skipped",
        },
        "llm": {"available": True, "error_type": None, "error": None},
        "warnings": [],
    }

    client.post("/invoke", json={"job_id": "j-control"})

    assert _messages(caplog, logging.ERROR) == []
    assert _messages(caplog, logging.WARNING) == []
    # at most a DEBUG note explaining that random is expected here
    debug = _messages(caplog, logging.DEBUG)
    assert any("expected, correct mode for a control crawler" in m for m in debug), debug


def test_legacy_payload_without_a_report_is_unknown_not_degraded(
    client, fake_decision, caplog: pytest.LogCaptureFixture
):
    """A payload from before 2026-08-27 ~15:38 carries no report at all.

    The deployed function has emitted one on every invocation since; 361 of the
    6682 archived payloads predate it (decoded 2026-09-08). Absence must read as
    *unknown*. It used to read as degraded (and, with ``selection_mode ==
    "random"``, as a lost treatment arm), which is exactly backwards for the
    control-arm payloads that make up most of the archive.
    """
    caplog.set_level(logging.DEBUG)
    fake_decision["response"] = {"xpath": "//a[1]", "selection_mode": "random"}

    client.post("/invoke", json={"job_id": "j-legacy"})

    assert _messages(caplog, logging.ERROR) == []
    assert _messages(caplog, logging.WARNING) == []
    assert any("report=absent(legacy)" in m for m in _messages(caplog, logging.INFO))


def test_degraded_but_not_treatment_lost_warns_without_erroring(
    client, fake_decision, caplog: pytest.LogCaptureFixture
):
    """One requested filter of several failed: worth a look, not an alarm."""
    caplog.set_level(logging.DEBUG)
    fake_decision["response"] = {
        "xpath": "//a[4]",
        "degraded": True,
        "treatment_lost": False,
        "selection_mode": "llm",
        "filters": {"political": "ok", "mainstream_media": "failed: OSError: csv gone"},
        "warnings": ["Aversion to mainstream media channels ignored"],
    }

    client.post("/invoke", json={"job_id": "j-4"})

    assert any("DEGRADED" in m for m in _messages(caplog, logging.WARNING))
    assert _messages(caplog, logging.ERROR) == []


def test_skipped_filters_are_never_reported_as_casualties(
    client, fake_decision, caplog: pytest.LogCaptureFixture
):
    """``skipped`` means *not requested*; it must not be counted as a failure."""
    caplog.set_level(logging.DEBUG)
    fake_decision["response"] = {
        "xpath": "//a[5]",
        "degraded": True,
        "treatment_lost": False,
        "selection_mode": "llm",
        "filters": {"political": "failed: RuntimeError: boom", "fringe": "skipped"},
        "warnings": [],
    }

    client.post("/invoke", json={"job_id": "j-5"})

    warnings = _messages(caplog, logging.WARNING)
    assert any("failed_filters=['political']" in m for m in warnings), warnings
    assert any("not_requested=['fringe']" in m for m in warnings), warnings


def test_clean_response_logs_no_warning_or_error(
    client, fake_decision, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)
    fake_decision["response"] = {
        "xpath": "//a[1]",
        "degraded": False,
        "selection_mode": "llm",
        "filters": {"political": "ok", "similar": "ok"},
        "warnings": [],
    }

    client.post("/invoke", json={"job_id": "j-3"})

    assert _messages(caplog, logging.WARNING) == []
    assert _messages(caplog, logging.ERROR) == []
    assert any("decision completed" in m for m in _messages(caplog, logging.INFO))


def test_hard_error_field_logs_error(client, fake_decision, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)
    fake_decision["response"] = {"xpath": "", "error": "openai: 401 unauthorized"}

    client.post("/invoke", json={})

    assert any("401 unauthorized" in m for m in _messages(caplog, logging.ERROR))


def test_elapsed_ms_is_logged(client, fake_decision, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)

    client.post("/invoke", json={})

    assert any("elapsed_ms=" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_decision_exception_returns_500_and_logs_with_the_id(
    client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)

    def _boom(payload: str):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(server, "get_decision_function", lambda name=None: _boom)

    response = client.post("/invoke", json={}, headers={"X-Correlation-Id": "exp-9|j-9"})

    assert response.status_code == 500
    assert response.get_json()["error"] == "kaboom"
    assert any("exp-9|j-9" in m for m in _messages(caplog, logging.ERROR))


# ---------------------------------------------------------------------------
# /invoke - correlation ids
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("header", ["X-Correlation-Id", "X-Job-Id"])
def test_correlation_id_from_header_lands_in_every_record(
    client, fake_decision, caplog: pytest.LogCaptureFixture, header: str
):
    caplog.set_level(logging.DEBUG)

    client.post("/invoke", json={"a": 1}, headers={header: "exp-30|cr-1|q-2|j-525"})

    tagged = _tagged(caplog)
    assert tagged, "no log record carried a correlation_id"
    assert all(cid == "exp-30|cr-1|q-2|j-525" for cid, _ in tagged)
    assert all("exp-30|cr-1|q-2|j-525" in message for _, message in tagged)


@pytest.mark.parametrize("key", ["job_id", "correlation_id"])
def test_correlation_id_from_payload(
    client, fake_decision, caplog: pytest.LogCaptureFixture, key: str
):
    caplog.set_level(logging.DEBUG)

    client.post("/invoke", json={key: 525, "videos": []})

    tagged = _tagged(caplog)
    assert tagged
    assert all(cid == "525" for cid, _ in tagged)


def test_header_wins_over_payload(client, fake_decision, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)

    client.post("/invoke", json={"job_id": 111}, headers={"X-Correlation-Id": "hdr-1"})

    tagged = _tagged(caplog)
    assert tagged and all(cid == "hdr-1" for cid, _ in tagged)


def test_generated_id_is_used_when_nothing_is_supplied(
    client, fake_decision, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)

    client.post("/invoke", json={"videos": []})

    ids = {cid for cid, _ in _tagged(caplog)}
    assert len(ids) == 1, f"one request must use one id, got {ids}"
    # ``anon-`` so nobody hunts for a job with that id
    assert ids.pop().startswith("anon-")


def test_missing_correlation_id_is_reported_once_not_silently(
    client, fake_decision, caplog: pytest.LogCaptureFixture
):
    """The gap has to be visible.

    Correlation-id logging is inert while the caller sends no id -- every
    request lands under its own generated id and joins to nothing -- and that
    was true of every production request the runner made. It is now said out
    loud, once per process rather than once per decision.
    """
    caplog.set_level(logging.DEBUG)

    client.post("/invoke", json={"videos": []})
    client.post("/invoke", json={"videos": []})

    warnings = [m for m in _messages(caplog, logging.WARNING) if "no correlation id" in m]
    assert len(warnings) == 1, warnings
    assert "X-Correlation-Id" in warnings[0]
    assert "logged once per process" in warnings[0]
    # the second request says the same thing at DEBUG, not at WARNING
    assert any("no correlation id" in m for m in _messages(caplog, logging.DEBUG))


def test_a_supplied_correlation_id_produces_no_gap_warning(
    client, fake_decision, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)

    client.post("/invoke", json={}, headers={"X-Correlation-Id": "exp-30|cr-1|q-2|j-525"})

    assert not [m for m in _messages(caplog, logging.WARNING) if "no correlation id" in m]


def test_non_json_request_is_rejected_and_logged(client, fake_decision, caplog):
    caplog.set_level(logging.DEBUG)

    response = client.post("/invoke", data="not json", content_type="text/plain")

    assert response.status_code == 400
    assert any("not JSON" in m for m in _messages(caplog, logging.WARNING))


# ---------------------------------------------------------------------------
# Working directory
# ---------------------------------------------------------------------------


def test_ensure_decision_cwd_chdirs_and_logs_at_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)
    decision = _decision_dir(tmp_path)
    monkeypatch.setattr(server, "DECISION_DIR", decision)

    resolved = server.ensure_decision_cwd()

    assert resolved == decision.resolve()
    assert Path(os.getcwd()) == decision.resolve()
    assert any("Decision working directory" in m for m in _messages(caplog, logging.INFO))


def test_ensure_decision_cwd_logs_error_when_directory_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(server, "DECISION_DIR", tmp_path / "nope")

    assert server.ensure_decision_cwd() is None
    assert any("Decision directory not found" in m for m in _messages(caplog, logging.ERROR))


def test_ensure_decision_cwd_logs_error_when_lambda_function_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(server, "DECISION_DIR", tmp_path)

    assert server.ensure_decision_cwd() is None
    assert any("lambda_function.py not found" in m for m in _messages(caplog, logging.ERROR))


# ---------------------------------------------------------------------------
# mainstream_media_channels.csv
# ---------------------------------------------------------------------------


def test_missing_csv_is_reported_and_logged_at_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)
    decision = _decision_dir(tmp_path, csv=None)
    monkeypatch.setattr(server, "DECISION_DIR", decision)

    state = server.check_mainstream_media_csv()
    assert state["present"] is False
    assert state["ok"] is False

    server._log_csv_state(state)
    errors = _messages(caplog, logging.ERROR)
    assert any("MISSING" in m for m in errors), errors
    assert any("control arm" in m for m in errors), errors


@pytest.mark.skipif(not _HAS_SIBLING_REPO, reason="bot-decision-yt checkout not available")
def test_corrupted_csv_is_detected_with_the_sibling_repos_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)
    decision = _decision_dir(tmp_path, csv=_CORRUPT_CSV)
    monkeypatch.setattr(server, "DECISION_DIR", decision)

    state = server.check_mainstream_media_csv()

    assert state["ok"] is False
    assert state["checked_by"].endswith("test_mainstream_media_csv.py")
    joined = " | ".join(state["problems"])
    assert "header" in joined, state["problems"]

    server._log_csv_state(state)
    assert any("malformed" in m for m in _messages(caplog, logging.ERROR))


@pytest.mark.skipif(not _HAS_SIBLING_REPO, reason="bot-decision-yt checkout not available")
def test_the_real_shipped_csv_is_healthy():
    """The gate must pass on the file production actually reads, or it will be bypassed."""
    state = server.check_mainstream_media_csv()
    assert state["ok"] is True, state["problems"]
    assert state["checked_by"].endswith("test_mainstream_media_csv.py")


def test_csv_check_degrades_to_presence_only_without_the_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An orchestrator checkout without the sibling repo must not hard-fail."""
    monkeypatch.setattr(server, "DECISION_DIR", _decision_dir(tmp_path))
    monkeypatch.setattr(server, "_load_csv_checkers", lambda: (None, None, "no sibling repo"))

    state = server.check_mainstream_media_csv()

    assert state["ok"] is True
    assert state["checked_by"] == "presence-only"
    assert "no sibling repo" in state["note"]


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


# The cwd and the mainstream-media CSV are no longer part of /health.
#
# One server now hosts N project decision functions, so neither judgement is the
# server's to make: there is no single cwd to be right about, and whether
# mainstream_media_channels.csv is intact is a fact about bot-decision-yt, which
# reports it through the health() hook the registry calls. The replacements live
# in test_decision_registry.py (registry shape, hook results, dispatch); what
# stays here is the cwd gate itself, which still runs in the no-registry
# fallback, and the fd rule below.


# ---------------------------------------------------------------------------
# LambdaServerComponent.validate() reuses the same gate
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_SIBLING_REPO, reason="bot-decision-yt checkout not available")
def test_component_validate_flags_a_corrupted_csv(tmp_path: Path):
    from orchestrator.components.lambda_server import LambdaServerComponent

    decision = _decision_dir(tmp_path, csv=_CORRUPT_CSV)
    component = LambdaServerComponent()

    errors = component._validate_decision_data(decision)

    assert errors, "a corrupted CSV must be reported"
    assert "control arm" in errors[0]


def test_component_validate_flags_a_missing_csv(tmp_path: Path):
    from orchestrator.components.lambda_server import LambdaServerComponent

    decision = _decision_dir(tmp_path, csv=None)
    component = LambdaServerComponent()

    errors = component._validate_decision_data(decision)

    assert errors
    assert "mainstream_media_channels.csv" in errors[0]


def test_component_validate_accepts_a_healthy_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from orchestrator.components.lambda_server import LambdaServerComponent

    monkeypatch.setattr(server, "_load_csv_checkers", lambda: (None, None, "stubbed"))
    decision = _decision_dir(tmp_path)

    assert LambdaServerComponent()._validate_decision_data(decision) == []


# ---------------------------------------------------------------------------
# /health surfaces the file-descriptor count (round 7 fd-leak forensics)
# ---------------------------------------------------------------------------
#
# A socket leak in the decision function crossed the process's 1024 fd ceiling
# mid-run, after which every OpenAI call failed and the children/entertainment
# filter silently stopped applying -- with nothing observable to show it.
# /health now reports the count so the next occurrence is a glance.


def test_health_reports_open_file_descriptors(client, tmp_path: Path, monkeypatch):
    # A tmp decision package, so this test measures fds rather than whether the
    # sibling bot-decision-yt checkout happens to be importable on this box.
    monkeypatch.setattr(server, "DECISION_DIR", _decision_dir(tmp_path))

    body = client.get("/health").get_json()
    assert "open_files" in body
    fds = body["open_files"]
    # On this Linux host both are real numbers; the count is well under the limit.
    assert isinstance(fds["open"], int) and fds["open"] > 0
    assert isinstance(fds["soft_limit"], int) and fds["soft_limit"] > 0
    assert fds["near_limit"] is False


def test_health_goes_degraded_near_the_fd_ceiling(client, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(server, "DECISION_DIR", _decision_dir(tmp_path))
    # Simulate a process about to hit the wall: near_limit must flip status,
    # which is the whole point -- it warns before calls start failing.
    monkeypatch.setattr(
        server,
        "_fd_stats",
        lambda: {
            "open": 1000,
            "soft_limit": 1024,
            "near_limit": True,
        },
    )
    body = client.get("/health").get_json()
    assert body["status"] == "degraded"
    assert body["open_files"]["near_limit"] is True


def test_fd_stats_never_raises(monkeypatch):
    # /health must answer even if the fd counters cannot be read (e.g. no
    # /proc). Force the read to fail and confirm a shaped result, not an
    # exception.
    monkeypatch.setattr(server.os, "listdir", lambda p: (_ for _ in ()).throw(OSError("no /proc")))
    stats = server._fd_stats()
    assert stats["open"] is None
    assert stats["near_limit"] is None


# ---------------------------------------------------------------------------
# The server uses waitress in production, not werkzeug's dev server
# ---------------------------------------------------------------------------
#
# The werkzeug dev server leaks an epoll fd under concurrency (reproduced with a
# bare threaded Flask app: 0 -> 17 over 90 concurrent requests). On this VM that
# reached 844 leaked fds in one soak round and silently disabled a filter once
# the ceiling was hit. waitress holds no per-connection epoll. These tests pin
# which server main() actually starts.


def test_production_mode_serves_with_waitress(monkeypatch):
    import waitress

    called = {}
    monkeypatch.setattr(server, "run_startup_checks", lambda: {})
    monkeypatch.setattr(server, "get_decision_function", lambda: None)
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: called.setdefault("waitress", kw))
    monkeypatch.setattr(server.app, "run", lambda **kw: called.setdefault("werkzeug", kw))
    monkeypatch.setattr("sys.argv", ["server", "--port", "5001"])

    server.main()

    assert "waitress" in called, "production mode must serve with waitress"
    assert "werkzeug" not in called, "the dev server must not be used in production"


def test_debug_mode_keeps_the_dev_server(monkeypatch):
    import waitress

    called = {}
    monkeypatch.setattr(server, "run_startup_checks", lambda: {})
    monkeypatch.setattr(server, "get_decision_function", lambda: None)
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: called.setdefault("waitress", kw))
    monkeypatch.setattr(server.app, "run", lambda **kw: called.setdefault("werkzeug", kw))
    monkeypatch.setattr("sys.argv", ["server", "--debug"])

    server.main()

    # --debug is a local convenience where the reloader/debugger matter and
    # concurrency does not; the dev server is fine there.
    assert "werkzeug" in called
    assert "waitress" not in called
