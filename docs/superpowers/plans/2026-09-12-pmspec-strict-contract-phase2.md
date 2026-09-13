# pmspec strict contract (phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Anything the compiler would drop, ignore or leave unresolved becomes an error; `--lax` downgrades it to a recorded warning. Two documented DSL features that never worked — `proxy.from_pool` and `additional_sessions` — are implemented, custom decision-strategy schemas can be registered, and the PyYAML timestamp crash goes away.

**Architecture:** One mechanism, `Drops`: a small collector that every layer (validator, parser, converter, compiler) reports "would be dropped" constructs into; strict mode turns them into errors, lax mode into warnings plus a count. `SpecValidator`, `SpecParser` and `SpecConverter` gain a `lax` flag; `compile_spec` finally runs the validator (it never did) and threads `lax` through. New behaviour lives next to the code it corrects: proxy pools in the converter (where the refusal is today), `additional_sessions` in the parser's crawler expansion, strategy schema registration in the validator's existing `_strategy_schema`.

**Tech Stack:** Python 3.10+, PyYAML, jsonschema (existing). Tests: pytest, `click.testing.CliRunner`.

**Spec:** `docs/superpowers/specs/2026-09-11-pmspec-lock-and-provenance-design.md` §4 (binding). Deviations, decided during planning: (1) unknown-key detection is done in the validator against explicit key sets rather than `additionalProperties: false` in `experiment-spec.schema.json`, because that schema has drifted from the parser (no `data_pools`, no `strategy`/`treatment`, no `from_pool`) and flipping it would reject every real spec; (2) `proxy.from_pool` with several matching rows assigns each row to at most one crawler instead of erroring — CLAUDE.md's "one stable exit IP per bot" is the requirement; (3) `additional_sessions` (used by the shipped example and the autocompleted study, silently ignored today) is implemented, not rejected.

## Global Constraints

- Line length 100; `make check` passes after every task (baseline 1394 passed, 33 skipped).
- Python 3.10 compatible.
- Strict is the default everywhere (`pmspec validate/compile/upload/generate`, MCP `validate_experiment_spec`/`compile_experiment_spec`); `--lax` / `lax=True` is the only way to downgrade, and a lax compile stamps `provenance.lax: true` (already in the lock format) and prints `N construct(s) would have been dropped`.
- Every message names the construct and its path (`crawlers[12].session.workflow[3].lambda_select`), one line.
- Nothing new may call `warnings.append` in `validator.py`/`parser.py`/`converter.py` without a comment saying why it is not a drop.
- Timestamps: Europe/Zurich wall clock via `time_utils`; a `datetime`/`date` scalar from PyYAML is normalised, never `.strip()`ed.
- Commit after every task with the session's attribution trailer.

## File structure

| path | responsibility |
|---|---|
| `pmspec/spec/drops.py` (new) | `Drops` collector: `report(path, what, fix)`; `errors`/`warnings` views by mode; `count` |
| `pmspec/spec/validator.py` (modify) | `SpecValidator(lax=)`; unknown-key check; drop sites; strategy schema registry |
| `pmspec/spec/parser.py` (modify) | `SpecParser(lax=)`; `additional_sessions`; datetime normalisation |
| `pmspec/spec/converter.py` (modify) | `SpecConverter(lax=)`; unresolved-variable scan; proxy pools |
| `pmspec/lock/build.py` (modify) | runs the validator; threads `lax`; drop count in warnings |
| `pmspec/cli.py`, `orchestrator/mcp_server/server.py` (modify) | `--lax`/`lax=` on validate; drop count surfaced |
| `pmspec/tests/test_strict_contract.py` (new) | table-driven: one row per §4 rule, strict vs lax |
| `pmspec/tests/test_proxy_pools.py`, `test_additional_sessions.py`, `test_strategy_schemas.py` (new) | the three features |
| `docs/pmspec.md`, `CLAUDE.md` (modify) | the contract, `--lax`, proxy pools, `strategy_schemas`, `additional_sessions` |

---

### Task 1: The `Drops` collector and `lax` on the three components

**Files:**
- Create: `pmspec/spec/drops.py`
- Modify: `pmspec/spec/validator.py` (`ValidationResult`, `SpecValidator.__init__`, `validate`), `pmspec/spec/parser.py` (`ParsedSpec`, `SpecParser.__init__`), `pmspec/spec/converter.py` (`ConversionResult`, `SpecConverter.__init__`)
- Test: `pmspec/tests/test_strict_contract.py` (the collector part)

**Interfaces:**
- Produces:
  ```python
  # drops.py
  class Drops:
      def __init__(self, lax: bool = False) -> None
      def report(self, path: str, what: str, fix: str) -> None   # message = f"{path}: {what}; {fix}"
      @property messages: list[str]; count: int; lax: bool
      def merge_into(self, errors: list[str], warnings: list[str]) -> None
          # strict: messages -> errors; lax: messages -> warnings, plus one trailing
          # warning f"{count} construct(s) would have been dropped (lax mode)" when count > 0
  ```
  `ValidationResult.dropped: int`, `ParsedSpec.dropped: int`, `ConversionResult.dropped: int` (default 0); `SpecValidator(schema_path=None, *, lax=False)`, `SpecParser(base_dir=None, seed=None, *, lax=False)`, `SpecConverter(*, seed=None, resolve_times=True, lax=False)`; each owns `self.drops = Drops(lax)` and calls `self.drops.merge_into(errors, warnings)` before building its result, setting `dropped=self.drops.count`.

- [ ] **Step 1: Write the failing tests**

```python
# pmspec/tests/test_strict_contract.py
"""§4 of the lock design: whatever would be dropped, ignored or left unresolved is
an error; --lax turns it into a warning and a count. One row per rule."""

from __future__ import annotations

import pytest

from pmspec.spec.converter import SpecConverter
from pmspec.spec.drops import Drops
from pmspec.spec.parser import SpecParser
from pmspec.spec.validator import SpecValidator


class TestDrops:
    def test_strict_puts_messages_in_errors(self):
        d = Drops()
        d.report("crawlers[0].configuration.params.foo", "unknown parameter", "remove it")
        errors: list[str] = []
        warnings: list[str] = []
        d.merge_into(errors, warnings)
        assert errors == ["crawlers[0].configuration.params.foo: unknown parameter; remove it"]
        assert warnings == [] and d.count == 1

    def test_lax_puts_messages_in_warnings_with_a_count(self):
        d = Drops(lax=True)
        d.report("a", "x", "y")
        d.report("b", "x", "y")
        errors: list[str] = []
        warnings: list[str] = []
        d.merge_into(errors, warnings)
        assert errors == []
        assert warnings[:2] == ["a: x; y", "b: x; y"]
        assert warnings[2] == "2 construct(s) would have been dropped (lax mode)"

    def test_no_drops_no_count_line(self):
        warnings: list[str] = []
        Drops(lax=True).merge_into([], warnings)
        assert warnings == []


class TestComponentsCarryLax:
    def test_validator_result_has_dropped(self):
        result = SpecValidator().validate({"experiment": {"name": "x"}, "crawlers": [{"name": "b"}]})
        assert hasattr(result, "dropped") and result.dropped == 0

    def test_parser_and_converter_accept_lax(self):
        parsed = SpecParser(lax=True).parse("experiment: {name: x}\ncrawlers:\n  - name: b\n")
        assert parsed.dropped == 0
        assert SpecConverter(lax=True).convert(parsed).dropped == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest pmspec/tests/test_strict_contract.py -v`
