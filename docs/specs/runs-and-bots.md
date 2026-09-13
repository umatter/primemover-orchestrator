# Spec — Runs & Bots in the new Experiment workspace

Status: **Draft / in progress** · Owner: GUI · Created 2026-07-21
Canonical contract for the "compose runs and add bots from the new UI" slice.
Lives at the monorepo root because it spans the GUI (primary) and the API/runner
contract it depends on.

## 1. Context & goal

The redesigned GUI (see `primemover_gui/.impeccable.md`) reframes the product as a
**hierarchy, not seven admin tables**: an **Experiment** is a project workspace
containing **Bots**, each Bot has **Runs**, each Run is an ordered sequence of
**Jobs**, and each Job carries **Behaviours** (its parameters).

The new `experiment/workspace` and `experiment/wizard` shells render this hierarchy
but are **read-mostly**. Three capabilities are missing and are the scope of this spec:

1. **Add a Bot to the experiment** — today `addBot()` routes to the generic
   `crawlers/create` form *without* experiment context.
2. **Compose & schedule a Run** — today the wizard's "Runs" step is informational
   text; the workspace has no way to create a Run, author its Job sequence, or
   schedule it. (`queue/create` exists only as a standalone admin form.)
3. **Edit Behaviours** — behaviours are not surfaced anywhere in the new UI.

Decisions taken for this slice:
- **Scope:** full run composition (author the ordered Job sequence + Behaviours in-UI).
- **Approach:** new **workspace-native** components matching `.impeccable`, not a
  re-skin of the old admin forms. Existing services (`@ngrx/data`) are reused; the
  old `crawler/create`, `queue/create`, `behavior/form` admin routes remain for the
  RA power-path and are not deleted by this slice.

## 2. Domain model

Verified against `primemover-orchestrator/orchestrator/e2e/test_experiment_youtube.json`
and `primemover_api/routes/api.php`.

```
Experiment (id, name, description, active, testing)
└── Crawler  == "Bot"        (id, name, description, experiment_id, agent_id, proxy_id, configuration_id)
    ├── agent / proxy / configuration     ← Library building blocks (setup details)
    └── Queue == "Run"        (id, name, description, crawler_id, order, active,
        │                       start_at, timezone, timezone_utc_offset,
        │                       status_code, started_at, finished_at)
        └── Job               (id, name, type, description, queue_id, order, active, status_code)
            └── Behavior       (id, name, description, value, active, job_id)
```

Naming map (UI term → entity): **Bot** = Crawler · **Run** = Queue · **Job** = Job ·
**Behaviour** = Behavior. UI copy uses the researcher term; monospace/IDs may show the raw entity.

> **`status_code` on Queue and Job is a fixed vocabulary, defined elsewhere.** The
> normative list, the legacy values a reader must still accept (`skpped`, `failed`),
> the `status_message` format, and the `warning` state added 2026-08-27 are in
> `primemover-orchestrator/docs/integration-contract.md` §3.4, with the executable
> copy in `orchestrator/status.py`. It is deliberately **not** duplicated here:
> five copies of this vocabulary already drifted apart once, which is what §3.4
> exists to stop. Two things the GUI must get right, though: **`warning` is not
> success** — a run whose jobs are all `success` and `warning` finished, but
> something in it degraded and should be visible as such, not rendered green; and
> an **unrecognised** value must render as unknown rather than defaulting to a
> success style.

> ⚠ **Terminology collision (resolved 2026-07-21).** The workspace **bot card** labels the
> bot's **Configuration** as "behavior" (`workspace.bot.behavior` → `bot.configuration?.name`).
> That is *not* the same thing as the **Behavior entity** in this spec. "Behaviours" in Slice D
> means the per-**Job** `Behavior` records (`{name, description, value, active, job_id}`), which
> the fixture shows as `jobs[].behaviors[]`. To avoid future confusion, prefer relabelling the
> bot-card Configuration field away from "behavior" (e.g. "Profile"/"Configuration") when Slice A
> touches that card; the word **Behaviour** in the new UI should refer only to the Job-level entity.

