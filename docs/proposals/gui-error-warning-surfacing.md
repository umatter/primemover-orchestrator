# Plan: make the error/warning overhaul visible in the GUI

**Status:** explored 2026-09-07; **action plan below, not yet built.** Follows
the 2026-08-27/28 error/warning overhaul (orchestrator #27/#30, runner #62/#68/#74,
api #56, gui #48/#51, bot-decision-yt #5). Contract: `docs/integration-contract.md`
§3.4 / §7.3, executable copy `orchestrator/status.py`.

## Problem

The overhaul made the runner tell the truth: jobs and runs now report `warning`
when they finish degraded, and `status_message` carries a real diagnostic
instead of one of seven fixed strings. The API stores all of it. But a
researcher looking at the GUI still cannot answer the three questions the
overhaul exists for:

1. **Which of my runs should I look at?** There is no experiment-level health,
   no status filter, and a run that ended `warning` at the queue level shows as
   a blue "Running" pill on the workspace forever.
2. **What went wrong?** `status_message` is rendered truncated to a table cell,
   recoverable only by hovering.
3. **Did the treatment actually apply?** The decision function's verdict
   (`degraded`, `treatment_lost`, which filter failed) exists only in the runner
   log and a MinIO object. The API has no column for it, so the GUI cannot show
   it at all.

## What is already true (do not redo)

- **Deploy is current.** ex63 serves `ghcr.io/umatter/primemover_gui:sha-593a027`
  = head of GUI `main`. The runner jar rebuilt 2026-09-03 writes the new
  messages; the DB holds 547 `warning` jobs and 73 `warning` queues. The §0
  "nothing is deployed" text in the contract and CLAUDE.md is stale.
- **The leaf is right.** `app-status` (`src/app/queue/status/`) knows all five
  codes, all legacy aliases (`failed`, `skpped`, …), renders unknown as a grey
  "Unknown" pill, and takes `statusMessage`. Its alias table mirrors
  `status.py` and `Queue.php`.
- **The API's rollups exist.** `GET /queues` carries `jobs_count`,
  `jobs_succeeded_count`, `jobs_warning_count`, `jobs_failed_count`,
  `jobs_skipped_count`; every crawler carries `latest_queues_count /
  succeeded / failed / degraded` over a rolling 24h window.
- **Tests exist for the pieces that were touched**: bot-detail `runState()`,
  workspace tiles (GUI); alias parity, rollups, N+1 guards (API).

Everything above the leaf, and everything about *why*, is the gap.

## Findings that shape the plan

The full inventories are in the session log; the facts that drive the design:

| # | Finding | Where |
|---|---|---|
| F1 | A **queue-level `warning`** with no `warning` job is counted by no crawler rollup: `latest_queues_succeeded/failed` exclude it and `latest_queues_degraded` is derived from *jobs*. Workspace `botState()` then returns `running`. | api `Crawler.php:300-337`; gui `experiment-workspace.component.ts:130-139` |
| F2 | The API and the queue model still **deny queue `warning` exists** (OpenAPI enum, docblock) — runner has sent it since 08-28. | api `RunnerQueueController.php:51-52`, `Queue.php:57-58` |
| F3 | bot-detail's hand-rolled `runState()` maps an **unknown code to `scheduled`**, the one thing the contract forbids. | gui `bot-detail.component.ts:94-106` |
| F4 | **No `filter[status_code]`** on queues/jobs/crawlers; Spatie's default turns an unknown filter into HTTP 400. `status_code` is sortable but not filterable. | api `Queue.php:230-235`, `Job.php:118-123` |
| F5 | **No experiment-level status** anywhere: no column, no aggregate endpoint, and experiment cards show contact + updated_at only. | api `ExperimentRepository.php`; gui `experiment/index/experiment.component.html:108-141` |
| F6 | Job rollup and nested jobs are on **disjoint endpoints**: `/queues` has counts, no jobs; `/queues/{id}` has jobs, no counts. | api `Queue.php:227`, `QueueController.php:90-95` |
| F7 | `status_message` is **always truncated** (`truncate` + `max-w-xs`), full text only in `title`/`aria-label`. Messages run to 1800 chars (runner cap). | gui `status.component.html:49-55`, `bot-detail.component.html:77-84` |
| F8 | The decision verdict (`degraded`, `treatment_lost`, `selection_mode`, `filters`, `llm`) is **persisted nowhere in the API** — only the runner log, the MinIO `lambda-jobdata-*` object, and as prose inside `status_message`. | runner `LambdaSelectJobHandler.java:153-285`; api grep: none |
| F9 | `GET /v1/file/{report}` calls a repository method that **does not exist** (500). Only the whole-job ZIP download works. | api `ReportController.php:217-222` vs `Report/Repository/ReportRepository.php` |
| F10 | `queues.status_code` has **no index**; every crawler rollup subselect scans it. | api migrations |
| F11 | The shared pill's spec is a **non-compiling stub outside the curated list**; `normalizeStatus` has zero executing tests. | gui `tsconfig.spec.json:10-18` |
| F12 | **Nothing refreshes.** No polling anywhere; states change only on navigation. | gui grep `interval\|timer` |
| F13 | `captcha_solved` is **never written** by the runner (no call site); `captcha_found` is a substring match on the page source. Neither is rendered. | runner `JobService.java:212-215`; gui `Job.ts:23-24` |
| F14 | `/queues` leads with `processed/processing/reviewed` Yes/No columns; the typed `jobs_*_count` are never rendered. | gui `queue.component.html:85-95` |

## Design rules (from the contract; the plan must not violate them)

- **`is_bad` = `failure OR warning`.** Every "needs attention" count, filter and
  colour uses that predicate, never `failure` alone.
- **Unknown is unknown.** Never `success`, never `scheduled`, never dropped from
  a tally.
- **Accept legacy spellings on read**, via the existing alias table. Never
  normalise on write, never add an `in:` rule (a 422 leaves the row stuck at
  `running` — api `RunnerJobUpdateRequest.php:22-27`).
- **Never branch on `selection_mode` alone.** `treatment_lost` is the alarm,
  `degraded` is "look at this", `selection_mode` is a descriptor.
- **Absence of a verdict is unknown, not clean.** Legacy jobs, non-lambda jobs
  and mixed-version deploys must render as "no verdict", never as green.
- **`filters[x] == "skipped"` means not requested**, never a failure.

## The plan

Five phases, ordered so each ships alone and each is safe under mixed
versions. Phases 1–3 need no runner change. Phase 4 is the one that carries
the scientific signal and is the only one touching three repos.

### Phase 1 — Correctness (small PRs, no new endpoints)

Fixes the places where the GUI or API currently shows something *wrong*, as
opposed to missing.

**API (`primemover_api`)**
- Count queue-level `warning` in the crawler rollup (F1, F2): add
  `latest_queues_warning` = runs in window whose **own** `status_code` ∈
  `statusVariants('warning')`; redefine `latest_queues_degraded` as
  `queue warning OR any job warning` so the four appended counts sum to
  `latest_queues_count` for finished runs. Update the `Queue` docblock and the
  runner-queue OpenAPI enum to include `warning`. Tests: extend
  `CrawlerQueueStatusTest` (queue warning with zero warning jobs counts once).
- Index `queues.status_code` (F10). One migration.
- Regenerate `storage/api-docs/*.json` (stale since 08-12) and document
  `latest_queues_*` and `jobs_*_count` on the read endpoints.

**GUI (`primemover_gui`)**
- `bot-detail.runState()`: unknown → `'unknown'` (grey "Unknown" pill), never
  `scheduled`; `scheduled` only when `status_code` is null/empty *and*
  `started_at` is null (F3). Colour `status_message` amber for `warning`, not
  only red for `failed`.
- `experiment-workspace.botState()`: consume `latest_queues_warning`; order
  failed → warning → succeeded → running; treat a run total that exceeds
  succeeded+failed+warning as "in progress", never as "running forever".
  Add a **Warning** tile beside Degraded (or fold Degraded into Warning —
  recommend fold: one amber number, tooltip splits lost vs degraded).
- `run-composer.tally()`: add an `unknown` bucket and render it (F11 sibling).
- Type `status_code` as `QueueStatusCode = 'running'|'success'|'warning'|'failure'|'skipped'|(string & {})`
  on `Queue`, `Job`; make every switch exhaustive with a `default` that
  yields unknown.
- Move `status.component.spec.ts` into the curated list, rewrite it as a real
  test of `normalizeStatus`, every alias, unknown default, and message
  rendering (F11). Delete the dead `shared/job-status` scaffold.
- Fix `run-composer` and `bot-detail` to show the queue's **own**
  `status_message` as the primary line and the reconstructed rollup as
  secondary (they can disagree today).

**Orchestrator (docs)**
- Contract §0 and CLAUDE.md: replace "nothing is deployed" with the actual
  deployed state and the date.

*Acceptance:* a run whose queue is `warning` with 0 warning jobs shows amber on
the workspace bot card, in the bot-detail run list, and in the Degraded/Warning
tile. An unknown code shows "Unknown" in all four places. `npm run test.ci`
executes the status pill spec (count moves).

### Phase 2 — Find the bad ones (filter + sort)

**API**
- `filter[status_code]` on `/queues`, `/jobs`, and `/crawlers` (crawlers via
  latest run status), implemented with `statusVariants()` so `failure` matches
  `failed` and `warning` matches `warn`. Accept a comma list.
- `filter[bad]=1` = `status_code ∈ variants(failure) ∪ variants(warning)`
  (the contract's `is_bad`), so the GUI does not have to know the alias table
  to ask the right question.
- Sort by **severity** (`sortBy=severity` → `FIELD(normalised status, …)`),
  since sorting the raw string splits `failed` from `failure` (F4).
- Add the five `jobs_*_count` to `/queues/{id}` and to the six `queues-*` list
  endpoints (F6), so a run detail no longer needs a second request per run.

**GUI**
- Status filter chips (All / Needs attention / Warning / Failure / Skipped /
  Running / Success) on `/queues`, `/jobs`, and the bot-detail run list; "Needs
  attention" is the default on bot-detail when any bad run exists.
- On `/queues` replace the three Yes/No columns with the job rollup
  (`succeeded / warning / failed / skipped` of `total`, as a compact stacked bar)
  and keep `processed`/`processing` in a tooltip (F14).

*Acceptance:* one click shows only `failure OR warning` runs of an experiment;
legacy `failed` rows appear in that view.

### Phase 3 — Health above the bot (experiment rollup)

**API**
- `GET /experiments/{id}/health?hours=24` (default 24, max 720): `{bots,
  runs: {total, running, success, warning, failure, skipped, unknown},
  jobs: {…same…}, bad_runs: n, last_finished_at}`, computed with one grouped
  query over queues joined to crawlers, alias-aware. Also append
  `runs_bad_count` (window) to the experiment list so cards can show a dot
  without N requests. Make the crawler window a query param `?hours=` too
  (F12 sibling).

**GUI**
- Experiment cards (`/experiments`): a health strip (green / amber / red / grey
  unknown) with "N of M runs need attention" and last activity (F5).
- Workspace header: the same numbers as tiles, with the window selector
  (24h / 7d / all).
- Bot cards: pill reflects the worst run in window, tooltip lists the counts.

*Acceptance:* from `/experiments` a researcher can see which study has bad runs
without opening it.

### Phase 4 — The verdict: did the treatment apply? (runner + API + GUI)

This is the only phase that carries the scientific signal, and the only one
touching the runner. It also fixes the historical reading problem (6543
`lambda-jobdata` objects already exist).

**Decision:** persist the verdict on the job row, written by the runner from
the parsed `LambdaSelectJobResult` it already holds. Nullable = unknown.

**API**
- Migration on `jobs`: `decision_degraded` (bool, null), `decision_treatment_lost`
  (bool, null), `decision_selection_mode` (string, null), `decision_report`
  (JSON, null: `{filters, llm, warnings[≤4], applied[]}`). Index the two
  booleans. Accept them on the runner PUT (`RunnerJobUpdateRequest`), same
  "accept, never 422" posture as `status_code`.
- Rollups: `jobs_treatment_lost_count`, `jobs_degraded_count` on queues (and
  `/experiments/{id}/health`); `latest_queues_treatment_lost` on crawlers.
- Fix `GET /v1/file/{report}` (F9) so the GUI can offer "download the raw
  decision payload" per job, and add `GET /jobs/{id}/decision` that streams
  the parsed `lambda-jobdata` object from MinIO — this is what makes the
  **historical** 6543 verdicts visible without a backfill.
- Optional backfill command (`php artisan decisions:backfill`) reading MinIO and
  populating the new columns for existing lambda jobs.

**Runner**
- `JobService.updateJobState` PUT body: add the four fields when the job is a
  lambda select job and `hasDegradationReport()` is true; omit otherwise (null
  = unknown on the API). No status_code changes.

**GUI**
- Job row / run detail: a red **"Treatment lost"** badge when
  `decision_treatment_lost === true`, an amber **"Degraded"** badge for
  `decision_degraded === true`, a grey "no verdict" mark when null on a
  lambda-select job. Badge tooltip: which filters failed, LLM error type, mode.
- Run and experiment rollups: "treatment lost in N runs" as the top-line alarm
  (red), separate from generic warnings.
- Job detail drawer: the parsed report (filters table: ok / not requested /
  failed + reason), a "download raw payload" link, and the `_job_warnings_`
  list.

**Deploy order** (mixed versions are safe because null = unknown): API
migration first (accepts and ignores nothing), then GUI (renders "no verdict"
for null), then runner jar (starts writing). Never the runner first.

*Acceptance:* a treated crawler whose decision degraded to random shows
"Treatment lost" on the job, the run, the bot card and the experiment health
strip, with the failed filter named. A control crawler picking at random shows
nothing. A pre-overhaul job shows "no verdict".

### Phase 5 — Message UX and liveness

- `status_message`: first clause always visible, click to expand in place
  (details/summary), copy button; on skipped jobs parse `previous job <id>`
  into a link (F7).
- Diagnostics drawer per job: message, timestamps, `captcha_found`
  (labelled "captcha string seen on page", since that is what it is — F13),
  report list with per-object download once F9 is fixed.
- Polling: while a run is `running` in the current view, refresh its queue and
  jobs every 15s (single `timer` + `switchMap` in bot-detail and run-composer,
  paused when the tab is hidden) (F12).
- Toast when a run in the open workspace transitions to `failure` or
  `treatment_lost`.

## Sizing

| phase | repos | size | value |
|---|---|---|---|
| 1 Correctness | api, gui, docs | 2–3 days | stops the GUI showing wrong states today |
| 2 Filter/sort | api, gui | 2 days | "show me the bad runs" |
| 3 Experiment health | api, gui | 2–3 days | triage from the top |
| 4 Verdict | api, runner, gui | 4–5 days | the scientific signal; the reason the overhaul exists |
| 5 Message UX / polling | gui (+api F9) | 2 days | reading the diagnosis without leaving the GUI |

Recommended order: **1 → 4 → 2 → 3 → 5**. Phase 4 goes second because
`treatment_lost` is the alarm the whole overhaul was built to raise, it is the
one thing no amount of GUI polish can surface without the API change, and the
history is already sitting in MinIO. Phases 2 and 3 are polish on top of data
the API already has.

## Test and CI notes

- GUI runs only the curated spec list (`tsconfig.spec.json` + `angular.json`
  `test.include`); every new spec must be added to **both** and the
  "Executed N of N" count checked.
- API: keep `QueueStatusTest::test_the_alias_table_matches_the_orchestrator`
  green; new filters get a legacy-spelling test each; `/health` gets an N+1
  guard like `CrawlerQueueStatusTest`.
- Runner: the four new PUT fields need a `JobServiceTest` case for "report
  present" and "report absent"; CI runs on JDK 17 and 25.
- Contract: each phase updates `docs/integration-contract.md` (§3.4 for the
  new counts and filters, §7.3 for the persisted verdict) and `status.py` gains
  the `decision_*` field names so the orchestrator and MCP tools read the same
  columns.

## Out of scope

- Reclassifying historical jobs (a `warnings[]` payload without a degradation
  report stays as recorded; the runner deliberately does not reinterpret it).
- Alert delivery (Grafana rules exist; SMTP is a separate item).
- A GUI-side HAR viewer.
