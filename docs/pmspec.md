# pmspec — the experiment designer

`pmspec` (the `pmspec/` package of this repo, CLI `pmspec`, MCP tools `*_experiment_spec`,
`upload_experiment`, …) provides a YAML-based Domain-Specific Language (DSL) for defining PrimeMover browser automation experiments. It supports factorial experimental designs, reusable templates, and automatic expansion into API-compatible JSON.

## Workflow: From Plain English to Running Experiments

The recommended workflow for creating experiments is:

### Step 1: Describe Your Experiment in Plain English

Start with a plain English description of your experimental design, similar to what you'd write in an R Markdown file or research proposal:

```
Study: Web Search Polarization
- 100 crawlers with different political orientations
- Usage types: 25% visits only, 25% searches only, 50% both
- Pi (political orientation) sampled uniformly from -1 to 1
- Kappa (engagement level) sampled from discrete distribution
- Media outlets selected based on proximity to crawler's pi value
- Search terms matched to crawler's political orientation
```

### Step 2: Draft YAML Specification with Claude

Ask Claude to translate your plain English description into a YAML specification:

```
"Based on my research design above, please draft a YAML specification
using the PrimeMover experiment designer DSL. Use:
- Proportional factor levels for usage types (25/25/50 split)
- Uniform distribution for pi
- Data pools for outlets and search terms with scoring"
```

Claude will generate a YAML spec using the DSL features documented below.

### Step 3: Compile to a lock

```bash
git add my-study.yaml outlets.csv && git commit -m "study: spec v1"   # spec and pools first
pmspec compile my-study.yaml --seed 42        # writes my-study.lock.yaml beside it
git add my-study.lock.yaml && git commit -m "study: lock v1"
```

The order matters: **commit the spec and its pools, then compile, then commit the
lock.** The lock records the commit it was compiled from, with a `-dirty` suffix when
the tree was not clean, and `upload --mode real` refuses a dirty or unknown commit.

The **lock** is the reproducibility artifact: every crawler expanded, every random
draw fixed by the one seed, every data pool inlined, secrets left as
`${secret:…}` placeholders, and session starts expressed as offsets from a
`schedule.anchor`. It is committable by construction — `compile` refuses a literal
credential — and byte-deterministic: the same spec, pools and seed produce the
byte-identical lock, from any working directory, so its sha256 (the server-side binding)
only changes when an input does. Its header records the spec's path relative to the
repository root, the spec's sha256, the git commit it was compiled from, the generator,
the pmspec version and the seed; there is no timestamp.

### Step 4: Upload the lock

```bash
pmspec secrets init my-study.lock.yaml        # scaffold ~/.config/primemover/secrets.toml
pmspec secrets check my-study.lock.yaml       # which ${secret:…} keys it needs, none printed
pmspec upload my-study.lock.yaml --mode test  # VM runner, name prefixed test-…, dirty commit ok
pmspec upload my-study.lock.yaml --mode real  # as in the lock; refuses a dirty/unknown commit
```

`secrets init` writes a mode-600 TOML skeleton with one `key = ""` per placeholder the lock
references, grouped into `[section]` tables by the dotted key's prefix (`oxylabs.a.username` →
`[oxylabs.a]`). `secrets check` reports each of those as MISSING until filled in by hand. It
refuses to touch an existing file unless `--force`, which merges in place: every existing value
(filled or not) and every table the lock does not reference are kept, and only the still-missing
leaves are added -- it prints which keys were added and which were already present, and never a
value. The merge rewrites the file from the parsed data, so comments and key order in the
existing file are not preserved (values are; keys come out sorted within each table). If the
path is a symlink the rewrite lands on its target and the link is kept. A key the lock needs as a
string but the file holds as a table (or a non-string) is a conflict: init refuses and changes
nothing, matching `secrets check`, which reports such a key MISSING.

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

### Step 6: Day to day

```bash
pmspec status 181                                    # provenance + health, no lock needed
pmspec status 181 my-study.lock.yaml --deep          # + verify: BOUND, and jobs/behaviours too
pmspec diff my-study.lock.yaml my-study-v2.lock.yaml # what changed between two locks
pmspec audit 181 my-study.lock.yaml --hours 24       # did the treated bots actually get the treatment?
pmspec cleanup --dry-run                             # list test-mode experiments eligible for deletion
pmspec cleanup --older-than 7d --yes                 # delete them
```

`status` is a superset of `verify`: without a lock it reports provenance (the `pmspec.*`
preferences the experiment was uploaded with) and the `--hours`-windowed health; give it a lock and
it also runs the same BOUND/structural check as `verify`, `--deep` included. It is the one command
to reach for when checking in on an experiment later, lock in hand or not. It exits 1 when the
experiment carries no pmspec provenance at all (nothing was ever bound to a lock), when the given
lock is not bound or not identical, or when any job lost its treatment.

`diff` compares two locks directly — provenance side by side, the anchor, and crawler-level
structural differences — with no live experiment or API call. Use it before uploading a revision,
to see exactly what would change relative to the lock already bound to a live experiment.

`audit` closes the gap `verify` leaves open: `verify`/`status` prove the *lock* matches what was
*uploaded*, not what the bots *did*. For every job in the lock that carries a `strategy` behaviour
(see "Treatment via the decision strategy" below) — or, with no `strategy`, every
`lambdasingleselectjob`, which is a **params-based** YouTube study (the standing treatment canary,
the e2e scenarios): its treatment is read from the crawler's `configuration.params`
(`pi`/`psi`/`kappa`/`tau`/`beta`) and audited under `youtube_v1` — matched to the live job by
(crawler name, queue name, job ordinal; queues sharing a name are ordered by `start_offset`) and
considering only finished jobs in queues that finished inside `--hours`, `audit` compares the
lock's expected treatment against the decision function's own reported verdict:

| outcome | meaning |
|---|---|
| `ok` | the job requested and received exactly the treatment the lock's behaviours describe |
| `unfinished` | the job's `status_code` is a running state — never a failure, `--strict` or not |
| `shape` | the live job at this ordinal is a different `type` than the lock's, or the live queue has more jobs than the lock's (one finding per queue; its ordinals are still audited) — the queue drifted; always a failure |
| `unreported` | the job finished but carries no decision verdict at all (or a NULL/unrecognised `status_code`) — unknown, not clean |
| `mismatch` | the decision function requested a different filter set than the lock's treatment implies |
| `lost` | `decision_treatment_lost` — the bot was configured to be treated and produced control-arm data |
| `degraded` | some, but not all, of the requested filters applied |
| `missing` | the lock names a crawler, queue or job ordinal with no live counterpart |

`pi`/`psi`/`kappa`/`tau`/`beta` treatments under the `youtube_v1` strategy map to decision-function
filters as `pi→mainstream_media`, `psi→children_entertainment`, `kappa→similar`, `tau→political`,
`beta≠0→fringe`; any other strategy expects every non-empty treatment key as a filter of the same
name, unless its schema declares `x-requested: {<key>: <filter name>}`. Two filters are **hygiene**,
not treatment, and never count as a mismatch in either direction: `children_entertainment` runs for
every crawler (the decision function reports it `ok` unconditionally and the runner persists it in
`requested`, psi or not) and `already_watched` is always-on history filtering. A consequence: **`psi`
cannot be verified from the report** — the filter's state is `ok` for `psi=0` and `psi=1` alike.
`audit` exits 1 on any `shape`, `mismatch` or `lost` regardless of `--strict`; `--strict`
additionally fails on `unreported`, `degraded` and `missing`, and on an audit that examined **no job
at all** (a window that excludes everything is not a clean run; the report says so either way).
`--sample K` audits a seeded random sample of `K` crawlers instead of all of them, to bound how many
jobs it reads; `--hours` and `--sample` must be at least 1. See
`.claude/skills/primemover-debug/SKILL.md` for using it to diagnose a study that finished green but
may have lost its treatment arm.