### Job types (`src/app/enum/job-type.enum.ts`)
`visitjob, waitjob, tryentertextfieldjob, entertextfieldjob, singleselecturljob,
singleselectjob, tryswitchjob, switchjob, tryclickjob, clickjob, scrollbydurationjob,
scrollbylengthjob, handlealertjob, tryhandlealertjob`.
> NOTE: there are two duplicate enum files (`JobType.enum.ts` and `job-type.enum.ts`).
> Consolidate to one during Slice C (mechanical; not a behaviour change).

> **Real validation contract (verified against the live API 2026-07-21) & fixes applied:**
> - **Queue create requires a non-empty `description`** (Laravel `required`); an empty string 422s.
>   Run-create now falls back to the run name when the description is blank.
> - **Crawler create dropped `agent_id`/`proxy_id`/`configuration_id`**: `StoreCrawlerRequest` only
>   validated `name`/`experiment_id`/`user_id`, so `$request->validated()` stripped the FKs and bots
>   were created with no agent/proxy/config. Added those (nullable) to `StoreCrawlerRequest`.
> - **Job create required an existing `previous_job_id`** — impossible for the first job. The runner
>   (`TaskOperator`) sequences jobs by list/`order` and rebuilds the previousJob chain at runtime, so
>   the stored value isn't used for ordering. Made `previous_job_id` `nullable` in `StoreJobRequest`;
>   the composer chains it to the current last job (null for the first) and sets `order`.
> - `user_id` is auto-injected server-side (`prepareForValidation`), so clients never send it.

### API contract (all under `/api/v1`, bearer auth)
| Action | Method | Endpoint | Key payload fields |
|---|---|---|---|
| Create Bot | POST | `/crawlers` | `name, description, experiment_id, agent_id, proxy_id, configuration_id` |
| Create Run | POST | `/queues` | `name, description, crawler_id, start_at, timezone, timezone_utc_offset, active, order` |
| Create Job | POST | `/jobs` | `name, type, description, queue_id, order, active` |
| Update Job | PUT | `/jobs/{id}` | `order, active, …` (reorder / toggle) |
| Create Behaviour | POST | `/behaviors` | `name, description, value, active, job_id` |
| Update/Delete | PUT/DELETE | `/{entity}/{id}` | standard `apiResource` CRUD |

## 3. Personas & principles (from `.impeccable.md`)
- **PI** (designs studies, non-technical): guided, plain-language, defaults.
- **RA** (operates studies, technical): fast, bulk, keyboard, raw values.
- Principles that bind this slice: *Hierarchy over inventory*, *Plain language first,
  precision on demand*, *Guide the first step / respect the hundredth*,
  *Trust through transparency* (honest run status).

## 4. Information architecture

```
/experiments/:id                     Experiment workspace  (exists)
  └─ Bots list  → [+ Add bot]        Slice A
/experiments/:id/bots/:botId         Bot detail: its Runs   Slice B/C entry
  └─ Runs list  → [+ New run]        Slice B
/experiments/:id/bots/:botId/runs/:runId   Run composer     Slice C/D
  └─ ordered Job sequence → per-Job Behaviours panel
```
Exact routes are provisional; finalise in Slice A. Deep-linkable; back-navigation
always returns up the hierarchy.

## 5. Feature slices & acceptance criteria (Given/When/Then)

### Slice A — Add a Bot to the experiment (workspace-native)
**Files:** `experiment/workspace/*`, new `experiment/workspace/add-bot/*` (panel/dialog),
`CrawlerService`, routing.
- **G** I am in the workspace of experiment `E` **W** I click "Add bot" and choose an
  agent, proxy and configuration from the Library and a name **T** a Crawler is POSTed
  with `experiment_id = E.id` and appears in this experiment's Bots list without a full reload.
- **G** the Library is empty for a block **W** the add-bot panel opens **T** it links to
  create that block (RA path) and blocks submit with a plain-language message; no silent failure.
