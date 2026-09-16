# pmspec phase 6 Implementation Plan — runs locks, per-crawler assignment, seeded timing

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A longitudinal study is a fleet lock plus one *runs lock* per day that appends queues to the fleet's crawlers by name; per-bot data comes from `assign:` pool draws; waits and session starts can be seeded ranges; nested Multilogin flags are validated.

**Architecture:** One new lock kind (`kind: runs`) sharing model, io, parts, anchor, provenance and secrets code with the fleet lock; upload posts per-crawler queue lists to the API's existing `crawlers/{id}/upload`; verify/audit scope to the lock's own queues. `assign:` generalises the proxy-pool draw in the converter. Waits and jitter are deferred tokens resolved per crawler in the converter, like per-bot draws. One small `primemover_api` change lets a runs lock update a preference.

**Tech Stack:** Python 3.10+, PyYAML, jsonschema, Click, FastMCP; PHP/Laravel for Task 0.

**Spec:** `docs/superpowers/specs/2026-09-16-pmspec-phase6-design.md` — read it first; every task below argues from it.

## Global Constraints

- Line length 100; `make check` passes after every task (baseline 1774 passed, 33 skipped); Python 3.10.
- Byte-determinism: a lock is a pure function of (spec text, base lock, pool bytes, seed). Never `datetime.now()` or an unseeded RNG in anything that reaches a lock.
- Strict contract: a construct the pipeline would silently drop goes through `self.drops.report(path, what, fix)`; nothing new appends to `warnings` without a comment saying why it is not a drop.
- No secret in a lock, a preference, a log line or an MCP response. A runs lock carries no `${secret:…}` and no proxy.
- Format number stays `pmspec_lock: 2`; every existing lock (fleet, no `kind`) loads unchanged; `make schema` must be a no-op after the schema is regenerated and committed.
- Every seeded draw derives from the lock's one seed with a distinct namespace string (`f"{seed}:assign:{name}"`, `f"{seed}:wait:{i}:{j}:{k}"`, `f"{seed}:start:{i}:{j}"`) — never from the shared `self.rng`, so adding a draw somewhere does not shift every other draw.
- Commit after every task with the session's attribution trailer (`Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and the `Claude-Session:` line).

## File structure

| path | responsibility |
|---|---|
| `primemover_api` — `app/Modules/Configuration/Http/Request/ConfigurationUpdateRequest.php`, `…/Repository/ConfigurationRepository.php`, `tests/Feature/ConfigurationPreferencesUpdateTest.php` (Task 0) | `PUT configurations/{id}` accepts `preferences: [{name, value}]`, synced by name |
| `pmspec/lock/model.py` | `Lock.kind`, `Provenance.base_lock_sha256/base_spec_path`, runs-crawler shape, `LockError` on a runs lock with fleet-only keys |
| `pmspec/schema/lock-2.schema.json` | `kind`, base provenance, `if kind == runs then` crawler items are `{name, queues, updates?}` |
| `pmspec/lock/runs.py` (new) | `load_base(spec_base_dir, runs_for) -> BaseLock`, `expand_from_base(raw_spec, base) -> raw_spec` (`{base.*}` substitution), `base_variables(crawler) -> dict` |
| `pmspec/lock/build.py` | `compile_spec` detects `runs_for`, loads the base, expands `from_base`, runs the converter in runs mode, fills `kind`/base provenance |
| `pmspec/spec/validator.py` | `_KEYS_ROOT_RUNS`, `_KEYS_CRAWLER_RUNS`, `_KEYS_UPDATES`, `_KEYS_ASSIGN`, `_KEYS_START_AT`, `_MLX_FLAGS`; runs-mode refusals; `assign`/`wait range`/`jitter`/flag rules |
| `pmspec/spec/parser.py` | `wait: {min,max}` → `%%pmspec-wait:<min>:<max>%%`; `start_at: {from, jitter}` passed through as a dict |
| `pmspec/spec/assign.py` (new) | `resolve_assignments(crawlers, pool_manager, seed, taken) -> list[str]`; substitution of `{name.column}` |
| `pmspec/spec/timing.py` (new) | `resolve_wait_tokens(api_crawlers, seed)`, `resolve_start_jitter(crawlers, seed)`; token regexes |
| `pmspec/spec/converter.py` | calls assign → draws → proxies (shared `taken`) → waits/jitter; `runs=True` mode emits `{name, queues, updates}` only |
| `pmspec/spec/design_expander.py` | none (substitution of `{name.column}` lives in `assign.py`, applied after expansion) |
| `pmspec/lock/upload.py` | `upload_lock` branches on `lock.kind`: `_upload_runs(...)` — refusals, name→id map, per-crawler `POST crawlers/{id}/upload`, idempotent skip, updates, provenance |
| `pmspec/lock/api.py` | `ApiSession.supports_preference_update()` probe (also on `_McpSession`) |
| `pmspec/lock/verify.py`, `status.py`, `audit.py`, `diff.py` | runs-lock scoping, kind/base in status, diff of two runs locks |
| `pmspec/cli.py`, `orchestrator/mcp_server/server.py` | `-e` required for a runs lock; MCP `compile_experiment_spec` takes the base lock in `files=`; `upload_experiment` requires `experiment_id` for a runs lock |
| `pmspec/spec/schema_gen.py` | new keys in the generated spec schema |
| `docs/pmspec.md`, `CLAUDE.md` | § Longitudinal studies, § Assignment, § Timing; CLAUDE.md pmspec paragraph |
| tests | `pmspec/tests/test_lock_runs.py`, `test_assign.py`, `test_timing.py`, `test_mlx_flags.py`, `test_lock_runs_upload.py`, `test_lock_runs_verify.py`; additions to `test_lock_io.py`, `test_lock_diff.py`, `test_lock_status.py`, `test_lock_audit.py`, `test_schema_gen.py`, `orchestrator/tests/test_mcp_lock_tools.py` |