`cleanup` never deletes by name. A candidate must have a name starting `test-` **and**
`pmspec.mode == "test"` in its preferences — the prefix only narrows which experiments are fetched;
the preference is what actually authorises deletion. `--dry-run` lists candidates and deletes
nothing; `--older-than 7d|36h|90m` keeps only experiments whose latest `pmspec.uploaded_at` is at
least that old (omit it and an experiment with no parseable timestamp is still a candidate);
`--yes` skips the confirmation prompt. The experiments it can see are the caller's login's:
under the shared MCP identity (a service account) that is *everyone's* pmspec test experiments,
so prefer `older_than`, and never run it while a colleague's test is in flight.

### The seed

There is exactly one: `--seed N` (or `design.seed` when no flag is given). `compile`
refuses to produce an unseeded lock. Pools, file selections and design expansion all
derive from it, so the same seed reproduces every draw.

One seed, several *streams*. Everything that draws takes its own, named, sub-stream
of the recorded number, so that two independent things in a design cannot move
together:

| stream | draws | derived from |
|---|---|---|
| design expansion | factor cells, `design.parameters` distributions | the seed itself |
| `files` | `!file {select: random}` | `random.Random(f"{parser_seed}:files")` |
| `pools` | a data pool's `{select: random}` | `random.Random(f"{parser_seed}:pools")` |
| `assign:<name>` | one pool row per crawler for an `assign:` entry | `random.Random(f"{seed}:assign:<name>")` |
| `<i>` | per-bot template `with:` draws for crawler *i* | `random.Random(f"{seed}:{i}")` |
| `start:<i>:<j>` | a session's start within its jitter window | `random.Random(f"{seed}:start:{i}:{j}")` |
| `wait:<i>:<j>:<k>` | a wait range's seconds | `random.Random(f"{seed}:wait:{i}:{j}:{k}")` |

(`parser_seed` is `seed + 1`, the parser's base; it is not recorded separately —
`provenance.seed` is the one number, and every stream above is a function of it.)
`select: top` and `select: first` draw nothing — `top` sorts by the `score:`
expression and `first` slices — so only `select: random` consumes the `pools`
stream. `DataPoolManager` builds a `FileLoader` of its own to read pool sources;
it shares the `pools` stream and never samples from it.

The `files` and `pools` streams were split in 2026-09; before that both were seeded
with `parser_seed` directly and both select with `rng.sample`, so a `!file
{select: random}` and a pool `{select: random}` over lists of the same length drew
the *same rows*. Splitting them changes the bytes of any existing lock that uses a
`!file` or a pool — recompile and re-commit rather than expecting an old lock to
reproduce.

---

## Overview

Instead of manually creating complex JSON structures, you can define experiments in human-readable YAML:

```yaml
experiment:
  name: "My Study"

design:
  factors:
    condition:
      levels: [control, treatment]
  replications: 5

crawlers:
  - from_design: true
    name: "bot_{condition}_{replica:02d}"
    # ... configuration
```

This automatically generates 10 crawlers (2 conditions × 5 replications) with proper naming and parameter mapping.

### Runner assignment

The `experiment:` mapping accepts an optional runner choice:

- `runner_user_id` (optional, integer): which runner executes the study. `1` = ex63
  (the default when omitted), `3` = the runner-only VM.

### Locks, provenance and verify — for generators

A lock can also be *written* directly by a generator (R, Python) instead of compiled from a
spec; `pmspec validate my.lock.yaml` accepts either format below, checked against
`pmspec/schema/lock-1.schema.json` or `lock-2.schema.json` by its own `pmspec_lock`
value. Format 1 shape:

```yaml
pmspec_lock: 1
provenance: {spec_path, spec_sha256, generator, git_commit, pmspec_version, seed, lax}
experiment: {name, description, runner_user_id}
schedule: {anchor: upload | "YYYY-MM-DDTHH:MM:SS"}   # Europe/Zurich wall clock
crawlers:            # the API upload shape, except:
  - …
    proxy: {…, username: "${secret:k.u}", password: "${secret:k.p}"}   # placeholders, never literals
    queues:
      - start_offset: "+5m"                                            # not start_at
```

**Format 2** factors out the repetition a large study's crawlers otherwise repeat verbatim: each
distinct queue body and agent body is written once under `parts`, keyed by a `q:`/`a:`-prefixed,
16-hex-character sha256 of its own canonical JSON, and each crawler carries only a `{ref: "q:…",
start_offset: "+0s"}` / `{ref: "a:…", name: …}` pointing at one. `pmspec compile` now writes format
2 by default (a study with 300 crawlers drawn from a handful of distinct diets shrinks
accordingly); a hand-written generator may still emit format 1, and `validate`/`load_lock`
materialise either one back into the same full-crawler shape everything downstream (`upload`,
`verify`, `diff`, `status`, `audit`, the MCP) already expects, so nothing else needs to change:

```yaml
pmspec_lock: 2
parts:
  queues:
    q:3f9a1c2b4d5e6f70: {name: Daily diet, jobs: […]}       # everything but start_offset
  agents:
    a:77b0e29a1c3d4f56: {identification: mlx, location: US-IL-CHICAGO}   # everything but name
crawlers:
  - name: bot_01
    agent: {ref: "a:77b0e29a1c3d4f56", name: Agent 01}
    queues:
      - {ref: "q:3f9a1c2b4d5e6f70", start_offset: "+0s"}
```

Through the MCP: `compile_experiment_spec(yaml_content, files={"pool.csv": "<content>"}, seed=…)`
returns the lock text; `upload_experiment(lock_content, mode=…)` and
`verify_experiment(experiment_id, lock_content)` take it from there. The MCP cannot see the
repo the spec lives in, so provenance is the caller's to supply: pass
`git_commit="<short HEAD>"` (suffix `-dirty` if the tree is not clean) and
`spec_path="design/spec.yaml"` and the lock records them verbatim; without them it records
`git_commit: null` and a `real` upload is refused. The complete loop from a laptop session is
therefore: commit the spec and pools → `compile_experiment_spec(…, git_commit=…, spec_path=…)` →
write the returned lock into the repo and commit it → `upload_experiment(lock, mode=…)` →
`verify_experiment`.

For checking in later, two more MCP tools mirror the CLI's day-to-day commands:
`experiment_status(experiment_id, hours=24, lock_content=None, deep=False)` reports provenance and
health always, and — when `lock_content` is given — the same BOUND/structural verify as
`verify_experiment`, `deep` included, all in one call; `diff_locks(lock_a, lock_b)` compares two
locks' provenance, anchor and crawler structure with no live experiment or API call at all;
`audit_experiment(experiment_id, lock_content, hours=24, sample=None, strict=False)` mirrors
`pmspec audit` — did each finished lambda job apply the treatment the lock says it should.
`summarize_yaml_spec` accepts a lock as well as a spec (auto-detected), like `pmspec summary`.

## The strict contract

`pmspec validate`, `pmspec compile` and MCP `compile_experiment_spec`/`validate_experiment_spec`
all share one rule: **whatever the pipeline would silently drop, ignore, or leave unresolved is
an error, not a warning.** A spec that "compiles with some warnings" is, by default, a spec that
refuses to compile — the warnings name real constructs that never reached the runner. This
replaced a validator that reported the same conditions as warnings, which is why an older spec
that "expects remaining warnings" is a spec with drops that were never fixed.

Checked, in the order reported:

| what | where |
|---|---|
| unknown key | spec root, `experiment`, `defaults`, `design`, each crawler, its `configuration`, `proxy`, `agent`, `session`/`additional_sessions`/`queues`, `defaults.proxy` |
| unknown `configuration.params` key | each crawler (only `pi`/`psi`/`kappa`/`tau`/`alpha`/`beta`/`active`/`search_terms`/`media_outlet_urls` are read downstream) |
| unknown `design.factors.*.maps_to` param | the design block |
| `lambda_select.treatment` set without `lambda_select.strategy` | the decision function never sees a `strategy`-less treatment |
| unknown job type in a workflow | each crawler's session(s), including the `jobs:` of a `repeat:` item and every template |
| an unknown key on a `template:`/`repeat:` item | the parser reads exactly `template`, `repeat`, `with`, `jobs` on such an item; a `weight: 0.7` or `condition: ...` beside them is silently ignored |
| a session/queue with no start time | each crawler's session(s) |
| an unresolved `${var}`/`{var}` after template and design expansion | anywhere a value is substituted |
| a `lambda_select` strategy with no schema found (see "Strategy schemas" below) | each crawler's session(s) |
| a `strategy_schemas` path that is missing or not JSON | named as such (`strategy_schemas.<name>: declared path '...' is missing or not JSON`), not as "no schema" |
| a proxy pool row with no usable host (see "Proxy pools" below) | the pool, once, when no row has a `hostname`/`host` column; the crawler, when its row's host or port is blank |

Each construct is reported once. A top-level `- teleport: x` is seen by both the validator and
the parser but counted and printed once; an unresolved variable that reaches many places (one
`${query}` in a template used by 300 bots × 4 jobs) is one message, not 1200:
`crawlers[*].queues[*].jobs[*].behaviors.text: unresolved variable '${query}' would reach the
bot as a literal in 1200 place(s) (first: crawlers[0].queues[0].jobs[2].behaviors.text); ...`.
The drop count counts groups.

Two things are deliberately kept as warnings, not drops: an `oxylabs`-type proxy with no
`hostname`/`port` (the runner supplies them, so a drop would ask for what must not be there), and
a plain `http` proxy with no `hostname` (the converter substitutes a `127.0.0.1:8080` placeholder,
which `pmspec upload`'s credential check catches anyway).

**`--lax`** (on `validate` and `compile`; also the MCP `lax` parameter) downgrades every drop to a
warning plus one summary line, `N construct(s) would have been dropped (lax mode)`, and keeps
going. The summary line appears only under `--lax`: a strict run lists the drops as its errors
and prints no count. A lax lock records `provenance.lax: true` (and, once uploaded, `pmspec.lax` on the
experiment) so a lax compile can never be mistaken for a clean one downstream. `pmspec upload`
takes no `--lax` of its own: compile with `--lax` to inspect a lock, but never upload a lax lock
as a real study — the whole point of the flag is that something in it is being read wrong or not
at all.

### Proxy pools

`proxy: {from_pool: true, pool: <data pool name>, match_field: <column>}` draws one row per bot
from a CSV `data_pools` entry, matched on the bot's `agent.location` (or whatever field
`match_field` names) so several bots sharing a location each get a distinct exit IP rather than
one shared proxy. The CSV needs `hostname,port,username,password,type` columns plus the
`match_field` column; `${secret:key.u}`/`${secret:key.p}` placeholders in `username`/`password`
are resolved at upload time like any other proxy credential, not at compile time. Running out of
rows for a location is a compile **error** (never a silent fallback to a shared proxy) — add more
rows or fewer bots at that location. So is a pool whose rows have no `hostname`/`host` column
(`ip`, say), or a row whose host or port is blank: an empty hostname is the runner's "no proxy"
signal, so such a row would quietly compile to a direct connection. The same block works as
`defaults: {proxy: {from_pool: true, ...}}` for every crawler without its own `proxy:`. See
`pmspec/tests/test_proxy_pools.py` for the exact CSV shape.

### Per-crawler assignment (`assign:`)

`assign:` is the proxy-pool rule generalised to any data: one pool **row per crawler**, whose
columns become substitution variables for that crawler alone. It is how "bot 17 replays panelist
510121845" or "bot 17 gets its own outlet list" is expressed — something factors (shared levels)
and sampled parameters (scalars) cannot say.

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

- **Without replacement across crawlers**, per pool: each named assignment takes a row no crawler
  has taken yet. Running out is a compile **error** naming the crawler, the pool, the match and how
  many rows were already taken — never a silent reuse. `proxy: {from_pool: …}` shares the same
  bookkeeping, so one pool feeding both an `assign:` and the proxies can never hand a row out
  twice.
- `match_field: <column>` compares that column with the crawler's `agent.location`;
  `match_value: "<value>"` (a literal, or anything already substituted — `"{city}"`,
  `"{base.comscore_person_id}"`) overrides what it is compared with. With neither, any row
  matches; `match_field` with no `agent.location` and no `match_value` is an error.
- `select: random` (the default) draws among the free matching rows from the lock's seed;
  `select: first` takes them in pool order — with no `match_field` that is "row *i* to bot *i*"
  for a pre-built table. Both need a seed: an unseeded draw is not a randomization record.
- Every column of the drawn row is `{<assignment>.<column>}` (and `{…:fmt}`), substituted
  anywhere in that crawler: its name, preferences, agent and proxy fields, template `with:`
  values, treatment blocks. A column nothing references is not a drop — the row is data. A
  **leftover** `{panelist.…}` (a misspelt column, an unknown pool) is a drop, so it can never
  reach a bot as a literal.
- **Values keep their CSV types.** A numeric column is drawn as a number, so `{panelist.zip:>10}`
  formats an int and `{panelist.region:.2f}` on a text column is a compile error; a blank cell is
  the empty string `""`, not a missing value. This is the opposite of `{base.…}` (above), whose
  values are always strings because a preference is a string on the wire — `{base.ideology:.2f}`
  therefore always fails.
- An assignment **name must be an identifier** — a letter or underscore followed by letters,
  digits or underscores — because it becomes a `{name.column}` placeholder; `pan-elist` would
  match no placeholder pattern and reach the bot verbatim with nothing reported. A name equal to a
  factor, a `design.parameters` name, or `base` is an error too (`{x}` would be ambiguous).
- The validator checks `from_pool` is present and names a declared pool, `match_field` names one
  of that pool's columns, and `select` is `random` or `first`; any other key inside an entry is a
  drop. The lock records the drawn values where they were substituted — it *is* the assignment
  record — and `provenance.pools` already hashes the pool file.
- Runs specs (above) may use `assign:` under the same rules, seeded by the runs lock's own
  seed — with one consequence of the kind: a runs crawler has **no `agent:` block**, so there
  is no `agent.location` for `match_field` to fall back on and it must be paired with an
  explicit `match_value`. Typically a base variable — today's Comscore session drawn per bot
  from a `sessions` pool with `match_value: "{base.comscore_person_id}"`, or
  `match_value: "{base.location}"` for the fleet's own geography.

### Multilogin profile flags

A factor can reach the Multilogin profile itself:
`maps_to: {agent.multilogin_profile.parameters.flags.<flag>: {control: mask, treated: natural}}`,
and an explicit `agent: {multilogin_profile: {parameters: {flags: {…}}}}` on a crawler works the
same way — both are deep-merged over the default profile and land in the API's agent JSON. A
`<flag>` that is not one of the default profile's own flags is a **drop** (Multilogin ignores an
unknown flag silently, so the treatment would simply not exist).

Flag **values are checked too**, against the vocabulary
[`docs/multilogin-profile-flags.md`](multilogin-profile-flags.md) §2 measured against the live
`POST /profile/create` — `canvas_noise` in `{mask, natural, disabled}`, `geolocation_popup` in
`{prompt, allow, block}`, `natural` rather than `real` everywhere, and so on. A value outside its
flag's set is an **error**, not a drop: the API answers 400 `wrong <x> flag` at profile creation,
so the bot never runs and every bot in the experiment fails at setup on the VM — the same failure
shape as `core_version: 142`. `startup_behavior` is pinned to `{recover, custom}` precisely because the API
does not validate it at all: it stores any string verbatim, so a typo there is otherwise silent. A value that still carries a `{placeholder}` is judged after expansion, when
it has a value; note that `<flag>_masking: custom` on fonts/localization/media_devices/navigator/
timezone/webrtc is accepted here but needs the matching `parameters.fingerprint.<block>`, which
pmspec does not check.

