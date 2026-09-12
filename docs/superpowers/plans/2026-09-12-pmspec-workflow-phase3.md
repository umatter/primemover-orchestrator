# pmspec workflow (phase 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The day-to-day commands on top of a trustworthy lock: `pmspec status` (specification + execution on one screen), `pmspec diff` (two locks), `pmspec cleanup` (test experiments only), `preview`/`summary` reading locks, and the two MCP `upload_experiment` gaps (`allow_dirty`/`allow_past`; dry-run consults the secrets store).

**Architecture:** Three small modules in `experiment_designer/lock/` — `status.py`, `diff.py`, `cleanup.py` — each a pure function over a session-shaped object (`get`/`get_bytes`/`delete`) returning a dataclass with `render()`, so the CLI and the MCP are thin and share one implementation, exactly like `verify`. `summary`/`preview` gain a lock path via `SummaryGenerator.from_lock`. Nothing new touches the API.

**Tech Stack:** Python 3.10+, existing `requests`/`httpx` sessions, pytest with in-memory fake sessions.

**Spec:** `docs/superpowers/specs/2026-09-11-pmspec-lock-and-provenance-design.md` §5 and the §6 round-trip test; §2's "`preview` and `summary` accept both". Deviations decided in planning: (1) `pmspec status` takes an optional lock argument — binding is only meaningful against a specific lock, so without one "bound" means "the server carries a `pmspec.lock_sha256`"; (2) `cleanup` finds candidates by the `test-` name prefix first (the experiments list carries no preferences) and then confirms `pmspec.mode=test` per experiment via `GET experiments/{id}` — the preference remains the only thing that authorises deletion.

## Global Constraints

- Line length 100; `make check` passes after every task (baseline 1469 passed, 33 skipped); Python 3.10.
- Every MCP write tool: dry-run by default; `_require_local_write_target()` before any mutating request; the AST scan in `orchestrator/tests/test_mcp_write_guard.py` must recognise the new deleting tool (its `_api_request("DELETE", …)` calls are already a mutation signal there).
- `cleanup` never deletes an experiment without `pmspec.mode == "test"` in its preferences, whatever its name.
- No output ever contains a resolved secret; all reads go through the session objects (`ApiSession` for CLIs, `_McpSession` for the MCP).
- Commit after every task with the session's attribution trailer.

## File structure

| path | responsibility |
|---|---|
| `experiment_designer/lock/status.py` (new) | `ExperimentStatus`, `experiment_status(session, experiment_id, hours, lock_text=None)`, `render()` |
| `experiment_designer/lock/diff.py` (new) | `LockDiff`, `diff_locks(text_a, text_b)`, `render()` |
| `experiment_designer/lock/cleanup.py` (new) | `CleanupCandidate`, `find_test_experiments(session, older_than=None)`, `delete_experiments(session, ids)` |
| `experiment_designer/lock/api.py` (modify) | `ApiSession.delete(path)` |
| `experiment_designer/summary/generator.py` (modify) | `from_lock(lock) -> ExperimentSummary`, `from_lock_text(text)` |
| `experiment_designer/cli.py` (modify) | `status`, `diff`, `cleanup`; `preview`/`summary` accept locks |
| `orchestrator/mcp_server/server.py` (modify) | `experiment_status`, `diff_locks`, `pm_cleanup_test_experiments`; `_McpSession.delete`; `upload_experiment(allow_dirty=, allow_past=)` + dry-run secrets check |
| `docs/experiment-designer.md`, `CLAUDE.md`, `.claude/skills/primemover-compose/SKILL.md`, `.claude/skills/primemover-debug/SKILL.md` (modify) | the commands |
| tests: `experiment_designer/tests/test_lock_status.py`, `test_lock_diff.py`, `test_lock_cleanup.py`, `test_lock_summary_roundtrip.py`, `test_lock_cli_workflow.py`; `orchestrator/tests/test_mcp_workflow_tools.py` | |

---

### Task 1: `pmspec status`

**Files:**
- Create: `experiment_designer/lock/status.py`
- Modify: `experiment_designer/cli.py` (new command), `orchestrator/mcp_server/server.py` (new read tool `experiment_status`)
- Test: `experiment_designer/tests/test_lock_status.py`, `orchestrator/tests/test_mcp_workflow_tools.py` (start it)