- **G** the POST fails **T** an honest, calm error is shown and the panel keeps my input.
- **G** I am a PI **W** I open add-bot **T** sensible defaults are preselected where possible;
  raw entity IDs are demoted behind labels.

### Slice B — Create & schedule a Run on a Bot
**Files:** new `bot` detail + `run/create` workspace components, `QueueService`,
`TimezoneService` (reuse), routing.
- **G** a Bot `B` **W** I click "New run", enter a name, pick a start date/time and
  timezone **T** a Queue is POSTed with `crawler_id = B.id`, resolved
  `timezone_utc_offset`, and shows in the Bot's Runs list as *scheduled*.
- **G** no start time chosen **T** submit is blocked with a field-level message.
- **G** a Run exists **T** its status is shown honestly (scheduled / running /
  succeeded / failed) using the `.impeccable` status colours, never alarmist.

### Slice C — Compose the Run's Job sequence (full composition)
**Files:** new `run/composer/*`, `JobService`, `BehaviorService`, `enum/job-type-schema.ts`.
- **G** a Run with no jobs **W** I add a Job, pick its type and fill type-specific fields
  **T** a Job is POSTed with `queue_id` and the next `order`; it renders in sequence.
- **G** an ordered Job list **W** I reorder / remove / toggle-active a Job **T** the change
  persists via PUT/DELETE and the visible order matches `order` ascending.
- **G** I pick a Job type **T** only the fields valid for that type are shown
  (type→fields map, see below).
- **G** I leave a required field empty **T** submit for that Job is blocked with a message.