Expected: `ModuleNotFoundError: pmspec.spec.drops`.

- [ ] **Step 3: Write drops.py**

```python
"""The strict contract's one mechanism.

A *drop* is a construct the pipeline would silently discard, ignore or leave
unresolved -- an unknown params key, a treatment without a strategy, an
unresolved ${var}. Every layer reports drops here; strict mode (the default)
turns them into errors, lax mode into warnings plus a count that the lock and
the upload record carry (provenance.lax / pmspec.lax)."""

from __future__ import annotations


class Drops:
    def __init__(self, lax: bool = False) -> None:
        self.lax = lax
        self._messages: list[str] = []

    def report(self, path: str, what: str, fix: str) -> None:
        self._messages.append(f"{path}: {what}; {fix}")

    @property
    def messages(self) -> list[str]:
        return list(self._messages)

    @property
    def count(self) -> int:
        return len(self._messages)

    def merge_into(self, errors: list[str], warnings: list[str]) -> None:
        if not self._messages:
            return
        if self.lax:
            warnings.extend(self._messages)
            warnings.append(f"{self.count} construct(s) would have been dropped (lax mode)")
        else:
            errors.extend(self._messages)
```

- [ ] **Step 4: Thread it through the three components**

`validator.py`: add `dropped: int = 0` to `ValidationResult`; `__init__(self, schema_path=None, *, lax=False)` sets `self.lax = lax` and `self.drops = Drops(lax)` — but `validate()` may be called more than once per instance, so reset `self.drops = Drops(self.lax)` at the top of `validate()`; before `return ValidationResult(...)`, call `self.drops.merge_into(errors, warnings)` and pass `dropped=self.drops.count`.

`parser.py`: `dropped: int = 0` on `ParsedSpec`; `__init__(self, base_dir=None, seed=None, *, lax=False)`; `self.drops = Drops(self.lax)` at the top of `parse()` next to `self.errors = []`; merge + `dropped=` in the final `ParsedSpec(...)` (both the early-return error paths can pass `dropped=0`).

`converter.py`: `dropped: int = 0` on `ConversionResult`; `__init__(self, *, seed=None, resolve_times=True, lax=False)`; `self.drops = Drops(self.lax)` at the top of `convert()`; merge + `dropped=` in the final `ConversionResult(...)`.

- [ ] **Step 5: Run tests**

Run: `venv/bin/pytest pmspec/tests/ -q`
Expected: PASS (no behaviour changed yet).

- [ ] **Step 6: Commit**

```bash
git add pmspec/spec/drops.py pmspec/spec/validator.py pmspec/spec/parser.py pmspec/spec/converter.py pmspec/tests/test_strict_contract.py
git commit -m "designer: Drops collector and lax flag on validator, parser, converter"
```

---

### Task 2: Validator drop sites — unknown params, treatment without strategy, unknown job type, missing start, unknown keys

**Files:**
- Modify: `pmspec/spec/validator.py` (`_validate_treatment_params` ~L195-245, `_validate_session` ~L385-405, `_validate_job_shorthand` ~L557-563, `_validate_lambda_select_treatment` ~L514-530, `_validate_variable_references` ~L636-665; new `_validate_unknown_keys`)
- Test: `pmspec/tests/test_strict_contract.py` (extend)

**Interfaces:**
- Consumes: Task 1 `self.drops`.
- Produces: the rules below. Key sets as class constants:
  ```python
  _KEYS_ROOT = {"version", "experiment", "defaults", "templates", "design", "data_pools", "strategy_schemas", "crawlers"}
  _KEYS_EXPERIMENT = {"name", "description", "runner_user_id", "generator"}
  _KEYS_CRAWLER = {"from_design", "name", "description", "active", "testing", "configuration", "proxy", "agent", "session", "additional_sessions", "queues"}
  _KEYS_SESSION = {"name", "description", "start", "start_at", "timezone", "timezone_utc_offset", "active", "workflow", "jobs"}
  _KEYS_PROXY = {"name", "description", "hostname", "host", "port", "username", "password", "type", "active", "location", "from_pool", "pool", "match_field"}
  _KEYS_AGENT = {"name", "description", "identification", "location", "multilogin_id", "multilogin_profile", "active", "custom_start_urls"}
  _KEYS_CONFIGURATION = {"name", "description", "params", "preferences", "active"}
  _KEYS_DEFAULTS = {"location", "browser", "timezone", "timezone_utc_offset", "captcha_mode", "proxy_type", "typing_mode", "selection_type", "active"}
  _KEYS_DESIGN = {"type", "seed", "factors", "parameters", "replications", "total_crawlers"}
  ```
  Verify each set against the parser/converter/expander `.get("…")` calls before finalising (grep `raw.get(`, `crawler.get(`, `expanded[`, `session.get(`, `proxy.get(`, `agent.get(`, `config.get(`, `design.get(` in `parser.py`, `converter.py`, `design_expander.py`); add any key they read that is missing here, and say so in the report. Workflow items and job option dicts are **not** checked (unknown job *types* are; unknown option keys inside a known job are deferred — comment says why).

- [ ] **Step 1: Write the failing tests** (append to `test_strict_contract.py`)

