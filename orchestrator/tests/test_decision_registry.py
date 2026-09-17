"""One local server, N project decision functions: registry, dispatch, /health.

The runner has always named the function it wants (``lambda_select``'s
``function`` field, which AWS mode passes straight to
``LambdaCaller.invokeAws``). Locally that name was dropped and one directory
served, so a box could host exactly one study. These tests pin the three things
that makes safe:

* the ``X-Function-Name`` header reaches the function it names, and a request
  without one still lands on the default -- an un-rebuilt runner must keep
  working, because the deploy order puts this repo before the jar;
* a name nobody registered is answered with **200 and a degraded verdict**, not
  a 5xx: the runner records a decision that did not apply rather than a
  transport failure it retries;
* two packages both called ``lambda_function``, both with a same-named helper
  next to them, cannot shadow each other. That failure would not raise. It
  would return a plausible recommendation computed by the *wrong study's* code,
  which is the same silent-wrong-answer class as the cwd bug that turned the
  ``pi=1`` arm into a second control arm for months.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest

from orchestrator.lambda_service import registry as reg
from orchestrator.lambda_service import server

# ---------------------------------------------------------------------------
# Fixtures: two fake decision packages and a registry pointing at them
# ---------------------------------------------------------------------------

#: Every package reads a data file the way the contract requires -- relative to
#: its own file, never to the cwd -- so a package that only works under a chdir
#: would fail these tests at import time.
_LAMBDA_FUNCTION = """
import json
from pathlib import Path

from helper import whoami

DATA = (Path(__file__).resolve().parent / "data.txt").read_text().strip()


def select_recommendation(payload):
    return {{
        "xpath": "//a[1]",
        "function": "{name}",
        "helper": whoami(),
        "data": DATA,
        "payload": json.loads(payload),
    }}
{hook}
"""

_HOOK = """

def health():
    return {"ok": True, "checked": "data.txt"}
"""

_SICK_HOOK = """

def health():
    return {"ok": False, "problems": ["data.txt is corrupted"]}
"""


def _package(root: Path, name: str, hook: str = "") -> Path:
    """A minimal decision package: entry module, same-named helper, data file."""
    path = root / name
    path.mkdir(parents=True)
    (path / "helper.py").write_text(f'def whoami():\n    return "{name}-helper"\n')
    (path / "data.txt").write_text(f"{name}-data\n")
    (path / "lambda_function.py").write_text(_LAMBDA_FUNCTION.format(name=name, hook=hook))
    return path


def _registry_file(root: Path, functions: dict[str, Path], default: str | None = None) -> Path:
    path = root / "decision-functions.toml"
    lines = []
    if default is not None:
        lines.append(f'default = "{default}"\n')
    for name, target in functions.items():
        # Quoted: a name may contain a dot, which is a table separator bare.
        lines.append(f'\n[functions."{name}"]\npath = "{target}"\n')
    path.write_text("".join(lines))
    return path


@pytest.fixture(autouse=True)
def clean_loaded_packages():
    """Never leak a fake package's modules into another test."""
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith(reg.MODULE_PREFIX):
            del sys.modules[name]


@pytest.fixture
def client():
    server.app.config["TESTING"] = False
    return server.app.test_client()


