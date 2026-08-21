"""The two safety properties every MCP write tool depends on.

``_require_local_write_target()`` and ``_write()`` are the only things standing
between an LLM-driven tool surface and the production experiment database. Every
one of the eight ``pm_`` tools funnels through them, so a regression here is not
one broken tool -- it is all of them at once, silently, in the direction of
mutating real research data.

Two properties are pinned:

1. **Local-only.** Writes are refused unless the API target is the local dev
   server. This is deliberately checked against the *resolved settings*, so a
   deployment pointed at a real host cannot write no matter what a caller asks
   for.
2. **Dry-run by default.** Without ``confirm=True`` a write must preview and must
   not reach the API at all. "Did not call the API" is asserted directly rather
   than inferred from the returned JSON, because the return value is what an
   LLM sees and the side effect is what actually matters.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from orchestrator.mcp_server import server as s


def _point_api_at(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """Make get_settings() report ``url`` as the API base for this test."""

    class _Settings:
        pm_api_base_url = url

    monkeypatch.setattr(s, "get_settings", lambda: _Settings())


class _RecordingApi:
    """Stands in for _api_request and records whether it was ever reached."""

    def __init__(self, result: Any = None, raises: Exception | None = None):
        self.calls: list[tuple[tuple, dict]] = []
        self._result = result if result is not None else {"data": {"id": 1}}
        self._raises = raises

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self._raises is not None:
            raise self._raises
        return self._result

    @property
    def called(self) -> bool:
        return bool(self.calls)


class TestRequireLocalWriteTarget:
    """Which API targets are allowed to be mutated."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8090/api/v1",
            "http://127.0.0.1:8090/api/v1",
            "http://localhost:8090",
            "http://127.0.0.1:8090/",
        ],
    )
    def test_local_dev_api_is_allowed(self, monkeypatch, url):
        _point_api_at(monkeypatch, url)
        assert s._require_local_write_target() is None

    @pytest.mark.parametrize(
        "url",
        [
            "https://pm-api.example.org/api/v1",
            "http://192.168.1.50:8090/api/v1",
            "http://10.0.0.5:8090",
            "https://primemover.qamelab.org/api/v1",
            # A hostname that merely starts with "localhost" must not slip
            # through a prefix/substring check.
            "http://localhost.example.org:8090/api/v1",
            "http://notlocalhost:8090/api/v1",
        ],
    )
    def test_remote_hosts_are_refused(self, monkeypatch, url):
        _point_api_at(monkeypatch, url)
        error = s._require_local_write_target()
        assert error is not None
        # The message must name the target, so a refusal is diagnosable rather
        # than mysterious.
        assert url in error
        assert "refusing to mutate" in error

    @pytest.mark.parametrize(
        "url",
        ["http://localhost:9999/api/v1", "http://127.0.0.1:3000", "http://localhost:8091"],
    )
    def test_right_host_wrong_port_is_refused(self, monkeypatch, url):
        _point_api_at(monkeypatch, url)
        assert s._require_local_write_target() is not None

    def test_localhost_without_an_explicit_port_is_allowed(self, monkeypatch):
        """Documents current behaviour rather than endorsing it.

        The port check is ``port is not None and port != 8090``, so a URL with no
        explicit port (implicit 80/443) passes the guard on the strength of the
        host alone. That is defensible -- the host restriction is the real
        control -- but it is a deliberate hole, so it is pinned here: if someone
        tightens the check to require :8090 explicitly, this test should fail and
        be updated consciously rather than the behaviour changing unnoticed.
        """
        _point_api_at(monkeypatch, "http://localhost/api/v1")
        assert s._require_local_write_target() is None


