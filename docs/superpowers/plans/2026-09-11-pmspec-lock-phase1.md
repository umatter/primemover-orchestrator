# pmspec lock + provenance (phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a pmspec experiment reproducible: a committed, self-contained lock file compiled from a spec; upload that writes provenance onto the experiment; `verify` that proves a live experiment is bound to, and structurally identical to, a lock.

**Architecture:** A new package `pmspec/lock/` owns everything downstream of the existing spec→API-JSON conversion: the lock dataclass and canonical YAML I/O, secret placeholders and the TOML store, the schedule anchor arithmetic, provenance gathering, the single uploader (replacing three copies of the upload code), and the verifier. `pmspec/spec/` gains only two constructor options on `SpecConverter`. The `pmspec` CLI, `orchestrator experiment upload` and the MCP server become thin callers of `lock/`.

**Tech Stack:** Python 3.10+, PyYAML, jsonschema, requests (existing); `tomli` (new, only on <3.11). Tests: pytest, `click.testing.CliRunner`, in-memory fakes for HTTP — nothing touches the network.

**Spec:** `docs/superpowers/specs/2026-09-11-pmspec-lock-and-provenance-design.md` (§1–§3 and §6 are phase 1; §4 and §5 are later phases, except `--mode` which is cheap enough to land here).

## Global Constraints

- Line length 100; `make check` (black, ruff `E,F,W,I,N,UP,B,C4`, mypy `--ignore-missing-imports`, pytest) must pass after every task. Run `make hooks` once if not done.
- `requires-python = ">=3.10"`: no `tomllib` without a `tomli` fallback, no `match`, no `Self`.
- Every timestamp the API receives is Europe/Zurich wall clock: use `pmspec.spec.time_utils.API_TIMEZONE` / `format_iso8601`; never `datetime.now()` bare.
- The only secret syntax is `${secret:<dotted.key>}`, key charset `[A-Za-z0-9_.-]`. No command ever prints a resolved value.
- Lock serialization is canonical: fixed key order, `sort_keys=False`, `allow_unicode=True`, `width=10**6`, block style, LF newlines. Same inputs → identical bytes.
- Detect-secrets runs on commit: use the value `testpass` for fake passwords in fixtures (it is on the exclude list).
- MCP write tools: dry-run by default, `_require_local_write_target()` before any mutating request, and the AST test `orchestrator/tests/test_mcp_write_guard.py` must keep passing.
- Commit after every task with the attribution lines from the session's system reminder.

## File structure

| path | responsibility |
|---|---|
| `pmspec/spec/converter.py` (modify) | `SpecConverter(seed=…, resolve_times=…)`: explicit seed overrides `design.seed`; `resolve_times=False` leaves each queue's raw `start_at` string untouched |
| `pmspec/lock/__init__.py` (new) | re-exports |
| `pmspec/lock/model.py` (new) | `Provenance`, `Lock` dataclasses; `LOCK_FORMAT_VERSION = 1`; `to_dict()` / `from_dict()` |
| `pmspec/lock/io.py` (new) | `dump_lock(lock) -> str`, `load_lock(text) -> Lock`, `sha256_text(text) -> str`, `is_lock_document(text) -> bool`, `validate_lock_dict(d) -> list[str]` (JSON schema) |
| `pmspec/schema/lock-1.schema.json` (new) | JSON schema for the lock document |
| `pmspec/lock/secrets.py` (new) | placeholder regex, `find_placeholders`, `find_literal_credentials`, `SecretStore`, `resolve_secrets` |
| `pmspec/lock/anchor.py` (new) | `derive_anchor`, `to_offset`, `resolve_start_at`, `parse_offset` |
| `pmspec/lock/provenance.py` (new) | `git_commit(path)`, `pmspec_version()`, `now_iso()` |
| `pmspec/lock/build.py` (new) | `compile_spec(...) -> CompileResult` |
| `pmspec/lock/api.py` (new) | `ApiSession` (requests-based login/GET/POST/PUT/multipart) — the one HTTP seam for CLI paths |
| `pmspec/lock/upload.py` (new) | `UploadOptions`, `provenance_preferences`, `upload_lock(...) -> UploadResult` |
| `pmspec/lock/verify.py` (new) | `Shape` builders for lock and live API, `compare_shapes`, `verify_lock`, `VerifyResult`, `Difference` |
| `pmspec/cli.py` (modify) | `compile`, `verify`, `secrets check`; `upload`/`generate`/`validate` re-based on `lock/` |
| `orchestrator/cli.py` (modify) | `experiment upload` delegates to `lock.upload.upload_lock` |
| `orchestrator/mcp_server/server.py` (modify) | `compile_experiment_spec`, `verify_experiment`; `validate_experiment_spec` and `upload_experiment` accept locks / `files=` |
| `pyproject.toml` (modify) | `tomli` dependency; package-data for `pmspec/schema/*.json` |
| `docs/pmspec.md` (modify) | new "Locks, provenance and verify" section |
| `pmspec/tests/test_lock_*.py` (new) | one file per module above |
| `orchestrator/tests/test_mcp_lock_tools.py` (new) | the four MCP tools |

Interfaces below are exact; later tasks import them by these names.

---

### Task 1: `SpecConverter` learns an explicit seed and unresolved times

**Files:**
- Modify: `pmspec/spec/converter.py:112-160` (`__init__`, `convert`) and `:407-415` (`_convert_queue`); `pmspec/spec/design_expander.py:205` (`_parse_design` seed precedence)
- Test: `pmspec/tests/test_lock_converter_options.py`

**Interfaces:**
- Produces: `SpecConverter(seed: int | None = None, resolve_times: bool = True)`. With `seed` set, `DesignExpander(seed=seed)` is used regardless of `design.seed`. With `resolve_times=False`, each API queue dict's `start_at` is the raw spec string (`"now"`, `"+5m"`, `"2026-10-01T10:00:00"`), and a missing start is emitted as `"now"`.

- [ ] **Step 1: Write the failing tests**

```python
# pmspec/tests/test_lock_converter_options.py
"""SpecConverter options the lock compiler relies on: an explicit seed that beats
design.seed, and a way to get the queue start strings back *unresolved* so the
lock can turn them into offsets from an anchor."""

from pmspec.spec.converter import SpecConverter
from pmspec.spec.parser import SpecParser

SPEC = """
version: "1.0"
experiment:
  name: Seeded
design:
  seed: 1
  factors:
    arm:
      levels: [control, treated]
      maps_to:
        params.pi: {control: 0, treated: 1}
  parameters:
    alpha:
      distribution: uniform
      min: 0
      max: 100
  replications: 1
crawlers:
  - from_design: true
    name: "bot_{arm}"
    session:
      start: "+5m"
      workflow:
        - visit: https://example.com
        - wait: 2s
"""


def _convert(**kwargs):
    parsed = SpecParser(seed=kwargs.pop("parser_seed", None)).parse(SPEC)
    assert not parsed.errors, parsed.errors
    result = SpecConverter(**kwargs).convert(parsed)
    assert not result.errors, result.errors
    return result


def test_explicit_seed_overrides_design_seed():
    by_design = _convert()
    explicit = _convert(seed=99)
    again = _convert(seed=99)
    draw = lambda r: [c["configuration"]["params"].get("alpha") for c in r.json_data]  # noqa: E731
    assert draw(explicit) == draw(again)
    assert draw(explicit) != draw(by_design)


def test_resolve_times_false_keeps_the_raw_start():
    result = _convert(resolve_times=False)
    starts = {q["start_at"] for c in result.json_data for q in c["queues"]}
    assert starts == {"+5m"}


def test_resolve_times_default_still_resolves():
    result = _convert()
    start = result.json_data[0]["queues"][0]["start_at"]
    assert start.startswith("20") and "+" in start[10:]  # ISO with an offset


def test_missing_start_is_now_when_unresolved():
    spec = SPEC.replace('      start: "+5m"\n', "")
    parsed = SpecParser().parse(spec)
    result = SpecConverter(resolve_times=False).convert(parsed)
    assert result.json_data[0]["queues"][0]["start_at"] == "now"
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest pmspec/tests/test_lock_converter_options.py -v`
Expected: `TypeError: SpecConverter.__init__() got an unexpected keyword argument 'seed'`.

- [ ] **Step 3: Implement**

In `converter.py`:

```python
class SpecConverter:
    def __init__(self, *, seed: int | None = None, resolve_times: bool = True) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        # An explicit seed wins over design.seed so a compile can pin every draw
        # from one number; None keeps today's behaviour (design.seed or unseeded).
        self.seed = seed
        # False leaves queue start strings as written ("+5m", "now", ISO) so the
        # lock compiler can express them as offsets from an anchor.
        self.resolve_times = resolve_times
```

In `convert`, replace the two expander lines:

```python
        design_seed = parsed.design.get("seed") if parsed.design else None
        expander = DesignExpander(seed=self.seed if self.seed is not None else design_seed)
```

In `design_expander.py`, `_parse_design` currently lets `design.seed` beat the constructor (`seed = design.get("seed", self.seed)` near line 205), which contradicts the constructor's own comment. Make the constructor win:

```python
        seed = self.seed if self.seed is not None else design.get("seed")
```

In `_convert_queue`:

```python
        start_at = queue.get("start_at", queue.get("start"))
        if not start_at:
            start_at = "now"
        if self.resolve_times:
            start_at = parse_relative_time(start_at)
```

- [ ] **Step 4: Run tests**

Run: `venv/bin/pytest pmspec/tests/ -q`
Expected: all pass (existing tests unaffected: defaults unchanged).

- [ ] **Step 5: Commit**

```bash
git add pmspec/spec/converter.py pmspec/spec/design_expander.py pmspec/tests/test_lock_converter_options.py
git commit -m "designer: SpecConverter takes an explicit seed and can leave starts unresolved"
```

---

### Task 2: Lock model, canonical I/O and JSON schema

**Files:**
- Create: `pmspec/lock/__init__.py`, `pmspec/lock/model.py`, `pmspec/lock/io.py`, `pmspec/schema/lock-1.schema.json`
- Modify: `pyproject.toml` (package-data)
- Test: `pmspec/tests/test_lock_io.py`

**Interfaces:**
- Produces:
  ```python
  LOCK_FORMAT_VERSION = 1
  @dataclass class Provenance: spec_path: str; spec_sha256: str; generator: str | None; git_commit: str | None; pmspec_version: str; compiled_at: str; seed: int; lax: bool = False
  @dataclass class Lock: provenance: Provenance; experiment: dict[str, Any]  # {name, description, runner_user_id}
                         schedule: dict[str, Any]  # {"anchor": "upload" | ISO wall clock}
                         crawlers: list[dict[str, Any]]  # API shape, start_offset instead of start_at
      def to_dict(self) -> dict[str, Any]; @classmethod def from_dict(cls, d) -> "Lock"
  def dump_lock(lock: Lock) -> str
  def load_lock(text: str) -> Lock            # raises LockError(list[str]) on schema failure
  def validate_lock_dict(d: dict) -> list[str]
  def is_lock_document(text: str) -> bool     # cheap: first non-comment line starts with "pmspec_lock:"
  def sha256_text(text: str) -> str
  class LockError(ValueError): errors: list[str]
  ```

- [ ] **Step 1: Write the failing tests**

```python
# pmspec/tests/test_lock_io.py
"""The lock is the reproducibility artifact, so two properties matter more than
anything else here: dumping is byte-deterministic, and a document that is not
a lock is refused with a message naming what is wrong."""

import hashlib

import pytest

from pmspec.lock.io import (
    LockError,
    dump_lock,
    is_lock_document,
    load_lock,
    sha256_text,
    validate_lock_dict,
)
from pmspec.lock.model import LOCK_FORMAT_VERSION, Lock, Provenance


def make_lock(**overrides) -> Lock:
    crawler = {
        "name": "bot_01",
        "description": "d",
        "configuration": {
            "name": "c",
            "description": "d",
            "active": 1,
            "params": {"pi": 1, "psi": 0, "kappa": 0, "tau": 0},
            "preferences": [{"name": "reliability", "value": "high"}],
        },
        "proxy": {
            "name": "p",
            "description": "d",
            "type": "http",
            "hostname": "pr.oxylabs.io",
            "port": 7777,
            "username": "${secret:oxylabs.a.username}",
            "password": "${secret:oxylabs.a.password}",
            "active": True,
        },
        "agent": {"name": "a", "identification": "mlx", "location": "US-NC", "active": True},
        "queues": [
            {
                "name": "s1",
                "description": "d",
                "start_offset": "+5m",
                "timezone": "Europe/Zurich",
                "timezone_utc_offset": "+02:00",
                "active": True,
                "jobs": [
                    {
                        "name": "visit",
                        "type": "visitjob",
                        "description": "d",
                        "active": True,
                        "behaviors": [{"name": "url", "value": "https://x", "description": "", "active": True}],
                    }
                ],
            }
        ],
    }
    fields = dict(
        provenance=Provenance(
            spec_path="design/spec.yaml",
            spec_sha256="ab" * 32,
            generator="build_spec.R",
            git_commit="a2024c9",
            pmspec_version="0.1.0",
            compiled_at="2026-09-11T14:02:11+02:00",
            seed=42,
        ),
        experiment={"name": "Study", "description": "", "runner_user_id": None},
        schedule={"anchor": "upload"},
        crawlers=[crawler],
    )
    fields.update(overrides)
    return Lock(**fields)


def test_dump_is_deterministic_and_starts_with_the_format_marker():
    a, b = dump_lock(make_lock()), dump_lock(make_lock())
    assert a == b
    assert a.startswith(f"pmspec_lock: {LOCK_FORMAT_VERSION}\n")
    assert "\r" not in a


def test_top_level_key_order_is_fixed():
    text = dump_lock(make_lock())
    keys = [line.split(":")[0] for line in text.splitlines() if line and not line.startswith(" ")]
    assert keys == ["pmspec_lock", "provenance", "experiment", "schedule", "crawlers"]


def test_round_trip():
    lock = make_lock()
    assert load_lock(dump_lock(lock)) == lock


def test_unicode_survives_unescaped():
    lock = make_lock(experiment={"name": "Zürich Ärzte", "description": "", "runner_user_id": None})
    assert "Zürich" in dump_lock(lock)


def test_is_lock_document():
    assert is_lock_document(dump_lock(make_lock()))
    assert is_lock_document("# comment\npmspec_lock: 1\n")
    assert not is_lock_document("version: '1.0'\nexperiment:\n  name: x\n")


def test_schema_rejects_missing_required_keys():
    d = make_lock().to_dict()
    del d["provenance"]["seed"]
    errors = validate_lock_dict(d)
    assert any("seed" in e for e in errors)


def test_schema_rejects_start_at_in_a_lock():
    d = make_lock().to_dict()
    d["crawlers"][0]["queues"][0]["start_at"] = "2026-01-01T00:00:00"
    assert any("start_at" in e for e in validate_lock_dict(d))


def test_load_raises_lock_error_with_all_messages():
    text = dump_lock(make_lock()).replace("pmspec_lock: 1", "pmspec_lock: 99")
    with pytest.raises(LockError) as exc:
        load_lock(text)
    assert exc.value.errors and "pmspec_lock" in exc.value.errors[0]


def test_sha256_text_matches_hashlib_on_utf8_bytes():
    text = dump_lock(make_lock())
    assert sha256_text(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest pmspec/tests/test_lock_io.py -v`
Expected: `ModuleNotFoundError: No module named 'pmspec.lock'`

- [ ] **Step 3: Write the schema**

`pmspec/schema/lock-1.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "pmspec lock, format 1",
  "type": "object",
  "required": ["pmspec_lock", "provenance", "experiment", "schedule", "crawlers"],
  "additionalProperties": false,
  "properties": {
    "pmspec_lock": {"const": 1},
    "provenance": {
      "type": "object",
      "required": ["spec_path", "spec_sha256", "pmspec_version", "compiled_at", "seed"],
      "additionalProperties": false,
      "properties": {
        "spec_path": {"type": "string"},
        "spec_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "generator": {"type": ["string", "null"]},
        "git_commit": {"type": ["string", "null"]},
        "pmspec_version": {"type": "string"},
        "compiled_at": {"type": "string"},
        "seed": {"type": "integer"},
        "lax": {"type": "boolean"}
      }
    },
    "experiment": {
      "type": "object",
      "required": ["name"],
      "additionalProperties": false,
      "properties": {
        "name": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "runner_user_id": {"type": ["integer", "null"]}
      }
    },
    "schedule": {
      "type": "object",
      "required": ["anchor"],
      "additionalProperties": false,
      "properties": {"anchor": {"type": "string", "minLength": 1}}
    },
    "crawlers": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["name", "configuration", "queues"],
        "properties": {
          "name": {"type": "string", "minLength": 1},
          "configuration": {"type": "object"},
          "proxy": {"type": "object"},
          "agent": {"type": "object"},
          "queues": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["name", "start_offset", "jobs"],
              "properties": {
                "start_offset": {"type": "string", "pattern": "^[+-][0-9]+[smhd]$"},
                "start_at": false,
                "jobs": {"type": "array"}
              }
            }
          }
        }
      }
    }
  }
}
```