Note the drop rule only sees the leaf — a typo one level up (`parameter.flags`, `flag`) is an
unknown key on `multilogin_profile`, which is deep-merged through unexamined, so check the
spelling of the path as well as of the flag.

### Strategy schemas

A `lambda_select` job's behaviours-channel `treatment` (see "Treatment via the decision strategy"
below) is validated against its `strategy`'s JSON Schema, resolved in order: an inline schema or
a path under `strategy_schemas: {<name>: <path or inline schema>}` in the spec (path resolved
against the spec's own directory); each directory listed in `PMSPEC_STRATEGY_SCHEMA_DIRS`
(`os.pathsep`-separated), as `<dir>/<name>.schema.json`; then the two `bot-decision-yt` sibling
locations (`orchestrator/lambda_service/decision/strategies/` and the checked-out
`bot-decision-yt/bot-decision-EN/strategies/`) as a soft fallback. No schema found anywhere is a
drop: the treatment is still forwarded to the decision function unchecked, so there is nothing to
validate against, not nothing wrong.

### Additional sessions

A crawler's `session` is its first queue; `additional_sessions:` (a list of the same shape) adds
more queues to the same bot — for example a "diet" session followed by a later "measurement"
session. It requires a `session` to add to (an error otherwise) and cannot be combined with a
bare `queues:` list on the same crawler. Each entry is checked exactly like `session` itself
(unknown keys, missing start time, …).

### Longitudinal studies: fleet lock + runs locks

A study that gives its bots a fresh session every day for six months cannot be one lock. The
only other way to add sessions after upload is to append *crawlers* with `upload -e`, which
makes crawler ≠ bot, 500 rows a day, and `verify`/`audit` per-day affairs by accident. So there
are two lock **kinds**:

- a **fleet lock** — everything else in this document: crawlers with their configuration, agent,
  proxy and first queues. It is the randomization record, compiled once and uploaded once;
- a **runs lock** (`kind: runs`) — *queues* added to the crawlers that fleet already created,
  matched by name. No `configuration`, `agent` or `proxy`, no `${secret:…}`, and bound to exactly
  one base: `provenance.base_lock_sha256` is the sha256 of the fleet lock **file** it was
  compiled against. A study that re-randomizes is a new fleet and a new experiment.

A static repeating schedule needs none of this — a fleet crawler with N queues at daily offsets
is already one lock (the standing treatment canary is exactly that). Runs locks are for the case
where today's content depends on state pmspec does not have: yesterday's measured ideology, this
week's trending terms, today's panelist session.

#### The runs spec

A spec is a runs spec **iff** its root carries `runs_for:`, a path to a fleet lock relative to
the spec:

```yaml
version: "1.0"
runs_for: design/fleet.lock.yaml     # its sha256 goes into the lock
seed: 20261001                       # a runs spec's own seed (below)
templates: {day: [...]}
crawlers:
  - name: wsp3_017                   # must exist in the base lock
    session: {name: "2026-10-01", start_at: "2026-10-01T10:00:00", workflow: [...]}
    additional_sessions: [...]
    updates: {preferences: {ideology: "0.31"}}     # optional, opt-in (below)

  - from_base: true                  # ... or one block for every base crawler
    name: "{base.name}"
    session:
      name: "{base.name} in {base.location}"
      start_at: {from: "2026-10-01T06:00:00", jitter: 6h}
      workflow:
        - template: day
          with: {pi: "{base.ideology}"}
```

`from_base: true` expands to one crawler block per base crawler, **in base order**, substituting
into every string of the block:

| variable | value |
|---|---|
| `{base.name}` | the crawler's name |
| `{base.location}` | the **agent's** location — a bot's geography, never shadowed by a preference of the same name |
| `{base.<preference>}` | every `configuration.preferences` entry of that crawler |

`{base.x:fmt}` formats as elsewhere, and a `{base.…}` naming nothing is a drop rather than a
literal that reaches the bot. **Base values are strings** — a preference is a string on the wire
— so a numeric format spec such as `{base.ideology:.2f}` is a compile error, not a rounded
number. (Assignment values, below, keep their CSV types and behave the opposite way.)

A runs spec may carry `templates`, `data_pools`, `strategy_schemas`, `assign:` (below) and
`defaults` restricted to session-level keys (`timezone`, `timezone_utc_offset`, `captcha_mode`,
`typing_mode`, `selection_type`, `active`). It refuses, as **errors** rather than drops:

| construct | why |
|---|---|
| root `design:` | the randomization happened in the base; a runs spec adds sessions to a fleet already randomized |
| root `experiment:` | the experiment comes from the base lock |
| a crawler's `agent:`, `proxy:` or `configuration:` | those live in the fleet spec |
| `updates:` outside `preferences`, or a non-scalar value | only `preferences.<name>` may be updated |
| a crawler name the base lock does not have | one error per name, judged on the name that would upload |
| `runs_for:` naming a runs lock, or a file it cannot read | a runs lock is bound to one base; chaining them would make "which fleet was this bot randomized into" unanswerable |

**The seed.** `design:` is refused, so `design.seed` has nowhere to live: a runs spec declares
`seed: N` at its root, or takes `--seed N`, and the flag wins. The base lock's seed is **never**
inherited — each night's draws are that night's — and, as everywhere, a lock is never unseeded.

#### The nightly loop

Ordinary pmspec, with one required flag:

```bash
# in the study repo, beside the committed fleet lock
pmspec compile design/2026-10-01.yaml -o locks/2026-10-01.lock.yaml --seed 20261001
git add locks/2026-10-01.lock.yaml && git commit -m "runs 2026-10-01"
pmspec upload locks/2026-10-01.lock.yaml --mode real -e 181 --dry-run   # the plan, writes nothing
pmspec upload locks/2026-10-01.lock.yaml --mode real -e 181             # apply, then verify
pmspec audit 181 locks/2026-10-01.lock.yaml --hours 24                  # next morning
```

`-e` is **required** for a runs lock (`a runs lock appends to an experiment: pass -e <id>`), and
so is `experiment_id` in MCP `upload_experiment`. Compile in the study repo: that is where the
provenance the lock records (`git_commit`, `spec_path`, pool hashes) is read from and where
`runs_for:` resolves. Over MCP, `compile_experiment_spec` takes the fleet lock's **text** in
`files=` under the bare filename `runs_for:` names (`{"fleet.lock.yaml": "<lock text>"}`) and
reports `kind` and `base_lock_sha256` back.

Before anything is written, the upload asks the live experiment — stopping at the first refusal:

1. the experiment's **first** upload must carry a `pmspec.lock_sha256` equal to this lock's
   `base_lock_sha256` (else: *created from lock X, this runs lock is based on Y*) and the same
   `pmspec.mode`;
2. every lock crawler name must match exactly one live crawler; missing or duplicated is an error
   naming it;
3. secrets, anchor (`--allow-past`) and the dirty-commit rules are a fleet upload's.

Then, per crawler in lock order, one `POST crawlers/{id}/upload` carrying only the queues not
already live — **a queue whose (wall-clock start, name) already exists on that crawler is
skipped**, so re-running the same lock after a half-finished upload adds nothing twice — then
that crawler's `updates:`, and finally the provenance preferences as "upload N", which for a runs
upload add `pmspec.kind = runs`, `pmspec.base_lock_sha256` and, when updates were applied,
`pmspec.updates` (a JSON blob of `{<crawler>: {"preferences.<name>": [old, new]}}`). A failure
leaves provenance untouched and prints the last crawler completed. The dry run performs every one
of those checks and reports crawlers, queues to append, queues already live and updates.

#### `updates:` — changing a preference mid-study

`updates: {preferences: {<name>: <value>}}` is the only mutation a runs lock may make, opt-in per
field. Params, agent and proxy fields are refused at validation: changing those mid-study is a
different experiment. The old value is read first and recorded beside the new one, so the fleet
lock stays the randomization record and provenance says how the live state departed from it.

This needs an API that syncs preferences on `PUT configurations/{id}`. The upload probes **once**,
before any write, and refuses outright when it does not:

```
this API cannot update preferences (PUT configurations/{id} strips them);
deploy primemover_api with preferences sync, or drop `updates:`
```

A lock with no `updates:` never probes and never needs the newer API.

#### What verify, status, audit and diff say about a runs lock

- **`verify`** compares only what this lock uploaded: per crawler, the live queues whose
  (wall-clock start, name) it asked for, plus the live value of exactly the preference names its
  `updates:` set. Queues another upload appended to the same bot are not differences; a lock queue
  with no live match surfaces as `crawlers.<bot>.queues.length`. Two sessions on the same day share
  a name, so the start is what tells them apart. The bot's params, proxy and location are the fleet
  lock's claim: neither compared nor even fetched. `--deep` compares jobs and behaviours as usual,
  and binding checks this runs lock's own sha against its own "upload N" preference. `pmspec upload`
  runs the same verify after applying, and its verdict decides the exit code.
- **`status`** labels each upload with its kind and base — `kind=runs base=<sha12> updates=<n>` —
  and ends with a line like `uploads: 1 fleet + 12 runs, last 2026-10-12T05:02:11`.
- **`audit`** takes a runs lock as it takes a fleet lock, matching by (crawler name, queue name,
  job ordinal); the bot's other sessions are not in the lock and are neither reported nor fetched.
  One difference: a runs crawler has no `configuration`, so the params fallback has nothing to
  read — a `lambdasingleselectjob` **without** a `strategy` behaviour is not audited at all, rather
  than audited against an assumed control arm — and each finding's arm comes from the job's own
  treatment (`pi=1,kappa=1`, or `reliability/lean` when the job names both).
- **`diff`** between two runs locks compares crawler names, queues and `updates`, and prints
  `base: <sha12> vs <sha12>` when they were compiled against different fleets. A runs lock against
  a fleet lock is refused.

### Generated JSON Schema

`pmspec/schema/experiment-spec.schema.json` is generated, not hand-written — run
`make schema` (`python -m pmspec.spec.schema_gen`) after changing any `_KEYS_*`,
`_OPTION_KEYS` or `_KNOWN_PARAMS` table in `spec/validator.py`, or the job registry.
`pmspec/tests/test_schema_gen.py::test_checked_in_schema_is_generated` fails if the
checked-in file and the generator's output disagree, so a table changed without regenerating is
caught in CI rather than by the next person who edits the schema by hand and re-introduces drift.
Every structural object (`experiment`, `defaults`, `design`, a crawler, its
`configuration`/`proxy`/`agent`/`session`) sets `additionalProperties: false` from the same key
set `_validate_unknown_keys` checks, so the two can no longer disagree; a workflow item does the
same from `_validate_workflow_item`'s job-type list. A per-job shorthand's options (e.g.
`click: {selector, task, flag, ...}`) and `configuration.params` stay `additionalProperties: true`,
since an unknown key there is the *validator's* drop to report (downgradable under `--lax`), not a
hard schema rejection. A factor's own keys (`levels`/`maps_to`, from `_KEYS_FACTOR`) get
`additionalProperties: false` in the schema and the same treatment at the Python level:
`_validate_design` reports any other key as a drop (`design.factors.<f>.<key>: unknown key; the
design expander never reads it`), since a misspelt `maps_to` is a factor with no mapping at all
and every cell gets the same params.

## Installation

```bash
# Install Python dependencies
pip install -r requirements.txt
```

## Quick Start

### 1. Create a Specification

Create a file `my-study.yaml`:

```yaml
version: "1.0"

experiment:
  name: "Google Search Personalization Study"
  description: "Investigating search result personalization"

defaults:
  location: US-IL-CHICAGO
  browser: mlx
  timezone: America/Chicago

crawlers:
  - name: "search_bot_01"
    description: "Search crawler"

    configuration:
      params:
        pi: 0
        alpha: 1

    proxy:
      hostname: "proxy.example.com"
      port: 8080
      type: http

    agent:
      identification: mlx
      location: US-IL-CHICAGO

    session:
      name: "Search Session"
      start: "2025-02-01T10:00:00-06:00"

      workflow:
        - visit: https://google.com
        - wait: 3s
        - type:
            text: "climate change"
            selector: "//input[@name='q']"
            submit: true
        - wait: 5s
        - scroll: 30s
```

### 2. Validate

```bash
python3 -m pmspec.cli validate my-study.yaml
```

### 3. Generate JSON

```bash
python3 -m pmspec.cli generate my-study.yaml -o output.json
```

### 4. Upload to API

```bash
# Set credentials
export PM_API_BASE_URL=http://localhost:8090/api/v1
export PM_API_EMAIL=admin@primemover.example
export PM_API_PASSWORD=password

# Dry run first
python3 -m pmspec.cli upload my-study.yaml --dry-run

# Upload
python3 -m pmspec.cli upload my-study.yaml
```

## Use Case: YouTube Recommendation Study

This example demonstrates a factorial design studying how political orientation and content type affect YouTube recommendations.

### Research Design

- **Factor 1**: Political Orientation (3 levels: left, center, right)
- **Factor 2**: Starting Content (2 levels: news, entertainment)
- **Replications**: 5 per condition
- **Total Crawlers**: 3 × 2 × 5 = 30

### YAML Specification

```yaml
version: "1.0"

experiment:
  name: "YouTube Recommendation Pathways Study"
  description: >
    Investigating how political orientation and starting content type
    affect YouTube recommendation algorithm trajectories.

defaults:
  location: US-IL-CHICAGO
  browser: mlx
  timezone: America/Chicago
  captcha_mode: always

# Reusable workflow templates
templates:
  cookie_consent:
    - try_click: "//button[contains(text(),'Accept all')]"
    - wait: 2s

  youtube_search:
    - type:
        text: "${search_term}"
        selector: "//*[@id='search']"
        submit: true
    - wait: 5s

  watch_and_recommend:
    - wait: 10s
    - lambda_select:
        task: youtube_video_select
        function: bot-decision-yt
        link_type: FUNCTION_NAME
    - wait: 10s

# Factorial Design Definition
#
# ⚠️ Treatment params are 0/1 SWITCHES, not intensities.
# `configuration.params.pi`, `.psi`, `.kappa` and `.tau` are read by the decision
# function as `int(round(x)) == 1`, so ONLY 0 or 1 are meaningful. A fractional
# value such as 0.5 rounds to 0 (Python rounds half-to-even) and *silently*
# disables the arm it looks like it configures -- there is no error. A factor that
# toggles such a param must therefore have 0/1 levels; a multi-level factor cannot
# map to a single switch (map it to preferences or search terms instead). The
# validator now rejects non-0/1 values for these four params.
design:
  type: factorial

  factors:
    # A 2-level treatment factor toggling a 0/1 switch param (the correct shape).
    mainstream_media_aversion:
      levels: [control, treated]
      maps_to:
        configuration.params.pi:
          control: 0
          treated: 1

    # A multi-level factor drives non-switch levers (preferences, search terms),
    # NOT a 0/1 param.
    political_orientation:
      levels: [left, center, right]
      maps_to:
        # Map to configuration preferences
        configuration.preferences.preference_political_videos:
          left: "1"
          center: "0"
          right: "-1"
        # Map to search terms (for variable substitution)
        initial_search:
          left: "progressive politics news"
          center: "today breaking news"
          right: "conservative news today"

    starting_content:
      levels: [news, entertainment]
      maps_to:
        starting_channel:
          news: "@WashingtonPost"
          entertainment: "@MrBeast"
        configuration.preferences.preference_entertainment:
          news: "0"
          entertainment: "1"

  replications: 5

# Crawler Template (expanded by design)
crawlers:
  - from_design: true
    name: "yt_{mainstream_media_aversion}_{political_orientation}_{starting_content}_{replica:02d}"
    description: "YouTube bot studying recommendation pathways"
    active: true
    testing: false

    configuration:
      name: "Config {political_orientation}_{starting_content}"
      params:
        # pi is set by design.factors.political_orientation.maps_to
        psi: 0
        alpha: 1
        tau: 0
        kappa: 0
        beta: 0
      preferences:
        - name: location
          value: "US-IL-CHICAGO"
        - name: cookie_pref
          value: '{"accept_all": true}'   # consumed by the runner: accept_all true -> "Accept all", false -> "Reject all"
        - name: usage_type
          value: "only_search"
        - name: order
          value: "{replica}"

    proxy:
      name: "Proxy {political_orientation}_{starting_content}"
      hostname: "proxy.example.com"
      port: 8080
      username: "user"
      password: "pass"
      type: http

    agent:
      name: "Agent {political_orientation}_{starting_content}"
      identification: mlx
      location: US-IL-CHICAGO

    session:
      name: "Daily Session"
      start: "2025-02-01T10:00:00-06:00"

      workflow:
        # Phase 1: Navigate to YouTube
        - visit: https://youtube.com
        - wait: 5s
        - template: cookie_consent

        # Phase 2: Initial search based on political orientation
        - template: youtube_search
          with:
            search_term: "{initial_search}"

        # Phase 3: Visit starting channel based on content type
        - visit: "https://youtube.com/{starting_channel}/videos"
        - wait: 5s

        # Phase 4: Click first video
        - try_click: '(//*[@id="video-title"])[1]'
        - wait: 10s

        # Phase 5: Follow recommendations (10 iterations)
        - repeat: 10
          template: watch_and_recommend

        # Phase 6: Final state capture
        - visit: https://youtube.com/feed/trending
        - scroll: 30s
        - wait: 5s
```

### Running the Study

```bash
# Preview the design
python3 -m pmspec.cli preview youtube-study.yaml

# Output:
# Experiment: YouTube Recommendation Pathways Study
#
# Factorial Design
# Design Type: factorial
# Factors:
#   political_orientation: ['left', 'center', 'right']
#     -> maps to: configuration.preferences.preference_political_videos, ...
#   starting_content: ['news', 'entertainment']
#     -> maps to: starting_channel, configuration.preferences.preference_entertainment
#
# Replications: 5
# political_orientation(3) × starting_content(2) × 5 replications = 30 crawlers

# Validate
python3 -m pmspec.cli validate youtube-study.yaml

# Generate JSON
python3 -m pmspec.cli generate youtube-study.yaml -o youtube-study.json

# Upload (dry run)
python3 -m pmspec.cli upload youtube-study.yaml --dry-run

# Upload to API
python3 -m pmspec.cli upload youtube-study.yaml
```

### Generated Crawlers

The design expands to 30 crawlers with names like:
- `yt_left_news_01` through `yt_left_news_05`
- `yt_left_entertainment_01` through `yt_left_entertainment_05`
- `yt_center_news_01` through `yt_center_news_05`
- ... and so on

Each crawler has:
- Correct `pi` parameter based on political orientation
- Correct search term substituted in the workflow
- Correct starting channel URL
- Unique name with replica number

---

## External Data Integration (Phase 4)

The experiment designer supports loading external data files, parameter distributions, and data pools with scoring expressions.

### External File References (`!file` tag)

Load data from CSV, JSON, or YAML files directly in your specification:

```yaml
crawlers:
  - name: "Bot"
    configuration:
      params:
        # Load all search terms from CSV
        search_terms: !file ./data/searchterms.csv

        # Load with selection options
        media_outlets: !file
          path: ./data/outlets.csv
          select: random
          count: 10
          filter:
            region: US
```

**Selection strategies:**
- `all` - Load all records (default)
- `random` - Random selection
- `first` - First N records
- `filter` - Filter by field values

### Parameter Distributions

Sample parameter values from probability distributions:

```yaml
design:
  seed: 42  # For reproducibility

  parameters:
    # Uniform distribution
    pi:
      distribution: uniform
      min: -1
      max: 1

    # Discrete distribution with proportions
    kappa:
      distribution: discrete
      values:
        0: 0.25   # 25% probability
        1: 0.25   # 25% probability
        2: 0.50   # 50% probability

    # Normal (Gaussian) distribution
    alpha:
      distribution: normal
      mean: 0.35
      std: 0.1

    # Gumbel distribution (for discrete choice models)
    epsilon:
      distribution: gumbel
      mu: -0.4557735
      beta: 0.0793006

    # Truncated normal (bounded)
    beta:
      distribution: truncated_normal
      mean: 0.25
      std: 0.1
      min: 0
      max: 0.5

    # Beta distribution
    engagement:
      distribution: beta
      alpha: 2
      beta: 5

    # Fixed value
    tau:
      value: 1.0
```

**Available distributions:**
| Distribution | Parameters | Use Case |
|--------------|------------|----------|
| `uniform` | `min`, `max` | Political orientation, random values |
| `discrete` | `values` (dict) | Categorical with proportions |
| `normal` | `mean`, `std` | Continuous with center tendency |
| `gumbel` | `mu`, `beta` | Error terms in choice models |
| `truncated_normal` | `mean`, `std`, `min`, `max` | Bounded continuous |
| `beta` | `alpha`, `beta` | Probabilities, proportions |
| `fixed` / `value` | `value` | Constants |

**Where a sampled value goes.** Every sampled parameter is written to the
crawler's `configuration.params.<name>`. The API keeps only the standard params
(`pi`, `psi`, `kappa`, `tau`, `alpha`, `beta`, `active`, `search_terms`,
`media_outlet_urls`); any other name in `configuration.params` is dropped on
upload. So a non-standard sampled parameter (`epsilon`, `engagement` above) is
only useful as a `{name}` / `{name:fmt}` substitution variable -- in a
preference value, a description, a job text -- and `pmspec compile` refuses a
spec that samples one and references it nowhere (`design.parameters.<name>:
sampled value lands in configuration.params, which the API drops`). When it is
referenced, the converter strips it from `configuration.params` so the emitted
JSON carries the value only where it was substituted.

### Proportional Factor Levels (Stratified Sampling)

Create stratified designs with specific proportions:

```yaml
design:
  factors:
    usage_type:
      levels:
        - id: only_visits
          proportion: 0.25      # 25% of crawlers
        - id: only_searches
          proportion: 0.25      # 25% of crawlers
        - id: searches_and_visits
          proportion: 0.50      # 50% of crawlers

  total_crawlers: 100  # Generate exactly 100 crawlers
```

This creates:
- 25 crawlers with `usage_type: only_visits`
- 25 crawlers with `usage_type: only_searches`
- 50 crawlers with `usage_type: searches_and_visits`

The counts are exact per factor. Each proportional factor's level sequence is shuffled with
the design seed, independently of every other factor, so two proportional factors cross at
random (a stratified random assignment: exact marginals, randomised joint distribution).
Until 2026-09-16 the expander handed every bot the *first* level with a count remaining of
each proportional factor, so two such factors were perfectly confounded — bots 1..k carried
level A of both — with no warning; a lock compiled before that date from a design with two
or more proportional factors under `total_crawlers` should be recompiled and the assignment
checked. Factors without proportions are cycled through their full cross, as before.

### Data Pools with Scoring

Define data pools and select records based on scoring expressions:

```yaml
data_pools:
  outlets:
    source: ./data/outlets_pool.csv
  search_terms:
    source: ./data/searchterms_pool.csv

crawlers:
  - from_design: true
    configuration:
      params:
        # Select top 10 outlets closest to crawler's pi
        media_outlet_urls:
          from_pool: outlets
          select: top
          count: 10
          score: "{alpha} - {tau} * pow(abs({crawler.pi} - {item.pi}), 2)"

        # Select top 5 search terms closest to crawler's pi
        search_terms:
          from_pool: search_terms
          select: top
          count: 5
          score: "{alpha} - {tau} * pow(abs({crawler.pi} - {item.pi}), 2)"
```

**Pool selection strategies:**
- `all` - All records
- `random` - Random N records
- `first` - First N records
- `top` - Top N by score expression

**Scoring expression variables:**
- `{crawler.pi}`, `{crawler.alpha}`, etc. - Crawler parameters
- `{item.pi}`, `{item.domain}`, etc. - Pool record fields
- `{alpha}`, `{tau}`, etc. - Design parameters

**Safe math functions:**
- `abs()`, `pow()`, `sqrt()`, `min()`, `max()`
- `log()`, `log10()`, `exp()`
- `ceil()`, `floor()`, `round()`

### Variable Substitution (Extended)

With Phase 4, additional variables are available:

| Variable | Description |
|----------|-------------|
| `{factor_name}` | Factor level ID |
| `{factor_name:value}` | Factor level value |
| `{param_name}` | Mapped parameter or sampled value |
| `{pi}`, `{kappa}`, `{alpha}` | Sampled parameter values |
| `{replica}` | Replication number |
| `{replica:02d}` | Zero-padded replica |
| `{cell_index}` | Cell index in design |
| `{cell_index:03d}` | Zero-padded cell index |

### Complete Phase 4 Example

```yaml
version: "1.0"

experiment:
  name: "Web Search Polarization Study"

data_pools:
  outlets:
    source: ./data/outlets_pool.csv
  terms:
    source: ./data/searchterms_pool.csv

design:
  seed: 42

  parameters:
    pi:
      distribution: uniform
      min: -1
      max: 1
    kappa:
      distribution: discrete
      values:
        0: 0.25
        1: 0.25
        2: 0.50
    alpha:
      distribution: normal
      mean: 0.35
      std: 0.1
    tau:
      value: 1.0

  factors:
    usage_type:
      levels:
        - id: only_visits
          proportion: 0.25
        - id: only_searches
          proportion: 0.25
        - id: searches_and_visits
          proportion: 0.50

  total_crawlers: 100

crawlers:
  - from_design: true
    name: "ws_{usage_type}_{cell_index:03d}"
    description: "PI={pi:.3f}, Kappa={kappa}"

    configuration:
      params:
        search_terms: !file
          path: ./data/searchterms_pool.csv
          select: first
          count: 10

    # ... rest of crawler config
```

---

## CLI Commands

| Command | Description |
|---------|-------------|
| `validate <file> [--seed N] [--strict] [--lax]` | Validate a spec or a lock |
| `compile <file> [-o out] --seed N [--anchor] [--generator] [--lax]` | Compile a spec into a committable lock |
| `upload <lock> --mode real\|test [-e ID] [--dry-run] [--record] [--allow-dirty] [--allow-past] [--deep] [--secrets] [--seed N]` | Upload a lock (or compile a spec, then upload) and verify. `-e` appends to an existing experiment, and is **required** for a runs lock |
| `verify <experiment_id> <lock> [--deep]` | Check a live experiment is bound to, and identical to, a lock |
| `status <experiment_id> [lock] [--hours N] [--deep]` | Provenance + health of a live experiment; with a lock, also verify |
| `diff <lock_a> <lock_b>` | Diff two locks: provenance, anchor, crawler-level structural differences |
| `audit <experiment_id> <lock> [--hours N] [--sample K] [--strict]` | Did each finished lambda job apply the treatment the lock says it should |
| `cleanup [--older-than 7d] [--dry-run] [--yes]` | Delete pmspec TEST experiments only (`test-` name **and** `pmspec.mode=test`) |
| `secrets check <lock> [--secrets]` | List the secrets a lock references and which are missing |
| `secrets init <lock> [--secrets] [--force]` | Write (or, with --force, merge) a TOML skeleton for a lock's secrets |
| `generate <file> [-o output.json] [--seed N] [--resolve-secrets]` | Convert a spec or lock to API JSON (GUI upload only; carries no provenance) |
| `preview <file> [--seed N]` | Preview factorial design; given a lock, its counts, anchor and provenance |
| `summary <file>... [-e ID] [--compare] [-f text\|json\|html] [--seed N]` | Experiment summary from a spec, a lock, or a live experiment; a spec `validate` would refuse is refused here too -- same three components, strict -- with the errors, no counts, and exit 1 |
| `schema jobs` | List all job types |
| `schema job <type>` | Show job type details |
| `init [-t template]` | Create from template |

**Options:**
- `--seed N` / `-s N` - Random seed for reproducible randomization (distributions, file selections)

Every command that takes a spec or a lock takes a **runs** spec or lock too (see "Longitudinal
studies" above); only `upload` changes shape, in requiring `-e <experiment id>` for one.

## Laravel Commands

```bash
# Validate
php artisan experiment:spec validate path/to/spec.yaml

# Generate JSON
php artisan experiment:spec generate spec.yaml --output=output.json

# Preview design
php artisan experiment:spec preview spec.yaml

# Upload
php artisan experiment:spec upload spec.yaml --dry-run
php artisan experiment:spec upload spec.yaml --experiment-id=5
```

## HTTP API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/experiments/spec/validate` | POST | Validate YAML |
| `/api/v1/experiments/spec/preview` | POST | Preview design |
| `/api/v1/experiments/spec/convert` | POST | Convert to JSON |
| `/api/v1/experiments/spec/upload` | POST | Upload experiment |
| `/api/v1/experiments/spec/job-types` | GET | List job types |
| `/api/v1/experiments/spec/template/{type}` | GET | Get template |

### Example API Call

```bash
curl -X POST http://localhost:8080/api/v1/experiments/spec/validate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "yaml": "experiment:\n  name: Test\ncrawlers:\n  - name: Bot",
    "strict": false
  }'
```

## Relative Timestamps

Session start times support relative timestamps for easier experiment scheduling:

| Format | Description | Example |
|--------|-------------|---------|
| `now` | Current time when spec is converted | `start: now` |
| `+Ns` | N seconds from now | `start: +30s` |
| `+Nm` | N minutes from now | `start: +5m` |
| `+Nh` | N hours from now | `start: +2h` |
| `+Nd` | N days from now | `start: +1d` |
| ISO 8601 | Fixed timestamp | `start: "2025-02-01T10:00:00-06:00"` |
| `{from, jitter}` | Drawn from the seed: `from` + U(0, `jitter`) — see "Seeded timing" | `start_at: {from: "+1h", jitter: 6h}` |

**Example: Staggered bot starts**

```yaml
crawlers:
  - name: "bot_01"
    session:
      start: now  # Starts immediately

  - name: "bot_02"
    session:
      start: +2m  # Starts 2 minutes after upload

  - name: "bot_03"
    session:
      start: +5m  # Starts 5 minutes after upload
```

Relative timestamps are resolved to ISO 8601 format with local timezone offset during JSON conversion.

---

## Workflow Shorthand Reference

| Shorthand | Example | Description |
|-----------|---------|-------------|
| `visit` | `visit: https://example.com` | Navigate to URL |
| `wait` | `wait: 10s` | Wait (supports s/m/h); `{min, max}` draws one from the seed |
| `click` | `click: "//button"` | Click element |
| `try_click` | `try_click: "//button"` | Click, continue on fail |
| `type` | `type: {text: "...", selector: "..."}` | Enter text |
| `scroll` | `scroll: 30s` | Scroll by duration |
| `lambda_select` | `lambda_select: {task: "...", function: "..."}` | AWS Lambda selection |
| `template` | `template: my_template` | Use template (with `with: {var: value}`) |
| `repeat` | `repeat: 10` | Repeat N times (with `template:` or `jobs: [...]`) |

A `template:`/`repeat:` item carries only `template`, `repeat`, `with` and `jobs`. There is no
`condition:`, `weight:` or `description:` on such an item; any other key is a drop (strict
contract above), because the parser would ignore it and every bot would run every step.

### Per-bot template draws

A `with:` value can draw a distinct row per bot from a data pool, instead of every bot substituting
the same literal:

```yaml
- template: diet_search_google
  with:
    query: {from_pool: queries, field: term, select: random}
    reliability: "{reliability}"          # a literal `{var}` substitution still works alongside a draw
```

- `from_pool` and `field` are required (names restricted to `[A-Za-z0-9_.-]+`); `field` names the
  pool column substituted in. `select` is `random` (default) or `first`; `count` is not accepted —
  one value per `with:` key, since N distinct values are N template calls.
- **Without replacement per crawler**: every draw from the same pool by the same crawler takes a
  row that crawler has not already drawn (across the whole session, `repeat:` included); running out
  is a compile error naming the crawler, pool and how many draws it had already made. Draws are
  seeded from the lock's seed and the crawler's index, so two bots draw independently but the whole
  lock stays byte-deterministic for a given seed. Because the key is the crawler *index*, adding a
  crawler or a replication in the middle of the list reshuffles every later bot's draws — treat a
  recompile after such an edit as a new draw, not a preserved one.
- `select` outside `random`/`first` is refused on every path (compile, `orchestrator experiment
  generate`, the MCP), not shipped to the bot as an unresolvable token.
- A `repeat:`+`template:` item draws again on every repetition, still without replacement within
  that crawler.

### Seeded timing

Two places where a study wants a *drawn* number rather than a fixed one, so the generator does not
have to draw it itself and keep the seed outside the lock.

**Wait ranges.** `wait: {min: 30s, max: 60s}` beside the existing `wait: 45s` / `wait: 45`:

```yaml
workflow:
  - visit: https://example.com
  - wait: {min: 30s, max: 60s}      # both required, durations, min <= max (equal is fine)
```

The parser emits a deferred token (templates expand before the design does), and the converter
resolves it per **(crawler i, queue j, job k)** with `random.Random(f"{seed}:wait:{i}:{j}:{k}")`,
uniform over whole seconds, inclusive of both bounds. The **lock stores the number**, so
`verify`, `diff` and the runner see an ordinary `wait`. A surviving token is a drop: a wait range
can never reach a bot as `%%pmspec-wait:30:60%%`.

**Start jitter.** `start_at: {from: "+1h", jitter: "6h"}` — `from` is anything `start_at` accepts
today (`now`, a relative offset, an ISO timestamp), `jitter` a duration. The session starts at
`from + U(0, jitter)`, drawn per **(crawler i, session j)** with
`random.Random(f"{seed}:start:{i}:{j}")` and stored as a concrete `start_offset` under the usual
anchor, so verify and audit need nothing new. The same form works in `additional_sessions` (each
session has its own index) and in runs specs.

Both streams are their own namespaces — independent of each other, of the design expander's
stream and of pool draws — so the whole lock stays byte-deterministic for a given seed, and two
bots jitter independently. The indices are positions in the crawler list, so inserting a crawler
or a replication in the middle reshuffles every later bot's draws: treat a recompile after such an
edit as a new draw, not a preserved one.

Both need a seed, and both refuse rather than silently falling back:

```
wait range needs a seed: use pmspec compile, or set design.seed
start jitter needs a seed: use pmspec compile, or set design.seed
```

`pmspec generate` (and MCP `convert_spec_to_json`) records no seed at all — it writes API JSON,
not a lock — so a jitter there is refused outright (`start_at jitter needs a seed: use pmspec
compile`) and the JSON falls back to `from`; a wait range leaves its token in place and is
reported as a drop. Anything drawn belongs in a lock, which is the only artifact that can
reproduce it.

## Treatment via the decision strategy (behaviours channel)

Treatment can be carried on the `lambda_select` job itself, rather than only in
`configuration.params`. Set a **`strategy`** (which decision logic runs) and a
**`treatment`** block (its config); both become job behaviours the decision
function reads:

```yaml
- lambda_select:
    task: youtube_video_select
    function: bot-decision-yt
    strategy: youtube_v1        # activates the behaviours channel
    treatment:
      pi: 1                     # strategy-specific config (0/1 switch here)
      tau: 0
```

- The channel activates **only** when `strategy` is present; a `treatment` block
  without a `strategy` is ignored by the decision function (the validator warns).
- The `treatment` values are validated against that strategy's schema shipped in
  `bot-decision-yt` (`strategies/<strategy>.schema.json`) when it can be found;
  e.g. `youtube_v1` still enforces the 0/1 switch rule on `pi/psi/kappa/tau`.
- Job-control keys (`task`, `link_type`, `function`) always win over `treatment`.
- Legacy specs that set treatment in `configuration.params` are unaffected — no
  `strategy`, so the decision function keeps reading `selectionConfiguration`.

## Variable Substitution

In crawler templates with `from_design: true`:

| Variable | Description |
|----------|-------------|
| `{factor_name}` | Level ID (e.g., "left") |
| `{factor_name:value}` | Level value |
| `{param_name}` | Mapped parameter from `maps_to` |
| `{replica}` | Replication number |
| `{replica:02d}` | Zero-padded replica |

## Best Practices

1. **Start with templates**: Use `pmspec init -t youtube` to get started
2. **Validate early**: Run `validate` before `generate`
3. **Use dry-run**: Always `upload --dry-run` first
4. **Use templates**: Define reusable workflow patterns
5. **Name systematically**: Use `{factor}_{replica:02d}` for clear naming

## Troubleshooting

**YAML parse error**: Check indentation, use spaces not tabs

**Variable not substituted**: Ensure variable is defined in `maps_to` or is a factor name

**Template not found**: Check template name matches exactly

**Upload fails**: Verify API credentials and endpoint URL

## Files

```
pmspec/
├── cli.py                  # CLI tool (pmspec)
├── README.md               # Quick reference
├── spec/
│   ├── parser.py           # YAML parsing, !file tag support
│   ├── converter.py        # JSON conversion
│   ├── design_expander.py  # Factorial/stratified expansion
│   ├── job_registry.py     # Job schemas
│   ├── validator.py        # Validation
│   ├── time_utils.py       # Relative timestamp parsing (now, +5m, +1h)
│   ├── file_loader.py      # External file loading (Phase 4)
│   ├── distributions.py    # Parameter distributions (Phase 4)
│   ├── expression.py       # Safe expression evaluator (Phase 4)
│   ├── data_pools.py       # Data pool management (Phase 4)
│   └── schema_gen.py       # Generates schema/experiment-spec.schema.json (Phase 5)
├── schema/
│   └── experiment-spec.schema.json  # Generated -- do not hand-edit, run `make schema`
├── examples/
│   ├── data/
│   │   ├── outlets_pool.csv      # Example media outlets
│   │   └── searchterms_pool.csv  # Example search terms
│   ├── simple-distribution-study.yaml
│   ├── trump-search-mini-study.yaml  # Relative timestamps example
│   └── websearch-study.yaml      # Full Phase 4 example
└── tests/
    ├── test_spec.py              # Core tests (24 tests)
    └── test_external_data.py     # Phase 4 tests (38 tests)

docs/
└── pmspec.md  # This documentation
```