@pytest.fixture
def two_functions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Two registered packages, ``alpha`` (the default) and ``beta``."""
    alpha = _package(tmp_path, "alpha", hook=_HOOK)
    beta = _package(tmp_path, "beta")
    path = _registry_file(tmp_path, {"alpha": alpha, "beta": beta}, default="alpha")
    monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))
    monkeypatch.setattr(server, "_registry", None)
    return {"alpha": alpha, "beta": beta, "registry": path}


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["alpha", "beta"])
def test_the_header_dispatches_to_the_function_it_names(client, two_functions, name: str):
    response = client.post("/invoke", json={"job_id": "j-1"}, headers={"X-Function-Name": name})

    assert response.status_code == 200
    body = response.get_json()
    assert body["function"] == name
    assert body["data"] == f"{name}-data"


def test_no_header_uses_the_registry_default(client, two_functions):
    """The transition case: a runner built before the header existed."""
    response = client.post("/invoke", json={"job_id": "j-1"})

    assert response.status_code == 200
    assert response.get_json()["function"] == "alpha"


def test_an_empty_header_is_treated_as_absent(client, two_functions):
    """A header the runner failed to fill in must land on the default.

    Reporting it as the unknown function ``''`` would replace one diagnosis
    (the caller sent nothing) with a wrong one (the caller asked for a function
    nobody registered).
    """
    response = client.post("/invoke", json={}, headers={"X-Function-Name": "   "})

    assert response.get_json()["function"] == "alpha"


def test_an_unknown_name_is_a_degraded_200_naming_what_is_registered(
    client, two_functions, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)

    response = client.post(
        "/invoke",
        json={"job_id": "j-7"},
        headers={"X-Function-Name": "bot-decision-nope", "X-Correlation-Id": "exp-1|j-7"},
    )

    # 200, because the runner turns a non-2xx into a retried transport failure
    # rather than a recorded verdict.
    assert response.status_code == 200
    body = response.get_json()
    assert body["degraded"] is True
    assert body["treatment_lost"] is True
    assert "bot-decision-nope" in body["error"]
    assert "alpha" in body["error"] and "beta" in body["error"]
    assert body["xpath"] == ""


def test_an_unknown_name_logs_exactly_one_warning_with_the_correlation_id(
    client, two_functions, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)

    client.post(
        "/invoke",
        json={},
        headers={"X-Function-Name": "nope", "X-Correlation-Id": "exp-1|cr-2|q-3|j-4"},
    )

    warnings = [m for m in _warnings(caplog) if "unknown decision function" in m]
    assert len(warnings) == 1, _warnings(caplog)
    assert "exp-1|cr-2|q-3|j-4" in warnings[0]


def test_the_payload_reaches_the_function_unchanged(client, two_functions):
    response = client.post(
        "/invoke", json={"job_id": "j-1", "pi": 1}, headers={"X-Function-Name": "beta"}
    )

    assert response.get_json()["payload"] == {"job_id": "j-1", "pi": 1}


# ---------------------------------------------------------------------------
# Module isolation
# ---------------------------------------------------------------------------


def test_two_packages_with_the_same_module_names_do_not_shadow_each_other(client, two_functions):
    """The one failure that would not raise: the wrong study's code answering."""
    first = client.post("/invoke", json={}, headers={"X-Function-Name": "alpha"}).get_json()
    second = client.post("/invoke", json={}, headers={"X-Function-Name": "beta"}).get_json()

    assert (first["helper"], first["data"]) == ("alpha-helper", "alpha-data")
    assert (second["helper"], second["data"]) == ("beta-helper", "beta-data")

    # And the other direction: alpha still answers as alpha after beta loaded.
    again = client.post("/invoke", json={}, headers={"X-Function-Name": "alpha"}).get_json()
    assert again["helper"] == "alpha-helper"


def test_the_bare_module_names_are_freed_after_an_import(client, two_functions):
    """``lambda_function`` and ``helper`` must not be left in sys.modules.

    Leaving them is what lets the *second* package pick up the first's modules;
    the unique ``pm_decision_*`` keys are where they actually live.
    """
    client.post("/invoke", json={}, headers={"X-Function-Name": "alpha"})

    assert "helper" not in sys.modules
    assert "lambda_function" not in sys.modules
    assert "pm_decision_alpha" in sys.modules
    assert "pm_decision_alpha.helper" in sys.modules


def test_the_package_directory_does_not_stay_on_sys_path(client, two_functions):
    before = list(sys.path)

    client.post("/invoke", json={}, headers={"X-Function-Name": "alpha"})

    assert sys.path == before
    assert str(two_functions["alpha"]) not in sys.path


def test_the_server_does_not_chdir_when_a_registry_names_the_functions(client, two_functions):
    """N functions cannot share one cwd, so data reads are file-relative."""
    cwd = os.getcwd()

    body = client.post("/invoke", json={}, headers={"X-Function-Name": "beta"}).get_json()

    assert os.getcwd() == cwd
    assert body["data"] == "beta-data"  # read relative to __file__, with no chdir