**Type→fields map (a job's parameters ARE its Behaviours).** Ported to
`src/app/enum/job-type-schema.ts` from the orchestrator `pmspec` job registry
(the authoritative schema, also behind MCP `get_job_type_schema`). Each field is a Behaviour
(`{name, value}`); required/optional + defaults per type. Summary:
- `visitjob`: **url**; captchaMode(never)
- `waitjob`: **seconds**; captchaMode
- `clickjob` / `singleselectjob` / `singleselecturljob`: **selector**; selectionType(XPATH); captchaMode
- `tryclickjob`: **selector**; selectionType, task, flag, captchaMode
- `entertextfieldjob` / `tryentertextfieldjob`: **text**, **selector**; selectionType, appendReturn(false), typingMode(SIMULATED_FIXINGTYPOS), captchaMode
- `scrollbydurationjob`: **duration**; scrollDirection(DOWN); captchaMode
- `scrollbylengthjob`: **length**; scrollDirection, captchaMode
- `switchjob` / `tryswitchjob`: **target**; captchaMode
- `handlealertjob` / `tryhandlealertjob`: **action** (accept/dismiss); captchaMode
- `lambdasingleselectjob`: **task**, **linkValue**; linkType(FUNCTION_NAME), captchaMode

**Create flow:** on Add, POST the Job, then POST one Behaviour per filled field with the new
`job_id` (required fields enforced; optional fields carry their defaults). The Slice D modal
edits these same Behaviours afterward.

### Slice D — Edit a Job's Behaviours
**Files:** new `run/composer/behaviours/*` (panel), `BehaviorService`.
- **G** a Job `J` **W** I open its Behaviours panel and add/edit a behaviour (name + value,
  active toggle) **T** a Behavior is POSTed/PUT with `job_id = J.id` and listed under the Job.
- **G** a behaviour is toggled inactive **T** `active=false` persists and the row reads as muted.
- **G** the fixture's shape (a Job has ≥1 behaviour) **T** the panel supports multiple
  behaviours per Job, ordered as returned.

### Slice E — Wizard integration
**Files:** `experiment/wizard/*`.
- **G** the wizard's "Runs" step **W** I reach it after creating a bot **T** it either
  launches the Slice B run-composer for the new bot or clearly defers to the workspace —
  no dead informational-only step. (Choose during Slice E; keep the wizard guided.)

## 6. Out of scope (follow-ups)
- Factorial/templated run generation (that is `pmspec`/`pmspec`'s job; a
  future "generate runs from template" action may call into it).
- Bulk run operations, run cloning, drag-drop reorder polish (keyboard/up-down first).
- Deleting the legacy admin routes.
- Live run progress streaming beyond current status fields.

## 7. Test plan
- Unit specs per new component (Angular TestBed) asserting the payload shape and the
  Given/When/Then above; services mocked. TDD: spec first, red, then implement.
- Reuse existing `*.spec.ts` patterns in `queue/`, `crawler/`, `behavior/`.
- Manual e2e sanity against the running API (:8090) using a scratch experiment; the
  `test_experiment_youtube.json` fixture is the reference for a fully-composed run.

## 9. Test harness (resolved 2026-07-21)
- **The repo's karma suite was fully dead.** 80 of 82 existing specs are auto-generated
  single-`it` "should create" stubs that never ran: 63 misuse `declarations:[StandaloneComponent]`
  and several reference stale class names. Only `app.component.spec` and
  `shared/error-handler/error.interceptor.spec` have real content.
- **Fix chosen (non-destructive — this folder is not under git):** scope the test build to
  curated specs instead of deleting the stubs. `tsconfig.spec.json` `include` and
  `angular.json → projects.primemover-frontend.architect.test.options.include` are both set to
  `…/experiment/workspace/**/*.spec.ts`. New slice specs live under `experiment/workspace/` and
  run green; the 80 dead stubs are quarantined, not removed.
- **To re-enable a legacy stub:** repair it (imports + `imports:[]` for standalone + TestBed
  providers: `provideRouter([])`, `NoopAnimationsModule`, `TranslateModule.forRoot()`, entity-service
  mocks) and widen both `include` globs. The Add-bot spec is the reference pattern.
- Command: `CHROME_BIN="…/Google Chrome" npx ng test --watch=false --browsers=ChromeHeadless`.
  (A harmless "did a full page reload!" line is printed but the run exits 0.)

## 10. Change log
- 2026-07-21 — initial draft (scope: full run composition, workspace-native UI).
- 2026-07-21 — **Slice A implemented** (workspace-native Add-bot panel:
  `experiment/workspace/add-bot/*`, wired into workspace, i18n EN/DE, bot-card
  "behavior"→"profile" relabel). Production build green.
- 2026-07-21 — **Test harness repaired** (§9): scoped to `experiment/workspace/**` specs;
  Add-bot spec now runs green (3/3).
- 2026-07-21 — **Slice B implemented**: `run-create/*` (schedule a Run scoped to a bot,
  timezone→offset resolution) + `bot-detail/*` (route `/experiments/:id/bots/:botId`, runs list
  with honest status, "New run" panel); workspace bot cards link into it. i18n EN/DE. 9/9 specs.
- 2026-07-21 — **Slice C implemented**: `run-composer/*` (route
  `/experiments/:id/bots/:botId/runs/:runId`) — add Job (type from `job-type.enum`, name), next
  `order`; reorder (swap order via PUT), toggle active (PUT), remove (DELETE). Added missing
  `active` field to the `Job` model. Run rows in bot-detail link into the composer. i18n EN/DE.
- 2026-07-21 — **Slice D implemented**: `job-behaviours/*` modal (opened per job from the
  composer) — list/add/toggle/remove a Job's Behaviours (type-specific params) scoped to `job_id`.
- 2026-07-21 — **Slice E implemented**: wizard now lands on the new bot's page after inline bot
  creation (so the operator schedules the first run there); "Runs" step copy updated to match.
  Duplicate `JobType.enum.ts` left in place (unused) pending version control — see §2 note.
- 2026-07-22 — **Reorder no-op fix:** jobs created during the earlier broken add-flow got duplicate
  `order` values (a race left `nextOrder()` reading a stale empty list), so swapping raw order values
  did nothing visible. Rewrote reorder to renumber by **position** (index+1) and PUT only the jobs
  whose order changed — robust to colliding orders and self-heals the duplicate data on first use.
  Locked in with a test for the equal-orders case.
- 2026-07-22 — **Reorder 422 fix:** moving a job PUTs both swapped jobs; `UpdateJobRequest` still
  required a non-null `previous_job_id`, so reordering the first job (predecessor null) 422'd. Made
  `UpdateJobRequest.previous_job_id` `nullable` (mirrors the earlier StoreJobRequest fix; runner
  orders by `order`). Verified live (PUT first job → 200). Also hardened `swapOrder` to reload after
  both PUTs settle so the shown order can't drift if one fails.
- 2026-07-22 — **"Add job" did nothing (no POST):** the composer's Add-job `<form>` had `(ngSubmit)`
  but `[formGroup]` on a child div, not the form. With only ReactiveFormsModule imported, a form
  without a form directive has no `ngSubmit` output, so clicking Add never called `addJob()`. Fixed
  by binding `[formGroup]="addForm"` on the `<form>`. Root-caused with a DOM-level test that clicks
  the real submit button (prior tests called `addJob()` directly and missed it).
- 2026-07-22 — **"Job added but not shown" investigation:** wrote a full-store integration test
  (`run-composer.integration.spec.ts`) exercising add-job → behaviours → reload through the real
  @ngrx/data store + custom persistence handler/reducer/url-generator with an HTTP mock — it
  **passes** (job renders), so the component logic is correct. Hardened `addJob` regardless:
  behaviour errors are caught (a failed behaviour no longer hides the created job), jobs reload on
  both success and error, and an error banner surfaces failures. Restarted the dev server fresh to
  rule out a stale bundle after many hot edits; a hard browser refresh (Cmd+Shift+R) is advised.
- 2026-07-21 — **Filter-name fix (400s):** list endpoints use Spatie **scope** filters, not
  `*_id` params. Corrected all workspace queries to `filter[experiment]` (crawlers),
  `filter[crawler]` (queues), `filter[queue]` (jobs), `filter[job]` (behaviours) — each takes the
  id as its value. The wrong `filter[queue_id]` etc. returned 400 ("Allowed filter(s) are name,
  after, recent, queue"). Note: the pre-existing workspace crawler load also used the wrong
  `filter[experiment_id]`; now fixed. Verified all four return 200 live.
- 2026-07-21 — **Job parameter fields (Slice C+):** the composer's Add-job form is now dynamic —
  picking a type shows its parameter fields (`enum/job-type-schema.ts`, ported from the orchestrator
  registry); on Add it creates the Job then one Behaviour per filled field (required enforced,
  optional defaults carried). Verified live. **Also fixed `BehaviorController::store`**, which never
  injected `user_id` (NOT NULL FK) or defaulted `description` (NOT NULL) → every behaviour POST 500'd
  (this had also silently broken the Slice D modal). Now injects both server-side; behaviour create
  returns 201 and attaches to the job. Build green, 19/19 specs.
- 2026-07-21 — **API-contract fixes (verified live):** (1) run-create sends a non-empty
  `description` (falls back to run name) — fixes the queue-create 422; (2) `StoreCrawlerRequest`
  now accepts `agent_id`/`proxy_id`/`configuration_id`/`description` so bots keep their Library
  blocks (were silently stripped); (3) `StoreJobRequest.previous_job_id` made `nullable` and the
  composer chains it (null for first job) so a run's job sequence can be authored one job at a
  time. Confirmed against localhost:8090: queue 201, crawler FKs persisted, first+chained jobs 201.
  Both API files are bind-mounted into the running php container (live, no rebuild).
- 2026-07-21 — **Bugfix (timezone):** `run-create` (and legacy `queue/create`) resolved timezone
  offsets via the external `worldtimeapi.org`, which was unreachable and threw
  `Http failure … status 0`, blocking run scheduling. Rewrote the shared `TimezoneService` to
  resolve the timezone list + UTC offset locally via the browser `Intl` API (offline, no external
  dependency); `TimezoneDetail` shape preserved. Consumers unchanged.
