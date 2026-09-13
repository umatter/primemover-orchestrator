# pmspec phase 4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lock format 2 (content-addressed queues/agents, ~40× smaller for the real study), per-bot template draws (`with: {from_pool, field}`), and `pmspec audit` (does the decision function report the treatment the lock says each job asked for).

**Architecture:** Format 2 is a serialisation concern only: `Lock` stays the phase-1 in-memory shape; `dump_lock` factors, `load_lock` materialises, everything downstream is untouched. Draws are a parser→converter handshake through a printable token, resolved per crawler in the converter where pools and the crawler index already are. The audit is one more pure function over a session (`experiment_designer/lock/audit.py`) with a `render()`, like `status`, reading the job rows' `decision_*` columns through `orchestrator.status` so the verdict rules stay in one place.

**Tech Stack:** Python 3.10+, PyYAML, jsonschema, existing sessions; pytest.

**Spec:** `docs/superpowers/specs/2026-09-13-pmspec-phase4-design.md` (binding). Phase-1/2/3 rules from `2026-09-11-pmspec-lock-and-provenance-design.md` stay in force (byte-determinism, strict contract, no secrets in output, MCP dry-run/local-only).

## Global Constraints

- Line length 100; `make check` passes after every task (baseline 1552 passed, 33 skipped); Python 3.10.
- Byte-determinism: `dump_lock` twice → identical bytes; parts sorted by key.
- A draw token that survives conversion is a drop (strict error / lax warning), never silently uploaded.
- The audit reads only `orchestrator.status` helpers for verdicts (`job_decision_reported`, `job_decision_verdict`, `is_terminal`, `normalize`); no second implementation of the status vocabulary.
- No output contains a resolved secret.
- Commit after every task with the session's attribution trailer.

## File structure

| path | responsibility |
|---|---|
| `experiment_designer/lock/model.py` (modify) | `LOCK_FORMAT_VERSION = 2`; `Lock.to_dict()` factors parts; `Lock.from_dict()` materialises (formats 1 and 2) |
| `experiment_designer/lock/parts.py` (new) | `part_key(obj, prefix)`, `factor_crawlers(crawlers) -> (parts, refs)`, `materialise(parts, refs) -> crawlers`, `PartCollision` |
| `experiment_designer/schema/lock-2.schema.json` (new) | format 2 schema |
| `experiment_designer/lock/io.py` (modify) | `validate_lock_dict` dispatches on `pmspec_lock` ∈ {1, 2} |
| `experiment_designer/spec/parser.py` (modify) | `with:` dict → draw token in `_apply_variables`/`_expand_template_ref`/`_expand_repeat` |
| `experiment_designer/spec/draws.py` (new) | `DRAW_TOKEN_RE`, `make_token`, `parse_token`, `resolve_draws(crawlers, pool_manager, seed) -> list[str]` |
| `experiment_designer/spec/converter.py` (modify) | calls `resolve_draws` after pool references, before proxies; `_scan_unresolved` reports tokens |
| `experiment_designer/spec/validator.py` (modify) | dict-valued `with:` entries must be `{from_pool: str, field: str, select?: random|first}` |
| `experiment_designer/lock/audit.py` (new) | `AuditResult`, `audit_experiment(session, experiment_id, lock_text, *, hours, sample, strict)`, expected-filter mapping |
| `experiment_designer/cli.py`, `orchestrator/mcp_server/server.py` (modify) | `audit` command / `audit_experiment` tool |
| `docs/experiment-designer.md`, `CLAUDE.md`, `.claude/skills/primemover-debug/SKILL.md` (modify) | format 2, draws, audit |
| tests | `experiment_designer/tests/test_lock_parts.py`, `test_draws.py`, `test_lock_audit.py`, `test_lock_cli_workflow.py` (+audit), `orchestrator/tests/test_mcp_workflow_tools.py` (+audit) |

---

### Task 1: Content-addressed parts — `parts.py`, format 2 model, schema, IO

**Files:**
- Create: `experiment_designer/lock/parts.py`, `experiment_designer/schema/lock-2.schema.json`
- Modify: `experiment_designer/lock/model.py`, `experiment_designer/lock/io.py`
- Test: `experiment_designer/tests/test_lock_parts.py`; adjust `test_lock_io.py` where it pins `pmspec_lock: 1` or key order