class TestWriteDryRunByDefault:
    """The default must be to describe the change, not to make it."""

    def test_dry_run_does_not_touch_the_api(self, monkeypatch):
        api = _RecordingApi()
        monkeypatch.setattr(s, "_api_request", api)
        _point_api_at(monkeypatch, "http://localhost:8090/api/v1")

        s._write("create experiment", "POST", "experiments", {"name": "x"})

        # The property that matters is the absence of the side effect.
        assert api.called is False, "dry run must not reach the API"

    def test_dry_run_previews_the_exact_request(self, monkeypatch):
        monkeypatch.setattr(s, "_api_request", _RecordingApi())
        _point_api_at(monkeypatch, "http://localhost:8090/api/v1")

        out = json.loads(
            s._write("create experiment", "POST", "experiments", {"name": "Study", "active": 1})
        )

        assert out["dry_run"] is True
        assert out["action"] == "create experiment"
        assert out["method"] == "POST"
        assert out["endpoint"] == "experiments"
        assert out["payload"] == {"name": "Study", "active": 1}
        assert "confirm=true" in out["to_apply"]
        assert "applied" not in out

    def test_dry_run_includes_current_state_and_warnings_when_given(self, monkeypatch):
        monkeypatch.setattr(s, "_api_request", _RecordingApi())
        _point_api_at(monkeypatch, "http://localhost:8090/api/v1")

        out = json.loads(
            s._write(
                "delete experiment",
                "DELETE",
                "experiments/7",
                None,
                current={"id": 7, "name": "Live study"},
                warnings=["this experiment has 40 processed queues"],
            )
        )

        assert out["current"] == {"id": 7, "name": "Live study"}
        assert out["warnings"] == ["this experiment has 40 processed queues"]

    def test_absent_current_and_warnings_are_omitted_not_null(self, monkeypatch):
        monkeypatch.setattr(s, "_api_request", _RecordingApi())
        _point_api_at(monkeypatch, "http://localhost:8090/api/v1")

        out = json.loads(s._write("noop", "POST", "things", {}))

        assert "current" not in out
        assert "warnings" not in out


class TestWriteConfirm:
    """With confirm=True the change is actually applied."""

    def test_confirm_calls_the_api_with_the_previewed_request(self, monkeypatch):
        api = _RecordingApi(result={"data": {"id": 42, "name": "Study"}})
        monkeypatch.setattr(s, "_api_request", api)
        _point_api_at(monkeypatch, "http://localhost:8090/api/v1")

        out = json.loads(
            s._write("create experiment", "POST", "experiments", {"name": "Study"}, confirm=True)
        )

        assert out["applied"] is True
        assert out["action"] == "create experiment"
        # _unwrap strips the {"data": ...} envelope.
        assert out["result"] == {"id": 42, "name": "Study"}

        assert len(api.calls) == 1
        args, kwargs = api.calls[0]
        assert args[0] == "POST"
        assert args[1] == "experiments"
        assert kwargs["data"] == {"name": "Study"}

    def test_api_failure_is_reported_as_not_applied(self, monkeypatch):
        api = _RecordingApi(raises=RuntimeError("422 Unprocessable Entity"))
        monkeypatch.setattr(s, "_api_request", api)
        _point_api_at(monkeypatch, "http://localhost:8090/api/v1")

        out = json.loads(s._write("add bot", "POST", "crawlers", {"name": "b"}, confirm=True))

        # Must not claim success, and must surface the reason.
        assert out["applied"] is False
        assert "422" in out["error"]
        assert out["action"] == "add bot"

    def test_confirm_still_returns_json_not_a_raised_exception(self, monkeypatch):
        monkeypatch.setattr(s, "_api_request", _RecordingApi(raises=Exception("boom")))
        _point_api_at(monkeypatch, "http://localhost:8090/api/v1")

        # An MCP tool that raises gives the caller a protocol error rather than a
        # usable message, so failures have to come back as JSON.
        out = json.loads(s._write("x", "POST", "y", {}, confirm=True))
        assert out["applied"] is False