- [ ] **Step 4: Write model.py**

```python
"""The lock: a fully expanded, self-contained experiment that upload and verify read.

Everything random is already drawn, every pool already inlined. The two things
deliberately *not* resolved are secrets (placeholders, so the file can be
committed) and queue start times (offsets from `schedule.anchor`, so the same
file can be uploaded on a different day)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

LOCK_FORMAT_VERSION = 1


@dataclass
class Provenance:
    spec_path: str
    spec_sha256: str
    generator: str | None
    git_commit: str | None
    pmspec_version: str
    compiled_at: str
    seed: int
    lax: bool = False


@dataclass
class Lock:
    provenance: Provenance
    experiment: dict[str, Any]
    schedule: dict[str, Any]
    crawlers: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        # Key order here IS the canonical order dump_lock() writes.
        return {
            "pmspec_lock": LOCK_FORMAT_VERSION,
            "provenance": asdict(self.provenance),
            "experiment": {
                "name": self.experiment.get("name", ""),
                "description": self.experiment.get("description", ""),
                "runner_user_id": self.experiment.get("runner_user_id"),
            },
            "schedule": {"anchor": self.schedule["anchor"]},
            "crawlers": self.crawlers,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Lock:
        prov = dict(d["provenance"])
        prov.setdefault("generator", None)
        prov.setdefault("git_commit", None)
        prov.setdefault("lax", False)
        return cls(
            provenance=Provenance(**prov),
            experiment=dict(d["experiment"]),
            schedule=dict(d["schedule"]),
            crawlers=list(d["crawlers"]),
        )
```

- [ ] **Step 5: Write io.py**

```python
"""Canonical lock serialization. Same lock in → same bytes out, always."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from .model import LOCK_FORMAT_VERSION, Lock

_SCHEMA_PATH = Path(__file__).parent.parent / "schema" / "lock-1.schema.json"


class LockError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class _CanonicalDumper(yaml.SafeDumper):
    pass


_RESOLVER = yaml.resolver.Resolver()


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    # Quote anything YAML would otherwise re-type (timestamps, "yes", numbers),
    # so a round trip through any YAML reader gives the string back.
    plain = _RESOLVER.resolve(yaml.ScalarNode, value, (True, False))
    style = None if plain == "tag:yaml.org,2002:str" else '"'
    if "\n" in value:
        style = "|"
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_CanonicalDumper.add_representer(str, _represent_str)


def dump_lock(lock: Lock) -> str:
    return yaml.dump(
        lock.to_dict(),
        Dumper=_CanonicalDumper,
        sort_keys=False,
        allow_unicode=True,
        width=10**6,
        default_flow_style=False,
    )


def validate_lock_dict(d: dict[str, Any]) -> list[str]:
    import jsonschema

    with open(_SCHEMA_PATH) as f:
        schema = json.load(f)
    validator = jsonschema.Draft202012Validator(schema)
    errors = []
    for e in sorted(validator.iter_errors(d), key=lambda e: list(e.absolute_path)):
        path = "/".join(str(p) for p in e.absolute_path) or "<root>"
        errors.append(f"{path}: {e.message}")
    if d.get("pmspec_lock") != LOCK_FORMAT_VERSION:
        errors.insert(0, f"pmspec_lock: expected {LOCK_FORMAT_VERSION}, got {d.get('pmspec_lock')!r}")
    return errors


def load_lock(text: str) -> Lock:
    try:
        d = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise LockError([f"YAML parse error: {e}"]) from e
    if not isinstance(d, dict):
        raise LockError(["not a mapping"])
    errors = validate_lock_dict(d)
    if errors:
        raise LockError(errors)
    return Lock.from_dict(d)


def is_lock_document(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped.startswith("pmspec_lock:")
    return False


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
```

`pmspec/lock/__init__.py`:

```python
"""Lock artifacts: compile, secrets, upload with provenance, verify."""

from .io import LockError, dump_lock, is_lock_document, load_lock, sha256_text
from .model import LOCK_FORMAT_VERSION, Lock, Provenance

__all__ = [
    "LOCK_FORMAT_VERSION",
    "Lock",
    "LockError",
    "Provenance",
    "dump_lock",
    "is_lock_document",
    "load_lock",
    "sha256_text",
]
```

- [ ] **Step 6: Package the schema**

In `pyproject.toml`, after `[tool.setuptools.packages.find]`:

```toml
[tool.setuptools.package-data]
pmspec = ["schema/*.json"]
```

- [ ] **Step 7: Run tests**

Run: `venv/bin/pytest pmspec/tests/test_lock_io.py -v`
Expected: PASS. If `test_schema_rejects_start_at_in_a_lock` fails, check that `"start_at": false` is honoured by `Draft202012Validator` (it is; a `false` subschema rejects the property).

- [ ] **Step 8: Commit**

```bash
git add pmspec/lock pmspec/schema/lock-1.schema.json pmspec/tests/test_lock_io.py pyproject.toml
git commit -m "designer: lock model, canonical YAML I/O and schema"
```

---

### Task 3: Secret placeholders and the TOML store

**Files:**
- Create: `pmspec/lock/secrets.py`
- Modify: `pyproject.toml` (dependency)
- Test: `pmspec/tests/test_lock_secrets.py`

**Interfaces:**
- Produces:
  ```python
  PLACEHOLDER_RE = re.compile(r"\$\{secret:([A-Za-z0-9_.-]+)\}")
  SELF_CONFIGURING_PROXY_TYPES = frozenset({"oxylabs"})
  def find_placeholders(obj: Any) -> set[str]                       # every key referenced anywhere in obj
  def find_literal_credentials(crawlers: list[dict]) -> list[str]  # paths like "crawlers[3].proxy.password"
  class SecretStore:
      @classmethod def load(cls, path: Path | None = None) -> "SecretStore"   # default: $PMSPEC_SECRETS_FILE or ~/.config/primemover/secrets.toml
      def get(self, key: str) -> str                                 # KeyError -> MissingSecret(key)
      def missing(self, keys: Iterable[str]) -> list[str]
  class MissingSecret(KeyError); class InsecureSecretsFile(PermissionError)
  def resolve_secrets(obj: Any, store: SecretStore) -> Any          # deep copy with placeholders replaced
  def mask_secrets(obj: Any) -> Any                                 # identity today; exists so callers never print resolved objects
  ```

- [ ] **Step 1: Write the failing tests**

```python
# pmspec/tests/test_lock_secrets.py
"""Secrets never live in a lock and never leak out of a resolve."""

import os
from pathlib import Path

import pytest

from pmspec.lock.secrets import (
    InsecureSecretsFile,
    MissingSecret,
    SecretStore,
    find_literal_credentials,
    find_placeholders,
    resolve_secrets,
)


@pytest.fixture
def store_file(tmp_path: Path) -> Path:
    p = tmp_path / "secrets.toml"
    p.write_text('[oxylabs.us_nc]\nusername = "cust-nc"\npassword = "testpass"\n')
    p.chmod(0o600)
    return p


def test_find_placeholders_walks_nested_structures():
    obj = {"a": [{"b": "${secret:x.y}"}, "plain"], "c": {"d": "${secret:z}"}}
    assert find_placeholders(obj) == {"x.y", "z"}


def test_literal_credentials_are_reported_by_path():
    crawlers = [
        {"proxy": {"type": "http", "username": "${secret:a.u}", "password": "hunter2"}},
        {"proxy": {"type": "oxylabs", "username": "", "password": ""}},
        {"proxy": {"type": "http", "username": "", "password": ""}},
    ]
    assert find_literal_credentials(crawlers) == ["crawlers[0].proxy.password"]


def test_self_configuring_proxy_may_carry_literals():
    crawlers = [{"proxy": {"type": "oxylabs", "username": "acct", "password": "testpass"}}]
    assert find_literal_credentials(crawlers) == []


def test_store_resolves_dotted_keys(store_file: Path):
    store = SecretStore.load(store_file)
    assert store.get("oxylabs.us_nc.username") == "cust-nc"
    assert store.missing(["oxylabs.us_nc.password", "nope.x"]) == ["nope.x"]


def test_missing_key_names_the_key(store_file: Path):
    with pytest.raises(MissingSecret, match="nope.x"):
        SecretStore.load(store_file).get("nope.x")


def test_group_readable_file_is_refused(store_file: Path):
    store_file.chmod(0o640)
    with pytest.raises(InsecureSecretsFile, match="chmod 600"):
        SecretStore.load(store_file)


def test_default_path_comes_from_env(store_file: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PMSPEC_SECRETS_FILE", str(store_file))
    assert SecretStore.load().get("oxylabs.us_nc.password") == "testpass"


def test_resolve_replaces_only_placeholders_and_does_not_mutate(store_file: Path):
    store = SecretStore.load(store_file)
    obj = {"proxy": {"username": "${secret:oxylabs.us_nc.username}", "host": "h"}}
    out = resolve_secrets(obj, store)
    assert out == {"proxy": {"username": "cust-nc", "host": "h"}}
    assert obj["proxy"]["username"].startswith("${secret:")


def test_non_string_leaf_in_store_is_an_error(tmp_path: Path):
    p = tmp_path / "s.toml"
    p.write_text("[a]\nb = 5\n")
    p.chmod(0o600)
    with pytest.raises(MissingSecret, match="a.b"):
        SecretStore.load(p).get("a.b")
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest pmspec/tests/test_lock_secrets.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Add the dependency**

In `pyproject.toml` `dependencies`, after `"jsonschema>=4.0.0",`:

```toml
    "tomli>=2.0.0; python_version < '3.11'",  # lock secrets store; tomllib is 3.11+
```

Then `venv/bin/pip install -e ".[dev]"` (no-op on 3.12 beyond metadata).

- [ ] **Step 4: Implement**

```python
"""Secret placeholders in locks and the file they resolve from at upload time.

A lock says ``${secret:oxylabs.us_nc.password}``; the machine doing the upload
holds a mode-600 TOML file with that key. The value exists in memory for the
duration of one HTTP request and is never written or printed."""

from __future__ import annotations

import copy
import os
import re
import stat
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on the 3.10 CI job
    import tomli as tomllib

PLACEHOLDER_RE = re.compile(r"\$\{secret:([A-Za-z0-9_.-]+)\}")
SELF_CONFIGURING_PROXY_TYPES = frozenset({"oxylabs"})
DEFAULT_SECRETS_FILE = Path.home() / ".config" / "primemover" / "secrets.toml"
_CREDENTIAL_FIELDS = ("username", "password")


class MissingSecret(KeyError):
    pass


class InsecureSecretsFile(PermissionError):
    pass


def find_placeholders(obj: Any) -> set[str]:
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            found.update(PLACEHOLDER_RE.findall(node))
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    return found


def find_literal_credentials(crawlers: list[dict[str, Any]]) -> list[str]:
    """Paths of proxy credentials that are non-empty literals (not placeholders)."""
    paths = []
    for i, crawler in enumerate(crawlers):
        proxy = crawler.get("proxy") or {}
        if not isinstance(proxy, dict) or proxy.get("type") in SELF_CONFIGURING_PROXY_TYPES:
            continue
        for field in _CREDENTIAL_FIELDS:
            value = proxy.get(field)
            if isinstance(value, str) and value and not PLACEHOLDER_RE.fullmatch(value):
                paths.append(f"crawlers[{i}].proxy.{field}")
    return paths


class SecretStore:
    def __init__(self, data: dict[str, Any], source: str) -> None:
        self._data = data
        self.source = source

    @classmethod
    def load(cls, path: Path | None = None) -> SecretStore:
        resolved = path or Path(os.environ.get("PMSPEC_SECRETS_FILE") or DEFAULT_SECRETS_FILE)
        mode = stat.S_IMODE(resolved.stat().st_mode)
        if mode & 0o077:
            raise InsecureSecretsFile(
                f"{resolved} is readable by group/other (mode {mode:o}); run: chmod 600 {resolved}"
            )
        with open(resolved, "rb") as f:
            return cls(tomllib.load(f), str(resolved))

    def get(self, key: str) -> str:
        node: Any = self._data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise MissingSecret(f"{key} (not in {self.source})")
            node = node[part]
        if not isinstance(node, str):
            raise MissingSecret(f"{key} is not a string in {self.source}")
        return node

    def missing(self, keys: Iterable[str]) -> list[str]:
        out = []
        for key in sorted(set(keys)):
            try:
                self.get(key)
            except MissingSecret:
                out.append(key)
        return out


def resolve_secrets(obj: Any, store: SecretStore) -> Any:
    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return PLACEHOLDER_RE.sub(lambda m: store.get(m.group(1)), node)
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(copy.deepcopy(obj))


def mask_secrets(obj: Any) -> Any:
    """Locks already hold placeholders; this exists so every printer calls the
    same function and a future resolved-object printer cannot forget to."""
    return obj
```

- [ ] **Step 5: Run tests**

Run: `venv/bin/pytest pmspec/tests/test_lock_secrets.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pmspec/lock/secrets.py pmspec/tests/test_lock_secrets.py pyproject.toml
git commit -m "designer: secret placeholders and the mode-600 TOML store"
```

---

### Task 4: Schedule anchor and offsets

**Files:**
- Create: `pmspec/lock/anchor.py`
- Test: `pmspec/tests/test_lock_anchor.py`

**Interfaces:**
- Produces:
  ```python
  UPLOAD_ANCHOR = "upload"
  class AnchorError(ValueError)
  def parse_offset(text: str) -> timedelta                       # "+5m" -> 5 min; ValueError otherwise
  def format_offset(delta: timedelta) -> str                     # 300s -> "+5m"; 90s -> "+90s"; negative allowed
  def derive_anchor(raw_starts: list[str], override: str | None = None) -> tuple[str, list[str]]
      # returns (anchor, offsets) — anchor is "upload" or a naive Europe/Zurich wall-clock ISO string;
      # raises AnchorError on mixed relative/absolute or unparseable values
  def resolve_anchor(anchor: str, now: datetime | None = None) -> datetime   # tz-aware in API_TIMEZONE
  def resolve_start_at(anchor_dt: datetime, offset: str) -> str  # format_iso8601(anchor_dt + parse_offset(offset))
  ```

- [ ] **Step 1: Write the failing tests**

```python
# pmspec/tests/test_lock_anchor.py
"""Queue starts in a lock are offsets from one anchor, so the same lock can be
uploaded on any day and every session keeps its spacing."""

from datetime import datetime, timedelta

import pytest

from pmspec.lock.anchor import (
    UPLOAD_ANCHOR,
    AnchorError,
    derive_anchor,
    format_offset,
    parse_offset,
    resolve_anchor,
    resolve_start_at,
)
from pmspec.spec.time_utils import API_TIMEZONE


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("+5m", 300), ("+30s", 30), ("+2h", 7200), ("+1d", 86400), ("-10m", -600), ("+0s", 0)],
)
def test_parse_and_format_offset_round_trip(text, seconds):
    assert parse_offset(text) == timedelta(seconds=seconds)
    assert parse_offset(format_offset(timedelta(seconds=seconds))) == timedelta(seconds=seconds)


def test_format_offset_picks_the_largest_exact_unit():
    assert format_offset(timedelta(seconds=300)) == "+5m"
    assert format_offset(timedelta(seconds=90)) == "+90s"
    assert format_offset(timedelta(days=2)) == "+2d"


def test_relative_only_spec_anchors_at_upload():
    anchor, offsets = derive_anchor(["now", "+5m", "+9m"])
    assert anchor == UPLOAD_ANCHOR
    assert offsets == ["+0s", "+5m", "+9m"]


def test_absolute_only_spec_anchors_at_the_earliest():
    anchor, offsets = derive_anchor(["2026-10-01T12:00:00", "2026-10-01T10:00:00"])
    assert anchor == "2026-10-01T10:00:00"
    assert offsets == ["+2h", "+0s"]


def test_absolute_with_zone_is_converted_to_zurich_wall_clock():
    anchor, offsets = derive_anchor(["2026-07-15T03:00:00Z"])
    assert anchor == "2026-07-15T05:00:00"
    assert offsets == ["+0s"]


def test_mixed_relative_and_absolute_is_an_error():
    with pytest.raises(AnchorError, match="mixed"):
        derive_anchor(["+5m", "2026-10-01T10:00:00"])


def test_unparseable_start_is_an_error():
    with pytest.raises(AnchorError, match="tomorrow"):
        derive_anchor(["tomorrow"])


def test_override_wins_and_rebases_offsets():
    anchor, offsets = derive_anchor(["2026-10-01T10:00:00"], override="2026-10-01T09:00:00")
    assert anchor == "2026-10-01T09:00:00"
    assert offsets == ["+1h"]


def test_override_upload_keeps_spacing_of_absolute_starts():
    anchor, offsets = derive_anchor(["2026-10-01T12:00:00", "2026-10-01T10:00:00"], override=UPLOAD_ANCHOR)
    assert anchor == UPLOAD_ANCHOR and offsets == ["+2h", "+0s"]


def test_resolve_anchor_upload_is_now_in_the_apis_zone():
    fixed = datetime(2026, 10, 1, 10, 0, tzinfo=API_TIMEZONE)
    assert resolve_anchor(UPLOAD_ANCHOR, now=fixed) == fixed