```python
BASE = """
version: "1.0"
experiment:
  name: Strict
crawlers:
  - name: bot
    session:
      start: "+1m"
      workflow:
        - visit: https://example.com
"""


def _validate(yaml_text: str, lax: bool = False):
    import yaml

    return SpecValidator(lax=lax).validate(yaml.safe_load(yaml_text))


class TestValidatorDropRules:
    """One row per §4 rule: strict -> error, lax -> warning + count."""

    ROWS = [
        pytest.param(
            BASE.replace("  - name: bot\n", "  - name: bot\n    configuration:\n      params: {reliability: high}\n"),
            "crawlers[0].configuration.params.reliability",
            id="unknown-params-key",
        ),
        pytest.param(
            BASE + "design:\n  factors:\n    arm:\n      levels: [a, b]\n      maps_to:\n        params.lean: {a: L, b: R}\n  replications: 1\n",
            "design.factors.arm.maps_to.params.lean",
            id="unknown-maps_to-param",
        ),
        pytest.param(
            BASE.replace("        - visit: https://example.com\n",
                         "        - lambda_select:\n            task: t\n            function: f\n            treatment: {reliability: high}\n"),
            "lambda_select",
            id="treatment-without-strategy",
        ),
        pytest.param(
            BASE.replace("        - visit: https://example.com\n", "        - teleport: https://example.com\n"),
            "unknown job type 'teleport'",
            id="unknown-job-type",
        ),
        pytest.param(
            BASE.replace('      start: "+1m"\n', ""),
            "crawler[0].session: no start time",
            id="missing-session-start",
        ),
        pytest.param(BASE + "sceduled: true\n", "sceduled", id="unknown-root-key"),
        pytest.param(
            BASE.replace("  - name: bot\n", "  - name: bot\n    proxxy: {hostname: h}\n"),
            "crawler[0].proxxy",
            id="unknown-crawler-key",
        ),
        pytest.param(
            BASE.replace('      start: "+1m"\n', '      start: "+1m"\n      begin: now\n'),
            "crawler[0].session.begin",
            id="unknown-session-key",
        ),
    ]

    @pytest.mark.parametrize("yaml_text,needle", ROWS)
    def test_strict_is_an_error(self, yaml_text, needle):
        r = _validate(yaml_text)
        assert not r.valid
        assert any(needle in e for e in r.errors), r.errors
        assert r.dropped >= 1

    @pytest.mark.parametrize("yaml_text,needle", ROWS)
    def test_lax_is_a_warning_with_a_count(self, yaml_text, needle):
        r = _validate(yaml_text, lax=True)
        assert r.valid, r.errors
        assert any(needle in w for w in r.warnings), r.warnings
        assert any("would have been dropped (lax mode)" in w for w in r.warnings)
        assert r.dropped >= 1


class TestValidatorKeepsWarnings:
    def test_self_configuring_proxy_without_host_is_only_a_warning(self):
        r = _validate(BASE.replace("  - name: bot\n", "  - name: bot\n    proxy: {type: oxylabs}\n"))
        assert r.valid and r.dropped == 0
        assert any("no hostname" in w for w in r.warnings)

    def test_every_example_spec_and_research_spec_uses_only_known_keys(self):
        """The key sets are a whitelist; this pins them to the specs that exist."""
        import glob
        from pathlib import Path

        import yaml

        from pmspec.spec.file_loader import create_file_tag_loader

        paths = glob.glob("pmspec/examples/*.yaml") + [
            p for p in [
                "/home/umatter/projects/autocompleted_misinformation/design/experiment_spec.yaml",
            ] if Path(p).exists()
        ]
        for p in paths:
            loader, _ = create_file_tag_loader(base_dir=Path(p).parent)
            raw = yaml.load(Path(p).read_text(), Loader=loader)
            r = SpecValidator().validate(raw)
            unknown = [e for e in r.errors if "unknown key" in e]
            assert unknown == [], (p, unknown)
```

The last test reads a file outside the repo when present; that is deliberate — it is the spec this phase exists to keep compiling — and it skips silently on a machine without it.

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest pmspec/tests/test_strict_contract.py -v -k "DropRules or KeepsWarnings"`
Expected: the strict rows fail (`r.valid` is True today); the key-set test fails with `AttributeError`/no "unknown key" handling — either way RED.

- [ ] **Step 3: Implement the drop sites**

In `_validate_treatment_params`, both `warnings.append(...)` for unknown params become:

```python
                    self.drops.report(
                        f"design.factors.{fname}.maps_to.{path}",
                        f"'{leaf}' is not carried by the API/runner/decision-function pipeline",
                        "route it through a template `with:` variable or a lambda_select "
                        "`strategy` + `treatment` block",
                    )
```
and
```python
                    self.drops.report(
                        f"crawler[{i}].configuration.params.{key}",
                        "unknown parameter, dropped by the pipeline",
                        "only pi/psi/kappa/tau/alpha/beta/active/search_terms/media_outlet_urls "
                        "reach the bot; use a configuration preference or a treatment block",
                    )
```

`_validate_lambda_select_treatment`, the treatment-without-strategy warning → `self.drops.report(f"{prefix}.lambda_select", "'treatment' is set but 'strategy' is not, so the decision function ignores it", "add strategy: <name>")`.

`_validate_job_shorthand`, unknown job type → `self.drops.report(prefix, f"unknown job type '{job_type}'", "see `pmspec schema jobs`")`.

`_validate_session`, missing start → `self.drops.report(prefix, "no start time", "every session needs start: now | +5m | <ISO wall clock>")`.

`_validate_variable_references`: delete the "may not be defined" heuristic warning (the converter's post-expansion scan in Task 3 replaces it); keep the method returning empty lists, with a comment pointing at `SpecConverter._scan_unresolved`.

New `_validate_unknown_keys(spec)` called as step 0 in `validate()` (before schema validation, so its message is the first thing a reader sees):

```python
    def _validate_unknown_keys(self, spec: dict[str, Any]) -> None:
        """Keys the pipeline never reads are dropped silently; report them.

        The JSON schema is a drifted, partial mirror of the parser, so the
        parser's accepted keys are the authority here, not additionalProperties.
        Workflow items are not checked: unknown job *types* are, but option keys
        inside a known job vary per type and false positives there would cost
        more than the rare typo they would catch.
        """

        def check(obj: Any, allowed: set[str], path: str) -> None:
            if not isinstance(obj, dict):
                return
            for key in obj:
                if key not in allowed:
                    self.drops.report(f"{path}.{key}" if path else str(key), "unknown key", "remove it or fix the spelling")

        check(spec, self._KEYS_ROOT, "")
        check(spec.get("experiment"), self._KEYS_EXPERIMENT, "experiment")
        check(spec.get("defaults"), self._KEYS_DEFAULTS, "defaults")
        check(spec.get("design"), self._KEYS_DESIGN, "design")
        for i, crawler in enumerate(spec.get("crawlers") or []):
            prefix = f"crawler[{i}]"
            check(crawler, self._KEYS_CRAWLER, prefix)
            if not isinstance(crawler, dict):
                continue
            check(crawler.get("configuration"), self._KEYS_CONFIGURATION, f"{prefix}.configuration")
            if isinstance(crawler.get("proxy"), dict):
                check(crawler["proxy"], self._KEYS_PROXY, f"{prefix}.proxy")
            check(crawler.get("agent"), self._KEYS_AGENT, f"{prefix}.agent")
            if isinstance(crawler.get("session"), dict):
                check(crawler["session"], self._KEYS_SESSION, f"{prefix}.session")
            for j, extra in enumerate(crawler.get("additional_sessions") or []):
                check(extra, self._KEYS_SESSION, f"{prefix}.additional_sessions[{j}]")
            for j, queue in enumerate(crawler.get("queues") or []):
                check(queue, self._KEYS_SESSION, f"{prefix}.queues[{j}]")