**Interfaces:**
- Consumes: `experiment_designer.lock.verify.verify_lock` (when a lock is given), session `.get(path, params)`.
- Produces:
  ```python
  @dataclass class ProvenanceUpload: label: str; values: dict[str, str]        # one per "upload N" label, values keyed by pmspec.* name
  @dataclass class ExperimentStatus:
      experiment_id: int; name: str; runner_user_id: int | None; uploads: list[ProvenanceUpload]
      health: dict[str, Any]           # the /health body verbatim
      verify: VerifyResult | None      # when lock_text given
      @property bound_to_any_lock -> bool      # any pmspec.lock_sha256 present
      @property ok -> bool             # (verify is None or verify.ok) and health.get("treatment_lost_jobs", 0) == 0
      def render(self) -> str
  def experiment_status(session, experiment_id: int, *, hours: int = 24, lock_text: str | None = None, deep: bool = False) -> ExperimentStatus
  ```
  Reads: `GET experiments/{id}` (name, `runner_user_id`, `preferences`), `GET experiments/{id}/health?hours=N`. Groups `pmspec.*` preferences by their `description` (the upload label); an experiment with none → `uploads == []` and `render()` says `no pmspec provenance: not uploaded by pmspec`. `render()` lines: header (`experiment 181  "Study"  runner=3`), one block per upload (label, mode, lock sha (12), commit, generator, uploaded_at/by, anchor), then `health (last 24h): runs … jobs … bad_runs=… bad_jobs=… treatment_lost_jobs=… degraded_jobs=…` (keys absent from the body are printed as `unknown`, never `0`), then the verify block if any.
  CLI: `pmspec status EXPERIMENT_ID [LOCK] [--hours 24] [--deep]` → prints `render()`, exit 0 iff `ok`. MCP: `experiment_status(experiment_id: int, hours: int = 24, lock_content: str | None = None, deep: bool = False)` returns the dataclass as JSON (verify as in `verify_experiment`) plus `render`.

- [ ] **Step 1: Write the failing tests**

```python
# experiment_designer/tests/test_lock_status.py
"""status: what was uploaded (provenance), and how it is running (health), on one screen."""

from experiment_designer.lock.status import experiment_status


class FakeSession:
    base_url = "http://fake"

    def __init__(self, prefs, health, name="Study", runner=None):
        self.prefs, self.health, self.name, self.runner = prefs, health, name, runner
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == "experiments/181":
            return {"id": 181, "name": self.name, "user_id": 5, "runner_user_id": self.runner,
                    "preferences": self.prefs, "crawlers": []}
        if path == "experiments/181/health":
            return self.health
        raise AssertionError(path)


def _prefs(label, **values):
    return [{"name": f"pmspec.{k}", "description": label, "value": v} for k, v in values.items()]


HEALTH = {"experiment_id": 181, "window_hours": 24, "bots": 3,
          "runs": {"success": 5, "failure": 1}, "jobs": {"success": 40, "warning": 2},
          "bad_runs": 1, "bad_jobs": 2, "treatment_lost_jobs": 0, "degraded_jobs": 2,
          "last_finished_at": "2026-09-12 06:00:00"}


def test_groups_provenance_by_upload_label():
    prefs = _prefs("upload 1", lock_sha256="a" * 64, mode="test", git_commit="abc1234",
                   anchor="2026-09-11T10:00:00+02:00") + \
            _prefs("upload 2", lock_sha256="b" * 64, mode="test", git_commit="abc1234-dirty",
                   anchor="2026-09-12T10:00:00+02:00")
    s = experiment_status(FakeSession(prefs, HEALTH, runner=3), 181)
    assert [u.label for u in s.uploads] == ["upload 1", "upload 2"]
    assert s.uploads[1].values["git_commit"] == "abc1234-dirty"
    assert s.runner_user_id == 3 and s.bound_to_any_lock and s.verify is None
    text = s.render()
    assert "upload 2" in text and "aaaaaaaaaaaa" in text and "runner=3" in text
    assert "treatment_lost_jobs=0" in text and "bad_runs=1" in text


def test_hours_is_passed_to_health():
    fake = FakeSession(_prefs("upload 1", lock_sha256="a" * 64), HEALTH)
    experiment_status(fake, 181, hours=6)
    assert ("experiments/181/health", {"hours": 6}) in fake.calls


def test_no_provenance_is_said_not_assumed():
    s = experiment_status(FakeSession([], HEALTH), 181)
    assert s.uploads == [] and not s.bound_to_any_lock
    assert "not uploaded by pmspec" in s.render()


def test_absent_decision_counts_render_as_unknown():
    health = {k: v for k, v in HEALTH.items() if k not in ("treatment_lost_jobs", "degraded_jobs")}
    s = experiment_status(FakeSession([], health), 181)
    assert "treatment_lost_jobs=unknown" in s.render()
    assert s.ok  # unknown is not a failure


def test_treatment_lost_makes_status_not_ok():
    s = experiment_status(FakeSession([], {**HEALTH, "treatment_lost_jobs": 1}), 181)
    assert not s.ok and "treatment_lost_jobs=1" in s.render()


def test_with_a_lock_runs_verify(monkeypatch):
    from experiment_designer.lock import status as status_mod

    class V:
        ok = False
        bound = False
        differences = []
        extra_live_crawlers = []

        def render(self):
            return "UNBOUND x"

    monkeypatch.setattr(status_mod, "verify_lock", lambda *a, **k: V())
    s = experiment_status(FakeSession([], HEALTH), 181, lock_text="pmspec_lock: 1\n")
    assert s.verify is not None and not s.ok and "UNBOUND x" in s.render()
```