def test_resolve_anchor_fixed_is_naive_wall_clock_in_zurich():
    dt = resolve_anchor("2026-10-01T10:00:00")
    assert dt.tzinfo is not None and dt.utcoffset() == timedelta(hours=2)


def test_resolve_start_at_across_dst_keeps_wall_clock_arithmetic():
    # 2026-10-25 02:00 CEST -> 03:00 CET is the autumn change; a +1d offset must
    # land on the next day's same wall-clock hour, not 25 hours later.
    anchor = resolve_anchor("2026-10-24T10:00:00")
    assert resolve_start_at(anchor, "+1d") == "2026-10-25T10:00:00+01:00"
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest pmspec/tests/test_lock_anchor.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
"""One anchor, many offsets. See the spec §1 "schedule.anchor + start_offset"."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from pmspec.spec.time_utils import (
    API_TIMEZONE,
    _to_api_wall_clock,
    format_iso8601,
    is_relative_time,
)

UPLOAD_ANCHOR = "upload"
_OFFSET_RE = re.compile(r"^([+-])(\d+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class AnchorError(ValueError):
    pass


def parse_offset(text: str) -> timedelta:
    m = _OFFSET_RE.match(text.strip().lower())
    if not m:
        raise ValueError(f"not an offset: {text!r}")
    sign, amount, unit = m.groups()
    seconds = int(amount) * _UNIT_SECONDS[unit]
    return timedelta(seconds=-seconds if sign == "-" else seconds)


def format_offset(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    sign = "-" if total < 0 else "+"
    total = abs(total)
    for unit in ("d", "h", "m"):
        if total and total % _UNIT_SECONDS[unit] == 0:
            return f"{sign}{total // _UNIT_SECONDS[unit]}{unit}"
    return f"{sign}{total}s"


def _parse_absolute(value: str) -> datetime:
    """Naive Europe/Zurich wall clock as a tz-aware datetime."""
    wall = _to_api_wall_clock(value)
    try:
        parsed = datetime.fromisoformat(wall)
    except ValueError as e:
        raise AnchorError(f"cannot parse start time {value!r}") from e
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(API_TIMEZONE).replace(tzinfo=None)
    return parsed.replace(tzinfo=API_TIMEZONE)


def _wall_clock(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def derive_anchor(raw_starts: list[str], override: str | None = None) -> tuple[str, list[str]]:
    relative = [s for s in raw_starts if is_relative_time(s)]
    absolute = [s for s in raw_starts if not is_relative_time(s)]
    if relative and absolute:
        raise AnchorError(
            "mixed relative and absolute session starts; use one kind, or pass --anchor"
        )

    if absolute:
        instants = [_parse_absolute(s) for s in absolute]
        if override == UPLOAD_ANCHOR:
            # Keep the spacing, drop the date: offsets from the earliest, anchored at upload.
            base = min(instants)
            return UPLOAD_ANCHOR, [format_offset(_naive_delta(base, dt)) for dt in instants]
        anchor_dt = _parse_absolute(override) if override else min(instants)
        offsets = [format_offset(_naive_delta(anchor_dt, dt)) for dt in instants]
        return _wall_clock(anchor_dt), offsets

    offsets = []
    for s in relative:
        offsets.append("+0s" if s.strip().lower() == "now" else format_offset(parse_offset(s)))
    if override and override != UPLOAD_ANCHOR:
        return _wall_clock(_parse_absolute(override)), offsets
    return UPLOAD_ANCHOR, offsets


def _naive_delta(a: datetime, b: datetime) -> timedelta:
    # Wall-clock difference: the API stores digits, not instants.
    return b.replace(tzinfo=None) - a.replace(tzinfo=None)


def resolve_anchor(anchor: str, now: datetime | None = None) -> datetime:
    if anchor == UPLOAD_ANCHOR:
        return (now or datetime.now(API_TIMEZONE)).replace(microsecond=0)
    return _parse_absolute(anchor)


def resolve_start_at(anchor_dt: datetime, offset: str) -> str:
    naive = anchor_dt.replace(tzinfo=None) + parse_offset(offset)
    return format_iso8601(naive.replace(tzinfo=API_TIMEZONE))
```

`is_relative_time("tomorrow")` is False, so it reaches `_parse_absolute`, whose `fromisoformat` raises → `AnchorError("cannot parse start time 'tomorrow'")`.

- [ ] **Step 4: Run tests**

Run: `venv/bin/pytest pmspec/tests/test_lock_anchor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pmspec/lock/anchor.py pmspec/tests/test_lock_anchor.py
git commit -m "designer: schedule anchor and start offsets for locks"
```

---

### Task 5: Provenance helpers and the compiler

**Files:**
- Create: `pmspec/lock/provenance.py`, `pmspec/lock/build.py`
- Test: `pmspec/tests/test_lock_build.py`

**Interfaces:**
- Consumes: Task 1 `SpecConverter(seed=, resolve_times=False)`; Task 2 `Lock`, `Provenance`, `dump_lock`, `sha256_text`; Task 3 `find_literal_credentials`; Task 4 `derive_anchor`, `AnchorError`.
- Produces:
  ```python
  # provenance.py
  def git_commit(path: Path) -> str | None      # short HEAD, "-dirty" suffix if `git status --porcelain` non-empty; None if not a repo / git missing
  def pmspec_version() -> str                   # importlib.metadata.version("primemover-orchestrator"), else "unknown"
  def now_iso() -> str                          # format_iso8601(datetime.now(API_TIMEZONE))
  # build.py
  @dataclass class CompileResult: lock: Lock | None; errors: list[str]; warnings: list[str]
  def compile_spec(spec_text: str, *, base_dir: Path, spec_path: str, seed: int | None,
                   anchor: str | None = None, generator: str | None = None, lax: bool = False,
                   git_root: Path | None = None) -> CompileResult
  ```

- [ ] **Step 1: Write the failing tests**

```python
# pmspec/tests/test_lock_build.py
"""compile: spec + seed -> lock. Deterministic, refuses what cannot be committed."""

import subprocess
from pathlib import Path

import pytest

from pmspec.lock.build import compile_spec
from pmspec.lock.io import dump_lock
from pmspec.lock.provenance import git_commit

SPEC = """
version: "1.0"
experiment:
  name: Compiled
  generator: build_spec.R
design:
  factors:
    arm:
      levels: [control, treated]
      maps_to:
        params.pi: {control: 0, treated: 1}
  parameters:
    alpha:
      distribution: uniform
      min: 0
      max: 1
  replications: 2
data_pools:
  outlets:
    type: csv
    source: outlets.csv
crawlers:
  - from_design: true
    name: "bot_{arm}_{replica:02d}"
    proxy:
      type: http
      hostname: pr.oxylabs.io
      port: 7777
      username: "${secret:oxylabs.u}"
      password: "${secret:oxylabs.p}"
    session:
      start: "+5m"
      workflow:
        - visit: https://example.com
        - wait: 2s
"""


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    (tmp_path / "outlets.csv").write_text("domain,lean\nnyt.com,L\nfox.com,R\n")
    (tmp_path / "spec.yaml").write_text(SPEC)
    return tmp_path


def _compile(spec_dir: Path, text: str = SPEC, **kw):
    kw.setdefault("seed", 42)
    return compile_spec(text, base_dir=spec_dir, spec_path="spec.yaml", **kw)


def test_compile_is_byte_deterministic(spec_dir: Path):
    a = _compile(spec_dir)
    b = _compile(spec_dir)
    assert not a.errors and a.lock is not None
    a.lock.provenance.compiled_at = b.lock.provenance.compiled_at = "fixed"
    assert dump_lock(a.lock) == dump_lock(b.lock)


def test_different_seed_changes_draws_not_structure(spec_dir: Path):
    a = _compile(spec_dir, seed=1).lock
    b = _compile(spec_dir, seed=2).lock
    names = lambda lk: [c["name"] for c in lk.crawlers]  # noqa: E731
    draws = lambda lk: [c["configuration"]["params"]["alpha"] for c in lk.crawlers]  # noqa: E731
    assert names(a) == names(b) and len(names(a)) == 4
    assert draws(a) != draws(b)


def test_unseeded_compile_is_refused(spec_dir: Path):
    result = _compile(spec_dir, seed=None)
    assert result.lock is None
    assert any("seed" in e for e in result.errors)


def test_design_seed_is_used_when_no_flag(spec_dir: Path):
    text = SPEC.replace("design:\n", "design:\n  seed: 7\n")
    result = _compile(spec_dir, text, seed=None)
    assert result.lock is not None and result.lock.provenance.seed == 7


def test_literal_credential_is_refused(spec_dir: Path):
    text = SPEC.replace('password: "${secret:oxylabs.p}"', 'password: "hunter2"')
    result = _compile(spec_dir, text)
    assert result.lock is None
    assert any("crawlers[0].proxy.password" in e for e in result.errors)


def test_relative_starts_become_offsets_from_upload(spec_dir: Path):
    lock = _compile(spec_dir).lock
    assert lock.schedule == {"anchor": "upload"}
    assert {q["start_offset"] for c in lock.crawlers for q in c["queues"]} == {"+5m"}
    assert all("start_at" not in q for c in lock.crawlers for q in c["queues"])


def test_mixed_starts_are_a_compile_error(spec_dir: Path):
    text = SPEC + """
  - name: fixed_bot
    session:
      start: "2026-10-01T10:00:00"
      workflow:
        - wait: 1s
"""
    result = _compile(spec_dir, text)
    assert result.lock is None and any("mixed" in e for e in result.errors)


def test_provenance_header(spec_dir: Path):
    lock = _compile(spec_dir).lock
    p = lock.provenance
    assert p.spec_path == "spec.yaml"
    assert len(p.spec_sha256) == 64
    assert p.generator == "build_spec.R"
    assert p.seed == 42 and p.lax is False
    assert p.git_commit is None  # tmp_path is not a repo


def test_generator_flag_overrides_spec(spec_dir: Path):
    lock = _compile(spec_dir, generator="hand").lock
    assert lock.provenance.generator == "hand"


def test_git_commit_reports_dirty(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    assert git_commit(tmp_path) is not None and not git_commit(tmp_path).endswith("-dirty")
    (tmp_path / "x").write_text("x")
    assert git_commit(tmp_path).endswith("-dirty")


def test_git_commit_none_outside_a_repo(tmp_path: Path):
    assert git_commit(tmp_path) is None


def test_experiment_block_and_runner(spec_dir: Path):
    text = SPEC.replace("  name: Compiled\n", "  name: Compiled\n  runner_user_id: 3\n")
    lock = _compile(spec_dir, text).lock
    assert lock.experiment == {"name": "Compiled", "description": "", "runner_user_id": 3}
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest pmspec/tests/test_lock_build.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write provenance.py**

```python
"""Facts about where a lock came from. Cheap, best-effort, never raises."""

from __future__ import annotations

import subprocess
from datetime import datetime
from importlib import metadata
from pathlib import Path

from pmspec.spec.time_utils import API_TIMEZONE, format_iso8601


def git_commit(path: Path) -> str | None:
    try:
        head = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return f"{head}-dirty" if status.strip() else head


def pmspec_version() -> str:
    try:
        return metadata.version("primemover-orchestrator")
    except metadata.PackageNotFoundError:
        return "unknown"


def now_iso() -> str:
    return format_iso8601(datetime.now(API_TIMEZONE).replace(microsecond=0))
```

- [ ] **Step 4: Write build.py**

```python
"""spec -> lock. Everything random is drawn here, once, from one seed."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pmspec.spec.converter import SpecConverter
from pmspec.spec.parser import SpecParser

from .anchor import AnchorError, derive_anchor
from .io import sha256_text
from .model import Lock, Provenance
from .provenance import git_commit, now_iso, pmspec_version
from .secrets import find_literal_credentials


@dataclass
class CompileResult:
    lock: Lock | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def compile_spec(
    spec_text: str,
    *,
    base_dir: Path,
    spec_path: str,
    seed: int | None,
    anchor: str | None = None,
    generator: str | None = None,
    lax: bool = False,
    git_root: Path | None = None,
) -> CompileResult:
    # Pools/file selections draw from seed+1, the design expander from seed:
    # two independent streams, one recorded number.
    parser = SpecParser(base_dir=base_dir, seed=None if seed is None else seed + 1)
    parsed = parser.parse(spec_text)
    if parsed.errors:
        return CompileResult(None, list(parsed.errors), list(parsed.warnings))

    effective_seed = seed if seed is not None else (parsed.design or {}).get("seed")
    if effective_seed is None:
        return CompileResult(
            None,
            ["no seed: pass --seed N or set design.seed; a lock is never unseeded"],
            list(parsed.warnings),
        )
    if seed is None:
        # design.seed only: re-parse so the pool stream is seeded from it too.
        parser = SpecParser(base_dir=base_dir, seed=effective_seed + 1)
        parsed = parser.parse(spec_text)

    converter = SpecConverter(seed=effective_seed, resolve_times=False)
    result = converter.convert(parsed)
    errors = list(result.errors)
    warnings = list(result.warnings)

    crawlers: list[dict[str, Any]] = result.json_data
    literal = find_literal_credentials(crawlers)
    if literal:
        errors.extend(
            f"{path}: literal credential; use ${{secret:<key>}} so the lock can be committed"
            for path in literal
        )

    raw_starts = [q["start_at"] for c in crawlers for q in c.get("queues", [])]
    try:
        anchor_value, offsets = derive_anchor(raw_starts, override=anchor)
    except AnchorError as e:
        errors.append(str(e))
        anchor_value, offsets = "upload", []

    if errors:
        return CompileResult(None, errors, warnings)

    it = iter(offsets)
    for crawler in crawlers:
        for queue in crawler.get("queues", []):
            queue.pop("start_at", None)
            queue["start_offset"] = next(it)
            # Canonical queue key order: name, description, start_offset, then the rest.
            ordered = {k: queue[k] for k in ("name", "description", "start_offset") if k in queue}
            ordered.update({k: v for k, v in queue.items() if k not in ordered})
            queue.clear()
            queue.update(ordered)

    provenance = Provenance(
        spec_path=spec_path,
        spec_sha256=sha256_text(spec_text),
        generator=generator or parsed.experiment.get("generator"),
        git_commit=git_commit(git_root or base_dir),
        pmspec_version=pmspec_version(),
        compiled_at=now_iso(),
        seed=int(effective_seed),
        lax=lax,
    )
    lock = Lock(
        provenance=provenance,
        experiment={
            "name": result.experiment_name,
            "description": result.experiment_description,
            "runner_user_id": result.experiment_runner_user_id,
        },
        schedule={"anchor": anchor_value},
        crawlers=crawlers,
    )
    return CompileResult(lock, [], warnings)
```

Note `experiment.generator` is a new optional spec key. If `SpecValidator`'s JSON schema (`pmspec/schema/experiment-spec.schema.json`) has `additionalProperties: false` on `experiment`, add `"generator": {"type": "string"}` there; the parser itself ignores unknown keys.

- [ ] **Step 5: Run tests**

Run: `venv/bin/pytest pmspec/tests/test_lock_build.py -v`
Expected: PASS. If `test_different_seed_changes_draws_not_structure` fails because both draws are equal, the expander's `_parse_design` seed precedence from Task 1 was not applied.

- [ ] **Step 6: Commit**

```bash
git add pmspec/lock/provenance.py pmspec/lock/build.py pmspec/tests/test_lock_build.py pmspec/schema/experiment-spec.schema.json
git commit -m "designer: compile a spec into a lock with provenance"
```

---

### Task 6: `pmspec compile`, and `validate`/`generate` understand locks

**Files:**
- Modify: `pmspec/cli.py` (`validate` at `:54-168`, `generate` at `:169-217`, new `compile`)
- Test: `pmspec/tests/test_lock_cli_compile.py`

**Interfaces:**
- Consumes: Task 5 `compile_spec`; Task 2 `dump_lock`, `load_lock`, `is_lock_document`, `LockError`; Task 3 `find_placeholders`, `SecretStore`, `resolve_secrets`; Task 4 `resolve_anchor`, `resolve_start_at`.
- Produces: `pmspec compile FILE [-o OUT] [--seed N] [--anchor A] [--generator T] [--lax]` (default OUT = `<stem>.lock.yaml` beside FILE; exit 1 on errors). `pmspec validate FILE` accepts a lock (prints `Lock: valid` + counts). `pmspec generate FILE [--resolve-secrets]` works from a spec or a lock.
- Also produces a helper other tasks reuse: `pmspec/lock/resolve.py` with
  `def resolve_for_upload(lock: Lock, *, store: SecretStore | None, now: datetime | None = None) -> tuple[list[dict], datetime]` — deep-copies crawlers, turns `start_offset` into `start_at` from the resolved anchor, resolves secrets when a store is given (raises `MissingSecret`), and returns `(api_crawlers, anchor_dt)`.

- [ ] **Step 1: Write the failing tests**

```python
# pmspec/tests/test_lock_cli_compile.py
from pathlib import Path

from click.testing import CliRunner

from pmspec.cli import cli
from pmspec.lock.io import is_lock_document, load_lock