**Interfaces:**
- Produces:
  ```python
  # parts.py
  class PartCollision(ValueError)
  def canonical_json(obj) -> str                         # json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
  def part_key(obj, prefix: str) -> str                  # f"{prefix}:{sha256(canonical_json)[:16]}"
  def factor_crawlers(crawlers) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]
      # parts = {"queues": {key: body}, "agents": {key: body}}; refs = crawlers with
      # agent -> {"ref": key, "name": agent["name"]} and each queue -> {"ref": key, "start_offset": q["start_offset"]}
      # body(queue) = queue minus start_offset; body(agent) = agent minus name; raises PartCollision if two
      # different bodies hash to one key
  def materialise(parts, refs) -> list[dict[str, Any]]   # inverse; unknown ref -> KeyError with the ref in the message
  ```
  `model.py`: `LOCK_FORMAT_VERSION = 2`; `Lock.to_dict()` emits `parts` (queues then agents, each sorted by key) before `crawlers`, with crawler key order `name, description, configuration, proxy, agent, queues`; `Lock.from_dict(d)` accepts `pmspec_lock` 1 (crawlers as-is) or 2 (`materialise`). `io.validate_lock_dict` picks `lock-1.schema.json` or `lock-2.schema.json` by `pmspec_lock`, else the "expected 1 or 2" error.

- [ ] **Step 1: Write the failing tests**

```python
# experiment_designer/tests/test_lock_parts.py
"""Format 2: every distinct queue and agent body once, crawlers by reference."""

import copy

import pytest

from experiment_designer.lock.io import dump_lock, load_lock, sha256_text, validate_lock_dict
from experiment_designer.lock.model import LOCK_FORMAT_VERSION, Lock
from experiment_designer.lock.parts import PartCollision, factor_crawlers, materialise, part_key

from .test_lock_io import make_lock


def _fleet(n: int) -> Lock:
    base = make_lock()
    crawlers = []
    for i in range(n):
        c = copy.deepcopy(base.crawlers[0])
        c["name"] = f"bot_{i:03d}"
        c["agent"]["name"] = f"Agent {i:03d}"
        c["configuration"]["params"]["pi"] = i % 2          # two arms -> two distinct diets? no: params are per crawler, queues identical
        crawlers.append(c)
    return Lock(provenance=base.provenance, experiment=base.experiment, schedule=base.schedule, crawlers=crawlers)


def test_part_key_is_content_addressed_and_order_independent():
    a = {"x": 1, "y": [1, 2]}
    b = {"y": [1, 2], "x": 1}
    assert part_key(a, "q") == part_key(b, "q")
    assert part_key(a, "q").startswith("q:") and len(part_key(a, "q")) == 2 + 16
    assert part_key(a, "q") != part_key({"x": 2, "y": [1, 2]}, "q")


def test_factor_and_materialise_round_trip():
    lock = _fleet(5)
    parts, refs = factor_crawlers(lock.crawlers)
    assert len(parts["queues"]) == 1 and len(parts["agents"]) == 1
    assert all("ref" in q and "start_offset" in q and "jobs" not in q for c in refs for q in c["queues"])
    assert all(set(c["agent"]) == {"ref", "name"} for c in refs)
    assert materialise(parts, refs) == lock.crawlers


def test_dump_writes_format_2_and_load_materialises():
    lock = _fleet(3)
    text = dump_lock(lock)
    assert text.startswith(f"pmspec_lock: {LOCK_FORMAT_VERSION}\n") and LOCK_FORMAT_VERSION == 2
    assert "\nparts:\n" in text and text.index("parts:") < text.index("crawlers:")
    assert load_lock(text) == lock


def test_dump_is_deterministic():
    assert dump_lock(_fleet(4)) == dump_lock(_fleet(4))


def test_size_collapses_for_a_fleet():
    small = len(dump_lock(_fleet(1)))
    big = len(dump_lock(_fleet(200)))
    assert big < small + 200 * 400, (small, big)   # ~a few hundred bytes per crawler ref, not a full queue each


def test_format_1_still_loads(tmp_path):
    lock = make_lock()
    d = lock.to_dict()
    # hand-build a format-1 document: crawlers inline, no parts
    d1 = {k: v for k, v in d.items() if k != "parts"}
    d1["pmspec_lock"] = 1
    d1["crawlers"] = lock.crawlers
    import yaml
    text = yaml.safe_dump(d1, sort_keys=False, allow_unicode=True)
    assert load_lock(text) == lock


def test_unknown_format_is_refused():
    text = dump_lock(make_lock()).replace("pmspec_lock: 2", "pmspec_lock: 3", 1)
    errors = validate_lock_dict(__import__("yaml").safe_load(text))
    assert any("pmspec_lock" in e for e in errors)


def test_dangling_ref_is_an_error():
    parts, refs = factor_crawlers(_fleet(1).crawlers)
    refs[0]["queues"][0]["ref"] = "q:0000000000000000"
    with pytest.raises(KeyError, match="q:0000000000000000"):
        materialise(parts, refs)


def test_collision_is_an_error(monkeypatch):
    from experiment_designer.lock import parts as parts_mod

    monkeypatch.setattr(parts_mod, "part_key", lambda obj, prefix: f"{prefix}:same")
    lock = _fleet(2)
    lock.crawlers[1]["queues"][0]["name"] = "different"
    with pytest.raises(PartCollision):
        factor_crawlers(lock.crawlers)
```
Note the fleet helper comment: params differ per crawler but live in `configuration`, so queues stay identical — one queue part. Keep that comment in the file.