---

### Task 0: API — `PUT configurations/{id}` syncs `preferences` by name (`primemover_api`)

**Files:**
- Modify: `app/Modules/Configuration/Http/Request/ConfigurationUpdateRequest.php` (rules + drop the `except(['preferences'])`), `app/Modules/Configuration/Repository/ConfigurationRepository.php:41-47` (`update`)
- Test: `tests/Feature/ConfigurationPreferencesUpdateTest.php` (new)

**Interfaces:**
- Request body may carry `preferences: [{name: string, value: string, description?: string}]`; absent ⇒ preferences untouched (today's behaviour); present ⇒ for each entry, update the value (and description if given) of the configuration's preference with that name, or create it (`active: 1`, `user_id` = the authenticated user) if none exists. **Never deletes** a preference not named. Response `data` is `$configuration->fresh()->load('preferences')`.
- An empty list `preferences: []` is accepted and changes nothing (pmspec's support probe sends exactly this).
- Validation: `preferences` optional array; `preferences.*.name` required string ≤ 255; `preferences.*.value` present (string, may be `""`).

- [ ] Write the failing feature test (three cases: update existing value, create missing, untouched sibling survives; plus `preferences: []` is a 200 no-op) using the repo's existing configuration factory and an authenticated user, in the style of `tests/Feature/Config*`.
- [ ] Run `vendor/bin/phpunit tests/Feature/ConfigurationPreferencesUpdateTest.php` — RED (preferences stripped, values unchanged).
- [ ] Implement: rules `'preferences' => ['sometimes', 'array']`, `'preferences.*.name' => ['required', 'string', 'max:255']`, `'preferences.*.value' => ['present']`; remove `preferences` from `except()` (keep `params` excluded — unchanged behaviour); repository `update()` does `$configuration->update(Arr::except($data, ['preferences']))` then, if `array_key_exists('preferences', $data)`, loops `updateOrCreate(['name' => $p['name']], ['value' => $p['value'], 'description' => $p['description'] ?? $existing?->description, 'active' => 1, 'user_id' => Auth::id()])` on `$configuration->preferences()`.
- [ ] GREEN; run the whole suite (`vendor/bin/phpunit`) and `vendor/bin/pint --test`; commit on a branch `feat/configuration-preferences-update`, open the PR with `gh pr create`, note the PR URL in the report. Deploy is the user's (`git pull` on ex63 — bind-mounted, no migration).

---

### Task 1: The runs lock — model, io, schema

**Files:**
- Modify: `pmspec/lock/model.py`, `pmspec/schema/lock-2.schema.json`, `pmspec/lock/io.py` (nothing beyond what `from_dict` needs), `pmspec/lock/parts.py` (`factor_crawlers`/`materialise` must accept crawlers without `agent`)
- Test: `pmspec/tests/test_lock_runs.py` (new), `pmspec/tests/test_lock_io.py` (+1), `pmspec/tests/test_lock_parts.py` (+1)

**Interfaces:**
```python
@dataclass
class Provenance:
    ...  # as today
    base_lock_sha256: str | None = None     # runs locks only
    base_spec_path: str | None = None

@dataclass
class Lock:
    provenance: Provenance
    experiment: dict[str, Any]
    schedule: dict[str, Any]
    crawlers: list[dict[str, Any]] = field(default_factory=list)
    kind: str = "fleet"                     # "fleet" | "runs"

    @property
    def is_runs(self) -> bool: ...
```
- `to_dict()`: emits `kind: runs` right after `pmspec_lock` **only** for a runs lock (a fleet lock's bytes are unchanged — assert in a test that `dump_lock` of an existing fleet fixture is byte-identical to before this task); for runs, `provenance` gains the two base fields (omitted for fleet); `parts.agents` is `{}`; each crawler is `{name, queues: [{ref, start_offset}], updates?}` — `updates` only when present, shape `{"preferences": {name: str}}`.
- `from_dict()`: `kind = d.get("kind", "fleet")`; for runs, `materialise` must tolerate a missing `agent` (fix `parts.materialise` to skip the agent step when the crawler has no `agent`; `factor_crawlers` likewise). `LockError` (not a schema error — a *message*) for a runs lock whose crawler carries `configuration`, `agent` or `proxy`: `"runs lock crawler '<name>' carries <key>; a runs lock adds queues only"`; and for `updates` with a key other than `preferences`.
- Schema: `"kind": {"enum": ["fleet", "runs"]}` optional; provenance gains `base_lock_sha256` (64-hex) and `base_spec_path`; crawler items use `"if": {"properties": {...}}` at the *root* level — root `allOf: [{if: {properties: {kind: {const: runs}}, required: [kind]}, then: {properties: {crawlers: {items: <runs item>}}}, else: {properties: {crawlers: {items: <fleet item, as today>}}}]`; the runs item: `required: [name, queues]`, `additionalProperties: false`, `properties: {name, queues (as today), updates: {type: object, additionalProperties: false, properties: {preferences: {type: object, additionalProperties: {type: string}}}}}`.

- [ ] Tests (RED): runs lock round-trips through `dump_lock`/`load_lock` byte-identical; fleet fixture bytes unchanged; `kind` absent ⇒ `lock.kind == "fleet"`; a runs lock with `agent` ⇒ `LockError` naming the crawler and key; `updates: {params: …}` ⇒ `LockError`; schema rejects a runs crawler item with `configuration`.
- [ ] Implement; `make check`; commit `lock: runs kind (queues appended to a fleet's crawlers by name)`.

---

### Task 2: Runs spec — `runs_for`, `from_base`, compile, validate, preview/summary/diff

**Files:**
- Create: `pmspec/lock/runs.py`
- Modify: `pmspec/lock/build.py` (`compile_spec`), `pmspec/spec/validator.py`, `pmspec/spec/converter.py` (runs mode), `pmspec/spec/parser.py` (accept `from_base` crawlers with no session until expanded — see below), `pmspec/lock/diff.py`, `pmspec/cli.py` (`preview`/`summary` on a runs lock print base sha and crawler/queue counts), `pmspec/spec/schema_gen.py`
- Test: `pmspec/tests/test_lock_runs.py` (+), `pmspec/tests/test_lock_diff.py` (+), `pmspec/tests/test_lock_cli_compile.py` (+)

**Interfaces:**
```python
# pmspec/lock/runs.py
@dataclass
class BaseLock:
    lock: Lock            # the fleet lock, loaded
    sha256: str           # sha256_text of its bytes
    path: str             # as recorded: the fleet lock's provenance.spec_path is base_spec_path

def load_base(base_dir: Path, runs_for: str) -> BaseLock          # LockError if not a fleet lock
def base_variables(crawler: dict) -> dict[str, str]                # {"name", "location", <every preference name>}
def expand_from_base(raw: dict, base: BaseLock) -> tuple[dict, list[str]]
    # replaces every crawler block with from_base: true by one block per base crawler,
    # substituting {base.<var>} / {base.<var>:fmt} in every string of the block via
    # substitute_named(text, "base", values) -- create pmspec/spec/substitute.py in THIS task:
    #   NAMED_RE = re.compile(r"\{(?P<prefix>[A-Za-z_]\w*)\.(?P<col>[A-Za-z_][\w-]*)(?::(?P<fmt>[^}]+))?\}")
    #   def substitute_named(text: str, prefix: str, values: dict[str, Any]) -> str
    #   (a {prefix.col} whose col is not in values is left untouched; fmt via format(); a
    #   format error raises ValueError naming the column) -- Task 3 reuses it; in base order;
    # errors: a block with from_base and an explicit name that is not "{base.name}"-derived
    # is fine (the user may rename), but a resulting name absent from the base is an error.
```
- `compile_spec`: if the raw spec has `runs_for` → `base = load_base(base_dir, raw["runs_for"])`; `raw, errs = expand_from_base(raw, base)`; validator called with `mode="runs"` (`SpecValidator(..., runs=True)`); after parse/convert (converter `runs=True`), check every crawler name ∈ base names (error `"runs spec names crawler 'x' which the base lock does not have"`, listing all); `Lock(kind="runs", provenance=Provenance(..., base_lock_sha256=base.sha256, base_spec_path=base.lock.provenance.spec_path), experiment={"name": base.lock.experiment["name"], ...copied}, ...)`. `experiment:` in a runs spec is refused (validator) — the name comes from the base.
- Validator runs mode: `_KEYS_ROOT_RUNS = {version, runs_for, defaults, templates, data_pools, strategy_schemas, crawlers}`; `_KEYS_CRAWLER_RUNS = {from_base, name, description, session, additional_sessions, queues, updates, assign}`; `design`, `experiment`, and per-crawler `agent`/`proxy`/`configuration` are **errors** (message: `"crawler[i].agent: a runs spec adds sessions to existing bots; agent/proxy/configuration live in the fleet spec"`), not drops; `updates` must be `{preferences: {str: scalar}}` (error otherwise: `"crawler[i].updates.params: only preferences can be updated"`); `defaults` in runs mode may carry only session-level keys (`timezone`, `timezone_utc_offset`, `captcha_mode`, `typing_mode`, `selection_type`, `active`) — others are errors. Fleet mode: `runs_for`, `from_base`, `updates` are unknown keys (drops) as today's rule would make them.
- Converter `runs=True`: `_convert_crawler` returns `{"name", "queues": [...converted...], "updates": crawler.get("updates")}` (omit `updates` key when None); no configuration/proxy/agent conversion, so no placeholder-proxy warnings. `validate_expanded` still runs on the queues' jobs.
- `pmspec diff a b`: both runs → compare crawler name sets, per-crawler queue lists (by part hash + offset) and `updates`; runs vs fleet → error `"cannot diff a runs lock against a fleet lock"`. `preview`/`summary` on a runs lock: header `Runs lock for <experiment name> (base <sha[:12]>)`, crawlers, queues, jobs, anchor, seed.
- schema_gen: `runs_for` (string), `from_base` (boolean), `updates`, `assign` (Task 3 fills the object), `wait` range and `start_at` object (Task 4) — add the keys now with the right types so the drift test stays meaningful; regenerate.

- [ ] Tests (RED): compile a fleet spec → lock; a runs spec naming two of its crawlers explicitly → runs lock with `kind: runs`, base sha equal to `sha256_text(fleet_lock_text)`, experiment name copied, 2 crawlers with queues and no agent; `from_base: true` with `name: "{base.name}"` and a preference substitution `{base.arm}` in a `with:` → one crawler per base crawler, values substituted; unknown name ⇒ error listing it; `design:` in a runs spec ⇒ error; `agent:` on a runs crawler ⇒ error; byte-determinism (compile twice); `diff` runs/runs and runs/fleet; `pmspec compile day.yaml` CLI prints `Kind: runs  Base: <sha12>`.
- [ ] Implement; `make schema`; `make check`; commit `pmspec: runs specs compile against a fleet lock`.

---

### Task 3: `assign:` — per-crawler pool draws as substitution variables

**Files:**
- Create: `pmspec/spec/assign.py`
- Modify: `pmspec/spec/converter.py` (`convert`: call after expansion, before pool-reference resolution; thread a shared `taken: dict[str, set[int]]` into `_assign_pool_proxies`), `pmspec/spec/validator.py` (`_KEYS_CRAWLER` += `assign`; `_KEYS_ASSIGN = {from_pool, match_field, match_value, select}`; rules), `pmspec/spec/schema_gen.py`
- Test: `pmspec/tests/test_assign.py` (new), `pmspec/tests/test_proxy_pools.py` (+1: shared taken set)

**Interfaces:**
```python
# pmspec/spec/assign.py
ASSIGN_KEYS = frozenset({"from_pool", "match_field", "match_value", "select"})

def resolve_assignments(
    crawlers: list[dict[str, Any]],
    pool_manager: DataPoolManager | None,
    seed: int | None,
    taken: dict[str, set[int]],
) -> list[str]:
    """For each crawler (in order) and each `assign` entry: draw one free row
    (matching `match_field == match_value or agent.location`), mark it taken,
    substitute `{<name>.<col>}` / `{<name>.<col>:fmt}` in every string of the
    crawler dict, then delete the `assign` key. Returns errors; a crawler whose
    draw failed keeps its `{name.col}` placeholders (the unresolved scan reports
    them)."""
```
- RNG per assignment name: `random.Random(f"{seed}:assign:{name}")`, one instance for the whole crawler list (so crawler i's draw depends on the draws before it, as a without-replacement sequence should).
- Substitution: implement `substitute_named(text, prefix, values, fmt_ok=True)` in `pmspec/spec/substitute.py` (new, tiny) and use it here and in Task 2's `{base.*}` — one regex, one place: `\{(?P<prefix>[A-Za-z_]\w*)\.(?P<col>[A-Za-z_][\w-]*)(?::(?P<fmt>[^}]+))?\}`. Non-string row values are formatted with `str()` unless a `fmt` is given (then `format(value, fmt)`; a format error is an error naming the column).
- Errors (exact strings, tests pin them): unknown pool `crawlers[i].assign.<name>: unknown pool 'p' (define it under data_pools)`; no match `crawlers[i].assign.<name>: pool 'p' has no row with <field> == '<value>'`; exhausted `crawlers[i].assign.<name>: pool 'p' has no free row with <field> == '<value>' (N taken)`; no location `crawlers[i].assign.<name>: match_field needs agent.location or match_value`; bad format `crawlers[i]: {name.col:fmt} cannot format <value!r>`.
- Validator: each `assign` entry needs `from_pool` naming a declared `data_pools` key (error), `match_field` must be a column of that pool (error; column set from `DataPoolManager.rows`), `select ∈ {random, first}` (error), keys outside `_KEYS_ASSIGN` are drops; an assignment name equal to a factor name, a `design.parameters` name, or `base` is an error `"crawler[i].assign.<name>: collides with a design variable"`.
- Converter ordering in `convert`: expansion → `resolve_assignments` → pool references → `resolve_draws` → defaults.proxy copy → `_assign_pool_proxies(all_crawlers, pool_manager, taken)` → convert. `taken` is one dict shared by assign and proxies.

- [ ] Tests (RED): two crawlers, pool of 3 rows, `select: first` → rows 0 and 1, values in preference and name; `select: random` seeded → deterministic per seed, differs across seeds; `match_field: region` against `agent.location`; `match_value: "{city}"` on a design crawler; exhaustion error text; unknown pool; bad column; name collision; a leftover `{panelist.zip}` when the draw failed is a drop; the proxy pool and an `assign` on the same pool never share a row; `{outlets.urls:>10}` formatting; runs spec `assign` with `match_value: "{base.comscore_person_id}"`.
- [ ] Implement; `make schema`; `make check`; commit `designer: assign: draws one pool row per crawler as substitution variables`.

---

### Task 4: Seeded waits and session start jitter

**Files:**
- Create: `pmspec/spec/timing.py`
- Modify: `pmspec/spec/parser.py` (`_expand_shorthand` `wait` branch: dict value → token; `_expand_session`: `start_at` dict `{from, jitter}` kept as dict), `pmspec/spec/converter.py` (`_convert_queue`: a dict `start_at` is passed through untouched when `resolve_times=False` — compile path — and is an error when `resolve_times=True` — the legacy `generate` path — `"start_at jitter needs a seed: use pmspec compile"`; after conversion call `resolve_start_jitter` then `resolve_wait_tokens`), `pmspec/spec/validator.py` (`_OPTION_KEYS["wait"] = {min, max}`; `_KEYS_START_AT = {from, jitter}`; rules), `pmspec/lock/build.py` (jitter resolution happens before `derive_anchor` reads `start_at`), `pmspec/spec/schema_gen.py`
- Test: `pmspec/tests/test_timing.py` (new)

**Interfaces:**
```python
# pmspec/spec/timing.py
WAIT_TOKEN_RE = re.compile(r"%%pmspec-wait:(\d+):(\d+)%%")
def wait_token(min_s: int, max_s: int) -> str
def resolve_wait_tokens(api_crawlers: list[dict], seed: int | None) -> list[str]
    # job k of queue j of crawler i: behaviour "seconds" == token → random.Random(f"{seed}:wait:{i}:{j}:{k}").randint(min, max); returns errors (seed None: one error, tokens left in place → drop)
def resolve_start_jitter(crawlers: list[dict], seed: int | None) -> list[str]
    # queue j of crawler i with start_at == {"from": s, "jitter": d}: start_at = s shifted by random.Random(f"{seed}:start:{i}:{j}").randint(0, d_seconds); relative `from` ("+1h") → "+<n>s"; absolute → ISO wall clock + seconds; "now" → "+<n>s"
```
- Parser: `wait: {min: "30s", max: "60s"}` → `_parse_duration` each, `min ≤ max` else error `"wait: min (60s) exceeds max (30s)"`; behaviours `seconds = wait_token(min, max)`. `wait: {min: 5}` without `max` is an error.
- Validator: `wait` dict form allowed keys `{min, max}` (both required — error otherwise); `start_at` dict allowed keys `{from, jitter}` (both required); `jitter` must parse as a duration; strings unchanged.
- Lock: the resolved integer/offset is what `dump_lock` writes; a surviving `%%pmspec-wait` token is reported by `_scan_unresolved` (extend `ANY_DRAW_TOKEN_RE`-style handling with a `WAIT_TOKEN_RE` group: message names it as a wait range).

- [ ] Tests (RED): `wait: {min: 30s, max: 60s}` on a 3-crawler design → three integers in [30, 60], not all equal (seed chosen so they differ; pin the values), identical on recompile, different under another seed; `min > max` error; `wait: {min: 5}` error; `start_at: {from: "+1h", jitter: "6h"}` → offsets in [+3600s, +25200s], pinned per seed, anchor `upload`; absolute `from` → anchor is the earliest resolved start, offsets non-negative; `pmspec generate` (resolve_times=True) on a jitter spec → the error; a surviving wait token is a drop; `additional_sessions` jitter uses `j = 1`.
- [ ] Implement; `make schema`; `make check`; commit `designer: seeded wait ranges and session start jitter`.

---

### Task 5: Nested Multilogin flags — proven path and unknown-flag drop

**Files:**
- Modify: `pmspec/spec/validator.py` (`_MLX_FLAGS = frozenset(DEFAULT_MULTILOGIN_PROFILE["parameters"]["flags"])` imported from `converter`; rule in `_validate_treatment_params`'s `maps_to` loop and in `_validate_crawlers` for an explicit `agent.multilogin_profile.parameters.flags` dict)
- Test: `pmspec/tests/test_mlx_flags.py` (new)

**Interfaces:**
- Rule: a `maps_to` path `agent.multilogin_profile.parameters.flags.<key>` or an explicit `agent.multilogin_profile.parameters.flags.<key>` with `<key> ∉ _MLX_FLAGS` → `self.drops.report(path, "unknown Multilogin flag; the profile ignores it", "one of: " + ", ".join(sorted(_MLX_FLAGS)))`. Values are not checked (spec §4).
- Test proves: spec with factor `privacy: {levels: [none, geo], maps_to: {agent.multilogin_profile.parameters.flags.geolocation_masking: {none: mask, geo: custom}}}` compiles; the lock's agent part carries `parameters.flags.geolocation_masking == "custom"` for the `geo` crawler and `"mask"` for `none`; the other 16 flags keep their defaults; `resolve_for_upload` JSON has the same; `flags.geolocaton_masking` (typo) is a drop with the fix text listing the flags.

- [ ] Tests (RED); implement; `make check`; commit `designer: nested Multilogin flags reach the profile; an unknown flag is a drop`.

---

### Task 6: Upload a runs lock

**Files:**
- Modify: `pmspec/lock/upload.py` (`upload_lock` → `_upload_fleet` (today's body) / `_upload_runs`), `pmspec/lock/api.py` (`supports_preference_update`), `orchestrator/mcp_server/server.py` (`_McpSession.supports_preference_update`, `upload_experiment`: `experiment_id` required for runs, dry-run plan fields), `pmspec/cli.py` (`upload`: `-e` required for a runs lock; print the plan)
- Test: `pmspec/tests/test_lock_runs_upload.py` (new; extend `FakeSession` from `test_lock_upload.py` with `crawler_uploads: dict[int, list]`, `configuration_puts`, `queues per crawler`), `orchestrator/tests/test_mcp_lock_tools.py` (+2), `orchestrator/tests/test_mcp_write_guard.py` (the AST scan must still see `upload_lock` as the guarded call — no new write verb bypasses it)

**Interfaces:**
```python
@dataclass
class UploadResult:
    ...  # as today, plus:
    kind: str = "fleet"
    queue_count: int = 0
    skipped_existing: int = 0
    updates_applied: int = 0

def supports_preference_update(session) -> bool   # in upload.py; uses session.supports_preference_update() if present, else False
```
- `_upload_runs(lock, lock_text, session, options)`:
  1. `experiment_id is None` → `UploadRefused("a runs lock appends to an experiment: pass -e <id>")`.
  2. `live = session.get(f"experiments/{id}")`; prefs; first upload = the `pmspec.lock_sha256` preference with description `"upload 1"`; its value must equal `lock.provenance.base_lock_sha256` else `UploadRefused(f"experiment {id} was created from lock {sha[:12]}, this runs lock is based on {base[:12]}")`; the `"upload 1"` `pmspec.mode` must equal `options.mode` else refused (`"experiment {id} is a {live_mode} experiment; upload this runs lock with --mode {live_mode}"`); `_check_commit` as today.
  3. `by_name = {c["name"]: c for c in live["crawlers"]}`; duplicates (two live crawlers with one name) → refused naming it; every lock crawler must be present → refused listing missing names.
  4. Anchor/offsets via `resolve_for_upload` (no secrets: `store=None`; assert `find_placeholders(lock.crawlers)` is empty — `LockError` otherwise).
  5. If any crawler has `updates` and not `supports_preference_update(session)` → `UploadRefused("this API cannot update preferences (PUT configurations/{id} strips them); deploy primemover_api with preferences sync, or drop `updates:`")` — **before** any write.
  6. Per crawler: existing live queues for that crawler from `_paginate(session, f"experiments/{id}/queues-unprocessed")` + `-processed` (fetched once, grouped by `crawler_id`); a lock queue whose `(wall(start_at), name)` matches an existing one is skipped (counted); the rest are posted as **one** `session.post_file(f"crawlers/{cid}/upload", "json", "queues.json", json.dumps(queues), "application/json")` per crawler (the endpoint takes a JSON list of queues). Then `updates`: read the live preference value from `by_name[name]["configuration"]["preferences"]`, `session.put(f"configurations/{config_id}", {"name": cfg["name"], "description": cfg.get("description"), "active": cfg.get("active", 1), "preferences": [{"name": k, "value": v}]})`, record `[old, new]`.
  7. Provenance: `provenance_preferences(...)` with `label = f"upload {n+1}"` plus `pmspec.kind = "runs"`, `pmspec.base_lock_sha256`, and `pmspec.updates = json.dumps({crawler: {"preferences.<k>": [old, new]}})` clipped to 255 (if longer, store `"<N crawlers updated; see lock>"` — the lock has the values). Written via the same `session.put(f"experiments/{id}", …)` as today's append path.
  8. `record_line` gains `kind=runs base=<sha12> queues=<n> skipped=<m>`.
- Dry-run (CLI `--dry-run`, MCP default): steps 1–5 run (read-only), then print/return `{"kind": "runs", "crawlers": n, "queues": m, "would_skip": k, "updates": u, "base_ok": true, "preference_update_supported": bool}`; nothing written.
- `supports_preference_update()` on `ApiSession`/`_McpSession`: one probe, cached on the session — `PUT configurations/<id of the first live crawler's configuration>` with that configuration's current `name`, `description`, `active` and `preferences: []`. Both API versions answer 200 (the old request strips the key and changes nothing; the new one syncs an empty list and changes nothing). They differ in the response: the old repository returns `$configuration->fresh()` with no `preferences` relation loaded; Task 0's returns `fresh()->load('preferences')`. Supported ⇔ the response `data` carries a `preferences` list. The probe never changes a value.

- [ ] Tests (RED): happy path — fake experiment with upload-1 sha == base, two crawlers; result posts one `crawlers/{id}/upload` per crawler with the resolved `start_at`, provenance "upload 2" with kind/base; re-run → every queue skipped, nothing posted, still a provenance "upload 3" (a re-run is an upload that added nothing — recorded, so status shows it); base mismatch / mode mismatch / missing name / duplicate name / no `-e` refusals with the exact messages; `updates` with unsupported API ⇒ refused before any post; with supported ⇒ one `configurations/{id}` PUT and `pmspec.updates` recorded; failure at crawler 2 of 3 (fake raises) ⇒ no provenance written, exception propagates with `"crawler <name>"` in the message; MCP `upload_experiment(runs lock, mode, experiment_id=None)` ⇒ error JSON; dry-run JSON has the plan fields; write-guard test green.
- [ ] Implement; `make check`; commit `pmspec: upload a runs lock (queues per crawler, idempotent, opt-in preference updates)`.

---

### Task 7: verify, status, audit for runs locks

**Files:**
- Modify: `pmspec/lock/verify.py` (`lock_shape` for runs: `{crawlers: {name: {queues: [...], updates: {...}}}}`; `live_shape` gains a `queue_filter` so a runs verify keeps only live queues matching the lock's `(start_at, name)` per crawler and, for updated preferences, only those preference names), `pmspec/lock/status.py` (`ProvenanceUpload` renders `kind=runs base=<sha12> updates=<n>`; summary line `uploads: 1 fleet + N runs, last <uploaded_at>`), `pmspec/lock/audit.py` (`_arm_label` for a runs crawler: `"runs"` unless the lock job's treatment carries `pi`/`reliability`… — read the arm from the job treatment when the crawler has no configuration; `_job_treatment` params fallback only when `configuration` exists; queues not in the lock are already skipped by name/position — add a test proving it)
- Test: `pmspec/tests/test_lock_runs_verify.py` (new), `pmspec/tests/test_lock_status.py` (+2), `pmspec/tests/test_lock_audit.py` (+2)

**Interfaces:**
- `verify_lock(runs_lock_text, session, experiment_id, deep)`: `bound` ⇔ the lock's sha is among the live `pmspec.lock_sha256` values (as today); anchor from the matching upload label (as today); expected = `{name: {"queues": [{name, start_at, jobs?}], "updates": {pref: new}}}`; actual = for each lock crawler, live queues filtered to the lock's `(start_at, name)` pairs (so other uploads' queues never appear as `extra`), plus `{"updates": {pref: live value}}` for the named preferences; `extra_live_crawlers` stays empty for runs; differences rendered as today. A lock queue with no live match shows as `crawlers.<name>.queues.length` / `<present>` vs `None` exactly as a fleet verify would.
- `status`: `_uploads` keeps every `pmspec.*` value; render adds `kind=` and `base=` when present; the summary line counts kinds.
- `audit`: with a runs lock, `arm` = the treatment's `reliability/lean` if both present in the job's behaviours, else `pi=…` from the treatment keys, else `"-"`; everything else unchanged. Test: a live experiment with queues from two runs uploads; auditing lock A reports only A's queues.

- [ ] Tests (RED); implement; `make check`; commit `pmspec: verify/status/audit scoped to a runs lock`.

---

### Task 8: MCP parity, schema, docs

**Files:**
- Modify: `orchestrator/mcp_server/server.py` (`compile_experiment_spec`: the base lock arrives in `files=` under the name `runs_for` names; `_materialise_files` already writes it; the result JSON gains `"kind"` and `"base_lock_sha256"`; docstring), `pmspec/spec/schema_gen.py` (final pass: every key from Tasks 2–4 present; `make schema` no-op), `docs/pmspec.md` (new sections: "Longitudinal studies: fleet lock + runs locks" after § Additional sessions — the nightly loop, `runs_for`, `from_base`, `updates`, idempotent upload, verify/audit scoping; "Per-crawler assignment (`assign:`)" after § Proxy pools; "Seeded timing" after § Per-bot template draws; "Multilogin flags" one paragraph under § Proxy pools' neighbour), `CLAUDE.md` (the pmspec paragraph: two sentences on runs locks and `assign`; the `experiment_designer/` → `pmspec/` line already done), `.claude/skills/primemover-compose/SKILL.md` (one line: a runs lock needs `experiment_id`)
- Test: `orchestrator/tests/test_mcp_lock_tools.py` (+2: compile a runs spec with the base lock in `files`; upload a runs lock dry-run returns the plan), `pmspec/tests/test_schema_gen.py` (drift test still green)

- [ ] Tests (RED for the MCP cases); implement; `make schema`; `make check`; commit `pmspec: MCP parity for runs locks; docs for phase 6`.

## Not in this plan
The `websearch_v1` decision strategy (`bot-decision-yt`, its own brainstorm); Multilogin flag *values* (a spike on ex63: create one profile through the Multilogin API with the candidate DNT/local-storage values, record the accepted vocabulary in `docs/`); rewriting `websearch_polarization`'s experiment-3 spec against phase 6 (in that repo, after this lands).