SPEC = """
version: "1.0"
experiment:
  name: CLI
crawlers:
  - name: bot
    session:
      start: "+5m"
      workflow:
        - visit: https://example.com
"""


def _write(tmp_path: Path) -> Path:
    p = tmp_path / "study.yaml"
    p.write_text(SPEC)
    return p


def test_compile_writes_sibling_lock(tmp_path: Path):
    spec = _write(tmp_path)
    out = CliRunner().invoke(cli, ["compile", str(spec), "--seed", "3"])
    assert out.exit_code == 0, out.output
    lock_path = tmp_path / "study.lock.yaml"
    assert is_lock_document(lock_path.read_text())
    assert load_lock(lock_path.read_text()).provenance.seed == 3
    assert "study.lock.yaml" in out.output


def test_compile_without_seed_fails_loudly(tmp_path: Path):
    out = CliRunner().invoke(cli, ["compile", str(_write(tmp_path))])
    assert out.exit_code == 1 and "seed" in out.output


def test_validate_accepts_a_lock(tmp_path: Path):
    spec = _write(tmp_path)
    CliRunner().invoke(cli, ["compile", str(spec), "--seed", "3"])
    out = CliRunner().invoke(cli, ["validate", str(tmp_path / "study.lock.yaml")])
    assert out.exit_code == 0, out.output
    assert "Lock" in out.output and "Crawlers:   1" in out.output


def test_validate_reports_lock_schema_errors(tmp_path: Path):
    bad = tmp_path / "bad.lock.yaml"
    bad.write_text("pmspec_lock: 1\nprovenance: {}\n")
    out = CliRunner().invoke(cli, ["validate", str(bad)])
    assert out.exit_code == 1 and "provenance" in out.output


def test_generate_from_lock_resolves_start_at_and_keeps_placeholders(tmp_path: Path):
    spec = _write(tmp_path).read_text().replace(
        "  - name: bot\n",
        '  - name: bot\n    proxy: {type: http, hostname: h, port: 1, username: "${secret:a.u}", password: "${secret:a.p}"}\n',
    )
    (tmp_path / "s.yaml").write_text(spec)
    CliRunner().invoke(cli, ["compile", str(tmp_path / "s.yaml"), "--seed", "1"])
    out = CliRunner().invoke(cli, ["generate", str(tmp_path / "s.lock.yaml")])
    assert out.exit_code == 0, out.output
    assert "${secret:a.u}" in out.output and "start_at" in out.output and "start_offset" not in out.output
    assert "unresolved" in out.output.lower() or "placeholder" in out.output.lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest pmspec/tests/test_lock_cli_compile.py -v`
Expected: `compile` unknown command; lock validate fails on YAML shape.

- [ ] **Step 3: Write resolve.py**

```python
# pmspec/lock/resolve.py
"""Turn a lock into the API's upload JSON: anchor -> start_at, placeholders -> values."""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from .anchor import resolve_anchor, resolve_start_at
from .model import Lock
from .secrets import SecretStore, resolve_secrets


def resolve_for_upload(
    lock: Lock, *, store: SecretStore | None, now: datetime | None = None
) -> tuple[list[dict[str, Any]], datetime]:
    anchor_dt = resolve_anchor(lock.schedule["anchor"], now=now)
    crawlers = copy.deepcopy(lock.crawlers)
    for crawler in crawlers:
        for queue in crawler.get("queues", []):
            queue["start_at"] = resolve_start_at(anchor_dt, queue.pop("start_offset"))
    if store is not None:
        crawlers = resolve_secrets(crawlers, store)
    return crawlers, anchor_dt
```

- [ ] **Step 4: Add `compile` and re-base `validate` / `generate`**

In `cli.py`, add imports near the others: `from .lock.build import compile_spec`, `from .lock.io import LockError, dump_lock, is_lock_document, load_lock`, `from .lock.resolve import resolve_for_upload`, `from .lock.secrets import SecretStore, find_placeholders`.

New command:

```python
@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("--output", "-o", type=click.Path(), help="Lock path (default: <stem>.lock.yaml)")
@click.option("--seed", "-s", type=int, help="The one seed every draw derives from")
@click.option("--anchor", help="Schedule anchor: 'upload' or a Europe/Zurich wall-clock ISO time")
@click.option("--generator", help="Free-text provenance: what produced the spec")
@click.option("--lax", is_flag=True, help="Downgrade strict-contract errors to warnings (recorded)")
def compile(file: str, output: str | None, seed: int | None, anchor: str | None,
            generator: str | None, lax: bool):
    """Compile a spec into a self-contained, committable lock file."""
    file_path = Path(file)
    result = compile_spec(
        file_path.read_text(),
        base_dir=file_path.parent,
        spec_path=str(file_path),
        seed=seed,
        anchor=anchor,
        generator=generator,
        lax=lax,
    )
    for warning in result.warnings:
        click.secho(f"  WARNING: {warning}", fg="yellow")
    if result.lock is None:
        click.secho("Compile FAILED", fg="red", bold=True)
        for error in result.errors:
            click.secho(f"  ERROR: {error}", fg="red")
        sys.exit(1)
    out_path = Path(output) if output else file_path.with_name(f"{file_path.stem}.lock.yaml")
    out_path.write_text(dump_lock(result.lock))
    lock = result.lock
    click.secho(f"Compiled: {out_path}", fg="green")
    click.echo(f"  Crawlers: {len(lock.crawlers)}")
    click.echo(f"  Seed:     {lock.provenance.seed}")
    click.echo(f"  Anchor:   {lock.schedule['anchor']}")
    click.echo(f"  Commit:   {lock.provenance.git_commit or 'none'}")
```

At the top of `validate`, before reading YAML with the file-tag loader:

```python
    content = Path(file).read_text()
    if is_lock_document(content):
        try:
            lock = load_lock(content)
        except LockError as e:
            click.secho("Lock validation FAILED", fg="red", bold=True)
            for error in e.errors:
                click.secho(f"  ERROR: {error}", fg="red")
            sys.exit(1)
        click.secho("Lock: valid", fg="green", bold=True)
        click.echo(f"Experiment: {lock.experiment['name']}")
        click.echo(f"Crawlers:   {len(lock.crawlers)}")
        click.echo(f"Total Jobs: {sum(len(q['jobs']) for c in lock.crawlers for q in c['queues'])}")
        click.echo(f"Anchor:     {lock.schedule['anchor']}")
        sys.exit(0)
```

Replace `generate`:

```python
@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("--output", "-o", type=click.Path(), help="Output file (default: stdout)")
@click.option("--pretty/--compact", default=True, help="Pretty-print JSON")
@click.option("--seed", "-s", type=int, help="Only when FILE is a spec: the seed to compile with")
@click.option("--resolve-secrets", is_flag=True, help="Resolve ${secret:...} from the secrets store")
def generate(file: str, output: str | None, pretty: bool, seed: int | None, resolve_secrets: bool):
    """Generate API upload JSON from a spec or a lock. Prefer `compile` + `upload`;
    this output carries no provenance and is only for the GUI's upload form."""
    file_path = Path(file)
    content = file_path.read_text()
    if is_lock_document(content):
        try:
            lock = load_lock(content)
        except LockError as e:
            for error in e.errors:
                click.secho(f"  ERROR: {error}", fg="red")
            sys.exit(1)
    else:
        result = compile_spec(content, base_dir=file_path.parent, spec_path=str(file_path), seed=seed)
        if result.lock is None:
            click.secho("Cannot generate: compile errors", fg="red")
            for error in result.errors:
                click.secho(f"  {error}", fg="red")
            sys.exit(1)
        lock = result.lock

    store = SecretStore.load() if resolve_secrets else None
    crawlers, _ = resolve_for_upload(lock, store=store)
    if store is None and find_placeholders(crawlers):
        n = len(find_placeholders(crawlers))
        click.secho(f"WARNING: {n} secret placeholder(s) left unresolved (pass --resolve-secrets)", fg="yellow", err=True)
    json_output = json.dumps(crawlers, indent=2 if pretty else None, ensure_ascii=False)
    if output:
        Path(output).write_text(json_output)
        click.secho(f"Generated: {output}", fg="green")
        click.echo(f"  Crawlers: {len(crawlers)}")
    else:
        click.echo(json_output)
```

`test_generate_from_lock_resolves_start_at_and_keeps_placeholders` reads `out.output`, which in `CliRunner` mixes stderr in by default, so the warning is visible there.

- [ ] **Step 5: Run tests**

Run: `venv/bin/pytest pmspec/tests/ -q`
Expected: PASS, including the pre-existing CLI tests in `test_spec.py` (generate's output JSON for a spec is unchanged apart from needing `--seed` when the spec has no `design.seed` — if an existing test calls `generate` on a seedless spec, update that test to pass `--seed 1` and say so in the commit message).

- [ ] **Step 6: Commit**

```bash
git add pmspec/cli.py pmspec/lock/resolve.py pmspec/tests/test_lock_cli_compile.py
git commit -m "pmspec: compile command; validate and generate read locks"
```

---

### Task 7: The one uploader — `ApiSession`, provenance preferences, mode

**Files:**
- Create: `pmspec/lock/api.py`, `pmspec/lock/upload.py`
- Test: `pmspec/tests/test_lock_upload.py`

**Interfaces:**
- Consumes: Task 2 `Lock`, `sha256_text`; Task 3 `SecretStore`, `find_placeholders`, `MissingSecret`; Task 6 `resolve_for_upload`.
- Produces:
  ```python
  # api.py
  class ApiError(RuntimeError): status: int | None; detail: str
  class ApiSession:
      def __init__(self, base_url: str, email: str, password: str, timeout: float = 60.0)
      def get(self, path: str, params: dict | None = None) -> Any        # unwraps {"data": ...}
      def post(self, path: str, json: dict) -> Any
      def put(self, path: str, json: dict) -> Any
      def post_file(self, path: str, field: str, filename: str, content: str, content_type: str) -> Any
      # login lazily on first call; ApiError on any non-2xx with the body's message
  # upload.py
  MODES = ("real", "test"); TEST_RUNNER_USER_ID = 3
  @dataclass class UploadOptions: mode: str; experiment_id: int | None = None; allow_dirty: bool = False; allow_past: bool = False; contact: str = "pmspec"; now: datetime | None = None; uploaded_by: str | None = None
  @dataclass class UploadResult: experiment_id: int; experiment_name: str; crawler_count: int; total_jobs: int; lock_sha256: str; anchor: str; mode: str; runner_user_id: int | None; record_line: str
  class UploadRefused(ValueError)
  def provenance_preferences(lock: Lock, *, lock_sha256: str, anchor_iso: str, mode: str, uploaded_at: str, uploaded_by: str, label: str = "upload 1") -> list[dict[str, str]]
  def effective_experiment(lock: Lock, mode: str, today: date) -> tuple[str, int | None]   # (name, runner_user_id) per the §5 table
  def upload_lock(lock_text: str, *, session: ApiSession, store: SecretStore | None, options: UploadOptions) -> UploadResult
  ```

- [ ] **Step 1: Write the failing tests**

```python
# pmspec/tests/test_lock_upload.py
"""Upload reads locks only, resolves secrets and the anchor in memory, and writes
provenance onto the experiment. A fake ApiSession records every call."""

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from pmspec.lock.io import dump_lock, sha256_text
from pmspec.lock.secrets import MissingSecret, SecretStore
from pmspec.lock.upload import (
    TEST_RUNNER_USER_ID,
    UploadOptions,
    UploadRefused,
    effective_experiment,
    provenance_preferences,
    upload_lock,
)
from pmspec.spec.time_utils import API_TIMEZONE

from .test_lock_io import make_lock


class FakeSession:
    base_url = "http://fake"

    def __init__(self, fail_upload: bool = False) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.fail_upload = fail_upload
        self.next_id = 181

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path.startswith("experiments/"):
            return {"id": int(path.split("/")[1]), "name": "Study", "user_id": 5,
                    "preferences": [{"name": "pmspec.lock_sha256", "value": "old"}]}
        return {}

    def post(self, path, json):
        self.calls.append(("POST", path, json))
        if path == "experiments":
            return {"id": self.next_id, "name": json["name"]}
        return {}

    def put(self, path, json):
        self.calls.append(("PUT", path, json))
        return {"id": int(path.split("/")[1])}

    def post_file(self, path, field, filename, content, content_type):
        self.calls.append(("POST_FILE", path, content))
        if self.fail_upload:
            from pmspec.lock.api import ApiError
            raise ApiError("upload failed", status=422, detail="bad json")
        return {}


@pytest.fixture
def store(tmp_path: Path) -> SecretStore:
    p = tmp_path / "s.toml"
    p.write_text('[oxylabs.a]\nusername = "u"\npassword = "testpass"\n')
    p.chmod(0o600)
    return SecretStore.load(p)


NOW = datetime(2026, 10, 1, 10, 0, tzinfo=API_TIMEZONE)


def _opts(**kw) -> UploadOptions:
    kw.setdefault("mode", "real")
    kw.setdefault("now", NOW)
    kw.setdefault("uploaded_by", "tester@box")
    return UploadOptions(**kw)


def test_creates_experiment_with_provenance_then_uploads_crawlers(store):
    lock = make_lock()
    text = dump_lock(lock)
    session = FakeSession()
    result = upload_lock(text, session=session, store=store, options=_opts())

    kinds = [c[0] for c in session.calls]
    assert kinds[:2] == ["POST", "POST_FILE"]
    create = session.calls[0][2]
    assert create["name"] == "Study" and create["active"] == 1
    prefs = {p["name"]: p["value"] for p in create["preferences"]}
    assert prefs["pmspec.lock_sha256"] == sha256_text(text)
    assert prefs["pmspec.seed"] == "42"
    assert prefs["pmspec.mode"] == "real"
    assert prefs["pmspec.anchor"] == "2026-10-01T10:00:00+02:00"
    assert prefs["pmspec.git_commit"] == "a2024c9"
    assert prefs["pmspec.uploaded_by"] == "tester@box"
    assert "testpass" not in str(create)
    assert result.experiment_id == 181 and result.lock_sha256 == sha256_text(text)
    assert result.record_line.startswith("2026-10-01T10:00:00+02:00 exp=181 mode=real lock=")


def test_uploaded_json_has_resolved_start_at_and_secrets(store):
    session = FakeSession()
    upload_lock(dump_lock(make_lock()), session=session, store=store, options=_opts())
    body = session.calls[1][2]
    assert '"start_at": "2026-10-01T10:05:00+02:00"' in body
    assert '"password": "testpass"' in body and "${secret:" not in body
    assert "start_offset" not in body


def test_missing_secret_aborts_before_any_request(tmp_path: Path):
    p = tmp_path / "empty.toml"
    p.write_text("")
    p.chmod(0o600)
    session = FakeSession()
    with pytest.raises(MissingSecret, match="oxylabs.a.username"):
        upload_lock(dump_lock(make_lock()), session=session, store=SecretStore.load(p), options=_opts())
    assert session.calls == []


def test_dirty_commit_refused_in_real_mode(store):
    lock = make_lock()
    lock.provenance.git_commit = "a2024c9-dirty"
    with pytest.raises(UploadRefused, match="dirty"):
        upload_lock(dump_lock(lock), session=FakeSession(), store=store, options=_opts())


def test_dirty_commit_allowed_in_test_mode_and_stamped(store):
    lock = make_lock()
    lock.provenance.git_commit = "a2024c9-dirty"
    session = FakeSession()
    upload_lock(dump_lock(lock), session=session, store=store, options=_opts(mode="test"))
    prefs = {p["name"]: p["value"] for p in session.calls[0][2]["preferences"]}
    assert prefs["pmspec.git_commit"] == "a2024c9-dirty" and prefs["pmspec.mode"] == "test"


def test_no_commit_refused_in_real_mode_unless_allow_dirty(store):
    lock = make_lock()
    lock.provenance.git_commit = None
    with pytest.raises(UploadRefused, match="commit"):
        upload_lock(dump_lock(lock), session=FakeSession(), store=store, options=_opts())
    upload_lock(dump_lock(lock), session=FakeSession(), store=store, options=_opts(allow_dirty=True))


def test_test_mode_forces_vm_runner_and_prefixes_name():
    name, runner = effective_experiment(make_lock(), "test", date(2026, 9, 11))
    assert name == "test-Study-20260911" and runner == TEST_RUNNER_USER_ID
    lock = make_lock(experiment={"name": "Study", "description": "", "runner_user_id": 1})
    assert effective_experiment(lock, "real", date(2026, 9, 11)) == ("Study", 1)


def test_fixed_anchor_in_the_past_is_refused(store):
    lock = make_lock(schedule={"anchor": "2020-01-01T00:00:00"})
    with pytest.raises(UploadRefused, match="past"):
        upload_lock(dump_lock(lock), session=FakeSession(), store=store, options=_opts())
    upload_lock(dump_lock(lock), session=FakeSession(), store=store, options=_opts(allow_past=True))