# ---------------------------------------------------------------------------
# The no-registry fallback
# ---------------------------------------------------------------------------


@pytest.fixture
def fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No registry file, and a tmp symlink standing in for the real one."""
    target = _package(tmp_path, "shipped", hook=_HOOK)
    link = tmp_path / "decision"
    link.symlink_to(target)
    monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(tmp_path / "absent.toml"))
    monkeypatch.setattr(server, "DECISION_DIR", link)
    monkeypatch.setattr(server, "_registry", None)
    monkeypatch.setattr(server, "_cwd_announced", False)
    return link


def test_without_a_registry_the_symlink_answers_as_bot_decision_yt(client, fallback):
    unnamed = client.post("/invoke", json={}).get_json()
    named = client.post(
        "/invoke", json={}, headers={"X-Function-Name": "bot-decision-yt"}
    ).get_json()

    assert unnamed["function"] == "shipped"
    assert named["function"] == "shipped"


def test_the_fallback_still_chdirs_for_an_un_updated_package(client, fallback):
    """Transitional: a bot-decision-yt checkout predating file-relative reads.

    It is the one place a chdir survives, and it is why the deploy order can put
    this repo before the decision function's own change.
    """
    client.post("/invoke", json={})

    assert Path(os.getcwd()) == fallback.resolve()


def test_health_says_the_registry_is_absent_in_the_fallback(client, fallback):
    body = client.get("/health").get_json()

    assert body["registry"] == "absent"
    assert body["default"] == "bot-decision-yt"
    assert list(body["functions"]) == ["bot-decision-yt"]


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_reports_every_function_and_its_hook(client, two_functions):
    body = client.get("/health").get_json()

    assert body["status"] == "healthy"
    assert body["registry"] == str(two_functions["registry"])
    assert body["default"] == "alpha"
    assert sorted(body["functions"]) == ["alpha", "beta"]

    alpha = body["functions"]["alpha"]
    assert alpha["path"] == str(two_functions["alpha"])
    assert alpha["loaded"] is True
    assert alpha["error"] is None
    # The hook is the project's own: this server no longer knows what a
    # mainstream-media CSV is.
    assert alpha["health"] == {"ok": True, "checked": "data.txt"}

    # A package without a hook reports nothing rather than a fabricated verdict.
    assert body["functions"]["beta"]["health"] is None
    assert body["functions"]["beta"]["loaded"] is True

    # The fd rule stayed.
    assert body["open_files"]["near_limit"] is False


def test_health_is_degraded_when_a_package_cannot_be_imported(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    good = _package(tmp_path, "good")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "lambda_function.py").write_text("import nonexistent_module_xyz\n")
    path = _registry_file(tmp_path, {"good": good, "broken": broken}, default="good")
    monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))
    monkeypatch.setattr(server, "_registry", None)

    response = client.get("/health")
    body = response.get_json()

    assert response.status_code == 200  # liveness: the code stays 200
    assert body["status"] == "degraded"
    assert body["functions"]["broken"]["loaded"] is False
    assert "nonexistent_module_xyz" in body["functions"]["broken"]["error"]
    assert body["functions"]["good"]["loaded"] is True


def test_health_is_degraded_when_a_registered_path_is_missing(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = _registry_file(tmp_path, {"gone": tmp_path / "not-here"}, default="gone")
    monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))
    monkeypatch.setattr(server, "_registry", None)

    body = client.get("/health").get_json()

    assert body["status"] == "degraded"
    assert "path not found" in body["functions"]["gone"]["error"]


def test_health_is_degraded_when_a_hook_reports_a_problem(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The whole point of the hook: the import succeeded and the arm is still dead."""
    sick = _package(tmp_path, "sick", hook=_SICK_HOOK)
    path = _registry_file(tmp_path, {"sick": sick}, default="sick")
    monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))
    monkeypatch.setattr(server, "_registry", None)

    body = client.get("/health").get_json()

    assert body["functions"]["sick"]["loaded"] is True
    assert body["functions"]["sick"]["health"]["problems"] == ["data.txt is corrupted"]
    assert body["status"] == "degraded"