```

The proxy-without-hostname warnings at ~L374-377 get a comment: `# A warning, not a drop: a self-configuring type (oxylabs) has no host by design.` and are skipped entirely when `proxy.get("type") in ("oxylabs",)` or `proxy.get("from_pool") is True`. The required-behaviour fallback warning (~L612) keeps its existing comment (defaults are substituted, nothing is dropped).

- [ ] **Step 4: Run tests**

Run: `venv/bin/pytest pmspec/tests/ -q`
Expected: the new rows pass. Existing tests that asserted a *warning* for these cases (`test_strategy_treatment.py::test_treatment_without_strategy_warns`, and any in `test_spec.py` asserting `valid` with an unknown param or a missing start) now see errors: update them to assert the error in strict mode and the warning under `SpecValidator(lax=True)`, and list each one you changed in the report.

- [ ] **Step 5: Commit**

```bash
git add pmspec/spec/validator.py pmspec/tests/
git commit -m "designer: validator reports drops -- unknown params/keys, treatment without strategy, unknown job type, missing start"
```

---

### Task 3: Converter — post-expansion unresolved-variable scan; parser — datetime scalars

**Files:**
- Modify: `pmspec/spec/converter.py` (`convert`, new `_scan_unresolved`), `pmspec/spec/parser.py` (`_expand_session` ~L331)
- Test: `pmspec/tests/test_strict_contract.py` (extend)

**Interfaces:**
- Produces: `SpecConverter._scan_unresolved(api_crawlers) -> None` reporting into `self.drops` for every string matching `_UNRESOLVED_RE = re.compile(r"\$\{(?!secret:)[^}]*\}|\{[A-Za-z_][A-Za-z0-9_]*(?::[^}]*)?\}")`; path like `crawlers[3].queues[0].jobs[2].behaviors.text`. Skips `agent.multilogin_profile` entirely (it is a JSON document with `%%PLACEHOLDER%%` tokens, not DSL variables). Parser: a `datetime`/`date` value for `start`/`start_at` becomes `value.isoformat()` (a `date` gets `T00:00:00` appended).

- [ ] **Step 1: Write the failing tests**

```python
class TestUnresolvedVariables:
    def _convert(self, yaml_text, lax=False):
        parsed = SpecParser(lax=lax).parse(yaml_text)
        assert not parsed.errors, parsed.errors
        return SpecConverter(lax=lax).convert(parsed)

    def test_template_variable_not_supplied_is_a_drop(self):
        text = BASE.replace(
            "    session:\n      start: \"+1m\"\n      workflow:\n        - visit: https://example.com\n",
            "    session:\n      start: \"+1m\"\n      workflow:\n        - template: t\n",
        ) + "templates:\n  t:\n    - type: {text: '${reliability}', selector: '//x'}\n"
        r = self._convert(text)
        assert any("${reliability}" in e and "crawlers[0].queues[0].jobs[0]" in e for e in r.errors), r.errors
        lax = self._convert(text, lax=True)
        assert not lax.errors and lax.dropped == 1

    def test_design_variable_not_defined_is_a_drop(self):
        text = BASE.replace("  - name: bot\n", "  - name: bot_{arm}_{colour}\n    from_design: true\n") + \
            "design:\n  factors:\n    arm:\n      levels: [a, b]\n  replications: 1\n"
        r = self._convert(text)
        assert any("{colour}" in e for e in r.errors), r.errors
        assert not any("{arm}" in e for e in r.errors)

    def test_supplied_with_variable_is_fine(self):
        text = BASE.replace(
            "        - visit: https://example.com\n",
            "        - template: t\n          with: {reliability: high}\n",
        ) + "templates:\n  t:\n    - type: {text: '${reliability}', selector: '//x'}\n"
        assert self._convert(text).errors == []

    def test_multilogin_profile_json_is_not_scanned(self):
        text = BASE.replace(
            "  - name: bot\n",
            "  - name: bot\n    agent:\n      multilogin_profile: {notes: 'keep {this} and ${that}'}\n",
        )
        assert self._convert(text).errors == []


class TestTimestampScalars:
    def test_unquoted_iso_timestamp_does_not_crash(self):
        text = BASE.replace('start: "+1m"', "start: 2026-10-01T10:00:00")
        parsed = SpecParser().parse(text)
        assert not parsed.errors, parsed.errors
        r = SpecConverter().convert(parsed)
        assert r.json_data[0]["queues"][0]["start_at"].startswith("2026-10-01T10:00:00")

    def test_unquoted_date_becomes_midnight(self):
        text = BASE.replace('start: "+1m"', "start: 2026-10-01")
        r = SpecConverter().convert(SpecParser().parse(text))
        assert r.json_data[0]["queues"][0]["start_at"].startswith("2026-10-01T00:00:00")
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest pmspec/tests/test_strict_contract.py -v -k "Unresolved or Timestamp"`
Expected: unresolved rows pass through with no error today (fail); timestamp rows raise `AttributeError: 'datetime.datetime' object has no attribute 'strip'`.

- [ ] **Step 3: Implement**

`converter.py`:

```python
_UNRESOLVED_RE = re.compile(r"\$\{(?!secret:)[^}]*\}|\{[A-Za-z_][A-Za-z0-9_]*(?::[^}]*)?\}")
```

```python
    def _scan_unresolved(self, api_crawlers: list[dict[str, Any]]) -> None:
        """Report every DSL variable that survived expansion.

        `${var}` is a template variable nobody supplied via `with:`; `{var}` is a
        design variable no factor or parameter defines. Either reaches the runner
        (and the decision function) as a literal string, which is the silent
        failure the strict contract exists to end. The Multilogin profile is
        skipped: it is a JSON document with its own %%TOKEN%% placeholders.
        """

        def walk(node: Any, path: str) -> None:
            if isinstance(node, str):
                for m in _UNRESOLVED_RE.finditer(node):
                    self.drops.report(
                        path,
                        f"unresolved variable {m.group(0)!r} would reach the bot as a literal",
                        "supply it with `with:` on the template call, or define it as a "
                        "factor/parameter",
                    )
            elif isinstance(node, dict):
                for k, v in node.items():
                    if k == "multilogin_profile":
                        continue
                    walk(v, f"{path}.{k}")
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{path}[{i}]")

        for i, crawler in enumerate(api_crawlers):
            walk(crawler, f"crawlers[{i}]")
```