def test_append_mode_puts_numbered_provenance_and_skips_create(store):
    session = FakeSession()
    upload_lock(dump_lock(make_lock()), session=session, store=store,
                options=_opts(experiment_id=42))
    kinds = [c[0] for c in session.calls]
    assert kinds == ["GET", "PUT", "POST_FILE"]
    put = session.calls[1][2]
    assert put["name"] == "Study" and put["user_id"] == 5          # resent unchanged
    assert all(p["description"] == "upload 2" for p in put["preferences"])


def test_a_spec_is_rejected(store):
    with pytest.raises(UploadRefused, match="compile"):
        upload_lock("version: '1.0'\nexperiment: {name: x}\n", session=FakeSession(), store=store, options=_opts())


def test_provenance_values_fit_the_api_column():
    lock = make_lock()
    prefs = provenance_preferences(lock, lock_sha256="f" * 64, anchor_iso="2026-10-01T10:00:00+02:00",
                                   mode="real", uploaded_at="2026-09-11T10:00:00+02:00", uploaded_by="u@h")
    assert {p["name"] for p in prefs} == {
        "pmspec.lock_sha256", "pmspec.spec_sha256", "pmspec.seed", "pmspec.anchor", "pmspec.git_commit",
        "pmspec.generator", "pmspec.version", "pmspec.mode", "pmspec.lax", "pmspec.uploaded_at",
        "pmspec.uploaded_by",
    }
    assert all(len(p["value"]) <= 255 and isinstance(p["value"], str) for p in prefs)
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest pmspec/tests/test_lock_upload.py -v`
Expected: `ModuleNotFoundError: pmspec.lock.upload`.

- [ ] **Step 3: Write api.py**

```python
"""The one HTTP seam for the CLI paths. The MCP server has its own (_api_request)."""

from __future__ import annotations

from typing import Any

import requests


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


