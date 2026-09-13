# pmspec: a committed lock, provenance on the server, and a contract that cannot drop things

**Date:** 2026-09-11 · **Status:** approved design, not yet implemented
**Repos touched:** `primemover-orchestrator` only (`pmspec/`, the MCP server,
docs, the `primemover-compose` skill). No API migration, no GUI work, no runner work. The
two research repos that generate specs from R need small follow-up edits, listed in §7.

## Problem

`pmspec` was built as a design document from which an experiment can be set up
reproducibly. Two studies now use it in earnest (`autocompleted_misinformation`,
`elections_ad_targeting`), and both record the same gaps in their own CLAUDE.md files:

1. **Nothing binds a live experiment to the spec that produced it.** The `experiments`
   table holds `name, description, contact, active, user_id, runner_user_id`. Which YAML,
   which seed and which commit produced experiment 167 is a hand-maintained table in
   `~/control_room/CLAUDE.md`. There are also two seeds (`--seed` for pools and file
   selections, `design.seed` for factorial expansion) and nothing records either.
2. **The spec is not self-contained.** `data_pools[].source` resolves against the spec's
   directory on the CLI and against the orchestrator's CWD through the MCP, so MCP
   validation *always* reports `File not found` for every pool, and "shipping the YAML
   alone fails either way".
3. **The spec cannot be committed.** Proxy credentials are rendered into it, so the
   generated file lives under a gitignored `data/` directory. The artifact that should be
   the reproducibility anchor is exactly the one kept out of version control.
4. **Constructs are dropped silently.** Unknown `configuration.params` keys vanish; a
   `treatment` block without `strategy` is ignored and the job runs the legacy selection
   path, which is a control-arm diet under a treated-arm label; an unresolved
   `${reliability}` reaches the decision function as a literal string; `proxy.from_pool`
   is accepted and never implemented. All of these are warnings, and both projects have
   learned to "expect remaining warnings".
5. **Time is not in the spec's hands.** Relative starts (`+5m`) resolve when the spec is
   converted, fixed ones are Europe/Zurich wall clock, and an unquoted ISO timestamp is
   parsed by PyYAML into a `datetime` on which pmspec then crashes.
6. **Test versus real is a manual patch** — `runner_user_id: 3`, a `test-` name prefix,
   delete afterwards — repeated per project and per run.

Both projects **generate** their YAML from R rather than writing it by hand, because
randomization, blocking and assignment live in the research pipeline. pmspec is a compile
target for them; the factorial `design:` DSL is a convenience, not the centre of gravity.

## Decision

Split pmspec into a strict, self-contained **compiled artifact** (the *lock*) that any
generator targets, and the existing YAML DSL as one way of producing it.

```
spec.yaml  ──pmspec compile──▶  experiment.lock.yaml  ──pmspec upload──▶  API
(authored or                    (self-contained,          (+ provenance     ▲
 R-generated)                    committed, hashed)         preferences)     │
                                        └──────────── pmspec verify ─────────┘
```

- The lock is the design document: fully expanded, pools inlined, every random draw
  fixed by one recorded seed, secrets as placeholders, times as an anchor plus offsets.
  It is committable by construction and byte-deterministic.
- Upload reads locks only, resolves secrets and the anchor on the uploading machine,
  and writes a provenance record onto the experiment as **preferences** — the API's
  existing polymorphic key/value mechanism, accepted on `POST /experiments` and returned
  on `GET`, so no migration is needed.
- `verify` proves the live experiment is bound to a given lock (hash) and structurally
  identical to it.
- The compiler and validator are strict by default: whatever would be dropped, ignored
  or left unresolved is an error, with an explicit, recorded `--lax` escape.

Rejected alternatives:

- **Single artifact** (make today's YAML self-sufficient, verify by re-expanding it):
  reproducibility would depend on re-running the expansion identically — same pool files
  present and unchanged, both seeds, same pmspec version — and "what actually ran" would
  have to be re-derived rather than read. The MCP file problem would remain unless every
  generator inlined pools itself.
- **Server owns the spec** (API stores and expands YAML + pools; new columns; GUI panel):
  cross-repo rollout with a migration, and CLAUDE.md records what un-migrated API code
  did the last time (107 minutes of 500s on the runner's work feed). Preferences carry
  the same facts with no schema change. Storing the lock bytes server-side (the
  `primemover-api` MinIO bucket via `POST /upload`) is a possible later addition and is
  not part of this design.
- **Proxies referenced from the API library** or **runner-side provider types with geo**
  for secrets: both are cross-repo. Placeholders resolved at upload are orchestrator-only
  and put the secret on the same trust boundary as the existing `mcp-as-identity.sh`
  credentials file.

## Phasing

| phase | delivers | why in this order |
|---|---|---|
| 1 | lock format, `compile`, `${secret:}`, `schedule.anchor`, provenance preferences, `verify`, MCP `files=` | the reproducibility core; needs secrets and the anchor to make the lock committable and re-runnable |
| 2 | the strict contract, `proxy.from_pool`, strategy schema registration, the PyYAML timestamp fix | turns the known silent failures into errors once the artifact they would corrupt exists |
| 3 | `--mode test\|real`, `cleanup`, `status`, `diff`, docs and skill rewrite | workflow on top of a trustworthy artifact |

Each phase is a separate implementation plan and a separate PR series.

## 1. The lock format (phase 1)

```yaml
pmspec_lock: 1                              # lock format version, distinct from the spec `version`
provenance:
  spec_path: design/experiment_spec.yaml    # relative to the repo root at compile time
  spec_sha256: 9f3a…                        # sha256 of the source YAML bytes
  generator: code/design/build_spec.R       # free text: `experiment.generator` in the spec, or --generator
  git_commit: a2024c9                       # HEAD of the repo containing the spec; "-dirty" suffix if unclean
  pmspec_version: 0.2.0
  seed: 42                                  # the one seed; no timestamp, so the bytes are deterministic
  lax: false                                # true only when compiled with --lax (§4)
experiment:
  name: Autocompleted (Mis-)Information
  description: …
  runner_user_id: null                      # null = default runner (ex63)
schedule:
  anchor: "2026-10-01T10:00:00"             # Europe/Zurich wall clock, or the literal `upload`
crawlers:                                   # fully expanded; the API upload shape, with two differences
  - name: bot_treated_001
    description: …
    configuration:
      name: …
      params: {pi: 1, psi: 0, kappa: 0, tau: 0}
      preferences: [{name: reliability, value: "high"}, …]
    proxy:
      type: http
      hostname: pr.oxylabs.io
      port: 7777
      username: "${secret:oxylabs.us_nc.username}"   # difference 1: placeholders survive compile
      password: "${secret:oxylabs.us_nc.password}"
    agent: {…}
    queues:
      - name: Daily diet
        start_offset: "+0s"                            # difference 2: offsets from the anchor, not start_at
        jobs:
          - {type: …, name: …, behaviors: [{name, value}, …]}
```

Rules:

- **One seed.** Compile derives every RNG from `provenance.seed`: the design expander from
  `random.Random(seed)`, pools and file selections from `random.Random(seed + 1)`. A spec's
  `design.seed` is honoured; `--seed` overrides it; if neither is given, compile refuses.
  There is no unseeded lock.
- **Pools are inlined.** `data_pools` are resolved at compile time; the lock contains no
  file references. Size is crawler count × job count — a few MB for a 300-crawler study.
- **Placeholders survive compile.** The only secret syntax is `${secret:<dotted.key>}`,
  matched by `\$\{secret:([A-Za-z0-9_.-]+)\}`. Compile refuses a proxy `username` or
  `password` that is a non-empty literal unless the proxy `type` is a runner-side
  self-configuring one (today: `oxylabs`), so a lock is committable by construction.
- **`schedule.anchor` + `start_offset`.** Every queue start is an offset (`+Ns/m/h/d`,
  the existing relative syntax) from the anchor. A spec whose sessions use only relative
  starts gets `anchor: upload`. A spec with absolute starts gets `anchor` = the earliest
  absolute start and each queue's offset from it; absolute values with an explicit zone
  are converted to Europe/Zurich first (`time_utils._to_api_wall_clock`). Mixed absolute
  and relative starts in one spec are a compile error. `--anchor <ISO|upload>` overrides.
- **The lock's own hash is not stored in it.** `lock_sha256` is the sha256 of the file
  bytes, computed at upload and written to the server. `spec_sha256` inside the lock
  detects "the YAML was edited and not recompiled".
- **Serialization is canonical**: fixed top-level key order as shown, crawler keys in the
  order shown, `sort_keys=False`, `allow_unicode=True`, unlimited width, block style, LF
  newlines. Compiling the same inputs twice yields identical bytes; this is a test.
- **Two writers, one reader.** `pmspec compile` produces a lock from a spec; a generator
  may write one directly. Either way `pmspec validate <lock>` checks it against a JSON
  schema (`pmspec/spec/schemas/lock-1.schema.json`, shipped with the
  package). Upload, verify, diff, summary and status read locks only.

## 2. `compile` and the secrets store (phase 1)

### `pmspec compile spec.yaml [-o experiment.lock.yaml] [--seed N] [--anchor …] [--generator TEXT] [--lax]`

Default output path: `<spec stem>.lock.yaml` beside the spec. Pipeline, pure and
deterministic given `(spec bytes, pool bytes, seed)`:

1. **Parse** — `SpecParser(base_dir=spec.parent, seed=…)`, as today.
2. **Expand** — `SpecConverter.convert`, ending in the API-shaped crawler list, as today.
3. **Lockify** — new: rewrite each queue's `start_at` to `start_offset` against the
   resolved anchor; walk every string value and (a) leave `${secret:…}` untouched,
   (b) refuse literal credentials, (c) in strict mode refuse any unresolved `${var}` /
   `{var}`; gather provenance: `git -C <spec repo> rev-parse --short HEAD` plus
   `git status --porcelain` for the dirty flag (no git → `git_commit: null`, and upload
   in `real` mode refuses, §5), `importlib.metadata.version("primemover-orchestrator")`,
   sha256 of the spec bytes, and the spec path relative to that repo's toplevel
   (`git rev-parse --show-toplevel`; relative to the cwd when there is no repo). No
   timestamp: the lock's bytes depend on nothing but its inputs.
4. **Serialize** canonically (§1).

Existing commands are re-based on the lock rather than duplicated:

- `pmspec generate` (API JSON to stdout, for GUI upload) = `compile` + `resolve`
  (§3 steps 2–3). With placeholders present it prints them unresolved and warns, unless
  `--resolve-secrets` is passed; the resolved JSON is never written to disk by pmspec
  unless the caller asks for that flag with `-o`.
- `pmspec validate` accepts a spec or a lock (detected by the `pmspec_lock` key) and
  says which it got. `preview` and `summary` accept both too.
- `SpecConverter.convert` keeps returning `ConversionResult`; lockify is a new module
  `pmspec/lock/` (`model.py` for the dataclass + schema, `build.py` for
  lockify, `resolve.py` for §3 steps 2–3, `io.py` for canonical read/write). Nothing in
  `spec/` learns about locks.

### Secrets store

```toml
# ~/.config/primemover/secrets.toml   (mode 600; path from PMSPEC_SECRETS_FILE)
[oxylabs.us_nc]
username = "customer-…-st-us_north_carolina"
password = "…"
```

- Dotted key → nested table lookup; the leaf must be a string. A missing key is an
  upload error naming the key. There is no default value and no empty-string fallback.
- A file with mode other than 600 (or group/other readable) is refused with the `chmod`
  to run. On ex63 the file belongs to the `primemover` user, so the MCP path resolves
  there: no credential passes through the laptop or the MCP transport.
- `pmspec secrets check <lock>` lists the keys a lock references and which are missing,
  never printing values. `upload --dry-run` performs the same check.
- Resolution happens in memory, once, immediately before the HTTP request. Every
  diff, summary, log line and error message shows the placeholder, never the value.

### MCP surface (phase 1)

In `orchestrator/mcp_server/server.py`:

- `compile_experiment_spec(yaml_content, files: dict[str, str] = {}, seed: int | None = None, anchor: str | None = None, lax: bool = False) -> lock text`.
  `files` maps a pool's `source:` filename to its content; the parser gets a temporary
  `base_dir` populated from it. This also fixes spec-content validation: the four
  permanent `File not found` errors disappear once the caller passes the pools.
- `validate_experiment_spec(content, files={}, lax=False)` accepts spec or lock content.
- `upload_experiment(lock_content, mode, confirm=…)` accepts lock content only (a spec
  is rejected with "compile first"); `mode` is required (§5). Existing
  `dry_run` semantics and `_require_local_write_target()` are unchanged.
- `verify_experiment(experiment_id, lock_content)` (§3).

The write-guard AST test (`test_mcp_write_guard.py`) covers the new tools automatically.

## 3. `upload`, the provenance record and `verify` (phase 1)

### `pmspec upload <lock> [--experiment-id N] [--mode real|test] [--dry-run] [--record FILE] [--allow-dirty] [--allow-past]`

Reads **locks only**. Given a spec, it compiles to the sibling `<stem>.lock.yaml` first,
uploads that, and prints "commit `<lock>`" — there is never an upload whose lock does not
exist on disk. `--mode` is required and lands in phase 1 with the full §5 semantics (it is
a few lines once the uploader exists).

1. Load and schema-validate the lock. Refuse if `provenance.git_commit` is null or ends in
   `-dirty`, unless `--allow-dirty`.
2. Resolve the anchor: `upload` → `datetime.now(API_TIMEZONE)`; a fixed anchor must be in
   the future, else error (`--allow-past` overrides). `start_at = anchor + start_offset`,
   formatted by `time_utils.format_iso8601`.
3. Resolve `${secret:…}` (§2). Any missing key aborts before any request is made.
4. `POST /experiments` with `name`, `description`, `contact`, `active: 1`,
   `runner_user_id` (if set) and `preferences` = the provenance set below. In append mode
   (`--experiment-id`), `PUT /experiments/{id}` adds the same set with
   `description: "upload <n>"` where `n` = 1 + existing `pmspec.lock_sha256` entries;
   preference names may repeat and the verifier matches any. (Verified against the API:
   `update` reuses `ExperimentStoreRequest` and `PreferenceService::createManyForModel`
   *appends*; the request also requires `name` and `user_id`, so the PUT resends the
   experiment's current values unchanged.) Then the existing multipart
   `POST /experiments/{id}/upload`.
5. Run `verify` (below) on the result (shallow; `--deep` to include jobs). Exit 1 on any
   mismatch; the experiment is left in place and the diff is printed.
6. Print one line and append it to `--record FILE` if given:
   `2026-09-11T14:10:03+02:00 exp=181 mode=test lock=9f3a… commit=a2024c9 api=localhost:8090`.

### The provenance set

Written as experiment preferences (`name`, `description`, `value`; `value` ≤ 255 chars;
`description` is the upload label). No value is ever a secret.

| name | value |
|---|---|
| `pmspec.lock_sha256` | sha256 of the lock file bytes |
| `pmspec.spec_sha256` | `provenance.spec_sha256` |
| `pmspec.seed` | `provenance.seed` |
| `pmspec.anchor` | the *resolved* anchor, ISO 8601 with offset |
| `pmspec.git_commit` | `provenance.git_commit` |
| `pmspec.generator` | `provenance.generator` |
| `pmspec.version` | `provenance.pmspec_version` |
| `pmspec.mode` | `real` or `test` |
| `pmspec.lax` | `true` / `false` |
| `pmspec.uploaded_at` | ISO 8601 with offset |
| `pmspec.uploaded_by` | `<os user>@<hostname>` |

`GET /experiments/{id}` already returns `preferences`, so the MCP `get_experiment` shows
provenance with no API change. The GUI shows nothing until a panel is added; out of scope.

### `pmspec verify <experiment_id> <lock>` / MCP `verify_experiment`

1. **Binding.** sha256 of the local lock bytes ∈ the live `pmspec.lock_sha256` values →
   `BOUND`; else `UNBOUND`, reporting the local hash and every server-side hash. The
   usual cause is an edited-and-recompiled lock.
2. **Structure.** Build a normalized *shape* from the lock (anchor taken from the live
   `pmspec.anchor`, secrets masked as their placeholders) and from the API, then diff
   crawler-by-crawler matched on `name`: `configuration.params`, configuration
   preferences, agent location, proxy `type`/`hostname`/`port` (never credentials), queue
   count, and per queue `start_at` and — with `--deep` — the ordered job list (type +
   behaviours). Ignored: server-generated ids, timestamps, status fields, and the
   runner-populated fields. The shape builders and the diff live in
   `pmspec/lock/verify.py`; the existing summary-level comparator is unchanged.

   The live side has no single endpoint for the tree, so the reads are: `GET
   experiments/{id}` (crawlers, agent, proxy, configuration preferences, experiment
   preferences), `GET configurations/{id}` per crawler (params), the two paginated
   `experiments/{id}/queues-*` lists (name, start, crawler), and for `--deep` the
   `GET experiments/{id}/download` export — four CSVs carrying every job and behaviour in
   one request. Deep is opt-in because each export writes a zip into the API's own bucket;
   the post-upload verify is shallow unless `--deep` is passed.
3. **Output.** `BOUND`/`UNBOUND`, then the grouped diff. Exit 0 only if bound and
   identical. `verify` says nothing about execution; that is `status` (§5).

## 4. The strict contract (phase 2)

Principle: anything the compiler would drop, ignore or leave unresolved is an error. A
construct that the runner or the decision function will never see must not survive into a
lock. `--lax` (CLI) / `lax=true` (MCP) downgrades every item below to a warning, prints
`N constructs would have been dropped`, and stamps `provenance.lax: true` and
`pmspec.lax=true` — a lax upload is visible on the server for good.

| today | strict rule |
|---|---|
| unknown `configuration.params` key silently dropped (only `pi`, `psi`, `kappa`, `tau`, `search_terms`, `media_outlet_urls` reach the API) | error naming the key and the two routes that do reach the bot: factor `maps_to` → template `with:`, or `lambda_select.strategy` + `treatment` |
| `treatment:` without `strategy:` — warning, ignored downstream | error |
| `${var}` / `{var}` unresolved after expansion — heuristic warning, literal string sent to the lambda | error from a post-expansion scan of every string in the lock |
| `proxy.from_pool: true` — accepted, never implemented | **implemented**: `pool` names a data pool, `match_field` is compared exactly against the agent's `location`; zero or several matching rows is an error naming the location |
| unknown job type — warning | error |
| unknown top-level, crawler, session or job key — silently ignored | error via `additionalProperties: false` in the spec JSON schema |
| strategy schema not found (`autocomplete_v1`) — warning, `treatment` unvalidated | error unless a schema is supplied: `strategy_schemas: {name: path-or-inline}` in the spec, or a directory in `PMSPEC_STRATEGY_SCHEMA_DIRS`; the sibling `bot-decision-yt` lookup stays as the last fallback |
| unquoted ISO timestamp parsed by PyYAML into `datetime`, crash on `.strip()` | parser accepts `datetime`/`date` scalars and normalizes them to strings; no quoting rule |
| no `start` on a session — warning, "will use current time" | error: every queue in a lock has an explicit offset |
| already errors (missing `experiment.name`, empty factors, bad `replications`, …) | unchanged |

Stays a warning on purpose, each with a comment in the validator saying why it is not a
drop: a proxy without `hostname`/`port` on a self-configuring `type`; the `params` 0/1
advisory when a value is already valid. No other `warnings.append` may be added without
such a comment.

Errors carry the crawler/job path (`crawlers[12].session.workflow[3].lambda_select`),
the construct, and the fix; every message is one line so MCP callers see it whole.

## 5. Workflow (phase 3)

### Mode

`upload --mode test|real` is required on the CLI and the MCP; there is no default.

| | `test` | `real` |
|---|---|---|
| runner | forces `runner_user_id: 3` (the VM), overriding the lock | the lock's value (null → ex63) |
| name | `test-<name>-<YYYYMMDD>` | as in the lock |
| dirty or missing commit | allowed, stamped as-is | refused without `--allow-dirty` |
| provenance | `pmspec.mode=test` | `pmspec.mode=real` |

### `pmspec cleanup [--older-than 7d] [--dry-run] [--yes]`

Lists the caller's experiments carrying `pmspec.mode=test` (older than the threshold if
given), then deletes them via `DELETE /experiments/{id}/destroy` after a confirm. An
experiment without that preference is never a candidate, so a `real` study cannot be
swept by a typo. Same `_require_local_write_target()` guard as `pm_delete`.

### `pmspec status <experiment_id> [--hours 24]`

One screen: the provenance set (bound to which lock, mode, commit, generator), the
runner, then the six status buckets and `treatment_lost_jobs` / `degraded_jobs` from
`GET /experiments/{id}/health?hours=…`. It composes `verify` (specification) with
`health` (execution) and performs no analysis of its own. Exit 1 if unbound or if
`treatment_lost_jobs > 0`.

### `pmspec diff <lock-a> <lock-b>`

The §3 structural diff applied to two locks: "what changed between pre-pilot and pilot".
Provenance headers are shown side by side, not diffed.

### Docs and skills

- `docs/pmspec.md` is rewritten around *spec → lock → upload → verify*; the
  DSL reference becomes a section under "writing a spec", and a new section documents the
  lock schema for generators that write it directly.
- The MCP `doc://experiment-designer` resource follows the file.
- `.claude/skills/primemover-compose` gets the lock-first flow, the mode rule and the
  `files=` argument for pools.
- `pmspec init` templates use `${secret:…}` and `schedule.anchor`.

## 6. Testing

All in `pmspec/tests/` unless noted; no live infrastructure.

- **Lock determinism**: compile the same spec twice → identical bytes; compile with a
  different seed → different draws, identical structure.
- **Round trip**: `spec → lock → summary` equals `spec → summary` for the three example
  specs and the `--youtube-treatment` scenario.
- **Lock schema**: every example lock validates; each required key removed in turn fails
  with a message naming it.
- **Secrets**: literal credential refused at compile; placeholder resolved at upload from
  a fixture TOML; missing key aborts before any HTTP call (mocked `requests`); insecure
  file mode refused; no resolved value appears in any captured output.
- **Anchor**: relative-only spec → `upload`; absolute-only → earliest; mixed → error;
  `start_at` arithmetic across a DST boundary in Europe/Zurich.
- **Upload** (mocked API): provenance set sent on create; append mode numbers the upload
  label; post-upload verify failure exits 1; `--record` line format.
- **Verify**: bound/unbound; each diffed field perturbed in turn is reported at the right
  path; server-generated fields ignored.
- **Strict contract**: `test_strict_contract.py`, table-driven — one spec fragment per
  §4 row, asserting an error under strict and a warning plus the `lax` stamp under
  `--lax`; `from_pool` matching including the zero- and multi-match errors.
- **MCP**: `test_mcp_write_guard.py` covers the new tools by construction; a
  `files=` test proves a pool passed as content resolves.
- **Workflow**: mode table rows; `cleanup` never selects an experiment lacking
  `pmspec.mode=test`; `status` exit code on `treatment_lost_jobs`.

The real verification, as always, is compiling and uploading the two projects' specs in
`test` mode against the VM and reading `verify` and `status` — say so in the PR rather
than claiming it works.

## 7. Follow-ups in the research repos (recorded, not part of this work)

- `autocompleted_misinformation/code/design/build_spec.R`: already passes factor
  variables through `with:` and uses `proxy.from_pool`; needs `strategy_schemas:
  {autocomplete_v1: …}` pointing at its decision function's schema, and `runner_user_id`
  moves out of the generator flag into `--mode test`.
- `elections_ad_targeting/code/build_prepilot_spec.R`: replace the rendered Oxylabs
  username/password with `${secret:oxylabs.<state>.username}` / `.password` and a
  `secrets.toml` on ex63; the generated spec can then leave `data/` and be committed.
- `~/control_room/CLAUDE.md` "PrimeMover experiments" table: keep, but source rows from
  each project's `--record` ledger.

## 8. Out of scope

GUI provenance panel; API columns for provenance; server-side storage of lock bytes;
per-provider runner credential types; automatic ledger updates outside the `--record`
file; graded (non-0/1) treatment intensities.