def test_a_raising_hook_is_reported_not_propagated(tmp_path: Path):
    """/health must answer even when a package's own check is broken."""
    path = _package(tmp_path, "boom")
    (path / "lambda_function.py").write_text(
        "def select_recommendation(p):\n    return {}\n\n\ndef health():\n"
        "    raise RuntimeError('hook exploded')\n"
    )
    function = reg.DecisionFunction(name="boom", path=path)
    function.load()

    result = function.health()

    assert result is not None
    assert result["ok"] is False
    assert "hook exploded" in result["problems"][0]


# ---------------------------------------------------------------------------
# A registry this server cannot read
# ---------------------------------------------------------------------------


def test_a_malformed_registry_never_falls_back_silently(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Serving some other study's function would be invisible in every indicator."""
    path = tmp_path / "decision-functions.toml"
    path.write_text("this is not = valid = toml\n")
    monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))
    monkeypatch.setattr(server, "_registry", None)

    health = client.get("/health").get_json()
    assert health["status"] == "degraded"
    assert health["registry_problems"]

    body = client.post("/invoke", json={}).get_json()
    assert body["degraded"] is True
    assert body["treatment_lost"] is True


def test_two_functions_and_no_default_is_reported_rather_than_guessed(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Picking one at random would be a coin toss over which study ran."""
    path = _registry_file(
        tmp_path,
        {"alpha": _package(tmp_path, "alpha"), "beta": _package(tmp_path, "beta")},
        default=None,
    )
    monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))
    monkeypatch.setattr(server, "_registry", None)

    # A named request still works...
    named = client.post("/invoke", json={}, headers={"X-Function-Name": "beta"}).get_json()
    assert named["function"] == "beta"

    # ...while a headerless one says what is wrong.
    body = client.post("/invoke", json={}).get_json()
    assert body["degraded"] is True
    assert "no default decision function" in body["error"]


def test_a_single_function_needs_no_explicit_default(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = _registry_file(tmp_path, {"only": _package(tmp_path, "only")}, default=None)
    monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))
    monkeypatch.setattr(server, "_registry", None)

    assert client.post("/invoke", json={}).get_json()["function"] == "only"


def test_a_default_naming_an_unregistered_function_is_a_problem(tmp_path: Path):
    path = tmp_path / "decision-functions.toml"
    path.write_text(
        f'default = "ghost"\n\n[functions.real]\npath = "{_package(tmp_path, "real")}"\n'
    )

    registry = reg.load_registry(path, tmp_path / "decision")

    assert registry.default is None
    assert any("ghost" in problem for problem in registry.problems)


# ---------------------------------------------------------------------------
# Where the registry is read from
# ---------------------------------------------------------------------------


class TestRegistryPath:
    def test_the_env_var_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(tmp_path / "custom.toml"))
        from orchestrator.config.settings import Settings

        assert reg.registry_path(Settings()) == tmp_path / "custom.toml"

    def test_unset_means_the_documented_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("DECISION_FUNCTIONS_FILE", raising=False)
        from orchestrator.config.settings import Settings

        assert reg.registry_path(Settings()) == reg.DEFAULT_REGISTRY_FILE

    def test_an_empty_value_reads_as_unset_not_as_the_cwd(self, monkeypatch: pytest.MonkeyPatch):
        """``DECISION_FUNCTIONS_FILE=`` in .env parses as ``Path('.')``."""
        monkeypatch.setenv("DECISION_FUNCTIONS_FILE", "")
        from orchestrator.config.settings import Settings

        assert reg.registry_path(Settings()) == reg.DEFAULT_REGISTRY_FILE

    def test_a_tilde_is_expanded(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("DECISION_FUNCTIONS_FILE", "~/registry.toml")
        from orchestrator.config.settings import Settings

        assert reg.registry_path(Settings()) == Path.home() / "registry.toml"

    def test_the_settings_helper_agrees(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(tmp_path / "r.toml"))
        from orchestrator.config.settings import Settings

        assert Settings().get_decision_functions_file() == tmp_path / "r.toml"


# ---------------------------------------------------------------------------
# LambdaServerComponent.validate()
# ---------------------------------------------------------------------------


class TestComponentValidation:
    def _component(self):
        from orchestrator.components.lambda_server import LambdaServerComponent

        return LambdaServerComponent()

    def test_a_registry_entry_pointing_nowhere_is_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        path = _registry_file(tmp_path, {"gone": tmp_path / "not-here"}, default="gone")
        monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))

        ok, errors = self._component().validate()

        assert not ok
        assert any("gone" in e and "path not found" in e for e in errors)

    def test_a_directory_without_the_entry_module_is_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        empty = tmp_path / "empty"
        empty.mkdir()
        path = _registry_file(tmp_path, {"hollow": empty}, default="hollow")
        monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))

        _, errors = self._component().validate()

        assert any("lambda_function.py not found" in e for e in errors)

    def test_a_healthy_registry_raises_nothing_about_decisions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        path = _registry_file(tmp_path, {"alpha": _package(tmp_path, "alpha")}, default="alpha")
        monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))

        _, errors = self._component().validate()

        assert [e for e in errors if "decision" in e.lower()] == []

    def test_with_a_registry_the_symlink_is_no_longer_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A box that hosts its studies from the registry need not have one."""
        path = _registry_file(tmp_path, {"alpha": _package(tmp_path, "alpha")}, default="alpha")
        monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))
        component = self._component()
        component.orchestrator_path = tmp_path / "nowhere"

        _, errors = component.validate()

        assert [e for e in errors if "symlink" in e] == []


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def test_startup_checks_report_each_function(
    two_functions, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    caplog.set_level(logging.DEBUG)

    summary = server.run_startup_checks()

    assert summary["registry"] == str(two_functions["registry"])
    assert sorted(summary["functions"]) == ["alpha", "beta"]
    messages = [r.getMessage() for r in caplog.records]
    assert any("decision function 'alpha' ready" in m for m in messages), messages


def test_startup_checks_name_a_function_that_will_not_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "lambda_function.py").write_text("raise RuntimeError('bad package')\n")
    path = _registry_file(tmp_path, {"broken": broken}, default="broken")
    monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))
    monkeypatch.setattr(server, "_registry", None)

    server.run_startup_checks()

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("NOT importable" in m and "broken" in m for m in errors), errors