- [ ] **Step 2: RED** — `venv/bin/pytest experiment_designer/tests/test_lock_parts.py -v` → `ModuleNotFoundError`.

- [ ] **Step 3: Implement** `parts.py` per the interface; `model.py` (`to_dict` → factor; `from_dict` → materialise when `pmspec_lock == 2`); `lock-2.schema.json` (copy of `lock-1` with `pmspec_lock: {const: 2}`, a `parts` object `{queues: object, agents: object}` with `additionalProperties` bodies as objects, and crawler `agent`/`queues[]` items requiring `ref`; `start_offset` pattern kept on the ref; `jobs` forbidden on a ref); `io.validate_lock_dict` dispatch. Adjust `test_lock_io.py` assertions that pin `pmspec_lock: 1` / top-level key order (`pmspec_lock, provenance, experiment, schedule, parts, crawlers`) and the `test_schema_rejects_start_at_in_a_lock` (now `start_at` on a queue *ref*).

- [ ] **Step 4: `make check`.** The phase-1/2/3 tests that build locks via `make_lock()`/`compile_spec` must all pass unchanged — they never read the file layout, only `Lock`.

- [ ] **Step 5: Commit** — `lock: format 2 -- content-addressed queues and agents`.

---

### Task 2: Per-bot draws — token, parser, validator

**Files:**
- Create: `experiment_designer/spec/draws.py`
- Modify: `experiment_designer/spec/parser.py` (`_apply_variables`/`_substitute_in_structure`, `_expand_template_ref`, `_expand_repeat`), `experiment_designer/spec/validator.py` (`_validate_workflow_item` template/repeat branch)
- Test: `experiment_designer/tests/test_draws.py` (parser + validator half)

**Interfaces:**
- Produces:
  ```python
  # draws.py
  DRAW_TOKEN_RE = re.compile(r"%%pmspec-draw:([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):(random|first):(\d+)%%")
  def make_token(pool: str, field: str, select: str, ordinal: int) -> str
  def parse_token(text: str) -> tuple[str, str, str, int] | None       # fullmatch only
  def is_draw(value) -> bool                                            # dict with from_pool (str) and field (str)
  ```
  Parser: in `_apply_variables`, each `with:` value that `is_draw` becomes `make_token(pool, field, select or "random", ordinal)` with `ordinal` = its index among the draw-valued keys of that `with:` block; the string substitution then proceeds as today (`${query}` → token). `_expand_repeat` re-tokenises per repetition with ordinals continuing (`ordinal + repetition * n_draws`) so each repetition is a distinct draw. Validator: a dict-valued `with:` entry that is not `is_draw` (or has `count`, or an unknown `select`) is a drop naming the key.

- [ ] **Step 1: Failing tests**