class TestGuardBeatsConfirm:
    """The local-only guard is not something confirm=True can override."""

    @pytest.mark.parametrize("confirm", [False, True])
    def test_remote_target_is_refused_in_both_modes(self, monkeypatch, confirm):
        api = _RecordingApi()
        monkeypatch.setattr(s, "_api_request", api)
        _point_api_at(monkeypatch, "https://pm-api.example.org/api/v1")

        out = json.loads(
            s._write("delete experiment", "DELETE", "experiments/1", None, confirm=confirm)
        )

        assert "error" in out
        assert "refusing to mutate" in out["error"]
        assert api.called is False, "a refused write must not reach the API"
        # Crucially it must not look like it worked.
        assert out.get("applied") is not True
        assert out.get("dry_run") is not True

    def test_guard_is_checked_before_the_payload_is_even_built(self, monkeypatch):
        """A remote target is refused regardless of how malformed the request is."""
        api = _RecordingApi()
        monkeypatch.setattr(s, "_api_request", api)
        _point_api_at(monkeypatch, "https://pm-api.example.org/api/v1")

        out = json.loads(s._write("", "NOTAVERB", "", None, confirm=True))
        assert "error" in out
        assert api.called is False


def _mutating_tools() -> dict[str, bool]:
    """Map every tool that can mutate the API to whether it is guarded.

    Found structurally rather than by naming convention. An earlier version of
    this check enumerated ``pm_*`` names and claimed it would catch a tool that
    bypassed ``_write()`` -- it would not have, because a mutating tool without
    that prefix is invisible to a name check, and one already exists.

    "Mutating" means calling a write verb at a non-login endpoint. The login
    POST is excluded deliberately: several read-only tools authenticate before
    reading, and counting that as a mutation would make the check meaningless.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(s))
    write_verbs = {"post", "put", "delete", "patch"}
    result: dict[str, bool] = {}

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(
            isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "tool"
            for d in node.decorator_list
        ):
            continue

        mutates = False
        guarded = False
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue

            if isinstance(sub.func, ast.Name) and sub.func.id == "_require_local_write_target":
                guarded = True

            # _write() is BOTH the mutation and the guard: it is the sanctioned
            # way to write, so a tool using it must count as mutating, otherwise
            # every correctly-written tool disappears from this scan and the
            # check silently passes on an empty set.
            if isinstance(sub.func, ast.Name) and sub.func.id == "_write":
                guarded = True
                mutates = True

            # Endpoint strings are usually f-strings (JoinedStr), not plain
            # constants -- `f"{base}/login"`. Collecting only ast.Constant would
            # miss every auth call and flag read-only tools as mutating.
            strings: list[str] = []
            for expr in list(sub.args) + [k.value for k in sub.keywords]:
                if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                    strings.append(expr.value)
                elif isinstance(expr, ast.JoinedStr):
                    strings.extend(
                        v.value
                        for v in expr.values
                        if isinstance(v, ast.Constant) and isinstance(v.value, str)
                    )
            touches_login = any("login" in v.lower() for v in strings)

            # requests.post(...) / httpx.put(...) and friends
            if isinstance(sub.func, ast.Attribute) and sub.func.attr in write_verbs:
                if not touches_login:
                    mutates = True

            # _api_request("POST", ...)
            if isinstance(sub.func, ast.Name) and sub.func.id == "_api_request":
                verb = (
                    sub.args[0].value
                    if sub.args and isinstance(sub.args[0], ast.Constant)
                    else None
                )
                if isinstance(verb, str) and verb.upper() in {"POST", "PUT", "DELETE", "PATCH"}:
                    if not touches_login:
                        mutates = True

        if mutates:
            result[node.name] = guarded

    return result


class TestEveryMutatingToolIsGuarded:
    """Structural: no tool may write to the API without passing the local guard.

    Per-tool behaviour lives in test_mcp_write_tools.py. What is pinned here is
    the invariant across the whole module, found by inspecting the AST rather
    than by trusting the ``pm_`` naming convention.
    """

    # Tools that mutate WITHOUT the guard. This is a list of known product gaps,
    # not an approved pattern, and it is currently empty.
    #
    # It previously held upload_experiment, which POSTed via `requests` directly
    # -- bypassing _api_request and _write -- and defaulted to dry_run=False, so
    # it applied by default and could write to a remote API. Both are fixed; see
    # TestUploadExperimentIsGuarded below.
    KNOWN_UNGUARDED: set[str] = set()

    def test_the_expected_pm_write_tools_all_exist(self):
        expected = {
            "pm_create_experiment",
            "pm_add_bot",
            "pm_create_run",
            "pm_add_job",
            "pm_set_behaviour",
            "pm_activate_run",
            "pm_delete",
            "pm_compose_run",
        }
        missing = sorted(n for n in expected if not callable(getattr(s, n, None)))
        assert missing == [], f"write tools missing from the module: {missing}"

    def test_at_least_the_known_pm_tools_are_detected_as_mutating(self):
        """Guards the detector itself -- a broken detector would pass everything."""
        detected = set(_mutating_tools())
        assert {
            "pm_create_experiment",
            "pm_delete",
            "pm_add_job",
        } <= detected, (
            f"the AST scan stopped recognising known write tools; it detected {sorted(detected)}"
        )

    def test_no_new_unguarded_mutating_tool_has_appeared(self):
        unguarded = {name for name, guarded in _mutating_tools().items() if not guarded}
        assert unguarded == self.KNOWN_UNGUARDED, (
            "the set of unguarded mutating tools changed.\n"
            f"  now unguarded: {sorted(unguarded)}\n"
            f"  known gaps:    {sorted(self.KNOWN_UNGUARDED)}\n"
            "A new tool that writes to the API must go through _write() (which "
            "enforces dry-run-by-default and the local-only guard). If a known "
            "gap was fixed, remove it from KNOWN_UNGUARDED."
        )


class TestUploadExperimentIsGuarded:
    """upload_experiment writes without going through _write(), so it is pinned here.

    It is the one mutating tool that talks to the API through ``requests``
    directly rather than ``_api_request``. That means it gets neither of
    ``_write()``'s guarantees for free, and it previously had neither: it
    defaulted to ``dry_run=False`` and never consulted the local-only guard, so
    a deployment pointed at a remote API could upload to it by default.

    The structural scan above proves it now calls the guard. These tests prove
    the guard actually bites and that the default is a preview.
    """

    MINIMAL_SPEC = """