class ApiSession:
    def __init__(self, base_url: str, email: str, password: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._email = email
        self._password = password
        self._timeout = timeout
        self._token: str | None = None

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            r = requests.post(
                f"{self.base_url}/login",
                json={"email": self._email, "password": self._password},
                headers={"Accept": "application/json"},
                timeout=self._timeout,
            )
            self._raise_for(r, "login")
            self._token = r.json().get("access_token")
            if not self._token:
                raise ApiError("login: no access_token in response")
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    @staticmethod
    def _raise_for(r: requests.Response, what: str) -> None:
        if 200 <= r.status_code < 300:
            return
        try:
            detail = r.json().get("message") or r.text[:500]
        except ValueError:
            detail = r.text[:500]
        raise ApiError(f"{what}: HTTP {r.status_code}", status=r.status_code, detail=str(detail))

    @staticmethod
    def _unwrap(r: requests.Response) -> Any:
        body = r.json() if r.content else {}
        return body.get("data", body) if isinstance(body, dict) else body

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = requests.get(f"{self.base_url}/{path}", headers=self._headers(), params=params, timeout=self._timeout)
        self._raise_for(r, f"GET {path}")
        return self._unwrap(r)

    def post(self, path: str, json: dict[str, Any]) -> Any:
        r = requests.post(f"{self.base_url}/{path}", headers=self._headers(), json=json, timeout=self._timeout)
        self._raise_for(r, f"POST {path}")
        return self._unwrap(r)

    def put(self, path: str, json: dict[str, Any]) -> Any:
        r = requests.put(f"{self.base_url}/{path}", headers=self._headers(), json=json, timeout=self._timeout)
        self._raise_for(r, f"PUT {path}")
        return self._unwrap(r)

    def post_file(self, path: str, field: str, filename: str, content: str, content_type: str) -> Any:
        r = requests.post(
            f"{self.base_url}/{path}",
            headers=self._headers(),
            files={field: (filename, content, content_type)},
            timeout=max(self._timeout, 300.0),
        )
        self._raise_for(r, f"POST {path}")
        return self._unwrap(r)
```

- [ ] **Step 4: Write upload.py**

```python
"""Upload a lock: resolve, create (or append), write provenance, upload crawlers."""

from __future__ import annotations

import getpass
import json
import socket
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pmspec.spec.time_utils import API_TIMEZONE, format_iso8601

from .anchor import UPLOAD_ANCHOR, resolve_anchor
from .io import is_lock_document, load_lock, sha256_text
from .model import Lock
from .provenance import now_iso
from .resolve import resolve_for_upload
from .secrets import SecretStore, find_placeholders

MODES = ("real", "test")
TEST_RUNNER_USER_ID = 3
_PREF_MAX = 255


class UploadRefused(ValueError):
    pass


@dataclass
class UploadOptions:
    mode: str
    experiment_id: int | None = None
    allow_dirty: bool = False
    allow_past: bool = False
    contact: str = "pmspec"
    now: datetime | None = None
    uploaded_by: str | None = None


@dataclass
class UploadResult:
    experiment_id: int
    experiment_name: str
    crawler_count: int
    total_jobs: int
    lock_sha256: str
    anchor: str
    mode: str
    runner_user_id: int | None
    record_line: str


def _clip(value: Any) -> str:
    text = "" if value is None else str(value)
    return text[:_PREF_MAX]


def provenance_preferences(
    lock: Lock, *, lock_sha256: str, anchor_iso: str, mode: str, uploaded_at: str,
    uploaded_by: str, label: str = "upload 1",
) -> list[dict[str, str]]:
    p = lock.provenance
    values = {
        "pmspec.lock_sha256": lock_sha256,
        "pmspec.spec_sha256": p.spec_sha256,
        "pmspec.seed": p.seed,
        "pmspec.anchor": anchor_iso,
        "pmspec.git_commit": p.git_commit or "",
        "pmspec.generator": p.generator or "",
        "pmspec.version": p.pmspec_version,
        "pmspec.mode": mode,
        "pmspec.lax": "true" if p.lax else "false",
        "pmspec.uploaded_at": uploaded_at,
        "pmspec.uploaded_by": uploaded_by,
    }
    return [{"name": k, "description": label, "value": _clip(v)} for k, v in values.items()]


def effective_experiment(lock: Lock, mode: str, today: date) -> tuple[str, int | None]:
    name = lock.experiment["name"]
    runner = lock.experiment.get("runner_user_id")
    if mode == "test":
        return f"test-{name}-{today:%Y%m%d}", TEST_RUNNER_USER_ID
    return name, runner


def _check_commit(lock: Lock, options: UploadOptions) -> None:
    commit = lock.provenance.git_commit
    if options.mode == "test" or options.allow_dirty:
        return
    if commit is None:
        raise UploadRefused("lock has no git commit; commit the spec repo or pass --allow-dirty")
    if commit.endswith("-dirty"):
        raise UploadRefused(f"lock was compiled from a dirty tree ({commit}); commit, recompile, or pass --allow-dirty")


def upload_lock(
    lock_text: str, *, session: Any, store: SecretStore | None, options: UploadOptions
) -> UploadResult:
    if options.mode not in MODES:
        raise UploadRefused(f"mode must be one of {MODES}")
    if not is_lock_document(lock_text):
        raise UploadRefused("not a lock: compile the spec first (pmspec compile)")
    lock = load_lock(lock_text)
    _check_commit(lock, options)

    now = options.now or datetime.now(API_TIMEZONE)
    anchor_dt = resolve_anchor(lock.schedule["anchor"], now=now)
    if lock.schedule["anchor"] != UPLOAD_ANCHOR and anchor_dt < now and not options.allow_past:
        raise UploadRefused(f"anchor {lock.schedule['anchor']} is in the past; pass --allow-past to upload anyway")

    needed = find_placeholders(lock.crawlers)
    if needed and store is None:
        raise UploadRefused(f"lock references {len(needed)} secret(s) but no secrets store was given")
    if store is not None:
        missing = store.missing(needed)
        if missing:
            raise UploadRefused(f"missing secrets in {store.source}: {', '.join(missing)}")

    crawlers, _ = resolve_for_upload(lock, store=store, now=now)
    lock_sha = sha256_text(lock_text)
    anchor_iso = format_iso8601(anchor_dt)
    uploaded_at = now_iso()
    uploaded_by = options.uploaded_by or f"{getpass.getuser()}@{socket.gethostname()}"
    name, runner = effective_experiment(lock, options.mode, now.date())

    if options.experiment_id is None:
        payload: dict[str, Any] = {
            "name": name,
            "description": lock.experiment.get("description") or "Uploaded by pmspec",
            "contact": options.contact,
            "active": 1,
            "preferences": provenance_preferences(
                lock, lock_sha256=lock_sha, anchor_iso=anchor_iso, mode=options.mode,
                uploaded_at=uploaded_at, uploaded_by=uploaded_by,
            ),
        }
        if runner is not None:
            payload["runner_user_id"] = runner
        created = session.post("experiments", payload)
        experiment_id = int(created["id"])
    else:
        experiment_id = options.experiment_id
        live = session.get(f"experiments/{experiment_id}")
        existing = [p for p in live.get("preferences", []) if p.get("name") == "pmspec.lock_sha256"]
        label = f"upload {len(existing) + 1}"
        session.put(
            f"experiments/{experiment_id}",
            {
                "name": live["name"],
                "user_id": live["user_id"],
                "description": live.get("description"),
                "contact": live.get("contact"),
                "preferences": provenance_preferences(
                    lock, lock_sha256=lock_sha, anchor_iso=anchor_iso, mode=options.mode,
                    uploaded_at=uploaded_at, uploaded_by=uploaded_by, label=label,
                ),
            },
        )
        name = live["name"]

    session.post_file(
        f"experiments/{experiment_id}/upload", "json", "crawlers.json",
        json.dumps(crawlers, indent=2, ensure_ascii=False), "application/json",
    )
    del crawlers  # resolved secrets: nothing below may see them

    total_jobs = sum(len(q["jobs"]) for c in lock.crawlers for q in c["queues"])
    record = (
        f"{uploaded_at} exp={experiment_id} mode={options.mode} lock={lock_sha[:12]} "
        f"commit={lock.provenance.git_commit or 'none'} api={session.base_url}"
    )
    return UploadResult(
        experiment_id=experiment_id, experiment_name=name, crawler_count=len(lock.crawlers),
        total_jobs=total_jobs, lock_sha256=lock_sha, anchor=anchor_iso, mode=options.mode,
        runner_user_id=runner, record_line=record,
    )
```

- [ ] **Step 5: Run tests**

Run: `venv/bin/pytest pmspec/tests/test_lock_upload.py -v`
Expected: PASS. `test_uploaded_json_has_resolved_start_at...`: the fixture lock's queue offset is `+5m` and `NOW` is 10:00 → `10:05:00+02:00`.

- [ ] **Step 6: Commit**

```bash
git add pmspec/lock/api.py pmspec/lock/upload.py pmspec/tests/test_lock_upload.py
git commit -m "designer: one uploader for locks, with provenance preferences and mode"
```

---

### Task 8: `verify` — binding and structural diff

**Files:**
- Create: `pmspec/lock/verify.py`
- Test: `pmspec/tests/test_lock_verify.py`

**Interfaces:**
- Consumes: Task 2 `load_lock`, `sha256_text`; Task 4 `resolve_anchor`, `resolve_start_at`; Task 7 `ApiSession`-shaped session (`get`).
- Produces:
  ```python
  @dataclass class Difference: path: str; expected: Any; actual: Any
  @dataclass class VerifyResult: experiment_id: int; bound: bool; local_sha256: str; live_sha256s: list[str]; anchor: str | None; differences: list[Difference]; deep: bool
      @property def ok(self) -> bool   # bound and not differences
      def render(self) -> str          # "BOUND"/"UNBOUND" + grouped diff, never a secret
  def lock_shape(lock: Lock, anchor_dt: datetime, *, deep: bool) -> dict[str, Any]
  def live_shape(session, experiment_id: int, *, deep: bool) -> tuple[dict[str, Any], dict[str, Any]]   # (shape, experiment_json)
  def compare_shapes(expected: dict, actual: dict) -> list[Difference]
  def verify_lock(lock_text: str, *, session, experiment_id: int, deep: bool = False) -> VerifyResult
  ```
  A *shape* is `{"crawlers": {name: {"params": {...}, "preferences": {name: value}, "location": str, "proxy": {"type","hostname","port"}, "queues": [{"name", "start_at", "jobs": [{"type", "behaviors": {name: value}}]}]}}}`; without `deep`, each queue's `"jobs"` key is absent on both sides.

Where the live side comes from (no single endpoint returns the tree):
| level | request | notes |
|---|---|---|
| crawlers, agent, proxy, configuration preferences, experiment preferences | `GET experiments/{id}` | one call |
| configuration params | `GET configurations/{configuration_id}` per crawler | `params` is a HasMany of rows with columns `pi, psi, alpha, tau, kappa, beta, search_terms, media_outlet_urls` — take the first row, keep only non-null columns; normalise numbers via `float()` so `"1"` == `1` |
| queues (name, start_at, crawler_id) | `GET experiments/{id}/queues-unprocessed` and `…/queues-processed`, `per_page=500`, follow `meta.last_page` | `start_at` comes back as `"YYYY-MM-DD HH:MM:SS"`; compare on the first 19 chars of both sides |
| jobs + behaviours (`deep` only) | `GET experiments/{id}/download` → zip of four CSVs (`*_primemover_jobs.csv`: `ID, …, Type, …, Order, …, Queue ID`; `*_primemover_behaviors.csv`: `ID, Name, …, Value, …, Job ID`) | `session.get_bytes(path)` — add to `ApiSession` in this task: same as `get` but returns `r.content`; parse with `zipfile` + `csv` |

- [ ] **Step 1: Write the failing tests**

```python
# pmspec/tests/test_lock_verify.py
"""verify: is this live experiment the one this lock describes?"""

import csv
import io
import zipfile
from datetime import datetime
from typing import Any

from pmspec.lock.io import dump_lock, sha256_text
from pmspec.lock.verify import Difference, compare_shapes, lock_shape, verify_lock
from pmspec.spec.time_utils import API_TIMEZONE

from .test_lock_io import make_lock

NOW = datetime(2026, 10, 1, 10, 0, tzinfo=API_TIMEZONE)


class FakeSession:
    """Serves a live experiment that matches make_lock() uploaded at NOW."""

    base_url = "http://fake"

    def __init__(self, lock_text: str, *, mutate: dict[str, Any] | None = None) -> None:
        self.sha = sha256_text(lock_text)
        self.mutate = mutate or {}
        self.calls: list[str] = []

    def get(self, path, params=None):
        self.calls.append(path)
        if path == "experiments/7":
            return {
                "id": 7, "name": "Study", "user_id": 5,
                "preferences": [
                    {"name": "pmspec.lock_sha256", "value": self.mutate.get("sha", self.sha)},
                    {"name": "pmspec.anchor", "value": "2026-10-01T10:00:00+02:00"},
                ],
                "crawlers": [{
                    "id": 30, "name": "bot_01",
                    "agent": {"location": self.mutate.get("location", "US-NC")},
                    "proxy": {"type": "http", "hostname": "pr.oxylabs.io", "port": 7777,
                              "username": "cust", "password": "testpass"},
                    "configuration": {"id": 90, "preferences": [{"name": "reliability", "value": "high"}]},
                }],
            }
        if path == "configurations/90":
            return {"id": 90, "params": [{"pi": self.mutate.get("pi", "1"), "psi": "0", "kappa": "0",
                                          "tau": "0", "alpha": None, "beta": None,
                                          "search_terms": None, "media_outlet_urls": None}]}
        if path.startswith("experiments/7/queues-"):
            rows = [] if "processed" in path and "unprocessed" not in path else [
                {"id": 500, "name": "s1", "crawler_id": 30,
                 "start_at": self.mutate.get("start_at", "2026-10-01 10:05:00")}
            ]
            return {"data": rows, "meta": {"last_page": 1}}
        raise AssertionError(path)

    def get_bytes(self, path, params=None):
        self.calls.append(path)
        assert path == "experiments/7/download"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            jobs = io.StringIO()
            w = csv.writer(jobs)
            w.writerow(["ID", "Name", "Description", "Type", "Status Code", "Status Message", "Order",
                        "Active", "Updated At", "Created At", "Started At", "Finished At", "User ID", "Queue ID"])
            w.writerow([900, "visit", "d", "visitjob", "", "", 1, 1, "", "", "", "", 5, 500])
            z.writestr("7_primemover_jobs.csv", jobs.getvalue())
            beh = io.StringIO()
            w = csv.writer(beh)
            w.writerow(["ID", "Name", "Description", "Value", "Active", "Updated At", "Created At", "User ID", "Job ID"])
            w.writerow([1000, "url", "", self.mutate.get("url", "https://x"), 1, "", "", 5, 900])
            z.writestr("7_primemover_behaviors.csv", beh.getvalue())
            z.writestr("7_primemover_crawlers.csv", "ID\n30\n")
            z.writestr("7_primemover_queues.csv", "ID\n500\n")
        return buf.getvalue()


def test_bound_and_identical():
    text = dump_lock(make_lock())
    r = verify_lock(text, session=FakeSession(text), experiment_id=7)
    assert r.bound and r.differences == [] and r.ok
    assert r.render().startswith("BOUND")


def test_unbound_reports_both_hashes():
    text = dump_lock(make_lock())
    r = verify_lock(text, session=FakeSession(text, mutate={"sha": "0" * 64}), experiment_id=7)
    assert not r.bound and r.live_sha256s == ["0" * 64] and r.local_sha256 == sha256_text(text)
    assert "UNBOUND" in r.render()


def test_param_difference_is_reported_at_its_path():
    text = dump_lock(make_lock())
    r = verify_lock(text, session=FakeSession(text, mutate={"pi": "0"}), experiment_id=7)
    assert Difference("crawlers.bot_01.params.pi", 1.0, 0.0) in r.differences


def test_start_at_difference_uses_wall_clock_digits():
    text = dump_lock(make_lock())
    r = verify_lock(text, session=FakeSession(text, mutate={"start_at": "2026-10-01 10:06:00"}), experiment_id=7)
    assert any(d.path == "crawlers.bot_01.queues[0].start_at" for d in r.differences)


def test_location_difference():
    text = dump_lock(make_lock())
    r = verify_lock(text, session=FakeSession(text, mutate={"location": "US-TX"}), experiment_id=7)
    assert Difference("crawlers.bot_01.location", "US-NC", "US-TX") in r.differences


def test_shallow_verify_does_not_download_and_ignores_jobs():
    text = dump_lock(make_lock())
    s = FakeSession(text, mutate={"url": "https://other"})
    r = verify_lock(text, session=s, experiment_id=7)
    assert r.ok and not any("download" in c for c in s.calls)


def test_deep_verify_compares_job_behaviours():
    text = dump_lock(make_lock())
    s = FakeSession(text, mutate={"url": "https://other"})
    r = verify_lock(text, session=s, experiment_id=7, deep=True)
    assert r.deep and any(d.path == "crawlers.bot_01.queues[0].jobs[0].behaviors.url" for d in r.differences)
    ok = verify_lock(text, session=FakeSession(text), experiment_id=7, deep=True)
    assert ok.ok


def test_render_never_contains_credentials():
    text = dump_lock(make_lock())
    r = verify_lock(text, session=FakeSession(text, mutate={"pi": "0"}), experiment_id=7)
    assert "testpass" not in r.render() and "cust" not in r.render()


def test_compare_shapes_reports_missing_and_extra_crawlers():
    diffs = compare_shapes({"crawlers": {"a": {}, "b": {}}}, {"crawlers": {"a": {}, "c": {}}})
    paths = {d.path for d in diffs}
    assert paths == {"crawlers.b", "crawlers.c"}


def test_lock_shape_masks_nothing_but_never_includes_credentials():
    shape = lock_shape(make_lock(), NOW, deep=False)
    proxy = shape["crawlers"]["bot_01"]["proxy"]
    assert set(proxy) == {"type", "hostname", "port"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest pmspec/tests/test_lock_verify.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Add `get_bytes` to `ApiSession`**

```python
    def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        r = requests.get(f"{self.base_url}/{path}", headers=self._headers(), params=params,
                         timeout=max(self._timeout, 300.0))
        self._raise_for(r, f"GET {path}")
        return r.content
```

- [ ] **Step 4: Write verify.py**

```python
"""Is the live experiment the one this lock describes? Two answers: bound (hash)
and identical (structure). Execution is not this module's business."""

from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .anchor import resolve_anchor, resolve_start_at
from .io import load_lock, sha256_text
from .model import Lock

_PARAM_COLUMNS = ("pi", "psi", "alpha", "tau", "kappa", "beta")


@dataclass(frozen=True)
class Difference:
    path: str
    expected: Any
    actual: Any


@dataclass
class VerifyResult:
    experiment_id: int
    bound: bool
    local_sha256: str
    live_sha256s: list[str]
    anchor: str | None
    differences: list[Difference] = field(default_factory=list)
    deep: bool = False

    @property
    def ok(self) -> bool:
        return self.bound and not self.differences

    def render(self) -> str:
        lines = []
        if self.bound:
            lines.append(f"BOUND  experiment {self.experiment_id} <- lock {self.local_sha256[:12]}")
        else:
            lines.append(f"UNBOUND  local lock {self.local_sha256[:12]}; server has "
                         + (", ".join(h[:12] for h in self.live_sha256s) or "no pmspec.lock_sha256"))
        if self.anchor:
            lines.append(f"anchor {self.anchor}")
        lines.append("structure: identical" + (" (deep)" if self.deep else "")
                     if not self.differences else f"structure: {len(self.differences)} difference(s)")
        for d in self.differences:
            lines.append(f"  {d.path}: expected {d.expected!r}, live {d.actual!r}")
        return "\n".join(lines)


def _num(value: Any) -> Any:
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _params(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    out = {}
    for k in _PARAM_COLUMNS:
        if raw.get(k) is not None:
            out[k] = _num(raw[k])
    for k in ("search_terms", "media_outlet_urls"):
        if raw.get(k):
            out[k] = list(raw[k])
    return out


def _wall(value: str) -> str:
    return value.replace(" ", "T")[:19]


def lock_shape(lock: Lock, anchor_dt: datetime, *, deep: bool) -> dict[str, Any]:
    crawlers: dict[str, Any] = {}
    for c in lock.crawlers:
        proxy = c.get("proxy") or {}
        entry: dict[str, Any] = {
            "params": _params((c.get("configuration") or {}).get("params")),
            "preferences": {p["name"]: str(p["value"]) for p in (c.get("configuration") or {}).get("preferences", [])},
            "location": (c.get("agent") or {}).get("location", ""),
            "proxy": {"type": proxy.get("type"), "hostname": proxy.get("hostname"), "port": proxy.get("port")},
            "queues": [],
        }
        for q in c.get("queues", []):
            queue: dict[str, Any] = {"name": q["name"], "start_at": _wall(resolve_start_at(anchor_dt, q["start_offset"]))}
            if deep:
                queue["jobs"] = [
                    {"type": j["type"], "behaviors": {b["name"]: str(b["value"]) for b in j.get("behaviors", [])}}
                    for j in q.get("jobs", [])
                ]
            entry["queues"].append(queue)
        crawlers[c["name"]] = entry
    return {"crawlers": crawlers}


def _paginate(session: Any, path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        body = session.get(path, params={"per_page": 500, "page": page})
        data = body.get("data", body) if isinstance(body, dict) else body
        rows.extend(data)
        last = (body.get("meta") or {}).get("last_page", 1) if isinstance(body, dict) else 1
        if page >= int(last):
            return rows
        page += 1


def _export_jobs(session: Any, experiment_id: int) -> dict[int, list[dict[str, Any]]]:
    """queue_id -> jobs (ordered) with behaviours, from the experiment download zip."""
    blob = session.get_bytes(f"experiments/{experiment_id}/download")
    jobs_by_queue: dict[int, list[dict[str, Any]]] = defaultdict(list)
    behaviours: dict[int, dict[str, str]] = defaultdict(dict)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = z.namelist()
        beh_name = next(n for n in names if n.endswith("_behaviors.csv"))
        job_name = next(n for n in names if n.endswith("_jobs.csv"))
        for row in csv.DictReader(io.TextIOWrapper(z.open(beh_name), encoding="utf-8")):
            behaviours[int(row["Job ID"])][row["Name"]] = row["Value"]
        for row in csv.DictReader(io.TextIOWrapper(z.open(job_name), encoding="utf-8")):
            jobs_by_queue[int(row["Queue ID"])].append(
                {"id": int(row["ID"]), "order": int(row["Order"] or 0), "type": row["Type"]}
            )
    return {
        qid: [{"type": j["type"], "behaviors": behaviours.get(j["id"], {})}
              for j in sorted(jobs, key=lambda j: (j["order"], j["id"]))]
        for qid, jobs in jobs_by_queue.items()
    }


def live_shape(session: Any, experiment_id: int, *, deep: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    exp = session.get(f"experiments/{experiment_id}")
    queues = _paginate(session, f"experiments/{experiment_id}/queues-unprocessed") + _paginate(
        session, f"experiments/{experiment_id}/queues-processed"
    )
    by_crawler: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for q in queues:
        by_crawler[int(q["crawler_id"])].append(q)
    jobs = _export_jobs(session, experiment_id) if deep else {}

    crawlers: dict[str, Any] = {}
    for c in exp.get("crawlers", []):
        config = c.get("configuration") or {}
        params_rows = []
        if config.get("id") is not None:
            params_rows = (session.get(f"configurations/{config['id']}") or {}).get("params") or []
        proxy = c.get("proxy") or {}
        entry: dict[str, Any] = {
            "params": _params(params_rows[0] if params_rows else None),
            "preferences": {p["name"]: str(p["value"]) for p in config.get("preferences", [])},
            "location": (c.get("agent") or {}).get("location", ""),
            "proxy": {"type": proxy.get("type"), "hostname": proxy.get("hostname"), "port": proxy.get("port")},
            "queues": [],
        }
        for q in sorted(by_crawler.get(int(c["id"]), []), key=lambda q: (_wall(q["start_at"]), q["name"])):
            queue: dict[str, Any] = {"name": q["name"], "start_at": _wall(q["start_at"])}
            if deep:
                queue["jobs"] = jobs.get(int(q["id"]), [])
            entry["queues"].append(queue)
        crawlers[c["name"]] = entry
    return {"crawlers": crawlers}, exp


def compare_shapes(expected: Any, actual: Any, path: str = "") -> list[Difference]:
    diffs: list[Difference] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            sub = f"{path}.{key}" if path else str(key)
            if key not in expected:
                diffs.append(Difference(sub, None, "<present>"))
            elif key not in actual:
                diffs.append(Difference(sub, "<present>", None))
            else:
                diffs.extend(compare_shapes(expected[key], actual[key], sub))
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            diffs.append(Difference(f"{path}.length", len(expected), len(actual)))
        for i, (e, a) in enumerate(zip(expected, actual)):
            diffs.extend(compare_shapes(e, a, f"{path}[{i}]"))
    elif expected != actual:
        diffs.append(Difference(path, expected, actual))
    return diffs


def verify_lock(lock_text: str, *, session: Any, experiment_id: int, deep: bool = False) -> VerifyResult:
    lock = load_lock(lock_text)
    local_sha = sha256_text(lock_text)
    actual, exp = live_shape(session, experiment_id, deep=deep)
    prefs = exp.get("preferences", [])
    live_shas = [p["value"] for p in prefs if p.get("name") == "pmspec.lock_sha256"]
    anchors = [p["value"] for p in prefs if p.get("name") == "pmspec.anchor"]
    anchor_iso = anchors[-1] if anchors else None
    anchor_dt = resolve_anchor(anchor_iso[:19]) if anchor_iso else resolve_anchor(lock.schedule["anchor"])
    expected = lock_shape(lock, anchor_dt, deep=deep)
    # Queue order inside a crawler: lock order vs. live sorted by start; sort the lock side too.
    for entry in expected["crawlers"].values():
        entry["queues"].sort(key=lambda q: (q["start_at"], q["name"]))
    return VerifyResult(
        experiment_id=experiment_id,
        bound=local_sha in live_shas,
        local_sha256=local_sha,
        live_sha256s=live_shas,
        anchor=anchor_iso,
        differences=compare_shapes(expected, actual),
        deep=deep,
    )
```

Both sides sort a crawler's queues by `(start_at wall clock, name)` so the lists line up regardless of upload order.

- [ ] **Step 5: Run tests**

Run: `venv/bin/pytest pmspec/tests/test_lock_verify.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pmspec/lock/verify.py pmspec/lock/api.py pmspec/tests/test_lock_verify.py
git commit -m "designer: verify binds a live experiment to its lock and diffs the structure"
```

---

### Task 9: `pmspec upload`, `pmspec verify`, `pmspec secrets check`

**Files:**
- Modify: `pmspec/cli.py` (`upload` at `:218-416` is replaced wholesale)
- Test: `pmspec/tests/test_lock_cli_upload.py`

**Interfaces:**
- Consumes: Task 5 `compile_spec`; Task 7 `ApiSession`, `ApiError`, `upload_lock`, `UploadOptions`, `UploadRefused`, `MODES`; Task 8 `verify_lock`; Task 3 `SecretStore`, `find_placeholders`, `InsecureSecretsFile`.
- Produces:
  - `pmspec upload FILE --mode real|test [--experiment-id N] [--dry-run] [--record PATH] [--allow-dirty] [--allow-past] [--deep] [--secrets PATH] [--seed N]`. A spec input is compiled to its sibling lock first (needs `--seed` or `design.seed`) and the user is told to commit it. Exit 1 on refusal, API error, or post-upload verify mismatch. Prints `result.record_line` last.
  - `pmspec verify EXPERIMENT_ID LOCK [--deep]` → prints `VerifyResult.render()`, exit 0 only when `ok`.
  - `pmspec secrets check LOCK [--secrets PATH]` → lists referenced keys, marks missing, exit 1 if any missing; never prints values.
  - Module-level `_session_from_env() -> ApiSession` reading `PM_API_BASE_URL`, `PM_API_USERNAME`/`PM_API_EMAIL`, `PM_API_PASSWORD` (same fallbacks the old code had), erroring with the same "Set environment variables" block when absent.

- [ ] **Step 1: Write the failing tests**

```python
# pmspec/tests/test_lock_cli_upload.py
from pathlib import Path

import pytest
from click.testing import CliRunner

from pmspec import cli as cli_module
from pmspec.cli import cli
from pmspec.lock.io import dump_lock
from pmspec.lock.upload import UploadResult

from .test_lock_io import make_lock


@pytest.fixture
def lock_file(tmp_path: Path) -> Path:
    p = tmp_path / "study.lock.yaml"
    p.write_text(dump_lock(make_lock()))
    return p


@pytest.fixture
def secrets_file(tmp_path: Path) -> Path:
    p = tmp_path / "secrets.toml"
    p.write_text('[oxylabs.a]\nusername = "u"\npassword = "testpass"\n')
    p.chmod(0o600)
    return p


@pytest.fixture
def fake_upload(monkeypatch: pytest.MonkeyPatch):
    calls = {}

    def _upload_lock(lock_text, *, session, store, options):
        calls["options"] = options
        calls["has_store"] = store is not None
        return UploadResult(181, "Study", 1, 1, "f" * 64, "2026-10-01T10:00:00+02:00",
                            options.mode, None, "2026-09-11T10:00:00+02:00 exp=181 mode=real lock=ffff commit=a2024c9 api=x")

    class _Verify:
        def __init__(self, ok):
            self.ok = ok
            self.bound = ok
            self.differences = []

        def render(self):
            return "BOUND" if self.ok else "UNBOUND"

    monkeypatch.setattr(cli_module, "upload_lock", _upload_lock)
    monkeypatch.setattr(cli_module, "verify_lock", lambda *a, **k: _Verify(calls.get("verify_ok", True)))
    monkeypatch.setattr(cli_module, "_session_from_env", lambda: object())
    return calls


def test_upload_requires_mode(lock_file):
    out = CliRunner().invoke(cli, ["upload", str(lock_file)])
    assert out.exit_code != 0 and "--mode" in out.output


def test_upload_dry_run_checks_secrets_and_does_not_upload(lock_file, secrets_file, fake_upload):
    out = CliRunner().invoke(cli, ["upload", str(lock_file), "--mode", "real", "--dry-run",
                                   "--secrets", str(secrets_file)])
    assert out.exit_code == 0, out.output
    assert "oxylabs.a.username" in out.output and "Dry run" in out.output
    assert "options" not in fake_upload


def test_upload_dry_run_reports_missing_secret(lock_file, tmp_path, fake_upload):
    empty = tmp_path / "e.toml"
    empty.write_text("")
    empty.chmod(0o600)
    out = CliRunner().invoke(cli, ["upload", str(lock_file), "--mode", "real", "--dry-run",
                                   "--secrets", str(empty)])
    assert out.exit_code == 1 and "MISSING" in out.output and "testpass" not in out.output


def test_upload_writes_record_and_verifies(lock_file, secrets_file, fake_upload, tmp_path):
    record = tmp_path / "ledger.txt"
    out = CliRunner().invoke(cli, ["upload", str(lock_file), "--mode", "test",
                                   "--secrets", str(secrets_file), "--record", str(record)])
    assert out.exit_code == 0, out.output
    assert fake_upload["options"].mode == "test" and fake_upload["has_store"]
    assert record.read_text().strip().endswith("api=x")
    assert "BOUND" in out.output and out.output.strip().endswith("api=x")


def test_upload_exits_1_when_post_upload_verify_fails(lock_file, secrets_file, fake_upload):
    fake_upload["verify_ok"] = False
    out = CliRunner().invoke(cli, ["upload", str(lock_file), "--mode", "test", "--secrets", str(secrets_file)])
    assert out.exit_code == 1 and "UNBOUND" in out.output and "exp=181" in out.output


def test_upload_compiles_a_spec_first_and_says_to_commit(tmp_path, secrets_file, fake_upload):
    spec = tmp_path / "s.yaml"
    spec.write_text("version: '1.0'\nexperiment: {name: S}\ncrawlers:\n  - name: b\n    session:\n      start: '+1m'\n      workflow:\n        - wait: 1s\n")
    out = CliRunner().invoke(cli, ["upload", str(spec), "--mode", "test", "--seed", "1", "--secrets", str(secrets_file)])
    assert out.exit_code == 0, out.output
    assert (tmp_path / "s.lock.yaml").exists() and "commit" in out.output.lower()


def test_verify_command(lock_file, fake_upload):
    out = CliRunner().invoke(cli, ["verify", "7", str(lock_file)])
    assert out.exit_code == 0 and "BOUND" in out.output
    fake_upload["verify_ok"] = False
    out = CliRunner().invoke(cli, ["verify", "7", str(lock_file)])
    assert out.exit_code == 1


def test_secrets_check(lock_file, secrets_file, tmp_path):
    out = CliRunner().invoke(cli, ["secrets", "check", str(lock_file), "--secrets", str(secrets_file)])
    assert out.exit_code == 0 and "oxylabs.a.password" in out.output and "testpass" not in out.output
    (tmp_path / "secrets.toml").write_text("[oxylabs.a]\nusername = 'u'\n")
    out = CliRunner().invoke(cli, ["secrets", "check", str(lock_file), "--secrets", str(secrets_file)])
    assert out.exit_code == 1 and "MISSING" in out.output
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest pmspec/tests/test_lock_cli_upload.py -v`
Expected: failures on `--mode` (unknown option), `verify`/`secrets` unknown commands, `cli_module.upload_lock` attribute missing.

- [ ] **Step 3: Implement**

Add imports to `cli.py`: `from .lock.api import ApiError, ApiSession`, `from .lock.secrets import InsecureSecretsFile, SecretStore, find_placeholders`, `from .lock.upload import MODES, UploadOptions, UploadRefused, upload_lock`, `from .lock.verify import verify_lock`. (Module-level, so tests can monkeypatch `cli_module.upload_lock` etc.)

```python
def _session_from_env() -> ApiSession:
    import os

    api_url = os.getenv("PM_API_BASE_URL")
    email = os.getenv("PM_API_USERNAME") or os.getenv("PM_API_EMAIL")
    password = os.getenv("PM_API_PASSWORD")
    if not all([api_url, email, password]):
        click.secho("API credentials not configured", fg="red")
        click.echo("Set environment variables:")
        click.echo("  PM_API_BASE_URL=http://localhost:8090/api/v1")
        click.echo("  PM_API_USERNAME=admin@primemover.example")
        click.echo("  PM_API_PASSWORD=password")
        sys.exit(1)
    return ApiSession(str(api_url), str(email), str(password))


def _load_store(path: str | None) -> SecretStore | None:
    try:
        return SecretStore.load(Path(path) if path else None)
    except FileNotFoundError:
        return None
    except InsecureSecretsFile as e:
        click.secho(f"Refusing secrets file: {e}", fg="red")
        sys.exit(1)


def _lock_text_for(file: str, seed: int | None) -> str:
    """A lock's text; a spec is compiled to its sibling lock first."""
    file_path = Path(file)
    content = file_path.read_text()
    if is_lock_document(content):
        return content
    result = compile_spec(content, base_dir=file_path.parent, spec_path=str(file_path), seed=seed)
    if result.lock is None:
        click.secho("Compile FAILED", fg="red", bold=True)
        for error in result.errors:
            click.secho(f"  ERROR: {error}", fg="red")
        sys.exit(1)
    lock_path = file_path.with_name(f"{file_path.stem}.lock.yaml")
    text = dump_lock(result.lock)
    lock_path.write_text(text)
    click.secho(f"Compiled {lock_path} -- commit it: this hash is what the server records", fg="cyan")
    return text


def _secrets_report(lock_text: str, store: SecretStore | None) -> list[str]:
    """Print referenced keys with OK/MISSING; return the missing ones."""
    from .lock.io import load_lock

    needed = sorted(find_placeholders(load_lock(lock_text).crawlers))
    if not needed:
        click.echo("Secrets: none referenced")
        return []
    missing = store.missing(needed) if store else list(needed)
    click.echo(f"Secrets ({store.source if store else 'no store found'}):")
    for key in needed:
        click.echo(f"  {'MISSING' if key in missing else 'OK':7} {key}")
    return missing


@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("--mode", type=click.Choice(MODES), required=True, help="test = VM runner, test- name prefix; real = as in the lock")
@click.option("--experiment-id", "-e", type=int, help="Append crawlers to an existing experiment")
@click.option("--dry-run", is_flag=True)
@click.option("--record", type=click.Path(), help="Append the one-line upload record to this file")
@click.option("--allow-dirty", is_flag=True)
@click.option("--allow-past", is_flag=True)
@click.option("--deep", is_flag=True, help="Post-upload verify down to jobs and behaviours (uses the export)")
@click.option("--secrets", "secrets_path", type=click.Path(), help="Secrets TOML (default: $PMSPEC_SECRETS_FILE or ~/.config/primemover/secrets.toml)")
@click.option("--seed", "-s", type=int, help="Only when FILE is a spec: the seed to compile with")
def upload(file, mode, experiment_id, dry_run, record, allow_dirty, allow_past, deep, secrets_path, seed):
    """Upload a lock (or compile a spec, then upload the lock) and verify the result."""
    lock_text = _lock_text_for(file, seed)
    store = _load_store(secrets_path)
    missing = _secrets_report(lock_text, store)
    if missing:
        click.secho(f"{len(missing)} secret(s) missing", fg="red")
        sys.exit(1)
    if dry_run:
        click.secho("Dry run - not uploading", fg="cyan")
        sys.exit(0)

    session = _session_from_env()
    options = UploadOptions(mode=mode, experiment_id=experiment_id, allow_dirty=allow_dirty, allow_past=allow_past)
    try:
        result = upload_lock(lock_text, session=session, store=store, options=options)
    except UploadRefused as e:
        click.secho(f"Refused: {e}", fg="red")
        sys.exit(1)
    except ApiError as e:
        click.secho(f"Upload failed: {e}", fg="red")
        if e.detail:
            click.echo(f"  {e.detail}")
        sys.exit(1)

    click.secho("Upload SUCCESS", fg="green", bold=True)
    click.echo(f"Experiment ID: {result.experiment_id}  ({result.experiment_name}, mode={result.mode})")
    click.echo(f"Crawlers: {result.crawler_count}  Jobs: {result.total_jobs}  Anchor: {result.anchor}")

    verdict = verify_lock(lock_text, session=session, experiment_id=result.experiment_id, deep=deep)
    click.echo(verdict.render())
    if record:
        with open(record, "a") as f:
            f.write(result.record_line + "\n")
    click.echo(result.record_line)
    sys.exit(0 if verdict.ok else 1)


@cli.command()
@click.argument("experiment_id", type=int)
@click.argument("lock", type=click.Path(exists=True))
@click.option("--deep", is_flag=True, help="Also compare jobs and behaviours (uses the experiment export)")
def verify(experiment_id, lock, deep):
    """Check that a live experiment is bound to, and identical to, a lock."""
    session = _session_from_env()
    try:
        result = verify_lock(Path(lock).read_text(), session=session, experiment_id=experiment_id, deep=deep)
    except ApiError as e:
        click.secho(f"Verify failed: {e}", fg="red")
        sys.exit(1)
    click.echo(result.render())
    sys.exit(0 if result.ok else 1)


@cli.group()
def secrets():
    """Secret placeholders in locks."""


@secrets.command("check")
@click.argument("lock", type=click.Path(exists=True))
@click.option("--secrets", "secrets_path", type=click.Path())
def secrets_check(lock, secrets_path):
    """List the secrets a lock references and which are missing from the store."""
    missing = _secrets_report(Path(lock).read_text(), _load_store(secrets_path))
    sys.exit(1 if missing else 0)
```

Delete the old `upload` body entirely (the `requests`-based create/upload code at `:218-416`). Update the module docstring's command list.

- [ ] **Step 4: Run tests**

Run: `venv/bin/pytest pmspec/tests/ -q && venv/bin/ruff check pmspec/ && venv/bin/mypy pmspec/ --ignore-missing-imports`
Expected: all pass. mypy will want `-> None` on the click functions and `Any`-typed session params — add them.

- [ ] **Step 5: Commit**

```bash
git add pmspec/cli.py pmspec/tests/test_lock_cli_upload.py
git commit -m "pmspec: upload reads locks, writes provenance, verifies; verify and secrets check commands"
```

---

### Task 10: MCP tools — `compile_experiment_spec`, `verify_experiment`; locks and `files=` on the existing tools

**Files:**
- Modify: `orchestrator/mcp_server/server.py` (`validate_experiment_spec` at `:2000-2075`, `upload_experiment` at `:2168-2367`, new tools next to them)
- Test: `orchestrator/tests/test_mcp_lock_tools.py`

**Interfaces:**
- Consumes: Task 5 `compile_spec`; Task 2 `dump_lock`, `load_lock`, `is_lock_document`, `LockError`; Task 7 `upload_lock`, `UploadOptions`, `UploadRefused`; Task 8 `verify_lock`; Task 3 `SecretStore`.
- Produces (all return JSON strings):
  - `compile_experiment_spec(yaml_content: str, files: dict[str, str] | None = None, seed: int | None = None, anchor: str | None = None, generator: str | None = None, lax: bool = False) -> {"lock": text, "crawler_count", "seed", "anchor", "warnings"} | {"errors": [...]}`
  - `validate_experiment_spec(yaml_content: str, strict: bool = False, files: dict[str, str] | None = None)` — a lock is validated against the lock schema and reported as `{"valid", "kind": "lock", ...}`; a spec with `files` gets them materialised in a temp dir used as `base_dir`.
  - `upload_experiment(lock_content: str, mode: str, experiment_id: int | None = None, dry_run: bool = True, deep_verify: bool = False)` — **signature change**: the first parameter is a lock; spec content returns `{"success": false, "error": "not a lock: call compile_experiment_spec first"}`. `runner_user_id` parameter is dropped (the lock and the mode decide). Uses a `_McpSession` adapter over `_api_request` so the AST write-guard test still sees the guarded verbs.
  - `verify_experiment(experiment_id: int, lock_content: str, deep: bool = False) -> VerifyResult as JSON (bound, differences, render)`.
- A shared helper `_materialise_files(files) -> tempfile.TemporaryDirectory` and `_McpSession` (methods `get/post/put/post_file/get_bytes`, `base_url`) — `post_file` and `get_bytes` are the two things `_api_request` cannot do, so they use `httpx` directly with `_api_token`, and both call `_require_local_write_target()` for the mutating one.

- [ ] **Step 1: Write the failing tests**

```python
# orchestrator/tests/test_mcp_lock_tools.py
"""The lock-aware MCP surface: compile with pools passed as content, validate a
lock, upload a lock (dry-run by default, local-only), verify."""

import json
from typing import Any

import pytest

from pmspec.lock.io import dump_lock
from pmspec.tests.test_lock_io import make_lock
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
    assert out["lock"].startswith("pmspec_lock: 1") and out["seed"] == 5 and out["crawler_count"] == 1


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
    monkeypatch.setattr(s, "_api_request", lambda *a, **k: calls.append(a) or {"data": {}})
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
        from pmspec.lock.upload import UploadResult

        seen["mode"] = options.mode
        seen["session"] = session
        return UploadResult(181, "test-Study-20260911", 1, 1, "f" * 64, "2026-10-01T10:00:00+02:00",
                            "test", 3, "rec")

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
    from pmspec.lock.verify import Difference, VerifyResult

    monkeypatch.setattr(
        s, "verify_lock",
        lambda *a, **k: VerifyResult(7, False, "a" * 64, ["b" * 64], None,
                                     [Difference("crawlers.bot_01.params.pi", 1.0, 0.0)]),
    )
    out = json.loads(s.verify_experiment(7, dump_lock(make_lock())))
    assert out["bound"] is False and out["ok"] is False
    assert out["differences"] == [{"path": "crawlers.bot_01.params.pi", "expected": 1.0, "actual": 0.0}]
    assert out["render"].startswith("UNBOUND")
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest orchestrator/tests/test_mcp_lock_tools.py -v`
Expected: `AttributeError: module has no attribute 'compile_experiment_spec'` etc.

- [ ] **Step 3: Implement**

Module-level imports in `server.py` (so tests can patch them): 

```python
from pmspec.lock.build import compile_spec
from pmspec.lock.io import LockError, dump_lock, is_lock_document, load_lock
from pmspec.lock.secrets import SecretStore, find_placeholders
from pmspec.lock.upload import MODES, UploadOptions, UploadRefused, upload_lock
from pmspec.lock.verify import verify_lock
```

(`server.py` already imports pmspec lazily inside tools with an ImportError guard; these lock modules import only stdlib + yaml/jsonschema/requests, all hard dependencies, so a top-level import is fine.)

Helpers:

```python
def _materialise_files(files: dict[str, str] | None) -> tempfile.TemporaryDirectory[str]:
    """Pools passed as content become files in a temp dir used as the spec's base_dir."""
    tmp = tempfile.TemporaryDirectory(prefix="pmspec-")
    for name, content in (files or {}).items():
        target = Path(tmp.name) / Path(name).name  # no directory traversal
        target.write_text(content)
    return tmp


class _McpSession:
    """ApiSession-shaped adapter over the MCP's own authenticated request path."""

    def __init__(self) -> None:
        self.base_url = get_settings().pm_api_base_url

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return _unwrap(_api_request("GET", path, params=params))

    def post(self, path: str, json: dict[str, Any]) -> Any:
        return _unwrap(_api_request("POST", path, data=json))

    def put(self, path: str, json: dict[str, Any]) -> Any:
        return _unwrap(_api_request("PUT", path, data=json))

    def post_file(self, path: str, field: str, filename: str, content: str, content_type: str) -> Any:
        import httpx

        settings = get_settings()
        with httpx.Client(timeout=300.0) as client:
            token = _api_token(client, settings)
            r = client.post(
                f"{settings.pm_api_base_url}/{path}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                files={field: (filename, content, content_type)},
            )
            r.raise_for_status()
            return _unwrap(r.json()) if r.content else {}

    def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        import httpx

        settings = get_settings()
        with httpx.Client(timeout=300.0) as client:
            token = _api_token(client, settings)
            r = client.get(f"{settings.pm_api_base_url}/{path}", params=params,
                           headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            return r.content
```

Tools:

```python
@mcp.tool()
def compile_experiment_spec(
    yaml_content: str,
    files: dict[str, str] | None = None,
    seed: int | None = None,
    anchor: str | None = None,
    generator: str | None = None,
    lax: bool = False,
) -> str:
    """Compile a YAML spec into a lock: the self-contained, committable artifact
    upload_experiment and verify_experiment read.

    Args:
        yaml_content: the spec
        files: data-pool files by the bare filename the spec's `source:` names,
            content as text -- this is how a pool reaches a content-only caller
        seed: the one seed; required unless the spec sets design.seed
        anchor: 'upload' or a Europe/Zurich wall-clock ISO time; default derived from the spec
        generator: free-text provenance ("code/design/build_spec.R")
        lax: downgrade strict-contract errors to warnings (recorded in the lock)
    Returns: {"lock": <text>, "crawler_count", "seed", "anchor", "warnings"} or {"errors": [...]}
    """
    with _materialise_files(files) as tmp:
        result = compile_spec(
            yaml_content, base_dir=Path(tmp), spec_path="<mcp>", seed=seed, anchor=anchor,
            generator=generator, lax=lax, git_root=None,
        )
    if result.lock is None:
        return json.dumps({"errors": result.errors, "warnings": result.warnings})
    return json.dumps({
        "lock": dump_lock(result.lock),
        "crawler_count": len(result.lock.crawlers),
        "seed": result.lock.provenance.seed,
        "anchor": result.lock.schedule["anchor"],
        "warnings": result.warnings,
    })
```

`git_root=None` with a temp `base_dir` means `git_commit` is `None` for MCP-compiled locks; the caller's repo is not on this machine. `upload_experiment` in `real` mode will therefore require `allow_dirty` — document that in the tool docstring: "for a `real` upload, compile with the pmspec CLI in the committed repo".

`validate_experiment_spec`: add `files: dict[str, str] | None = None`; at the top:

```python
    if is_lock_document(yaml_content):
        try:
            lock = load_lock(yaml_content)
        except LockError as e:
            return json.dumps({"valid": False, "kind": "lock", "errors": e.errors, "warnings": []})
        return json.dumps({
            "valid": True, "kind": "lock", "errors": [], "warnings": [],
            "summary": {"experiment_name": lock.experiment["name"], "crawler_count": len(lock.crawlers),
                        "anchor": lock.schedule["anchor"], "seed": lock.provenance.seed},
        })
```

and replace `create_file_tag_loader(base_dir=Path.cwd())` / `SpecParser()` with the temp dir from `_materialise_files(files)` as `base_dir` (wrap the body in `with`). Add `"kind": "spec"` to the spec result.

`upload_experiment` — replace wholesale:

```python
@mcp.tool()
def upload_experiment(
    lock_content: str,
    mode: str,
    experiment_id: int | None = None,
    dry_run: bool = True,
    deep_verify: bool = False,
) -> str:
    """Upload a LOCK (from compile_experiment_spec or `pmspec compile`) and verify it.

    Dry-run by default; local-only (the API target must be localhost:8090).
    mode: 'test' -> VM runner, 'test-<name>-<date>', dirty commit allowed;
          'real' -> as in the lock, refused without a clean commit.
    Secrets: ${secret:...} placeholders resolve from PMSPEC_SECRETS_FILE on this host.
    """
    if mode not in MODES:
        return json.dumps({"success": False, "error": f"mode must be one of {MODES}"})
    if not is_lock_document(lock_content):
        return json.dumps({"success": False, "error": "not a lock: call compile_experiment_spec first"})
    try:
        lock = load_lock(lock_content)
    except LockError as e:
        return json.dumps({"success": False, "errors": e.errors})

    needed = sorted(find_placeholders(lock.crawlers))
    if dry_run:
        return json.dumps({
            "success": True, "dry_run": True, "mode": mode,
            "experiment_name": lock.experiment["name"], "crawler_count": len(lock.crawlers),
            "anchor": lock.schedule["anchor"], "git_commit": lock.provenance.git_commit,
            "secrets_needed": needed, "to_apply": "call again with dry_run=false",
        })

    guard = _require_local_write_target()
    if guard:
        return json.dumps({"success": False, "error": guard})

    store = None
    if needed:
        try:
            store = SecretStore.load()
        except FileNotFoundError:
            return json.dumps({"success": False, "error": "lock needs secrets but no secrets file exists on this host (PMSPEC_SECRETS_FILE)"})
        except PermissionError as e:
            return json.dumps({"success": False, "error": str(e)})

    session = _McpSession()
    options = UploadOptions(mode=mode, experiment_id=experiment_id, contact="PrimeMover Orchestrator MCP")
    try:
        result = upload_lock(lock_content, session=session, store=store, options=options)
    except UploadRefused as e:
        return json.dumps({"success": False, "error": str(e)})
    except Exception as e:
        return json.dumps({"success": False, "error": f"Upload failed: {e}"})

    verdict = verify_lock(lock_content, session=session, experiment_id=result.experiment_id, deep=deep_verify)
    return json.dumps({
        "success": True, "experiment_id": result.experiment_id, "experiment_name": result.experiment_name,
        "crawler_count": result.crawler_count, "total_jobs": result.total_jobs, "mode": result.mode,
        "runner_user_id": result.runner_user_id, "lock_sha256": result.lock_sha256, "anchor": result.anchor,
        "record": result.record_line,
        "verify": {"ok": verdict.ok, "bound": verdict.bound,
                   "differences": [d.__dict__ for d in verdict.differences], "render": verdict.render()},
    })


@mcp.tool()
def verify_experiment(experiment_id: int, lock_content: str, deep: bool = False) -> str:
    """Is the live experiment bound to this lock (hash) and structurally identical to it?"""
    try:
        r = verify_lock(lock_content, session=_McpSession(), experiment_id=experiment_id, deep=deep)
    except LockError as e:
        return json.dumps({"error": "invalid lock", "errors": e.errors})
    except Exception as e:
        return json.dumps({"error": str(e)})
    return json.dumps({
        "experiment_id": r.experiment_id, "ok": r.ok, "bound": r.bound, "local_sha256": r.local_sha256,
        "live_sha256s": r.live_sha256s, "anchor": r.anchor, "deep": r.deep,
        "differences": [d.__dict__ for d in r.differences], "render": r.render(),
    })
```

`convert_spec_to_json` stays as-is for GUI-upload users but its docstring should say "prefer compile_experiment_spec; this output carries no provenance".

- [ ] **Step 4: Run the guard test and the new tests**

Run: `venv/bin/pytest orchestrator/tests/test_mcp_write_guard.py orchestrator/tests/test_mcp_lock_tools.py orchestrator/tests/test_mcp_write_tools.py -v`
Expected: PASS. If `test_mcp_write_guard.py`'s AST walk flags `_McpSession.post_file` (an `httpx` post outside `_api_request`), the tool that reaches it — `upload_experiment` — already calls `_require_local_write_target()` before constructing a session; extend the AST test's notion of "guarded" to accept a guard call anywhere earlier in the same function body if it does not already. Do not weaken it to name-based.

- [ ] **Step 5: Update the doc resource list**

`server.py` serves `doc://experiment-designer` from `docs/pmspec.md`; nothing to change here beyond Task 12's doc edit.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/mcp_server/server.py orchestrator/tests/test_mcp_lock_tools.py orchestrator/tests/test_mcp_write_guard.py
git commit -m "mcp: compile_experiment_spec, verify_experiment; upload_experiment takes a lock and a mode"
```

---

### Task 11: `orchestrator experiment upload` stops being a third copy

**Files:**
- Modify: `orchestrator/cli.py:3352-3510` (`experiment_upload`)
- Test: `orchestrator/tests/test_cli_experiment_upload.py`

**Interfaces:**
- Consumes: Task 9's `_lock_text_for` pattern (re-implemented locally with `compile_spec` + `dump_lock`), Task 7 `ApiSession`, `upload_lock`, `UploadOptions`, `UploadRefused`, `MODES`; Task 8 `verify_lock`.
- Produces: `orchestrator experiment upload FILE --mode real|test [-e N] [--dry-run] [--seed N] [--record PATH] [--allow-dirty] [--allow-past] [--deep]` with the same behaviour as `pmspec upload`, using `ctx.obj["settings"]` (`pm_api_base_url`, `pm_api_email`, `pm_api_password`) for the session.

- [ ] **Step 1: Write the failing test**

```python
# orchestrator/tests/test_cli_experiment_upload.py
"""orchestrator experiment upload is a thin wrapper over pmspec.lock."""

from pathlib import Path

import pytest

from pmspec.lock.io import dump_lock
from pmspec.lock.upload import UploadResult
from pmspec.tests.test_lock_io import make_lock
from orchestrator import cli as cli_module
from orchestrator.cli import cli


@pytest.fixture
def lock_file(tmp_path: Path) -> Path:
    p = tmp_path / "s.lock.yaml"
    p.write_text(dump_lock(make_lock()))
    return p


def test_upload_delegates_and_uses_settings_credentials(cli_runner, lock_file, monkeypatch, mock_settings):
    seen = {}

    def fake_upload_lock(lock_text, *, session, store, options):
        seen["base_url"] = session.base_url
        seen["mode"] = options.mode
        return UploadResult(9, "n", 1, 1, "f" * 64, "a", options.mode, None, "rec")

    class _V:
        ok = True

        def render(self):
            return "BOUND"

    monkeypatch.setattr(cli_module, "upload_lock", fake_upload_lock)
    monkeypatch.setattr(cli_module, "verify_lock", lambda *a, **k: _V())
    monkeypatch.setattr("orchestrator.cli.get_settings", lambda: mock_settings)
    monkeypatch.setattr("pmspec.lock.secrets.SecretStore.load", classmethod(lambda cls, p=None: None))
    out = cli_runner.invoke(cli, ["experiment", "upload", str(lock_file), "--mode", "test"])
    assert out.exit_code == 0, out.output
    assert seen == {"base_url": "http://localhost:8090/api/v1", "mode": "test"}
    assert "rec" in out.output


def test_upload_requires_mode(cli_runner, lock_file):
    out = cli_runner.invoke(cli, ["experiment", "upload", str(lock_file)])
    assert out.exit_code != 0 and "--mode" in out.output
```

Check how `orchestrator/tests/test_cli.py` builds `ctx.obj` (the `cli_runner` fixture and whether `get_settings` is patched at `orchestrator.cli.get_settings`); adapt the monkeypatch target to what the existing CLI tests do.

- [ ] **Step 2: Run to verify it fails**

Run: `venv/bin/pytest orchestrator/tests/test_cli_experiment_upload.py -v`
Expected: `--mode` unknown option; `cli_module.upload_lock` missing.

- [ ] **Step 3: Implement**

Add module-level imports in `orchestrator/cli.py` next to the existing ones (the orchestrator CLI uses lazy imports inside commands to keep startup fast; these lock modules are light, but to respect the pattern import them inside the command *and* expose module-level names for patching):

```python
from pmspec.lock.upload import upload_lock  # noqa: E402  (patched in tests)
from pmspec.lock.verify import verify_lock  # noqa: E402
```

If startup-time measurement (`time orchestrator --help`) regresses by more than ~50 ms, move them inside the command and patch via `pmspec.lock.upload.upload_lock` in the test instead.

Replace the body of `experiment_upload` with:

```python
    from pathlib import Path

    from pmspec.lock.api import ApiError, ApiSession
    from pmspec.lock.build import compile_spec
    from pmspec.lock.io import dump_lock, is_lock_document
    from pmspec.lock.secrets import InsecureSecretsFile, SecretStore, find_placeholders
    from pmspec.lock.io import load_lock
    from pmspec.lock.upload import UploadOptions, UploadRefused

    settings = ctx.obj["settings"]
    file_path = Path(file)
    lock_text = file_path.read_text()
    if not is_lock_document(lock_text):
        result = compile_spec(lock_text, base_dir=file_path.parent, spec_path=str(file_path), seed=seed)
        if result.lock is None:
            for error in result.errors:
                console.print(f"[red]  {error}[/red]")
            raise SystemExit(1)
        lock_path = file_path.with_name(f"{file_path.stem}.lock.yaml")
        lock_text = dump_lock(result.lock)
        lock_path.write_text(lock_text)
        console.print(f"[cyan]Compiled {lock_path} -- commit it[/cyan]")

    try:
        store = SecretStore.load()
    except FileNotFoundError:
        store = None
    except InsecureSecretsFile as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1) from e
    needed = sorted(find_placeholders(load_lock(lock_text).crawlers))
    missing = store.missing(needed) if store else needed
    for key in needed:
        console.print(f"  {'MISSING' if key in missing else 'OK':7} {key}")
    if missing:
        raise SystemExit(1)
    if dry_run:
        console.print("[cyan]Dry run - not uploading[/cyan]")
        return

    session = ApiSession(settings.pm_api_base_url, settings.pm_api_email, settings.pm_api_password)
    options = UploadOptions(mode=mode, experiment_id=experiment_id, allow_dirty=allow_dirty,
                            allow_past=allow_past, contact="orchestrator CLI")
    try:
        result = upload_lock(lock_text, session=session, store=store, options=options)
    except (UploadRefused, ApiError) as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1) from e
    console.print(f"[green]Upload SUCCESS[/green] experiment {result.experiment_id} ({result.mode})")
    verdict = verify_lock(lock_text, session=session, experiment_id=result.experiment_id, deep=deep)
    console.print(verdict.render())
    if record:
        with open(record, "a") as f:
            f.write(result.record_line + "\n")
    console.print(result.record_line)
    if not verdict.ok:
        raise SystemExit(1)