```python
# orchestrator/tests/test_mcp_workflow_tools.py
import json

import pytest

from orchestrator.mcp_server import server as s


@pytest.fixture(autouse=True)
def local_api(monkeypatch, mock_settings):
    monkeypatch.setattr(s, "get_settings", lambda: mock_settings)


def test_experiment_status_tool(monkeypatch):
    from experiment_designer.lock.status import ExperimentStatus, ProvenanceUpload

    st = ExperimentStatus(experiment_id=181, name="Study", runner_user_id=None,
                          uploads=[ProvenanceUpload("upload 1", {"mode": "test", "lock_sha256": "a" * 64})],
                          health={"bad_jobs": 0, "treatment_lost_jobs": 0}, verify=None)
    monkeypatch.setattr(s, "experiment_status", lambda *a, **k: st)
    out = json.loads(s.experiment_status_tool(181))
    assert out["ok"] is True and out["uploads"][0]["label"] == "upload 1" and "render" in out
```
(The MCP tool is registered under the name `experiment_status` via `@mcp.tool(name="experiment_status")` on a function called `experiment_status_tool`, so the module-level import `experiment_status` from `lock.status` can be patched — mirror how `verify_experiment` wraps `verify_lock`.)

- [ ] **Step 2: Run to verify they fail** — `venv/bin/pytest experiment_designer/tests/test_lock_status.py orchestrator/tests/test_mcp_workflow_tools.py -v` → `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# experiment_designer/lock/status.py
"""One screen: what pmspec uploaded (provenance), how it is running (health), and --
given a lock -- whether the two still agree (verify)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .verify import VerifyResult, verify_lock

_HEALTH_KEYS = ("bad_runs", "bad_jobs", "treatment_lost_jobs", "degraded_jobs")
_UPLOAD_KEYS = ("mode", "lock_sha256", "git_commit", "generator", "version", "seed", "anchor",
                "uploaded_at", "uploaded_by", "lax")


@dataclass
class ProvenanceUpload:
    label: str
    values: dict[str, str] = field(default_factory=dict)


@dataclass
class ExperimentStatus:
    experiment_id: int
    name: str
    runner_user_id: int | None
    uploads: list[ProvenanceUpload]
    health: dict[str, Any]
    verify: VerifyResult | None = None

    @property
    def bound_to_any_lock(self) -> bool:
        return any("lock_sha256" in u.values for u in self.uploads)

    @property
    def ok(self) -> bool:
        lost = self.health.get("treatment_lost_jobs")
        return (self.verify is None or self.verify.ok) and not (isinstance(lost, int) and lost > 0)

    def render(self) -> str:
        runner = "default" if self.runner_user_id is None else str(self.runner_user_id)
        lines = [f'experiment {self.experiment_id}  "{self.name}"  runner={runner}']
        if not self.uploads:
            lines.append("no pmspec provenance: not uploaded by pmspec")
        for u in self.uploads:
            v = u.values
            sha = v.get("lock_sha256", "")[:12]
            lines.append(
                f"{u.label}: mode={v.get('mode', '?')} lock={sha or '?'} "
                f"commit={v.get('git_commit') or 'none'} generator={v.get('generator') or '-'} "
                f"seed={v.get('seed', '?')} anchor={v.get('anchor', '?')} "
                f"uploaded={v.get('uploaded_at', '?')} by {v.get('uploaded_by', '?')}"
                + (" lax" if v.get("lax") == "true" else "")
            )
        hours = self.health.get("window_hours", "?")
        counts = " ".join(f"{k}={self.health.get(k, 'unknown')}" for k in _HEALTH_KEYS)
        lines.append(
            f"health (last {hours}h): bots={self.health.get('bots', '?')} "
            f"runs={self.health.get('runs', {})} jobs={self.health.get('jobs', {})} {counts} "
            f"last_finished_at={self.health.get('last_finished_at') or 'never'}"
        )
        if self.verify is not None:
            lines.append(self.verify.render())
        return "\n".join(lines)


def _uploads(prefs: list[dict[str, Any]]) -> list[ProvenanceUpload]:
    grouped: dict[str, ProvenanceUpload] = {}
    for p in prefs:
        name = str(p.get("name", ""))
        if not name.startswith("pmspec."):
            continue
        label = str(p.get("description") or "upload 1")
        grouped.setdefault(label, ProvenanceUpload(label)).values[name[len("pmspec."):]] = str(
            p.get("value", "")
        )
    return sorted(grouped.values(), key=lambda u: (len(u.label), u.label))


def experiment_status(
    session: Any, experiment_id: int, *, hours: int = 24, lock_text: str | None = None,
    deep: bool = False,
) -> ExperimentStatus:
    exp = session.get(f"experiments/{experiment_id}")
    health = session.get(f"experiments/{experiment_id}/health", params={"hours": hours})
    verify = (
        verify_lock(lock_text, session=session, experiment_id=experiment_id, deep=deep)
        if lock_text is not None
        else None
    )
    return ExperimentStatus(
        experiment_id=experiment_id,
        name=str(exp.get("name", "")),
        runner_user_id=exp.get("runner_user_id"),
        uploads=_uploads(exp.get("preferences") or []),
        health=health if isinstance(health, dict) else {},
        verify=verify,
    )
```
(`_uploads` sorts "upload 2" after "upload 10" correctly by length-then-name.) `health` comes back through the session's `_unwrap`; if the API wraps it in `data`, the session already unwraps.