Call `self._scan_unresolved(api_crawlers)` in `convert()` after the crawler loop, before building the result. For the path to read `jobs[2].behaviors.text`, behaviours are a list of `{name, value}` dicts in the API shape — make `walk` special-case a list item that is a dict with `name` and `value` keys: use `f"{path}.{item['name']}"` and walk only `value`.

`parser.py` `_expand_session`, right after the `start` → `start_at` rename:

```python
        # PyYAML resolves an unquoted 2026-10-01T10:00:00 to a datetime; the
        # time helpers expect the ISO string the author wrote.
        start = expanded.get("start_at")
        if isinstance(start, datetime):
            expanded["start_at"] = start.isoformat()
        elif isinstance(start, date):
            expanded["start_at"] = f"{start.isoformat()}T00:00:00"
```
with `from datetime import date, datetime` (check `datetime` is tested before `date`; a `datetime` is a `date`). The validator's `_is_valid_timestamp` gets the same normalisation (`str(value)` after the isinstance check) so an unquoted timestamp validates too.

- [ ] **Step 4: Run tests**

Run: `venv/bin/pytest pmspec/tests/ -q`
Expected: PASS. If an existing example spec now reports an unresolved variable, that is a real finding — report it, do not weaken the scan.

- [ ] **Step 5: Commit**

```bash
git add pmspec/spec/converter.py pmspec/spec/parser.py pmspec/spec/validator.py pmspec/tests/test_strict_contract.py
git commit -m "designer: unresolved variables are drops; unquoted timestamps no longer crash"
```

---

### Task 4: `additional_sessions`

**Files:**
- Modify: `pmspec/spec/parser.py` (`_expand_crawler` ~L295-306)
- Test: `pmspec/tests/test_additional_sessions.py`

**Interfaces:**
- Produces: a crawler with `session:` and `additional_sessions: [ … ]` expands to `queues = [session] + additional_sessions`, each through `_expand_session`; `additional_sessions` without `session` is a parser error (`crawler[i]: additional_sessions needs a session`); `queues:` together with either is an error.

- [ ] **Step 1: Write the failing tests**

```python
# pmspec/tests/test_additional_sessions.py
"""`additional_sessions` was in the shipped example and in a real study's spec, and
the parser ignored it -- the study's measurement sessions were silently dropped."""

from pmspec.spec.converter import SpecConverter
from pmspec.spec.parser import SpecParser

SPEC = """
version: "1.0"
experiment:
  name: Extra
crawlers:
  - name: bot
    session:
      name: Diet
      start: "+1m"
      workflow:
        - visit: https://a
    additional_sessions:
      - name: Measure
        start: "+2h"
        workflow:
          - visit: https://b
          - wait: 1s
"""


def test_additional_sessions_become_queues():
    parsed = SpecParser().parse(SPEC)
    assert not parsed.errors, parsed.errors
    queues = parsed.crawlers[0]["queues"]
    assert [q["name"] for q in queues] == ["Diet", "Measure"]
    assert len(queues[1]["jobs"]) == 2
    result = SpecConverter().convert(parsed)
    assert result.total_jobs == 3


def test_additional_sessions_without_session_is_an_error():
    text = SPEC.replace("    session:\n      name: Diet\n      start: \"+1m\"\n      workflow:\n        - visit: https://a\n", "")
    parsed = SpecParser().parse(text)
    assert any("additional_sessions needs a session" in e for e in parsed.errors), parsed.errors


def test_queues_and_session_together_is_an_error():
    text = SPEC + "    queues:\n      - name: q\n        start: now\n        jobs: []\n"
    parsed = SpecParser().parse(text)
    assert any("queues" in e and "session" in e for e in parsed.errors), parsed.errors
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest pmspec/tests/test_additional_sessions.py -v`
Expected: first test fails (`["Diet"]` only); the others fail (no error raised).

- [ ] **Step 3: Implement** in `_expand_crawler`:

```python
        # Expand session/queues
        extra = expanded.pop("additional_sessions", None)
        if "session" in expanded and "queues" in expanded:
            self.errors.append(f"crawler[{index}]: use either session/additional_sessions or queues, not both")
        if "session" in expanded:
            session = expanded.pop("session")
            expanded["queues"] = [self._expand_session(session, defaults, templates)]
            for extra_session in extra or []:
                expanded["queues"].append(self._expand_session(extra_session, defaults, templates))
        elif extra:
            self.errors.append(f"crawler[{index}]: additional_sessions needs a session to add to")
            expanded["queues"] = []
        elif "queues" in expanded:
            ...
```
`_expand_crawler` needs the crawler index for the message — check its signature (`_expand_crawler(self, crawler, defaults, templates, ...)`) and how `parse()` calls it in the loop (`for i, crawler_raw in enumerate(crawlers_raw)`); pass `index=i` through, defaulting to 0 for other callers. Also add `additional_sessions` to the design expander's substitution scope if `_substitute_all_variables` walks the whole crawler (it does — `crawler` is walked wholesale), so `{arm}` inside an additional session resolves; add a test row with `from_design: true` and `name: "M {arm}"` in an additional session asserting the queue name is substituted.

- [ ] **Step 4: Run tests**

Run: `venv/bin/pytest pmspec/tests/ -q`
Expected: PASS. `examples/websearch-polarization-study.yaml` now yields more queues; if a test pins its queue/job count, update the number and say so.

- [ ] **Step 5: Commit**

```bash
git add pmspec/spec/parser.py pmspec/tests/test_additional_sessions.py
git commit -m "designer: additional_sessions expands to extra queues instead of being ignored"
```

---

### Task 5: Proxy pools — `proxy.from_pool`

**Files:**
- Modify: `pmspec/spec/converter.py` (`convert` — replace the refusal at ~L200-208), `pmspec/spec/data_pools.py` (helper to fetch rows by pool name)
- Test: `pmspec/tests/test_proxy_pools.py`

**Interfaces:**
- Produces: for `proxy: {from_pool: true, pool: <name>, match_field: <column>}` — `pool` names a `data_pools` entry whose rows carry `hostname` (or `host`), `port`, `username`, `password`, `type` and the `match_field` column; rows where `row[match_field] == agent.location` are candidates; each row is assigned to **at most one crawler** (first unused candidate in row order — deterministic given the seeded pool load); the crawler's `proxy` becomes `{name: f"{pool}:{hostname}:{port}", description: f"from pool {pool} for {location}", hostname, port:int, username, password, type (default "http"), active: True}`. Errors (not drops — nothing would be silently dropped, the compile cannot proceed): unknown pool; no `match_field` or `pool`; agent without `location`; no candidate row for the location; candidates exhausted (`pool X has N rows for location L but M crawlers need one`). `DataPoolManager.rows(name) -> list[dict] | None`.

- [ ] **Step 1: Write the failing tests**