version: "1.0"
experiment:
  name: "Guard test"
crawlers:
  - name: "bot_01"
    session:
      workflow:
        - visit: https://example.com
"""

    def test_defaults_to_a_preview_and_uploads_nothing(self, monkeypatch):
        """Called with no dry_run argument at all, it must not upload."""
        _point_api_at(monkeypatch, "http://localhost:8090/api/v1")

        import requests

        def _explode(*args, **kwargs):
            raise AssertionError("upload_experiment performed a request during a default call")

        monkeypatch.setattr(requests, "post", _explode)

        out = json.loads(s.upload_experiment(self.MINIMAL_SPEC))

        assert out.get("dry_run") is True
        assert "to_apply" in out

    def test_remote_target_is_refused_when_applying(self, monkeypatch):
        _point_api_at(monkeypatch, "https://pm-api.example.org/api/v1")

        import requests

        def _explode(*args, **kwargs):
            raise AssertionError("upload_experiment reached the network for a remote target")

        monkeypatch.setattr(requests, "post", _explode)

        out = json.loads(s.upload_experiment(self.MINIMAL_SPEC, dry_run=False))

        assert out["success"] is False
        assert "refusing to mutate" in out["error"]

    def test_preview_still_works_against_a_remote_target(self, monkeypatch):
        """Validation is pure local work, so the guard must not block it.

        Refusing a dry run against a remote target would make the tool useless
        for checking a spec on any non-dev deployment, for no safety gain --
        nothing is written either way.
        """
        _point_api_at(monkeypatch, "https://pm-api.example.org/api/v1")

        out = json.loads(s.upload_experiment(self.MINIMAL_SPEC, dry_run=True))

        assert out["success"] is True
        assert out["dry_run"] is True

    def test_invalid_spec_is_rejected_before_any_guard_or_upload(self, monkeypatch):
        _point_api_at(monkeypatch, "http://localhost:8090/api/v1")

        out = json.loads(s.upload_experiment("this: is: not: a: valid: spec", dry_run=False))

        assert out["success"] is False
        assert out["errors"]
