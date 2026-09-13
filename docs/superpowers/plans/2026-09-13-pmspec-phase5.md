# pmspec phase 5 Implementation Plan — schema truth, last silent drops, pool provenance, secrets ergonomics

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Four small, independent improvements: (4) one source of truth for the spec's shape — the JSON schema is generated from the validator's key sets and the job registry; (5) the remaining silent-drop surfaces closed (job option keys, factor sub-keys, non-standard sampled parameters); (6) pool sha256s recorded in lock provenance and shown by `diff`; (7) `pmspec secrets init` and a secrets-store line on the MCP `/health`.

**Architecture:** No new subsystems. (4) is a generator script + a test that the checked-in schema equals its output. (5) extends the existing validator drop sites with per-job option key sets derived from the parser's shorthand branches. (6) adds a field to `Provenance` (lock format stays 2; old locks load with an empty map). (7) is a CLI subcommand and a five-line addition to the HTTP health handler.

**Tech Stack:** Python 3.10+, jsonschema, existing test style.

**Spec:** these are the numbered items 4–7 of the improvement list agreed on 2026-09-13; the phase-1 rules apply (byte-determinism, strict contract, no secrets in output). No separate design doc — each task's Interfaces block is the design.

## Global Constraints

- Line length 100; `make check` passes after every task (baseline 1650 passed, 33 skipped); Python 3.10.
- Strict contract: every new drop site goes through `self.drops.report(path, what, fix)`; nothing new appends to `warnings` without a comment saying why it is not a drop.
- Lock format number stays 2; a lock without `provenance.pools` still loads (defaults to `{}`).
- Commit after every task with the session's attribution trailer.

## File structure

| path | responsibility |
|---|---|
| `pmspec/spec/schema_gen.py` (new) | `build_schema() -> dict` from `SpecValidator._KEYS_*`, `JobRegistry`, and the shorthand option sets; `main()` writes `pmspec/schema/experiment-spec.schema.json` |
| `pmspec/spec/validator.py` (modify) | `_OPTION_KEYS: dict[str, frozenset[str]]` per shorthand; `_KEYS_FACTOR`; drop sites for job options, factor sub-keys, non-standard sampled params |
| `pmspec/lock/model.py`, `lock/build.py`, `lock/diff.py`, `schema/lock-2.schema.json` (modify) | `Provenance.pools: dict[str, str]`; compile fills it; `diff` shows per-pool changes |
| `pmspec/spec/data_pools.py` (modify) | `DataPoolManager.sources: dict[str, Path]` recorded on load |
| `pmspec/cli.py` (modify) | `secrets init LOCK`; `secrets check` unchanged |
| `orchestrator/mcp_server/server.py` (modify) | `/health` JSON with `secrets_store` |
| `Makefile` (modify) | `make schema` target |
| tests | `pmspec/tests/test_schema_gen.py`, `test_strict_contract.py` (+rows), `test_lock_build.py` (+pools), `test_lock_diff.py` (+pools), `test_lock_cli_workflow.py` (+secrets init), `orchestrator/tests/test_mcp_http_auth.py` (+health) |

---

### Task 1: Schema generated from the validator (item 4)

**Files:**
- Create: `pmspec/spec/schema_gen.py`, `pmspec/tests/test_schema_gen.py`
- Modify: `pmspec/schema/experiment-spec.schema.json` (regenerated), `Makefile`, `docs/pmspec.md` (one paragraph)