```python
# experiment_designer/tests/test_draws.py
"""with: {from_pool, field}: one draw per bot per key, without replacement within a bot."""

from pathlib import Path

import pytest

from experiment_designer.spec.converter import SpecConverter
from experiment_designer.spec.draws import DRAW_TOKEN_RE, make_token, parse_token
from experiment_designer.spec.parser import SpecParser
from experiment_designer.spec.validator import SpecValidator

SPEC = """
version: "1.0"
experiment: {name: D}
data_pools:
  queries:
    type: csv
    source: queries.csv
templates:
  search:
    - type: {text: "${query}", selector: "//x", appendReturn: true}
design:
  factors:
    arm:
      levels: [a, b]
  replications: 3
crawlers:
  - name: bot_{arm}_{replica}
    from_design: true
    session:
      start: "+1m"
      workflow:
        - template: search
          with: {query: {from_pool: queries, field: term}}
        - template: search
          with: {query: {from_pool: queries, field: term}}
"""
CSV = "term,cat\nalpha,x\nbeta,x\ngamma,y\ndelta,y\nepsilon,z\nzeta,z\n"


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    (tmp_path / "queries.csv").write_text(CSV)
    return tmp_path


def test_token_round_trip():
    t = make_token("queries", "term", "random", 0)
    assert DRAW_TOKEN_RE.fullmatch(t) and parse_token(t) == ("queries", "term", "random", 0)
    assert parse_token("plain") is None


def test_parser_emits_tokens_with_distinct_ordinals(spec_dir):
    parsed = SpecParser(base_dir=spec_dir).parse(SPEC)
    assert not parsed.errors, parsed.errors
    jobs = parsed.crawlers[0]["queues"][0]["jobs"]
    texts = [b["value"] for j in jobs for b in j["behaviors"] if b["name"] == "text"]
    assert [parse_token(t)[3] for t in texts] == [0, 0]    # ordinal within each with: block
    assert all(parse_token(t)[:3] == ("queries", "term", "random") for t in texts)


def test_repeat_draws_per_repetition(spec_dir):
    text = SPEC.replace(
        "        - template: search\n          with: {query: {from_pool: queries, field: term}}\n"
        "        - template: search\n          with: {query: {from_pool: queries, field: term}}\n",
        "        - repeat: 3\n          template: search\n          with: {query: {from_pool: queries, field: term}}\n",
    )
    parsed = SpecParser(base_dir=spec_dir).parse(text)
    jobs = parsed.crawlers[0]["queues"][0]["jobs"]
    ordinals = [parse_token(b["value"])[3] for j in jobs for b in j["behaviors"] if b["name"] == "text"]
    assert ordinals == [0, 1, 2]


def test_validator_accepts_a_draw_and_rejects_other_dicts():
    import yaml

    ok = SpecValidator().validate(yaml.safe_load(SPEC))
    assert ok.dropped == 0, ok.errors
    bad = SPEC.replace("{from_pool: queries, field: term}", "{from_pool: queries}")
    r = SpecValidator().validate(yaml.safe_load(bad))
    assert any("with.query" in e and "field" in e for e in r.errors), r.errors
    counted = SPEC.replace("{from_pool: queries, field: term}", "{from_pool: queries, field: term, count: 2}")
    r = SpecValidator().validate(yaml.safe_load(counted))
    assert any("count" in e for e in r.errors)
```

- [ ] **Step 2: RED.** — [ ] **Step 3: Implement** `draws.py`; in `parser.py` add a small `_tokenise_draws(variables, base_ordinal=0) -> dict` used by `_expand_template_ref` and inside `_expand_repeat`'s loop (per repetition, `base_ordinal = rep * n_draws`); validator rule. — [ ] **Step 4: `make check`.** — [ ] **Step 5: Commit** — `designer: with: values may be per-bot pool draws (parser + validator)`.

---

### Task 3: Per-bot draws — converter resolution, strict scan, compile

**Files:**
- Modify: `experiment_designer/spec/draws.py` (`resolve_draws`), `experiment_designer/spec/converter.py` (call site + `_scan_unresolved`), `experiment_designer/spec/data_pools.py` (`rows(name)` already exists)
- Test: `experiment_designer/tests/test_draws.py` (converter half), `test_lock_build.py` (one compile test)

**Interfaces:**
- Produces:
  ```python
  def resolve_draws(crawlers: list[dict], pool_manager, seed: int | None) -> list[str]   # errors
  ```
  For crawler `i`, an rng `random.Random(f"{seed}:{i}")` (deterministic, differs per crawler; `seed` None → `random.Random(f"None:{i}")` is still deterministic, but `compile_spec` always has a seed); walk every string in the crawler (skipping `agent.multilogin_profile`); for each token: rows = `pool_manager.rows(pool)` (None → error `unknown pool`), candidates = rows not yet taken by this crawler for this pool (per-crawler set), `field` must exist in the row (else error naming pool, field, columns); `random` → `rng.choice(candidates)`, `first` → `candidates[0]`; none left → error `crawlers[i]: pool 'queries' exhausted after N draw(s)`; replace the token with `str(row[field])`. Strings may hold more than one token. The converter calls it after `resolve_pool_references` and before `_assign_pool_proxies`, appending errors to `self.errors`; `_scan_unresolved` additionally reports any surviving `DRAW_TOKEN_RE` match as a drop (`unresolved pool draw`).