```

with the new options on the decorator: `--mode` (`click.Choice(MODES)`, required), `--record`, `--allow-dirty`, `--allow-past`, `--deep`. Delete the old `requests` code.

- [ ] **Step 4: Run tests**

Run: `venv/bin/pytest orchestrator/tests/test_cli.py orchestrator/tests/test_cli_experiment_upload.py -q`
Expected: PASS. If an existing `test_cli.py` case exercises the old upload with `requests` mocked, replace it with the new test (there is no old behaviour worth keeping).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/cli.py orchestrator/tests/test_cli_experiment_upload.py orchestrator/tests/test_cli.py
git commit -m "orchestrator: experiment upload delegates to the lock uploader"
```

---

### Task 12: Documentation

**Files:**
- Modify: `docs/pmspec.md` (the "Workflow" section at the top, `:5-88`; add a new section "Locks, provenance and verify" after "Runner assignment"), `CLAUDE.md` (the "Uploading Experiments" and "Experiment Designer" sections), `.claude/skills/primemover-compose/SKILL.md` (the `upload_experiment` line at `:56`)
- No new tests; `make check` still runs.

- [ ] **Step 1: Rewrite the workflow steps 3–5 in `docs/pmspec.md`**

Replace Steps 3–5 and "The `--seed` Option" with:

````markdown
### Step 3: Compile to a lock

```bash
pmspec compile my-study.yaml --seed 42        # writes my-study.lock.yaml beside it
git add my-study.yaml my-study.lock.yaml && git commit -m "study: lock v1"
```

The **lock** is the reproducibility artifact: every crawler expanded, every random
draw fixed by the one seed, every data pool inlined, secrets left as
`${secret:…}` placeholders, and session starts expressed as offsets from a
`schedule.anchor`. It is committable by construction — `compile` refuses a literal
credential — and byte-deterministic: the same spec and seed always produce the same
file. Its header records the spec's sha256, the git commit it was compiled from
(`-dirty` if the tree was not clean), the generator, the pmspec version and the seed.

### Step 4: Upload the lock

```bash
pmspec secrets check my-study.lock.yaml       # which ${secret:…} keys it needs, none printed
pmspec upload my-study.lock.yaml --mode test  # VM runner, name prefixed test-…, dirty commit ok
pmspec upload my-study.lock.yaml --mode real  # as in the lock; refuses a dirty/unknown commit
```

`--mode` is required. Upload resolves `${secret:key}` from `~/.config/primemover/secrets.toml`
(mode 600; or `$PMSPEC_SECRETS_FILE`), resolves the anchor (`upload` = now, Europe/Zurich),
creates the experiment with a **provenance record** as experiment preferences
(`pmspec.lock_sha256`, `pmspec.seed`, `pmspec.anchor`, `pmspec.git_commit`, `pmspec.mode`, …),
uploads the crawlers, then immediately runs `verify`. `--record ledger.txt` appends a one-line
record (`<time> exp=<id> mode=… lock=… commit=… api=…`) you can commit next to the lock.

### Step 5: Verify, any time later

```bash
pmspec verify 181 my-study.lock.yaml          # BOUND/UNBOUND + structural diff
pmspec verify 181 my-study.lock.yaml --deep   # also jobs and behaviours (via the export)
```

`BOUND` means the server's `pmspec.lock_sha256` matches this file. The structural diff
compares params, configuration preferences, agent location, proxy host/port/type, and
each queue's start time (credentials are never compared or printed). `verify` says nothing
about execution — use `GET /experiments/{id}/health` for that.

### The seed

There is exactly one: `--seed N` (or `design.seed` when no flag is given). `compile`
refuses to produce an unseeded lock. Pools, file selections and design expansion all
derive from it, so the same seed reproduces every draw.
````

Then add, after "### Runner assignment":

````markdown
### Locks, provenance and verify — for generators

A lock can also be *written* directly by a generator (R, Python) instead of compiled from a
spec; `pmspec validate my.lock.yaml` checks it against `pmspec/schema/lock-1.schema.json`.
Shape:

```yaml
pmspec_lock: 1
provenance: {spec_path, spec_sha256, generator, git_commit, pmspec_version, compiled_at, seed, lax}
experiment: {name, description, runner_user_id}
schedule: {anchor: upload | "YYYY-MM-DDTHH:MM:SS"}   # Europe/Zurich wall clock
crawlers:            # the API upload shape, except:
  - …
    proxy: {…, username: "${secret:k.u}", password: "${secret:k.p}"}   # placeholders, never literals
    queues:
      - start_offset: "+5m"                                            # not start_at
```

Through the MCP: `compile_experiment_spec(yaml_content, files={"pool.csv": "<content>"}, seed=…)`
returns the lock text; `upload_experiment(lock_content, mode=…)` and
`verify_experiment(experiment_id, lock_content)` take it from there. A lock compiled through the
MCP carries no git commit (the repo is not on that host), so a `real` upload needs the CLI in the
committed repo.
````

- [ ] **Step 2: CLAUDE.md**

In "Uploading Experiments (JSON Scenario)", add one sentence at the top: "For anything that is a study rather than a one-off scenario, use a **lock** (`pmspec compile` → `pmspec upload --mode …` → `pmspec verify`); the raw JSON path below records no provenance." In "Experiment Designer", after the first paragraph, add: "The reproducibility artifact is the *lock* (`docs/pmspec.md` § Locks): committed, hashed, bound to the experiment via `pmspec.*` preferences, checked by `pmspec verify`. `upload_experiment` takes a lock and a required `mode`."

- [ ] **Step 3: Skill**

`.claude/skills/primemover-compose/SKILL.md:56` — change the upload line to: "`compile_experiment_spec(yaml, files=…, seed=…)` → `upload_experiment(lock, mode='test', dry_run=true)` then `dry_run=false`; `verify_experiment(id, lock)` afterwards. Never upload a spec; `real` mode wants a lock compiled by the CLI in the committed repo."

- [ ] **Step 4: Run the full check and commit**

Run: `make check`
Expected: format/lint/typecheck/test all pass.

```bash
git add docs/pmspec.md CLAUDE.md .claude/skills/primemover-compose/SKILL.md
git commit -m "docs: spec -> lock -> upload -> verify"
```

---

## Not in this plan (later phases of the same spec)

- §4 strict contract (`--lax` today only stamps `provenance.lax`; nothing is downgraded yet because nothing is promoted yet).
- §5 `cleanup`, `status`, `diff`, the doc/skill rewrite around the lock (Task 12 is the minimum to make phase 1 discoverable).
- Deep verify is opt-in (`--deep`) because the export creates a zip in the API's own bucket per call; the spec's "ordered job list" diff is delivered, but not run by default after every upload.