**Interfaces:**
- `build_schema() -> dict`: draft-07 schema whose object definitions come from `SpecValidator._KEYS_ROOT/_KEYS_EXPERIMENT/_KEYS_DEFAULTS/_KEYS_DESIGN/_KEYS_FACTOR/_KEYS_CRAWLER/_KEYS_CONFIGURATION/_KEYS_PROXY/_KEYS_AGENT/_KEYS_SESSION` (each with `additionalProperties: false` — the validator's key sets *are* the contract now), `workflowItem` with one property per `JobRegistry.list_all()` yaml_name plus `template/repeat/with/jobs`, each shorthand's options object from `_OPTION_KEYS[yaml_name]` (Task 2 introduces `_OPTION_KEYS`; in this task derive from the parser's accepted keys the same way Task 2 will — put the table in `validator.py` now, Task 2 wires the drop sites), `job.type` enum = every `api_type`, `proxy.type` enum = `["http", "oxylabs", "brightdata"]` (the API's `Proxy::TYPES`), `params` keys = the validator's `_KNOWN_PARAMS` with `additionalProperties: true` (unknown params are the validator's drop, not the schema's), `strategy_schemas`, `data_pools` (`{type, source, description, columns, seed}`), `design.parameters` (`distribution|value` objects — `additionalProperties: true`). Keep `$id`/`title`/descriptions from the current file where they exist.
- `main()` writes the file with `json.dumps(indent=2, sort_keys=False)` + trailing newline; `make schema` runs it; test `test_checked_in_schema_is_generated` asserts `json.load(open(schema)) == build_schema()` so drift fails CI; `test_every_example_spec_validates_against_the_generated_schema` runs `jsonschema.validate` on the four example specs (with the file-tag loader) and expects: `simple-distribution-study.yaml`, `websearch-study.yaml`, `trump-search-mini-study.yaml` valid; `websearch-polarization-study.yaml` may fail only on its known pre-existing problems (assert the error path is one of a listed set, e.g. `wait` with a dict — decide from the actual output and pin it).
- The validator's `_validate_schema` keeps using the file; after this task the file and the key sets cannot disagree.

- [ ] Write the tests (RED: module missing); implement; regenerate; run the examples; `make check`; commit `designer: experiment-spec schema generated from the validator's key sets`.

---

### Task 2: The last silent-drop surfaces (item 5)

**Files:**
- Modify: `pmspec/spec/validator.py` (`_OPTION_KEYS`, `_KEYS_FACTOR`, `_validate_workflow_item`, `_validate_design`, `_validate_treatment_params`)
- Test: `pmspec/tests/test_strict_contract.py` (rows)

**Interfaces:**
- `_OPTION_KEYS` per shorthand, derived by reading `SpecParser._expand_shorthand` (the authority): e.g. `visit: {url}` (string form has none), `wait: {}` (scalar), `click/try_click: {selector, target, submit}`, `type/try_type: {text, selector, target, appendReturn, typingMode, submit}`, `scroll/try_scroll: {duration, direction}`, `scroll_length/try_scroll_length: {length, pixels, direction}`, `lambda_select: {task, function, link_value, link_type, captcha_mode, strategy, treatment, description}`, `single_select/single_select_url: {selector, target, select, flag}`, `handle_alert/try_handle_alert: {action}`, `switch/try_switch: {target, selector, field}` — **verify each against the parser and list what you found in the report**; a key the parser reads but the table omits is a false positive, so err on reading the parser twice.
- Drop sites: (a) a dict-valued shorthand option with a key outside `_OPTION_KEYS[job]` → `{prefix}.{job}.{key}: unknown option; the runner never sees it`; (b) `design.factors.<name>` keys outside `_KEYS_FACTOR = {levels, maps_to, proportions, description}` (check the expander for the real set) → drop; (c) a `design.parameters` name that is not in `_KNOWN_PARAMS`: the expander writes every sampled value into `configuration.params.<name>`, which the API drops. Rule: such a parameter is a drop (`design.parameters.<name>: sampled value lands in configuration.params, which the API drops; use it as a {<name>} substitution variable or give it a standard name`) **unless** the spec references `{<name>}` (or `{<name>:fmt}`) somewhere under `crawlers` — then it is a substitution variable, and `SpecConverter._convert_configuration` **strips** it from `configuration.params` before conversion (comment: the value has already been substituted where it was referenced; the API would drop the key anyway, and the strict scan must not see a "dropped" param that was in fact consumed).
- Rows in `TestValidatorDropRules.ROWS`: `unknown-job-option` (`- click: {selector: x, sped: fast}` → needle `click.sped`), `unknown-factor-key` (`mapsto:` → needle `design.factors.arm.mapsto`), `non-standard-sampled-param-unused` (`parameters: {foo: {distribution: uniform, min: 0, max: 1}}` with no `{foo}` reference → needle `parameters.foo`); plus a positive test: `parameters: {alpha_draw: …}` referenced as `{alpha_draw}` in a preference value → valid, converted params lack `alpha_draw`, preference carries the number.

- [ ] Tests (RED); implement; regenerate the schema (`make schema`, Task 1's test must still pass); `make check`; commit `designer: unknown job options, factor keys and unused sampled parameters are drops`.

---

### Task 3: Pool provenance (item 6)

**Files:**
- Modify: `pmspec/spec/data_pools.py` (`self.sources[name] = resolved path` in `load_pool`), `pmspec/lock/model.py` (`Provenance.pools: dict[str, str] = field(default_factory=dict)`), `pmspec/lock/build.py` (fill: `{name: sha256 of the pool file bytes}` for every loaded pool, sorted by name), `pmspec/schema/lock-2.schema.json` (`provenance.pools` object of 64-hex strings, optional), `pmspec/lock/diff.py` (render a `pools:` line per pool that differs or exists on one side only; `LockDiff.pools_changed: list[str]`; `identical` unaffected — provenance is not structure, but `render()` shows it)
- Test: `test_lock_build.py` (`provenance.pools == {"outlets": sha}`; changing one byte of the CSV changes only that entry and the lock bytes), `test_lock_diff.py` (a pool change is named), `test_lock_io.py` (a lock without `pools` loads with `{}`)

- [ ] Tests (RED); implement; `make check`; commit `lock: provenance records each pool's sha256; diff names the pool that changed`.

---

### Task 4: `pmspec secrets init` and the MCP health line (item 7)

**Files:**
- Modify: `pmspec/cli.py` (`secrets init LOCK [--secrets PATH]`), `orchestrator/mcp_server/server.py` (`/health`), docs
- Test: `test_lock_cli_workflow.py`, `orchestrator/tests/test_mcp_http_auth.py`

**Interfaces:**
- `pmspec secrets init LOCK [--secrets PATH]`: writes a TOML skeleton with one `[section]` per dotted-key prefix and `key = ""` for each leaf the lock references (`find_placeholders`), mode 600, refusing to overwrite an existing file unless `--force` (then it merges: existing values kept, missing keys added, and says which). Prints the path and the keys added; never prints values. `secrets check` afterwards reports every added key as MISSING until filled.
- MCP `GET /health` returns JSON `{"status": "ok", "secrets_store": "ok" | "missing" | "insecure"}` (the store path from `PMSPEC_SECRETS_FILE`/default, checked with `SecretStore.load()` — `FileNotFoundError` → missing, `InsecureSecretsFile` → insecure); the body stays small and unauthenticated; the edge's liveness check only reads the status code, so a missing store must **not** change the HTTP status (200 always).

- [ ] Tests (RED); implement; docs (`docs/pmspec.md` secrets section; CLAUDE.md MCP health sentence); `make check`; commit `pmspec: secrets init; MCP health reports the secrets store`.

## Not in this plan
Item 8 (`status` inferring "runner down"), item 9 (server-side lock storage), item 10 (generator template) — reassess after the first real end-to-end run.