- [ ] **Step 1: Failing tests** (append to `test_draws.py`)

```python
def _convert(spec_dir, text=SPEC, seed=7):
    parsed = SpecParser(base_dir=spec_dir, seed=seed).parse(text)
    assert not parsed.errors, parsed.errors
    return SpecConverter(seed=seed).convert(parsed)


def _typed(result):
    out = {}
    for c in result.json_data:
        out[c["name"]] = [b["value"] for q in c["queues"] for j in q["jobs"] for b in j["behaviors"] if b["name"] == "text"]
    return out


def test_each_bot_draws_its_own_values_without_replacement(spec_dir):
    r = _convert(spec_dir)
    assert r.errors == [], r.errors
    typed = _typed(r)
    assert len(typed) == 6
    for values in typed.values():
        assert len(values) == 2 and len(set(values)) == 2 and set(values) <= set(CSV.split()[1:]) | {"alpha", "beta", "gamma", "delta", "epsilon", "zeta"}
    assert len({tuple(v) for v in typed.values()}) > 1, "bots differ"
    assert not any(DRAW_TOKEN_RE.search(v) for vs in typed.values() for v in vs)


def test_draws_are_deterministic_per_seed(spec_dir):
    assert _typed(_convert(spec_dir, seed=7)) == _typed(_convert(spec_dir, seed=7))
    assert _typed(_convert(spec_dir, seed=7)) != _typed(_convert(spec_dir, seed=8))


def test_exhaustion_is_an_error(spec_dir):
    (spec_dir / "queries.csv").write_text("term\nonly\n")
    r = _convert(spec_dir)
    assert any("exhausted" in e and "queries" in e for e in r.errors), r.errors


def test_missing_field_column_is_an_error(spec_dir):
    r = _convert(spec_dir, SPEC.replace("field: term", "field: nope"))
    assert any("nope" in e and "term" in e for e in r.errors), r.errors


def test_first_select_is_stable(spec_dir):
    r = _convert(spec_dir, SPEC.replace("field: term}", "field: term, select: first}"))
    assert all(v == ["alpha", "beta"] for v in _typed(r).values())


def test_compile_never_writes_a_token(spec_dir):
    from experiment_designer.lock.build import compile_spec
    from experiment_designer.lock.io import dump_lock

    result = compile_spec(SPEC, base_dir=spec_dir, spec_path="s.yaml", seed=7)
    assert result.lock is not None, result.errors
    assert "%%pmspec-draw" not in dump_lock(result.lock)
```

- [ ] **Step 2: RED.** — [ ] **Step 3: Implement.** — [ ] **Step 4: `make check`.** — [ ] **Step 5: Commit** — `designer: per-bot pool draws resolved in the converter, without replacement`.

---

### Task 4: `pmspec audit` — core

**Files:**
- Create: `experiment_designer/lock/audit.py`
- Test: `experiment_designer/tests/test_lock_audit.py`

**Interfaces:**
- Consumes: `orchestrator.status.{job_decision_reported, job_decision_verdict, is_terminal, normalize}`; session `.get`; `lock.io.load_lock`; `lock.verify._paginate` (reuse for the queue lists).
- Produces:
  ```python
  YOUTUBE_V1 = {"pi": "mainstream_media", "psi": "children_entertainment", "kappa": "similar", "tau": "political", "beta": "fringe"}
  def expected_filters(strategy: str, treatment: dict[str, Any], schema: dict | None) -> set[str]
      # youtube_v1: key in YOUTUBE_V1 and value truthy (beta: != 0); else: schema.get("x-requested") mapping if present,
      # else every key whose str(value) not in {"", "0", "none", "None"}
  @dataclass class JobFinding: crawler: str; queue: str; ordinal: int; job_id: int | None; arm: str; outcome: str; detail: str
      # outcome in {"ok", "unreported", "mismatch", "lost", "degraded", "missing", "unfinished"}
  @dataclass class AuditResult: experiment_id: int; hours: int; sampled: int; total: int; findings: list[JobFinding]
      def counts(self) -> dict[tuple[str, str], int]        # (arm, outcome) -> n
      def ok(self, strict: bool) -> bool                    # no mismatch/lost; strict: also no unreported/degraded/missing
      def render(self) -> str
  def audit_experiment(session, experiment_id: int, lock_text: str, *, hours: int = 24, sample: int | None = None, strict: bool = False, schemas: dict[str, dict] | None = None) -> AuditResult
  ```
  Matching: `GET experiments/{id}` → crawlers by name (id); queue lists (both) → per crawler id, sorted like `verify`; jobs via `GET jobs?filter[queue]=<id>` sorted by `order` then `id`; the lock's job ordinal maps to the live job at the same index. `arm` label: `"{reliability}/{lean}"` from the crawler's configuration preferences when both exist, else `pi=…,psi=…,kappa=…,tau=…` from params. Jobs whose live `status_code` is not terminal → `unfinished` (never counted as bad). `sample=K` → `random.Random(seed=experiment_id).sample(crawler names, K)`.