def test_an_unknown_function_is_a_lookup_error_not_an_import_error(two_functions):
    """The two failures get different HTTP answers, so they must stay distinct."""
    with pytest.raises(reg.UnknownFunctionError):
        server.get_decision_function("nobody")


def test_json_is_still_what_the_function_receives(two_functions):
    """``select_recommendation`` takes a JSON *string*, as AWS Lambda hands it."""
    function = server.get_decision_function("alpha")

    result = function(json.dumps({"job_id": "j-2"}))

    assert result["payload"] == {"job_id": "j-2"}


# ---------------------------------------------------------------------------
# Concurrent first imports
# ---------------------------------------------------------------------------
#
# `sys.path` and `sys.modules` are process globals, so a per-function lock is
# not enough: two functions importing for the first time in parallel -- the
# normal state of a restarted server under 15 bots -- interleave their
# insert/import/re-key windows, and the loser binds the winner's `helper`. It
# does not raise. One study's code then answers under another study's name for
# the life of the process, with /health reporting both as loaded.

#: A package that takes long enough to import for the interleaving to be
#: reproducible rather than theoretical.
_SLOW_LAMBDA_FUNCTION = """
import time

from helper import whoami

time.sleep(0.02)


def select_recommendation(payload):
    return {"function": "%s", "helper": whoami()}
"""


def _slow_package(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "helper.py").write_text(f'def whoami():\n    return "{name}-helper"\n')
    (path / "lambda_function.py").write_text(_SLOW_LAMBDA_FUNCTION % name)
    return path