CLI in `experiment_designer/cli.py`:
```python
@cli.command()
@click.argument("experiment_id", type=int)
@click.argument("lock", required=False, type=click.Path(exists=True))
@click.option("--hours", default=24, show_default=True, type=int)
@click.option("--deep", is_flag=True)
def status(experiment_id: int, lock: str | None, hours: int, deep: bool) -> None:
    """Provenance + health of a live experiment; with LOCK, also verify."""
    session = _session_from_env()
    try:
        st = experiment_status(session, experiment_id, hours=hours,
                               lock_text=Path(lock).read_text() if lock else None, deep=deep)
    except ApiError as e:
        click.secho(f"Status failed: {e}", fg="red")
        sys.exit(1)
    except LockError as e:  # the lock file, not the server
        ...same "Lock invalid" block as verify...
    click.echo(st.render())
    sys.exit(0 if st.ok else 1)
```
MCP: `experiment_status_tool` registered as `experiment_status`, using `_McpSession()`; response `{experiment_id, name, runner_user_id, uploads: [{label, values}], health, ok, bound_to_any_lock, verify: {...}|None, render}`; verify serialised like `verify_experiment` (with `_redact_differences`).

- [ ] **Step 4: Run tests, then `make check`.** Add a CLI test in `experiment_designer/tests/test_lock_cli_workflow.py` (`status 181` with `_session_from_env` and `experiment_status` monkeypatched → prints render, exit code follows `ok`).

- [ ] **Step 5: Commit** — `git commit -m "pmspec: status -- provenance, health and optional verify on one screen"`

---

### Task 2: `pmspec diff`

**Files:**
- Create: `experiment_designer/lock/diff.py`
- Modify: `experiment_designer/cli.py`, `orchestrator/mcp_server/server.py` (`diff_locks` tool)
- Test: `experiment_designer/tests/test_lock_diff.py`