- [ ] **Step 1: Failing tests** — a `FakeSession` serving one experiment with two crawlers (`bot_t` arm `green/left` via preferences, `bot_c` params all 0), each one queue with two jobs (a `visitjob`, a lambda job with behaviours `strategy: youtube_v1, pi: 1, kappa: 1` for `bot_t`, `pi: 0…` for `bot_c`), and per-test job rows: `decision_report: {"requested": ["mainstream_media", "similar"], "applied": [...], "filters": {...}}`, `decision_treatment_lost`, `status_code`. Rows for: ok; unreported (`decision_report` null, status success); mismatch (`requested` lacks `similar`); lost (`decision_treatment_lost: true`); degraded (`applied` ⊂ `requested`, not lost); unfinished (`status_code: running`); missing (queue has one job fewer). Plus `expected_filters` unit rows: youtube_v1 table, `beta: -1 → fringe`, custom strategy default, custom with `x-requested`. Plus `--sample 1` audits one crawler; `render()` contains the (arm, outcome) counts and the first offending job id.

- [ ] **Step 2: RED.** — [ ] **Step 3: Implement.** — [ ] **Step 4: `make check`.** — [ ] **Step 5: Commit** — `pmspec: audit -- the treatment each finished job reported against the lock`.

---

### Task 5: `pmspec audit` — CLI and MCP

**Files:**
- Modify: `experiment_designer/cli.py`, `orchestrator/mcp_server/server.py`
- Test: `experiment_designer/tests/test_lock_cli_workflow.py`, `orchestrator/tests/test_mcp_workflow_tools.py`

**Interfaces:** `pmspec audit EXPERIMENT_ID LOCK [--hours 24] [--sample K] [--strict]` → `render()`, exit `0 if result.ok(strict) else 1`; `ApiError`/`LockError`/`AnchorError` handled like `status`. The strategy schemas for `x-requested` come from the lock? No — the lock carries no schemas; the CLI loads them the way the validator does (`SpecValidator(base_dir=lock dir)._strategy_schema`), so a `strategy_schemas` path or `PMSPEC_STRATEGY_SCHEMA_DIRS` next to the lock works; document that. MCP `audit_experiment(experiment_id, lock_content, hours=24, sample=None, strict=False)` → `{experiment_id, ok, sampled, total, counts: [{arm, outcome, n}], findings: [first 50], render}`, read-only.

- [ ] Tests (patch `audit_experiment` like the `status` tests do), implement, `make check`, commit — `pmspec: audit command and MCP tool`.

---

### Task 6: Docs

- `docs/experiment-designer.md`: lock format 2 (what `parts` is, that `pmspec validate` accepts both, size), the `with:` draw syntax (in "writing a spec" next to `with:`; the without-replacement rule; `select: first|random`), `pmspec audit` in "Step 6: Day to day" with the outcome table and the strategy mapping; CLI table rows. `CLAUDE.md` Experiment Designer paragraph: one sentence each. `.claude/skills/primemover-debug/SKILL.md`: after `experiment_status`, `audit_experiment` for "did the treated bots actually get the treatment". Verify every name against `--help`/`@mcp.tool`. `make check`; commit — `docs: lock format 2, per-bot draws, audit`.

## Not in this plan
Regenerating the autocompleted generator to use per-bot draws (that repo; one-line change in `build_spec.R`: `with = list(query = list(from_pool = "queries", field = "term"), ...)` plus a `queries` data pool from the pilot bank) — do after this merges.
