# pmspec phase 6: longitudinal runs, per-crawler assignment, seeded timing

**Date:** 2026-09-16 · **Status:** approved design, not yet implemented
**Repos touched:** `primemover-orchestrator`, plus one small `primemover_api` change for
`updates` (§2). Extends
`2026-09-11-pmspec-lock-and-provenance-design.md` and `2026-09-13-pmspec-phase4-design.md`;
their rules (byte-determinism, strict contract, no secrets in output, one seed) stay in force.

## Problem

Designing `websearch_polarization` experiment 3 (500 bots, 180 days, a Comscore session per
bot-day, ideology updated nightly, weekly trending terms) against pmspec on 2026-09-16
surfaced gaps that every lab study of this kind hits:

1. **A lock is static.** The only way to give bots new sessions after upload is to append
   *crawlers* with `upload -e`, so a bot-day becomes a crawler: crawler ≠ bot, 500 rows a day,
   and `verify`/`audit` become per-day affairs by accident.
2. **Per-bot data cannot be expressed.** "Bot 17 replays panelist 510121845" or "bot 17's own
   outlet list" has no home: factors are shared levels, sampled parameters are scalars, and
   pool draws exist only for `proxy:` and template `with:`.
3. **Timing is fixed or hand-drawn.** `wait` takes one duration and `start_at` one instant, so
   a generator draws the numbers itself and the seed lives outside the lock.
4. Two proportional factors were perfectly confounded (fixed the same day, `d9d99a3`; not
   part of this design).

## Decision

### 1. The runs lock

A second lock *kind*. A fleet lock is what exists today (crawlers with configuration, agent,
proxy, queues); it is the randomization record and is uploaded once. A **runs lock** adds
queues to the fleet's crawlers, matched by name.

```yaml
pmspec_lock: 2
kind: runs                                  # absent ⇒ fleet; every existing lock still loads
provenance:
  …as today…
  base_lock_sha256: <sha256 of the fleet lock file>
  base_spec_path: <the fleet lock's recorded spec_path>
experiment: {name: …}                       # copied from the base; upload refuses a mismatch
schedule: {anchor: upload | <Europe/Zurich wall-clock ISO>}
parts: {queues: {…}}                        # content-addressed as in format 2; no agents
crawlers:
  - name: wsp3_017                          # must exist in the base
    queues: [{ref: "q:…", start_offset: "+2h13m"}]
    updates: {preferences: {ideology: "0.31"}}   # optional, opt-in per field (§2)
```

No `configuration`, `agent` or `proxy` per crawler; no `${secret:…}` anywhere (proxies are
not in it). `LockError` on a runs lock that carries any of those keys.

The spec that compiles to it:

```yaml
version: "1.0"
runs_for: design/fleet.lock.yaml            # relative to the spec; its sha256 is recorded
templates: …                                # as today
crawlers:
  - name: wsp3_017                          # explicit …
    session: {name: "2026-10-01", start_at: "2026-10-01T10:00:00", workflow: […]}
    additional_sessions: […]
    updates: {preferences: {ideology: "0.31"}}
  - from_base: true                         # … or one block for every base crawler
    name: "{base.name}"
    session: {…, workflow: [{template: day, with: {pi: "{base.ideology0}"}}]}
```