**Interfaces:**
- Consumes: `lock.verify.lock_shape`, `compare_shapes`, `Difference`; `lock.io.load_lock`; `lock.anchor.resolve_anchor`.
- Produces:
  ```python
  @dataclass class LockDiff: provenance_a: dict; provenance_b: dict; anchor_a: str; anchor_b: str; differences: list[Difference]; only_in_a: list[str]; only_in_b: list[str]
      @property identical -> bool   # no differences and both only_in lists empty and anchors equal
      def render(self) -> str        # provenance side by side (spec_path, spec_sha256[:12], git_commit, generator, seed, lax), anchor line, then crawler-level diff, then the two only_in lists
  def diff_locks(text_a: str, text_b: str, *, deep: bool = True) -> LockDiff
  ```
  Shapes are built with a fixed reference anchor (`resolve_anchor("2000-01-01T00:00:00")`) on both sides so `start_at` compares offsets, not dates; a differing `schedule.anchor` is reported once as an anchor line, not per queue. Crawlers only in one lock are listed, not diffed. `deep=True` by default — the jobs are in both files, nothing to fetch.
  CLI: `pmspec diff LOCK_A LOCK_B` → `render()`, exit 0 iff identical. MCP: `diff_locks(lock_a: str, lock_b: str)` → JSON (`identical`, `differences`, `only_in_a/b`, `anchor_a/b`, `render`).

- [ ] **Step 1: Tests**

```python
# experiment_designer/tests/test_lock_diff.py
from experiment_designer.lock.diff import diff_locks
from experiment_designer.lock.io import dump_lock

from .test_lock_io import make_lock


def test_identical_locks():
    text = dump_lock(make_lock())
    d = diff_locks(text, text)
    assert d.identical and "identical" in d.render()


def test_param_change_is_a_difference():
    a = make_lock()
    b = make_lock()
    b.crawlers[0]["configuration"]["params"]["pi"] = 0
    d = diff_locks(dump_lock(a), dump_lock(b))
    assert not d.identical
    assert any(x.path == "crawlers.bot_01.params.pi" and x.expected == 1.0 and x.actual == 0.0 for x in d.differences)


def test_anchor_change_is_one_line_not_per_queue():
    a = make_lock()
    b = make_lock(schedule={"anchor": "2026-10-01T10:00:00"})
    d = diff_locks(dump_lock(a), dump_lock(b))
    assert d.anchor_a == "upload" and d.anchor_b == "2026-10-01T10:00:00"
    assert not any("start_at" in x.path for x in d.differences)
    assert not d.identical and "anchor" in d.render()


def test_job_behaviour_change_is_seen_by_default():
    a = make_lock()
    b = make_lock()
    b.crawlers[0]["queues"][0]["jobs"][0]["behaviors"][0]["value"] = "https://other"
    d = diff_locks(dump_lock(a), dump_lock(b))
    assert any(x.path.endswith("jobs[0].behaviors.url") for x in d.differences)


def test_crawlers_only_on_one_side_are_listed_not_diffed():
    a = make_lock()
    b = make_lock()
    extra = dict(a.crawlers[0]); extra = __import__("copy").deepcopy(extra); extra["name"] = "bot_02"
    b.crawlers.append(extra)
    d = diff_locks(dump_lock(a), dump_lock(b))
    assert d.only_in_b == ["bot_02"] and d.only_in_a == [] and not d.identical
    assert not any("bot_02" in x.path for x in d.differences)


def test_provenance_side_by_side_in_render():
    a = make_lock()
    b = make_lock()
    b.provenance.git_commit = "deadbee"
    text = diff_locks(dump_lock(a), dump_lock(b)).render()
    assert "a2024c9" in text and "deadbee" in text
```

- [ ] **Step 2: RED.** — [ ] **Step 3: Implement** `diff.py` per the interface (build both shapes with the fixed anchor and `deep=deep`; split crawler names into common/only-a/only-b; `compare_shapes` on the common subset). CLI `diff` and MCP `diff_locks` (read-only tool; `LockError` → error JSON). — [ ] **Step 4: tests + `make check`.** — [ ] **Step 5: Commit** `pmspec: diff two locks`.

---

### Task 3: `pmspec cleanup` (test experiments only)