def _race_one_round(registry: reg.Registry, names: list[str]) -> dict[str, object]:
    """Load every named function from its own thread, all released together."""
    import threading

    barrier = threading.Barrier(len(names))
    answers: dict[str, object] = {}
    errors: list[BaseException] = []

    def _first_request(name: str) -> None:
        try:
            function = registry.get(name)
            barrier.wait(timeout=5)
            answers[name] = function.load()("{}")
        except BaseException as exc:  # noqa: BLE001 - re-raised by the caller
            errors.append(exc)

    threads = [threading.Thread(target=_first_request, args=(name,)) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, errors
    return answers


def test_two_first_imports_in_parallel_do_not_cross_contaminate(tmp_path: Path):
    """Run twenty times: a race that fails one round in five is still a race."""
    names = ["alpha", "beta"]
    paths = {name: _slow_package(tmp_path, name) for name in names}
    registry_file = _registry_file(tmp_path, paths, default="alpha")

    for round_number in range(20):
        registry = reg.load_registry(registry_file, tmp_path / "decision")
        answers = _race_one_round(registry, names)

        for name in names:
            assert answers[name] == {
                "function": name,
                "helper": f"{name}-helper",
            }, f"round {round_number}: {name} answered {answers[name]}"


# ---------------------------------------------------------------------------
# A registered function whose package will not import
# ---------------------------------------------------------------------------


@pytest.fixture
def broken_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One good function and one registered package that raises on import."""
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "lambda_function.py").write_text("import nonexistent_module_xyz\n")
    path = _registry_file(
        tmp_path, {"good": _package(tmp_path, "good"), "broken": broken}, default="good"
    )
    monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))
    monkeypatch.setattr(server, "_registry", None)
    return broken


def test_a_package_that_will_not_import_is_a_degraded_200_not_a_500(client, broken_package):
    """For this job the treatment did not happen -- which is a verdict, not a 500.

    A 5xx is a transport failure to the runner: it retries and records a failed
    job, and the verdict the analysis joins on never gets written. A broken copy
    of a study's function is the same fact as a name nobody registered, seen
    from the other side.
    """
    response = client.post("/invoke", json={}, headers={"X-Function-Name": "broken"})

    assert response.status_code == 200
    body = response.get_json()
    assert body["degraded"] is True
    assert body["treatment_lost"] is True
    assert "decision function 'broken' failed to import" in body["error"]
    assert "nonexistent_module_xyz" in body["error"]
    assert body["xpath"] == ""


def test_the_import_failure_verdict_reads_as_lost_not_unknown(client, broken_package):
    """`status.lambda_verdict` is what every consumer reads; it must not say unknown."""
    from orchestrator.status import lambda_verdict

    body = client.post("/invoke", json={}, headers={"X-Function-Name": "broken"}).get_json()
    verdict = lambda_verdict(body)

    assert verdict.reported is True
    assert verdict.degraded is True
    assert verdict.treatment_lost is True


def test_the_import_failure_logs_one_warning_with_the_correlation_id(
    client, broken_package, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)

    client.post(
        "/invoke",
        json={},
        headers={"X-Function-Name": "broken", "X-Correlation-Id": "exp-2|j-8"},
    )

    warnings = [m for m in _warnings(caplog) if "decision not served" in m]
    assert len(warnings) == 1, _warnings(caplog)
    assert "exp-2|j-8" in warnings[0]


def test_a_registered_path_that_is_missing_is_the_same_degraded_answer(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = _registry_file(tmp_path, {"gone": tmp_path / "not-here"}, default="gone")
    monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))
    monkeypatch.setattr(server, "_registry", None)

    body = client.post("/invoke", json={}).get_json()

    assert body["treatment_lost"] is True
    assert "failed to import" in body["error"]
    assert "path not found" in body["error"]


def test_an_import_error_raised_by_the_decision_itself_is_still_a_500(
    client, two_functions, monkeypatch: pytest.MonkeyPatch
):
    """The distinction that keeps the verdict honest: loaded fine, then blew up."""

    def _lazy_import_boom(payload: str):
        raise ImportError("no module named 'openai'")

    monkeypatch.setattr(server, "get_decision_function", lambda name=None: _lazy_import_boom)

    response = client.post("/invoke", json={})

    assert response.status_code == 500
    assert "ImportError" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# The health() hook runs under a deadline
# ---------------------------------------------------------------------------


def test_a_hook_that_blocks_does_not_hang_health(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A hook is somebody else's code on the health path.

    One that reaches for the network would hang /health, and a health check that
    hangs reads as *down* to everything watching it.
    """
    monkeypatch.setattr(reg, "HOOK_TIMEOUT_SECONDS", 0.05)
    slow = _package(tmp_path, "slow")
    (slow / "lambda_function.py").write_text(
        "import time\n\n\ndef select_recommendation(p):\n    return {}\n\n\n"
        "def health():\n    time.sleep(5)\n    return {'ok': True}\n"
    )
    path = _registry_file(tmp_path, {"slow": slow}, default="slow")
    monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))
    monkeypatch.setattr(server, "_registry", None)

    body = client.get("/health").get_json()

    hook = body["functions"]["slow"]["health"]
    assert hook["ok"] is False
    assert "did not return within" in hook["problems"][0]
    # A check that did not finish is not a check that passed.
    assert body["status"] == "degraded"