```python
# pmspec/tests/test_proxy_pools.py
"""proxy.from_pool: one row of a proxy CSV per bot, matched on the agent's location."""

from pathlib import Path

import pytest

from pmspec.spec.converter import SpecConverter
from pmspec.spec.parser import SpecParser

SPEC = """
version: "1.0"
experiment:
  name: Pools
data_pools:
  us_proxies:
    type: csv
    source: proxies.csv
crawlers:
  - name: chi_1
    agent: {location: US-IL-CHICAGO}
    proxy: {from_pool: true, pool: us_proxies, match_field: location}
    session: {start: "+1m", workflow: [{wait: 1s}]}
  - name: chi_2
    agent: {location: US-IL-CHICAGO}
    proxy: {from_pool: true, pool: us_proxies, match_field: location}
    session: {start: "+1m", workflow: [{wait: 1s}]}
  - name: atl_1
    agent: {location: US-GA-ATLANTA}
    proxy: {from_pool: true, pool: us_proxies, match_field: location}
    session: {start: "+1m", workflow: [{wait: 1s}]}
"""

CSV = """hostname,port,username,password,type,location
p1.example,7777,${secret:pool.u},${secret:pool.p},http,US-IL-CHICAGO
p2.example,7777,${secret:pool.u},${secret:pool.p},http,US-IL-CHICAGO
p3.example,7777,${secret:pool.u},${secret:pool.p},http,US-GA-ATLANTA
"""


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    (tmp_path / "proxies.csv").write_text(CSV)
    return tmp_path


def _convert(spec_dir: Path, text: str = SPEC):
    parsed = SpecParser(base_dir=spec_dir).parse(text)
    assert not parsed.errors, parsed.errors
    return SpecConverter().convert(parsed)


def test_each_bot_gets_its_own_row_for_its_location(spec_dir):
    r = _convert(spec_dir)
    assert r.errors == []
    hosts = [c["proxy"]["hostname"] for c in r.json_data]
    assert hosts == ["p1.example", "p2.example", "p3.example"]
    assert r.json_data[0]["proxy"]["port"] == 7777
    assert r.json_data[0]["proxy"]["username"] == "${secret:pool.u}"
    assert r.json_data[0]["proxy"]["type"] == "http"


def test_exhausted_pool_is_an_error(spec_dir):
    text = SPEC + """  - name: chi_3
    agent: {location: US-IL-CHICAGO}
    proxy: {from_pool: true, pool: us_proxies, match_field: location}
    session: {start: "+1m", workflow: [{wait: 1s}]}
"""
    r = _convert(spec_dir, text)
    assert any("us_proxies" in e and "US-IL-CHICAGO" in e and "chi_3" in e for e in r.errors), r.errors


def test_no_row_for_location_is_an_error(spec_dir):
    r = _convert(spec_dir, SPEC.replace("US-GA-ATLANTA", "US-TX-AUSTIN"))
    assert any("US-TX-AUSTIN" in e for e in r.errors), r.errors


def test_unknown_pool_is_an_error(spec_dir):
    r = _convert(spec_dir, SPEC.replace("pool: us_proxies", "pool: nope"))
    assert any("nope" in e for e in r.errors)


def test_missing_location_is_an_error(spec_dir):
    r = _convert(spec_dir, SPEC.replace("    agent: {location: US-IL-CHICAGO}\n    proxy", "    proxy", 1))
    assert any("crawlers[0]" in e and "location" in e for e in r.errors), r.errors


def test_compile_inlines_the_proxy_and_keeps_placeholders(spec_dir):
    from pmspec.lock.build import compile_spec
    from pmspec.lock.io import dump_lock

    (spec_dir / "spec.yaml").write_text(SPEC)
    result = compile_spec(SPEC, base_dir=spec_dir, spec_path="spec.yaml", seed=1)
    assert result.lock is not None, result.errors
    text = dump_lock(result.lock)
    assert "from_pool" not in text and "p2.example" in text and "${secret:pool.p}" in text
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest pmspec/tests/test_proxy_pools.py -v`
Expected: every test hits the phase-2 refusal error.

- [ ] **Step 3: Implement**

`data_pools.py`: `def rows(self, name: str) -> list[dict[str, Any]] | None: return self.pools.get(name)`.

`converter.py`: replace the refusal block with a call to `self._assign_pool_proxies(all_crawlers, parsed.pool_manager)` placed **after** design expansion and pool-reference resolution (locations are known then) and before `_convert_crawler`:

```python
    def _assign_pool_proxies(
        self, crawlers: list[dict[str, Any]], pool_manager: DataPoolManager | None
    ) -> None:
        """Give each `proxy: {from_pool: true}` crawler its own row of the named pool.

        One exit IP per bot is the study requirement (a shared IP is both a
        detection signal and a confound across arms), so rows are handed out
        without replacement, in pool order, to crawlers in spec order; running
        out is an error, not a shared proxy.
        """
        used: dict[str, set[int]] = {}
        for i, crawler in enumerate(crawlers):
            proxy = crawler.get("proxy")
            if not (isinstance(proxy, dict) and proxy.get("from_pool") is True):
                continue
            prefix = f"crawlers[{i}].proxy"
            pool_name = proxy.get("pool")
            field = proxy.get("match_field")
            if not pool_name or not field:
                self.errors.append(f"{prefix}: from_pool needs `pool:` and `match_field:`")
                continue
            rows = pool_manager.rows(str(pool_name)) if pool_manager else None
            if rows is None:
                self.errors.append(f"{prefix}: unknown pool '{pool_name}' (define it under data_pools)")
                continue
            location = (crawler.get("agent") or {}).get("location")
            if not location:
                self.errors.append(f"{prefix}: from_pool matches on the agent's location, and crawlers[{i}].agent has none")
                continue
            taken = used.setdefault(str(pool_name), set())
            candidates = [k for k, row in enumerate(rows) if str(row.get(field)) == str(location)]
            free = [k for k in candidates if k not in taken]
            if not candidates:
                self.errors.append(f"{prefix}: pool '{pool_name}' has no row with {field} == {location!r}")
                continue
            if not free:
                self.errors.append(
                    f"{prefix}: pool '{pool_name}' has {len(candidates)} row(s) for {field} == "
                    f"{location!r}, all already assigned; crawler '{crawler.get('name')}' gets none"
                )
                continue
            taken.add(free[0])
            row = rows[free[0]]
            host = row.get("hostname") or row.get("host") or ""
            crawler["proxy"] = {
                "name": f"{pool_name}:{host}:{row.get('port', '')}",
                "description": f"from pool {pool_name} for {location}",
                "hostname": host,
                "port": row.get("port", 0),
                "username": row.get("username", ""),
                "password": row.get("password", ""),
                "type": row.get("type") or "http",
                "active": True,
            }
```
`_convert_proxy` already coerces `port` with `int(...)`; make sure a CSV string `"7777"` survives that. Import `DataPoolManager` for the annotation.