**Files:**
- Create: `experiment_designer/lock/cleanup.py`
- Modify: `experiment_designer/lock/api.py` (`delete`), `experiment_designer/cli.py`, `orchestrator/mcp_server/server.py` (`_McpSession.delete`; tool `pm_cleanup_test_experiments`), `orchestrator/tests/test_mcp_write_guard.py` (expected write-tool set)
- Test: `experiment_designer/tests/test_lock_cleanup.py`, `orchestrator/tests/test_mcp_workflow_tools.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass class CleanupCandidate: experiment_id: int; name: str; uploaded_at: str | None; mode: str
  def find_test_experiments(session, *, older_than: timedelta | None = None, now: datetime | None = None) -> list[CleanupCandidate]
      # GET experiments/experiments-list (names + ids); for each name starting with "test-", GET experiments/{id};
      # keep only those whose preferences contain pmspec.mode == "test"; older_than filters on the latest pmspec.uploaded_at (an experiment with no parseable uploaded_at is kept only when older_than is None)
  def delete_experiments(session, ids: list[int]) -> list[int]      # DELETE experiments/{id}/destroy each; returns deleted ids; stops at first ApiError and re-raises (partial list in e.args? no: raise CleanupError(deleted, failed_id, cause))
  def parse_age(text: str) -> timedelta                              # "7d", "36h", "90m"
  ```
  `ApiSession.delete(path)`; `_McpSession.delete(path)` → `_api_request("DELETE", path)`.
  CLI: `pmspec cleanup [--older-than 7d] [--dry-run] [--yes]`: lists candidates (`id  name  mode  uploaded_at`); without `--yes` prompts `Delete N test experiment(s)? [y/N]` (`click.confirm`); `--dry-run` lists and exits 0. Exit 1 on `CleanupError` after printing what was deleted.
  MCP: `pm_cleanup_test_experiments(older_than: str | None = None, confirm: bool = False)` — dry-run lists candidates; `confirm=true` deletes after `_require_local_write_target()`. Uses `_McpSession` for reads and its `.delete` for writes — the guard scan sees `_api_request("DELETE"…)` only inside `_McpSession.delete`, not in the tool body, so extend the scan's mutation signals with `.delete(` attribute calls (as done for `.post_file(`) and add `pm_cleanup_test_experiments` to the expected write-tool set and to the "known pm tools are detected" test.

- [ ] **Step 1: Tests**

```python
# experiment_designer/tests/test_lock_cleanup.py
from datetime import datetime, timedelta

import pytest

from experiment_designer.lock.api import ApiError
from experiment_designer.lock.cleanup import CleanupError, delete_experiments, find_test_experiments, parse_age
from experiment_designer.spec.time_utils import API_TIMEZONE

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=API_TIMEZONE)


class FakeSession:
    base_url = "http://fake"

    def __init__(self, experiments, fail_delete=None):
        self.experiments = experiments  # id -> {"name", "prefs"}
        self.deleted, self.calls, self.fail_delete = [], [], fail_delete

    def get(self, path, params=None):
        self.calls.append(("GET", path))
        if path == "experiments/experiments-list":
            return [{"id": i, "name": e["name"]} for i, e in self.experiments.items()]
        i = int(path.split("/")[1])
        return {"id": i, "name": self.experiments[i]["name"], "preferences": self.experiments[i]["prefs"]}

    def delete(self, path):
        self.calls.append(("DELETE", path))
        i = int(path.split("/")[1])
        if i == self.fail_delete:
            raise ApiError("DELETE failed", status=500)
        self.deleted.append(i)
        return {}


def _p(mode, uploaded_at=None):
    prefs = [{"name": "pmspec.mode", "description": "upload 1", "value": mode}]
    if uploaded_at:
        prefs.append({"name": "pmspec.uploaded_at", "description": "upload 1", "value": uploaded_at})
    return prefs


EXPS = {
    1: {"name": "test-Study-20260901", "prefs": _p("test", "2026-09-01T10:00:00+02:00")},
    2: {"name": "test-Study-20260911", "prefs": _p("test", "2026-09-11T10:00:00+02:00")},
    3: {"name": "test-looks-like-one", "prefs": _p("real")},        # name lies; preference says real
    4: {"name": "Real study", "prefs": _p("real")},
    5: {"name": "test-no-provenance", "prefs": []},                   # never uploaded by pmspec
}


def test_only_pmspec_test_experiments_are_candidates():
    fake = FakeSession(EXPS)
    ids = [c.experiment_id for c in find_test_experiments(fake, now=NOW)]
    assert ids == [1, 2]
    assert ("GET", "experiments/4") not in fake.calls          # non test- names are never fetched


def test_older_than_filters_on_uploaded_at():
    ids = [c.experiment_id for c in find_test_experiments(FakeSession(EXPS), older_than=timedelta(days=7), now=NOW)]
    assert ids == [1]


def test_delete_deletes_only_the_given_ids():
    fake = FakeSession(EXPS)
    assert delete_experiments(fake, [1, 2]) == [1, 2]
    assert fake.calls[-2:] == [("DELETE", "experiments/1/destroy"), ("DELETE", "experiments/2/destroy")]


def test_delete_stops_at_first_failure_and_reports_progress():
    fake = FakeSession(EXPS, fail_delete=2)
    with pytest.raises(CleanupError) as exc:
        delete_experiments(fake, [1, 2])
    assert exc.value.deleted == [1] and exc.value.failed_id == 2


@pytest.mark.parametrize("text,delta", [("7d", timedelta(days=7)), ("36h", timedelta(hours=36)), ("90m", timedelta(minutes=90))])
def test_parse_age(text, delta):
    assert parse_age(text) == delta


def test_parse_age_rejects_garbage():
    with pytest.raises(ValueError):
        parse_age("soon")
```
MCP tests (append to `test_mcp_workflow_tools.py`): dry-run makes no DELETE (patch `s._api_request` recorder; feed GET responses); `confirm=True` against a remote target → `refusing to mutate`; `confirm=True` local → DELETE calls for the candidates only.

