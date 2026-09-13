# pmspec phase 4: content-addressed locks, per-bot template draws, execution audit

**Date:** 2026-09-13 · **Status:** approved design, not yet implemented
**Repos touched:** `primemover-orchestrator` only. Extends
`2026-09-11-pmspec-lock-and-provenance-design.md`; that spec's rules stay in force.

## Problem

Three things the first real studies exposed after phases 1–3 shipped:

1. **The lock is huge.** The autocompleted study's lock is 46 MB: 600 queues written out, of
   which 6 are distinct (five arm diets and one measurement session), and 300 agent bodies of
   which 2. Git takes it, but every compile, verify and MCP round-trip pays for it.
2. **A template parameter cannot be drawn per bot.** `with:` values are strings substituted at
   parse time; pools resolve per crawler later. So every bot in the autocompleted diet types the
   same four queries — the only construction the DSL allows — although the design memo asks for
   per-bot variation and less repetition over months.
3. **The lock proves what was uploaded, not what the bots got.** The `$green` bug (a
   `repeat:` item dropping `with:`) was caught at compile time only because the strict contract
   existed; nothing checks, after a run, that the decision function reported the treatment the
   lock says each job asked for.

## Decision

### 1. Lock format 2 — content-addressed parts

```yaml
pmspec_lock: 2
provenance: {…}                      # unchanged
experiment: {…}                      # unchanged
schedule: {anchor: …}                # unchanged
parts:
  queues:
    q:3f9a1c…: {name: Daily diet, description: …, timezone: …, timezone_utc_offset: …, active: true,
                jobs: [ … ]}         # everything but start_offset
  agents:
    a:77b0e2…: {identification: mlx, location: US-IL-CHICAGO, multilogin_profile: {…}, active: true}
                                     # everything but name
crawlers:
  - name: bot_01
    description: …
    configuration: {…}               # unchanged (small, per bot)
    proxy: {…}                       # unchanged (per bot by design)
    agent: {ref: "a:77b0e2…", name: Agent 01}
    queues:
      - {ref: "q:3f9a1c…", start_offset: "+0s"}
```

- A part's key is `sha256` of its canonical JSON (`sort_keys=True, separators=(",", ":"),
  ensure_ascii=False`), prefixed `q:`/`a:`, truncated to 16 hex characters; a collision is a
  compile error (checked on insert, never assumed away).
- `load_lock` materialises parts back into the phase-1 in-memory `Lock` (every crawler carries
  full `queues` and `agent`), so `upload`, `verify`, `diff`, `status`, `summary` and the MCP need
  no change. `dump_lock` factors on write. Format 1 files still load; `compile` writes format 2.
- Byte-determinism holds: parts are emitted sorted by key. `lock_sha256` still binds the file.
- Schema `lock-2.schema.json`; `validate_lock_dict` dispatches on `pmspec_lock`.

### 2. Per-bot template draws

A `with:` value may be a pool draw:

```yaml
- template: diet_search_google
  with:
    query: {from_pool: queries, field: term, select: random}
    reliability: "{reliability}"
```

- `field` is required and names the column whose value is substituted. `select` is `random`
  (default) or `first`; `count` is not accepted here (one value per `with:` key — N distinct
  values are N template calls, which is what a generator writes anyway).
- **Per crawler, without replacement within a crawler**: every draw from the same pool by the
  same crawler takes a row not yet taken by that crawler; running out is an error naming the
  crawler, pool and count. Draws are seeded from the lock's seed and the crawler index, so the
  lock is byte-deterministic and two bots get different queries.
- Mechanism: the parser substitutes the template's `${query}` with a **deferred draw token**
  `%%pmspec-draw:<pool>:<field>:<select>:<n>%%` (`<n>` is the ordinal of this draw within the
  `with:` block, so two keys drawing from the same pool are distinct draws). The token lives only
  between parser and converter; it is never written to a lock, because a surviving token is a
  strict-contract drop and the compile fails. The converter, after pool-reference resolution and
  before proxy assignment, replaces every token per crawler; the strict scan reports any token
  that survives (a drop). The validator: a dict-valued `with:` entry must be a pool draw with
  `from_pool` (string), `field`; anything else is a drop.
- A `with:` dict applies to `template:` items and `repeat:`+`template:` items alike (each
  repetition draws again, without replacement).

### 3. `pmspec audit` — execution against the lock

`pmspec audit EXPERIMENT_ID LOCK [--hours 24] [--sample K] [--strict]`; MCP
`audit_experiment(experiment_id, lock_content, hours=24, sample=None, strict=False)`.

For every job in the lock that carries a `strategy` behaviour, matched to the live job by
(crawler name, queue name, job ordinal), considering only jobs whose `status_code` is a finished
one (`success`, `warning`, `failure`, legacy `failed`), and only queues finished inside the
window:

| check | source | outcome |
|---|---|---|
| reported | `decision_report` present (`status.job_decision_reported`) | `unreported` — unknown, never clean |
| requested | expected filter set from the lock vs `decision_report.requested` | `mismatch` |
| applied | `decision_treatment_lost` false and `applied ⊇ requested` (`status.job_decision_verdict`) | `lost` / `degraded` |

Expected filter set per strategy, from the lock's behaviours on that job:

- `youtube_v1` (the decision function's own table, `lambda_function.py:996-1004`):
  `pi==1 → mainstream_media`, `psi==1 → children_entertainment`, `kappa==1 → similar`,
  `tau==1 → political`, `beta!=0 → fringe`.
- any other strategy: every treatment key whose value is not in `{"", "0", "none", "None", 0}`,
  unless the strategy's schema carries `x-requested: {<key>: <filter name>}` (a JSON-schema
  extension the schema author may add); then the mapping is applied and keys not in it are
  ignored.

Output: a table per (arm — the lock crawler's `reliability`/`lean` or `pi/psi/kappa/tau` params —
× check) with counts, then the first 10 offending jobs with ids; `render()` never prints
secrets (there are none on this path). Exit 1 on any `mismatch` or `lost`; `unreported` and
`degraded` are printed and exit 0 unless `--strict`. A job absent on the live side is
`missing` (exit 1 with `--strict`, else reported). Reads: `GET experiments/{id}` (crawler ids by
name), the two queue lists, then `GET jobs?filter[queue]=<id>` per queue (`decision_*` columns are
on the row). `--sample K` audits a seeded random sample of K crawlers to bound the read count.

## Rejected

- Storing job-level treatment on the server so the audit needs no lock — it is the lock's job to
  say what was asked; the server records what happened.
- A generic `with:` expression language — YAGNI; one draw per key covers both studies.
- Compressing format-1 locks instead of factoring — solves the bytes, not the redundancy that
  makes `diff` and review unreadable.

## Testing

Format 2: round trip 1→2→1 equals; byte-determinism; the 46 MB study lock reduced below 2 MB
(measured in a test on a synthetic 300-crawler spec, not the research repo); a format-1 file
loads; collision path. Draws: two crawlers get different values, deterministic per seed;
without-replacement within a crawler; exhaustion error; missing `field`/column errors; a
surviving token is a drop; `repeat:` draws per repetition. Audit: table-driven fixtures for the
four outcomes and the strategy mapping; `x-requested`; sample; exit codes; MCP parity.