- [ ] **Step 4: Run tests**

Run: `venv/bin/pytest pmspec/tests/ -q`
Expected: PASS; `test_lock_build.py::test_boolean_from_pool_is_refused` (phase 1) must be rewritten to expect the new behaviour: with no `pool:` it errors with "needs `pool:`" — rename it `test_boolean_from_pool_without_pool_is_an_error`.

- [ ] **Step 5: Commit**

```bash
git add pmspec/spec/converter.py pmspec/spec/data_pools.py pmspec/tests/
git commit -m "designer: proxy.from_pool assigns one pool row per bot by location"
```

---

### Task 6: Strategy schema registration; missing schema is a drop

**Files:**
- Modify: `pmspec/spec/validator.py` (`_strategy_schema` ~L481-509, `_validate_lambda_select_treatment`, `validate` to capture `spec.get("strategy_schemas")`)
- Test: `pmspec/tests/test_strategy_schemas.py`

**Interfaces:**
- Produces: resolution order for `strategy_schemas` — (1) `spec["strategy_schemas"][name]`: an inline dict is the schema; a string is a path relative to the validator's `base_dir` (new `SpecValidator(..., base_dir: Path | None = None)`; the CLI passes the spec's directory, `compile_spec` passes its `base_dir`, the MCP passes the temp dir so `files=` can carry a schema); (2) each directory in `PMSPEC_STRATEGY_SCHEMA_DIRS` (os.pathsep-separated) as `<dir>/<name>.schema.json`; (3) the two existing sibling candidates. Not found → `self.drops.report(f"{prefix}.lambda_select", f"no schema for strategy '{name}', so its treatment cannot be validated", "add strategy_schemas: {<name>: <path or inline schema>} or set PMSPEC_STRATEGY_SCHEMA_DIRS")`.

- [ ] **Step 1: Write the failing tests**

```python
# pmspec/tests/test_strategy_schemas.py
import json
from pathlib import Path

import yaml

from pmspec.spec.validator import SpecValidator

SCHEMA = {
    "type": "object",
    "required": ["strategy", "reliability"],
    "properties": {"strategy": {"const": "autocomplete_v1"}, "reliability": {"enum": ["high", "low"]}},
}

SPEC = """
version: "1.0"
experiment: {name: S}
crawlers:
  - name: bot
    session:
      start: "+1m"
      workflow:
        - lambda_select:
            task: t
            function: f
            strategy: autocomplete_v1
            treatment: {reliability: high}
"""


def _validate(text, base_dir=None, lax=False, monkeypatch=None):
    return SpecValidator(base_dir=base_dir, lax=lax).validate(yaml.safe_load(text))


def test_missing_schema_is_a_drop():
    r = _validate(SPEC)
    assert not r.valid and any("no schema for strategy 'autocomplete_v1'" in e for e in r.errors), r.errors
    assert _validate(SPEC, lax=True).valid


def test_inline_schema_validates_the_treatment():
    text = SPEC + "strategy_schemas:\n  autocomplete_v1: " + json.dumps(SCHEMA) + "\n"
    assert _validate(text).valid
    bad = text.replace("reliability: high", "reliability: medium")
    r = _validate(bad)
    assert any("invalid for strategy 'autocomplete_v1'" in e for e in r.errors), r.errors


def test_schema_path_relative_to_the_spec(tmp_path: Path):
    (tmp_path / "s.schema.json").write_text(json.dumps(SCHEMA))
    text = SPEC + "strategy_schemas:\n  autocomplete_v1: s.schema.json\n"
    assert _validate(text, base_dir=tmp_path).valid
    r = _validate(text)  # no base_dir -> resolved against cwd, not found
    assert not r.valid


def test_env_dirs(tmp_path: Path, monkeypatch):
    (tmp_path / "autocomplete_v1.schema.json").write_text(json.dumps(SCHEMA))
    monkeypatch.setenv("PMSPEC_STRATEGY_SCHEMA_DIRS", str(tmp_path))
    assert _validate(SPEC).valid


def test_compile_with_files_carrying_the_schema_via_mcp():
    from orchestrator.mcp_server import server as s

    out = json.loads(
        s.compile_experiment_spec(
            SPEC + "strategy_schemas:\n  autocomplete_v1: s.schema.json\n",
            files={"s.schema.json": json.dumps(SCHEMA)},
            seed=1,
        )
    )
    assert "lock" in out, out
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest pmspec/tests/test_strategy_schemas.py -v`
Expected: `TypeError` on `base_dir`; missing-schema test sees a warning, not an error.

- [ ] **Step 3: Implement**

`SpecValidator.__init__(self, schema_path=None, *, lax=False, base_dir: Path | None = None)`; `validate()` stores `self._strategy_schemas = spec.get("strategy_schemas") or {}` before the crawler pass. `_strategy_schema(name)`:

```python
        declared = self._strategy_schemas.get(strategy)
        if isinstance(declared, dict):
            return declared
        candidates: list[Path] = []
        if isinstance(declared, str):
            candidates.append((self.base_dir or Path.cwd()) / declared)
        for d in filter(None, os.environ.get("PMSPEC_STRATEGY_SCHEMA_DIRS", "").split(os.pathsep)):
            candidates.append(Path(d) / f"{strategy}.schema.json")
        candidates += [ …the two existing sibling paths… ]
```
then the existing loop. In `_validate_lambda_select_treatment`, the "schema was not found" warning becomes the drop above. `validate` step 0's key set already allows `strategy_schemas` at the root (Task 2). The CLI `validate`/`compile` and `compile_spec` construct the validator with `base_dir` (Task 7 wires `compile_spec`; do the CLI `validate` here: `SpecValidator(base_dir=base_dir, lax=lax)` — add `--lax` to `pmspec validate` now, and `lax: bool = False` to the MCP `validate_experiment_spec`, whose `strict` keeps meaning "warnings as errors").

- [ ] **Step 4: Run tests**

Run: `venv/bin/pytest pmspec/tests/ orchestrator/tests/test_mcp_lock_tools.py -q`
Expected: PASS. The MCP test passes only once Task 7 makes `compile_spec` run the validator; if you are executing tasks in order it will fail here — mark it `xfail(strict=True, reason="validator wired into compile in Task 7")` and remove the marker in Task 7.

- [ ] **Step 5: Commit**

```bash
git add pmspec/spec/validator.py pmspec/cli.py orchestrator/mcp_server/server.py pmspec/tests/test_strategy_schemas.py
git commit -m "designer: strategy schemas registrable per spec or via PMSPEC_STRATEGY_SCHEMA_DIRS; missing schema is a drop"
```

---