- [ ] **Step 2: RED.** — [ ] **Step 3: Implement** (`cleanup.py`, `ApiSession.delete`, `_McpSession.delete`, CLI `cleanup`, MCP tool, guard-scan extension + expected-set update). — [ ] **Step 4: tests incl. `test_mcp_write_guard.py`, `make check`.** — [ ] **Step 5: Commit** `pmspec: cleanup deletes only pmspec test experiments`.

---

### Task 4: `preview` and `summary` read locks; the round-trip test

**Files:**
- Modify: `experiment_designer/summary/generator.py` (`from_lock`, `from_lock_text`), `experiment_designer/cli.py` (`preview`, `summary`), `orchestrator/mcp_server/server.py` (`summarize_yaml_spec` accepts a lock)
- Test: `experiment_designer/tests/test_lock_summary_roundtrip.py`

**Interfaces:**
- Produces: `SummaryGenerator.from_lock(lock: Lock) -> ExperimentSummary` — `source=DataSource.LOCK` (add to the enum), name/description from `lock.experiment`, crawlers via `_extract_crawler_summary_from_json` over `lock.crawlers` (queues carry `start_offset`, which the extractor does not read), `design_seed = lock.provenance.seed`, `has_design = False` (a lock is post-expansion; say so in `to_text()` as `source: lock (seed N, commit X)`); `from_lock_text(text)` = `load_lock` + `from_lock`. `preview LOCK` prints crawler count, queue and job totals, anchor, seed, provenance — no design formula (there is none). `summary LOCK` works wherever a spec does (text/json/html, `--compare` between any mix of spec and lock).

- [ ] **Step 1: Tests**