def test_the_hook_deadline_is_two_seconds():
    """Pinned: the value is part of the contract a hook author is held to."""
    assert reg.HOOK_TIMEOUT_SECONDS == 2.0


# ---------------------------------------------------------------------------
# Registry paths and names
# ---------------------------------------------------------------------------


def test_a_relative_path_resolves_against_the_registry_file(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Not against the process cwd, which belongs to whoever launched the server."""
    _package(tmp_path, "near")
    path = tmp_path / "decision-functions.toml"
    path.write_text('default = "near"\n\n[functions.near]\npath = "near"\n')
    monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))
    monkeypatch.setattr(server, "_registry", None)
    os.chdir(tmp_path.parent)  # a cwd that knows nothing about the package

    assert client.post("/invoke", json={}).get_json()["function"] == "near"


def test_two_names_that_slug_to_one_module_key_are_refused(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``bot.yt`` and ``bot-yt`` would share one sys.modules key.

    That is the same shadowing the unique names prevent, arrived at through the
    registry instead of through Python -- so neither is registered, and the
    registry says why rather than serving a coin toss.
    """
    path = _registry_file(
        tmp_path,
        {"bot.yt": _package(tmp_path, "one"), "bot-yt": _package(tmp_path, "two")},
        default="bot-yt",
    )
    monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(path))
    monkeypatch.setattr(server, "_registry", None)

    body = client.get("/health").get_json()

    assert body["functions"] == {}
    assert any("both import as module" in p for p in body["registry_problems"])
    assert body["status"] == "degraded"

    served = client.post("/invoke", json={}, headers={"X-Function-Name": "bot-yt"}).get_json()
    assert served["treatment_lost"] is True


def test_the_log_names_the_function_that_ran_not_the_slot(
    client, two_functions, caplog: pytest.LogCaptureFixture
):
    """``<default>`` says which slot was used, not which study ran."""
    caplog.set_level(logging.DEBUG)

    client.post("/invoke", json={})

    lines = [
        r.getMessage() for r in caplog.records if "video selection requested" in r.getMessage()
    ]
    assert lines and "function=alpha" in lines[0], lines
    assert "<default>" not in " ".join(lines)


def test_a_package_that_calls_sys_exit_at_import_is_a_verdict_not_a_500(tmp_path, monkeypatch):
    """SystemExit is a BaseException: `except Exception` let a module-level
    sys.exit() escape every layer and turn into a Flask 500 on /health and
    /invoke -- the one path that could take the liveness probe down."""
    pkg = tmp_path / "quitter"
    pkg.mkdir()
    (pkg / "lambda_function.py").write_text("import sys\nsys.exit(3)\n")
    reg = tmp_path / "decision-functions.toml"
    reg.write_text(f'default = "quitter"\n\n[functions.quitter]\npath = "{pkg}"\n')
    monkeypatch.setenv("DECISION_FUNCTIONS_FILE", str(reg))
    from orchestrator.lambda_service import server

    server._registry = None
    client = server.app.test_client()

    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json()["status"] == "degraded"
    assert "SystemExit" in (health.get_json()["functions"]["quitter"]["error"] or "")

    reply = client.post(
        "/invoke", json={"pagesource": "<html></html>"}, headers={"X-Function-Name": "quitter"}
    )
    assert reply.status_code == 200
    assert reply.get_json()["treatment_lost"] is True