### Task 7: `compile_spec` runs the validator; `lax` flows end to end; docs

**Files:**
- Modify: `pmspec/lock/build.py` (`compile_spec`), `pmspec/cli.py` (`compile` output), `orchestrator/mcp_server/server.py` (`compile_experiment_spec` response), `docs/pmspec.md`, `CLAUDE.md`
- Test: `pmspec/tests/test_strict_contract.py` (extend), `pmspec/tests/test_lock_build.py` (extend)

**Interfaces:**
- Produces: `compile_spec` loads the raw YAML with the file-tag loader (`create_file_tag_loader(base_dir)` + `resolve_file_references`), runs `SpecValidator(base_dir=base_dir, lax=lax).validate(raw)`, and merges its errors/warnings (deduplicated, order kept) with the parser's and converter's; `SpecParser(..., lax=lax)`, `SpecConverter(..., lax=lax)`; `CompileResult.dropped: int` = sum of the three; a lax lock has `provenance.lax = True` (already) and its warnings end with `N construct(s) would have been dropped (lax mode)`; `pmspec compile` prints the drop count line in yellow; MCP `compile_experiment_spec` returns `"dropped": N`.

- [ ] **Step 1: Write the failing tests**

```python
# append to test_strict_contract.py
class TestCompileIsStrict:
    SPEC = BASE.replace("  - name: bot\n", "  - name: bot\n    configuration:\n      params: {reliability: high}\n")

    def test_compile_refuses_a_drop(self, tmp_path):
        from pmspec.lock.build import compile_spec

        r = compile_spec(self.SPEC, base_dir=tmp_path, spec_path="s.yaml", seed=1)
        assert r.lock is None and any("params.reliability" in e for e in r.errors)

    def test_lax_compile_records_it(self, tmp_path):
        from pmspec.lock.build import compile_spec

        r = compile_spec(self.SPEC, base_dir=tmp_path, spec_path="s.yaml", seed=1, lax=True)
        assert r.lock is not None and r.lock.provenance.lax is True and r.dropped == 1
        assert any("1 construct(s) would have been dropped" in w for w in r.warnings)

    def test_cli_compile_lax_prints_the_count(self, tmp_path):
        from click.testing import CliRunner

        from pmspec.cli import cli

        (tmp_path / "s.yaml").write_text(self.SPEC)
        strict = CliRunner().invoke(cli, ["compile", str(tmp_path / "s.yaml"), "--seed", "1"])
        assert strict.exit_code == 1 and "params.reliability" in strict.output
        lax = CliRunner().invoke(cli, ["compile", str(tmp_path / "s.yaml"), "--seed", "1", "--lax"])
        assert lax.exit_code == 0 and "would have been dropped" in lax.output

    def test_mcp_compile_reports_dropped(self):
        import json

        from orchestrator.mcp_server import server as s

        strict = json.loads(s.compile_experiment_spec(self.SPEC, seed=1))
        assert "errors" in strict
        lax = json.loads(s.compile_experiment_spec(self.SPEC, seed=1, lax=True))
        assert lax["dropped"] == 1 and "lock" in lax
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest pmspec/tests/test_strict_contract.py -v -k CompileIsStrict`
Expected: `compile_spec` returns a lock in strict mode (fails); `dropped` missing.

- [ ] **Step 3: Implement** in `compile_spec`, before the parser:

```python
    FileTagLoader, file_loader = create_file_tag_loader(base_dir=base_dir, seed=None if seed is None else seed + 1)
    try:
        raw = yaml.load(spec_text, Loader=FileTagLoader)
        raw = resolve_file_references(raw, file_loader)
    except yaml.YAMLError as e:
        return CompileResult(None, [f"YAML parse error: {e}"], [])
    validation = SpecValidator(base_dir=base_dir, lax=lax).validate(raw if isinstance(raw, dict) else {})
```
then `SpecParser(..., lax=lax)`, `SpecConverter(..., lax=lax)`, and a small `_merge(*lists) -> list[str]` that concatenates and de-duplicates preserving order; `errors = _merge(validation.errors, parsed.errors, result.errors)`, same for warnings; `dropped = validation.dropped + parsed.dropped + result.dropped`; `CompileResult` gains `dropped: int = 0`. The count line is produced by each component's `Drops.merge_into` — with three components there could be up to three count lines; replace them with one: strip lines matching `r"^\d+ construct\(s\) would have been dropped"` from the merged warnings and append a single `f"{dropped} construct(s) would have been dropped (lax mode)"` when `dropped > 0`.

`pmspec compile`: after the warnings loop, if `result.dropped`, `click.secho(f"  {result.dropped} construct(s) would have been dropped -- lax lock", fg="yellow")` (the merged warning already says it; this line is the summary after the `Compiled:` block). MCP: add `"dropped": result.dropped` to the response. Remove the Task 6 xfail marker.

- [ ] **Step 4: Docs**

`docs/pmspec.md`: a new section "## The strict contract" after "Locks, provenance and verify": the rule (dropped/ignored/unresolved → error), the table of what is checked (unknown keys at root/experiment/defaults/design/crawler/configuration/proxy/agent/session; unknown params and `maps_to` params; `treatment` without `strategy`; unknown job type; missing session start; unresolved `${var}`/`{var}` after expansion; missing strategy schema), `--lax` and what it records (`provenance.lax`, `pmspec.lax`), and the two things that stay warnings. Then three short subsections: **Proxy pools** (`proxy: {from_pool: true, pool: <data pool>, match_field: <column>}`, the CSV columns, one row per bot, exhaustion is an error, placeholders in the CSV are fine); **Strategy schemas** (`strategy_schemas:` inline or path, `PMSPEC_STRATEGY_SCHEMA_DIRS`, sibling fallback); **Additional sessions** (`session` + `additional_sessions` → queues). Update the workflow-shorthand/CLI tables where `--lax` appears.

`CLAUDE.md` "Experiment Designer" section: one paragraph — the contract is strict by default; `additional_sessions` and `proxy.from_pool` now work; a spec that "expects remaining warnings" is a spec with drops; use `--lax` only to see a lock, never to upload a real study.

- [ ] **Step 5: Run `make check`, commit**

```bash
make check
git add pmspec/lock/build.py pmspec/cli.py orchestrator/mcp_server/server.py pmspec/tests/ docs/pmspec.md CLAUDE.md
git commit -m "pmspec: compile runs the validator; strict by default, --lax recorded; docs for the contract"
```

---

## Not in this plan
Phase 3 (§5): `cleanup`, `status`, `diff`, `preview`/`summary` on locks, MCP `allow_dirty`/`allow_past`. Research-repo generators (`autocompleted_misinformation`, `elections_ad_targeting`) adopting `strategy_schemas:` and a proxy-pool CSV — separate repos, noted in the PR.