```python
# experiment_designer/tests/test_lock_summary_roundtrip.py
"""§6: spec -> lock -> summary equals spec -> summary, for every example spec that compiles."""

from pathlib import Path

import pytest

from experiment_designer.lock.build import compile_spec
from experiment_designer.summary.generator import SummaryGenerator

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
COMPILABLE = ["simple-distribution-study.yaml", "websearch-study.yaml"]   # the two that pass strict validation


@pytest.mark.parametrize("name", COMPILABLE)
def test_spec_and_lock_summaries_agree(name):
    path = EXAMPLES / name
    gen = SummaryGenerator()
    from_spec = gen.from_yaml_file(path, seed=7)
    result = compile_spec(path.read_text(), base_dir=path.parent, spec_path=name, seed=7)
    assert result.lock is not None, result.errors
    from_lock = gen.from_lock(result.lock)
    assert [c.name for c in from_lock.crawlers] == [c.name for c in from_spec.crawlers]
    assert from_lock.total_jobs == from_spec.total_jobs and from_lock.total_queues == from_spec.total_queues
    assert from_lock.jobs_by_type == from_spec.jobs_by_type
    assert [c.params for c in from_lock.crawlers] == [c.params for c in from_spec.crawlers]
    assert from_lock.design_seed == 7


def test_lock_summary_text_names_its_source():
    path = EXAMPLES / COMPILABLE[0]
    result = compile_spec(path.read_text(), base_dir=path.parent, spec_path="x", seed=7)
    text = SummaryGenerator().from_lock(result.lock).to_text()
    assert "lock" in text.lower() and "seed" in text.lower()


def test_cli_preview_and_summary_accept_a_lock(tmp_path):
    from click.testing import CliRunner

    from experiment_designer.cli import cli
    from experiment_designer.lock.io import dump_lock

    path = EXAMPLES / COMPILABLE[0]
    result = compile_spec(path.read_text(), base_dir=path.parent, spec_path="x", seed=7)
    lock_path = tmp_path / "s.lock.yaml"
    lock_path.write_text(dump_lock(result.lock))
    out = CliRunner().invoke(cli, ["preview", str(lock_path)])
    assert out.exit_code == 0 and "Crawlers" in out.output and "anchor" in out.output.lower()
    out = CliRunner().invoke(cli, ["summary", str(lock_path)])
    assert out.exit_code == 0, out.output
```
If `from_spec` and `from_lock` disagree on `params` because the spec path stores sampled floats with different precision, that is a real finding — report it rather than loosening the assertion; the expected fix is in the lock side (it must carry exactly what the converter produced).

- [ ] **Step 2: RED.** — [ ] **Step 3: Implement.** — [ ] **Step 4: tests + `make check`.** — [ ] **Step 5: Commit** `pmspec: preview and summary read locks; spec/lock round-trip pinned`.

---

### Task 5: MCP `upload_experiment` — `allow_dirty`, `allow_past`, dry-run secrets check

**Files:**
- Modify: `orchestrator/mcp_server/server.py` (`upload_experiment`)
- Test: `orchestrator/tests/test_mcp_lock_tools.py` (extend)

**Interfaces:**
- `upload_experiment(lock_content, mode, experiment_id=None, dry_run=True, deep_verify=False, allow_dirty=False, allow_past=False)`; both flags go into `UploadOptions`. Dry-run additionally loads the secrets store when placeholders exist (`SecretStore.load()`; `FileNotFoundError` → `"secrets_store": "missing"`, insecure → its message) and reports `secrets_missing: [...]` — no values, ever — so a dry run answers "would this upload go through" for the secrets gate too. Docstring updated; `compile_experiment_spec`'s docstring still says a lock with no commit is refused in `real` mode — now add "unless allow_dirty".

- [ ] **Step 1: Tests** — `test_upload_dry_run_reports_missing_secrets` (`PMSPEC_SECRETS_FILE` pointed at an empty mode-600 TOML → `secrets_missing == ["oxylabs.a.password", "oxylabs.a.username"]`); `test_upload_dry_run_reports_no_store`; `test_upload_passes_allow_flags_to_upload_lock` (patch `upload_lock`, assert `options.allow_dirty and options.allow_past`).
- [ ] **Step 2: RED.** — [ ] **Step 3: Implement.** — [ ] **Step 4: `make check`.** — [ ] **Step 5: Commit** `mcp: upload_experiment allow_dirty/allow_past; dry run checks the secrets store`.

---

### Task 6: Docs and skills

**Files:** `docs/experiment-designer.md` (workflow: add `status`, `diff`, `cleanup` after "Step 5: Verify"; the CLI table; the MCP tool list in "Locks… for generators"), `CLAUDE.md` (Experiment Designer paragraph: the three commands, `cleanup` deletes only `pmspec.mode=test`), `.claude/skills/primemover-compose/SKILL.md` (after upload: `experiment_status`; test runs: `pm_cleanup_test_experiments`), `.claude/skills/primemover-debug/SKILL.md` (first step for a pmspec experiment: `experiment_status(id, lock_content=…)`).

- [ ] Verify every command/flag/tool against `--help` and the code; `make check`; commit `docs: status, diff, cleanup`.

## Not in this plan
GUI provenance panel; server-side lock storage; the research-repo generator fixes (autocompleted: `${query}`, `proxy_pool` data pool, `strategy_schemas`, `weight:`; elections: `${secret:…}`).