`from_base: true` iterates the base's crawlers in base order and exposes `{base.name}`,
`{base.location}` (the agent's) and `{base.<preference name>}` (every configuration preference)
as substitution variables, with `{…:fmt}` as elsewhere. A runs spec may carry `data_pools`,
`strategy_schemas`, `defaults` (session-level keys only) and `assign:` (§3). It refuses
`design:` (the randomization happened in the base), `proxy:`, `agent:` and `configuration:`
on a crawler — each a validation error, not a drop, because the author clearly meant a fleet
spec.

`pmspec compile` on a runs spec: load the base lock (a fleet lock; a runs lock as base is an
error), check every crawler name exists in it (error listing the missing names), expand
templates, draws and waits exactly as for a fleet spec under the same seed rules, derive the
anchor from the sessions' `start_at` as today, write `kind: runs` and the base fields. The
runs lock is byte-deterministic for a given spec, base and seed. `validate`, `preview`,
`summary` accept a runs spec/lock; `diff` compares two runs locks (crawler names, queues,
updates) and refuses a runs lock against a fleet lock.

A runs lock is bound to exactly one base. A study that re-randomizes is a new fleet and a new
experiment. Static repeating schedules (the canary) need none of this: a fleet crawler with N
queues at daily offsets is already a lock.

### 2. Upload, updates, provenance; what verify, status and audit mean

`pmspec upload runs.lock.yaml --mode real|test -e <id>` — `-e` is required for a runs lock,
and so is `experiment_id` in MCP `upload_experiment`. Before anything is written:

1. Fetch the experiment and its provenance preferences. The **first** upload's
   `pmspec.lock_sha256` must equal `base_lock_sha256` and its `pmspec.mode` must equal the
   requested mode; otherwise refuse ("created from a different fleet lock" / "test experiment").
   `--mode real` keeps the dirty-or-unknown-commit refusal, judged on the runs spec's repo.
2. Map crawler names to live ids from the experiment's crawlers; a name missing or duplicated
   live is an error naming it.
3. Resolve the anchor and offsets as today (`--allow-past` unchanged).

Then apply, per crawler in lock order: `POST crawlers/{id}/upload` with the crawler's queue
list (the same queue JSON the fleet upload sends, `start_at` resolved), skipping any queue
whose (start wall-clock, name) already exists live on that crawler — so re-running the same
lock after a mid-way failure is safe and adds nothing twice; then `updates`. Finally the
provenance preferences as "upload N": today's set plus `pmspec.kind = runs`,
`pmspec.base_lock_sha256`, and when updates were applied
`pmspec.updates = <JSON {crawler name: {"preferences.<name>": [old, new]}}>`. On failure nothing
is written to provenance and the last crawler completed is printed. Dry-run (the default over
MCP) prints crawlers / queues / updates counts and the first refusals, and writes nothing.

**Updates.** Only fields under `updates:` are touched, and only `preferences.<name>` is
accepted (params, agent and proxy fields are refused at validation: changing those mid-study
is a different experiment). The old value is read first and recorded with the new one.

The API cannot do this today: `PUT configurations/{id}` validates only `name`, `description`,
`active` (`ConfigurationUpdateRequest`), and there is no preferences resource. So `updates`
needs one small change in `primemover_api` — `ConfigurationUpdateRequest` accepts an optional
`preferences` list and the repository syncs it by name (update the value of an existing
preference, create a missing one, never delete) — shipped as its own PR before the pmspec
upload path is exercised. Until it is deployed, `pmspec upload` validates and records
`updates` in the lock and provenance but refuses to apply them with a message naming the API
version needed; the CLI/MCP detect support by probing the endpoint once per upload (a request
with an empty `preferences` list must be accepted, not stripped). This is the one item of the
phase outside this repo.

**verify** on a runs lock compares only what it uploaded: per crawler, the live queues whose
(start wall-clock, name) match the lock's; extra live queues belong to other uploads and are
ignored; a lock queue with no live match is a difference; `--deep` compares jobs as today.
With `updates`, the live preference must equal the new value. Crawler shape is the fleet
lock's verify, not repeated. `lock_sha256` binding checks the runs lock's own sha against the
matching "upload N" preference.

**status** lists every upload with its kind and base sha, and summarises "fleet + N runs
uploads, last <date>". **audit** takes a runs lock as it takes a fleet lock — it already
matches by (crawler name, queue name, job ordinal) inside a `finished_at` window; queues not in
the lock are skipped, not counted. **cleanup** is unchanged.

### 3. Per-crawler assignment (`assign:`)

```yaml
crawlers:
  - from_design: true
    name: "wsp3_{cell_index:03d}"
    assign:
      panelist: {from_pool: panelists, match_field: region, select: random}
      outlets:  {from_pool: outlet_sets, select: first}
    configuration:
      preferences:
        - {name: comscore_person_id, value: "{panelist.person_id}"}
        - {name: outlet_list, value: "{outlets.urls}"}
```

The proxy-pool rules, generalised:

- Runs **after design expansion and before every substitution pass**, per crawler in
  expansion order. Each named assignment takes one row from its pool, **without replacement
  across crawlers** for that pool; running out is a compile error naming the crawler, the
  pool and the count needed.
- `match_field: <column>` compares the row's column with the crawler's `agent.location`;
  `match_value: "{city}"` overrides what it is compared with. No `match_field` → any row.
- `select: random` (default) draws among the free matching rows with
  `random.Random(f"{seed}:assign:{name}")`; `select: first` takes them in pool order — with no
  match that is "row i to bot i" for a pre-built table.
- Every column of the drawn row is `{<assignment>.<column>}` (and `:fmt`), substituted anywhere
  in the crawler dict: names, preferences, agent and proxy fields, template `with:` values,
  treatment blocks. A leftover `{panelist.…}` is a drop (the existing unresolved-placeholder
  scan). A column referenced nowhere is not a drop — the row is data.
- The drawn values are recorded where they were substituted; the lock is the assignment
  record; `provenance.pools` already hashes the pool file.
- `proxy: {from_pool: …}` stays as it is and shares the without-replacement bookkeeping, so
  one pool used by both cannot hand a row out twice.
- Validator: `assign` is a known crawler key; each entry needs `from_pool` naming a declared
  pool; `match_field` must name a column of that pool; `select` ∈ {random, first}; an
  assignment name equal to a factor, a sampled parameter or `base` is an error (ambiguous
  `{x}`); `match_value` must resolve (a leftover is a drop).
- Runs specs may use `assign:` with the same rules (e.g. today's Comscore session per bot from a
  `sessions` pool with `match_value: "{base.comscore_person_id}"`), seeded by the runs lock's
  seed.

### 4. Seeded timing

**Wait ranges.** `wait: {min: 30s, max: 60s}` — durations as today, both required, `min ≤ max`.
The parser emits a deferred token (`%%pmspec-wait:<min>:<max>%%`, seconds) because templates
expand before design expansion; the converter resolves it per (crawler i, queue j, job k) with
`random.Random(f"{seed}:wait:{i}:{j}:{k}")`, integer seconds, uniform inclusive, and the lock
stores the number. A surviving token is a drop. `wait: 45s` / `wait: 45` unchanged.

**Start jitter.** `session.start_at: {from: "+1h", jitter: "6h"}` — `from` is anything
`start_at` accepts today; `jitter` a duration. The session starts at `from + U(0, jitter)`,
drawn per (crawler i, session j) with `random.Random(f"{seed}:start:{i}:{j}")`, integer
seconds, stored as a concrete `start_offset` under the usual anchor — verify and audit need
nothing new. Same form in `additional_sessions` and in runs specs.

**Nested Multilogin flags.** `maps_to: {agent.multilogin_profile.parameters.flags.<flag>: {…}}`
reaches the API agent JSON through `_set_nested_value` and the converter's deep-merge over
`DEFAULT_MULTILOGIN_PROFILE`; this item is a test proving the full path plus a validator rule:
a `multilogin_profile.parameters.flags.<key>` absent from the default profile's 17 flags is a
drop (Multilogin ignores unknown flags silently). Values are not checked — the repo holds no
authoritative vocabulary per flag, and Multilogin rejects a bad value at profile creation,
which the runner reports as a failed job rather than silently.

## Rejected

- Bot-day crawlers appended with `-e` (the status quo): crawler ≠ bot; 500 rows × 180 days;
  verify/audit per day by accident; analysis must reconstruct bots from a preference.
- One fleet lock with a schedule template materialised per day by pmspec: the daily content
  depends on state pmspec does not have (yesterday's ideology, today's Comscore session).
  The static case it would serve is already a fleet crawler with N queues.
- A mutable configuration by default: the fleet lock would stop describing the live crawler.
  Opt-in `updates` with old→new recorded keeps the randomization record and the current state
  apart.
- An index-keyed assignment table as a separate feature: `assign … select: first` with no
  match covers it.

## Testing

Runs lock: round-trip and byte-determinism; `kind` absent ⇒ fleet; a runs lock with
`agent`/`proxy`/`configuration` or a runs lock as base ⇒ `LockError`; compile refuses a missing
base name and `design:`; `from_base` substitution of name, location and preferences; `diff`
between two runs locks. Upload: dry-run plan; base-sha, mode, missing/duplicate-name refusals;
per-crawler POSTs against a fake session; idempotent re-run skips existing queues; updates
old→new in `pmspec.updates`; an API without preference support ⇒ refusal naming it, nothing
applied; failure leaves provenance untouched. Verify: scoped to the lock's
queues, extra live queues ignored, missing queue reported, updated preference checked. Status
lists kinds; audit skips out-of-lock queues. Assignment: without replacement; `match_field`,
`match_value`; `first` = pool order; exhaustion, unknown pool, bad column, name collision;
values land in names, preferences, agent fields, `with:`, treatment; leftover placeholder is a
drop; proxy pool and `assign` share the taken set. Waits and jitter: reproducible per seed,
different across crawlers, stored in the lock, surviving token is a drop, `min > max` error.
MLX flags: spec → lock → API JSON for one flag; unknown flag is a drop. API PR
(`primemover_api`): PHPUnit for preference sync by name, create-missing, never-delete.
MCP parity: `compile_experiment_spec` with a runs spec + base lock in `files=`,
`upload_experiment` with a runs lock (`experiment_id` required). Schema regenerated
(`make schema` drift test). Docs: `docs/pmspec.md` § Longitudinal studies, § Assignment,
§ Timing; CLAUDE.md's pmspec paragraph.
