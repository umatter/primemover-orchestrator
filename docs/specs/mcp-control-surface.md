# Spec — MCP control surface (drive PrimeMover from Claude)

Status: **Implemented (v1)** · Owner: orchestrator · Created 2026-07-21 · Updated 2026-07-22
Canonical contract for exposing PrimeMover's domain **write** operations as MCP tools so an
agent (Claude) can compose and operate experiments — mirroring the GUI 1:1.

## 1. Context & goal

The orchestrator already ships a consolidated MCP server
(`orchestrator/mcp_server/server.py`, FastMCP) with a rich **read/ops** surface: `list_*` /
`get_*` for experiments·crawlers·queues·jobs, traces, logs, lambda analysis, plus
`validate_experiment_spec`, `list_job_types`, `get_job_type_schema`, and a bulk
`upload_experiment`. What's missing is the **granular write surface** — the same mutations the
GUI performs. This spec adds them.

**Decisions taken (2026-07-21):**
- **Shape:** granular CRUD tools, mirroring the GUI entity-by-entity (not spec-driven upload).
- **Safety:** dry-run by default + explicit confirm — no tool mutates on first call.
- **Target:** local dev API only (`http://localhost:8090`); writes refuse any other host.

## 2. Entity & endpoint map (same contract as runs-and-bots.md §2)

Experiment → Crawler(Bot) → Queue(Run) → Job → Behavior. All standard `apiResource` CRUD under
`/api/v1`. Correlation ID format: `exp-{e}|cr-{c}|q-{q}|j-{j}`.

## 3. Tool inventory (new write tools)

Naming: `pm_<verb>_<entity>`. Every write tool takes `confirm: bool = False` (see §4) and
returns JSON. Entities use GUI-facing names in descriptions (Bot/Run) but real IDs in args.

| Tool | Method → endpoint | Required args |
|---|---|---|
| `pm_create_experiment` | POST `/experiments` | name, contact; opt description, testing |
| `pm_update_experiment` | PUT `/experiments/{id}` | id; changed fields |
| `pm_delete_experiment` | DELETE `/experiments/{id}` | id |
| `pm_add_bot` | POST `/crawlers` | experiment_id, name, agent_id, proxy_id, configuration_id |
| `pm_update_bot` | PUT `/crawlers/{id}` | id; changed fields |
| `pm_delete_bot` | DELETE `/crawlers/{id}` | id |
| `pm_create_run` | POST `/queues` | crawler_id, name, start_at, timezone, timezone_utc_offset; opt active |
| `pm_update_run` | PUT `/queues/{id}` | id; changed fields (e.g. active, start_at) |
| `pm_delete_run` | DELETE `/queues/{id}` | id |
| `pm_add_job` | POST `/jobs` | queue_id, type, name, order; opt description, active |
| `pm_update_job` | PUT `/jobs/{id}` | id; changed fields (order/active/…) |
| `pm_delete_job` | DELETE `/jobs/{id}` | id |
| `pm_reorder_jobs` | PUT `/jobs/{id}` ×N | queue_id, ordered id list → writes `order` |
| `pm_set_behaviour` | POST `/behaviors` or PUT `/behaviors/{id}` | job_id, name, value; opt active, id |
| `pm_delete_behaviour` | DELETE `/behaviors/{id}` | id |
| `pm_activate_run` | PUT `/queues/{id}` (`active=1`) | id |

Job `type` is validated against `list_job_types()` / `get_job_type_schema()` before any POST —
an invalid type is rejected in the dry-run, never sent.

## 4. Dry-run + confirm contract (safety)

Every write tool follows the same protocol:
- **`confirm=False` (default):** perform NO mutation. Return a **preview**:
  `{ "dry_run": true, "action": "<verb entity>", "method", "endpoint", "payload",
     "current": <GET of the target for update/delete, else null>,
     "diff": <field-level before→after for updates>,
     "warnings": [...], "to_apply": "call again with confirm=true" }`.
- **`confirm=True`:** execute the mutation and return `{ "applied": true, "result": <api json> }`.
- **Cascade warning:** delete tools list child entities that will be orphaned/removed
  (e.g. deleting a Run warns about its Jobs) in the dry-run `warnings`.
- Validation (missing FK, bad job type, empty required field) fails in the dry-run with a clear
  error and `applied:false` — even with `confirm=True`.

## 5. Host guard (local-only writes)

A shared `_require_local_write_target()` guard runs at the top of every write tool:
- Resolves `settings.pm_api_base_url`; if host is not `localhost`/`127.0.0.1` **or** port ≠ 8090,
  the tool returns `{ "error": "MCP writes are restricted to the local dev API (:8090); refusing
  to mutate <host>." }` and does nothing — even with `confirm=True`.
- Reads are unaffected (existing tools keep working against any configured API).
- Rationale: writes can trigger real browser-automation runs; blast radius is capped to dev.
  (A future `PM_MCP_ALLOW_REMOTE_WRITES` escape hatch is explicitly out of scope here.)

## 6. Implementation notes
- Extend `_api_request` to support `PUT` and `DELETE` (currently GET/POST only) — small change,
  same auth flow.
- Add a `_write_tool` helper that wraps: host guard → validate → dry-run/preview → confirm/apply,
  so all 16 tools share one code path and one test.
- Keep tools in `orchestrator/mcp_server/server.py` (the single consolidated server) or a new
  `mcp_server/tools/writes.py` registered into it — decide in implementation; prefer the module
  split for testability.
- No new deps. Follows orchestrator conventions (black/ruff line 100, lazy imports, `json.dumps`).

## 7. Acceptance criteria (Given/When/Then)
- **G** local API on :8090 **W** `pm_add_bot(experiment_id=E, …)` with `confirm=False`
  **T** no crawler is created; a preview with method/endpoint/payload is returned.
- **G** the same call **W** `confirm=True` **T** a crawler is POSTed and `applied:true` returned;
  a subsequent `get_crawler` shows it under experiment E.
- **G** `settings.pm_api_base_url` points at a non-local host **W** any write tool is called with
  `confirm=True` **T** it refuses and mutates nothing.
- **G** `pm_add_job(type="notarealjob", …)` **T** rejected in dry-run against the job-type schema;
  nothing is sent.
- **G** `pm_delete_run(id=Q)` dry-run **T** warnings list the Jobs that deletion would remove.
- **G** `pm_reorder_jobs(queue_id=Q, order=[3,1,2])` confirm **T** each Job's `order` is PUT to
  match the list; `list`/`get` reflect the new order.

## 8. Out of scope
- Remote/prod writes; bulk spec-driven authoring (already covered by `upload_experiment` +
  pmspec YAML — the two surfaces can coexist).
- Auth/role model beyond the existing PM_API_* service credentials.
- Triggering the Java runner process itself (that's `orchestrator up`/component mgmt, already MCP-exposed via ops tools).

## 9. Test plan
- Unit: mock `_api_request`; assert (a) dry-run sends nothing, (b) confirm sends the right
  method/endpoint/payload, (c) host guard blocks non-local, (d) job-type validation. Follows the
  orchestrator pytest patterns (`orchestrator/tests/`, `mock_settings`, `mock_httpx_client`).
- E2E (`--e2e`, live local API): create experiment → add bot → create run → add jobs → set
  behaviour → activate, then read back; mirrors `test_experiment_youtube.json` shape.

## 10. Open questions (for review)
1. Tool prefix — `pm_*` vs unprefixed (existing read tools are unprefixed: `list_experiments`).
   Consistency vs. a clear write namespace. **Proposed: keep `pm_` on writes to signal mutation.**
2. Do we want a single composite `pm_compose_run(bot_id, run={…}, jobs=[{…, behaviours:[…]}])`
   convenience tool on top of the granular ones (fewer round-trips for the common path)?
   Not in the CRUD decision, but cheap to add later.
3. Should `confirm=True` on delete require the entity's name echoed back (typo guard)?

## 11. Resolutions (2026-07-22)
1. **Prefix:** kept `pm_` on all write tools to signal mutation (read tools stay unprefixed).
2. **Composite tool:** added `pm_compose_run(crawler_id, run, jobs)` — builds a run + its ordered
   jobs (chained) + each job's behaviours in one call. Great for the "one prompt → whole run" demo.
3. **Delete typo-guard:** not added in v1; `pm_delete` uses the generic dry-run + `confirm=true`
   with a cascade warning. Revisit if it feels too easy to fire.

## 12. Change log
- 2026-07-21 — initial draft (granular CRUD, dry-run+confirm, local-only).
- 2026-07-22 — Added read tool **`list_library`** (agents/proxies/configurations with ids) so the
  full new-experiment path (`pm_add_bot`) works from Claude without the GUI. Wired into the
  primemover / primemover-compose skills. Verified live (10 agents, 10 proxies, 14 configs).
- 2026-07-22 — **Implemented v1** in `orchestrator/mcp_server/server.py`: tools `pm_create_experiment`,
  `pm_add_bot`, `pm_create_run`, `pm_activate_run`, `pm_add_job` (auto order/previous_job_id + type
  validation), `pm_set_behaviour`, `pm_delete` (entity+id, cascade warning), and composite
  `pm_compose_run`. Shared `_write()` (local guard → dry-run/confirm) and `_require_local_write_target()`.
  Extended `_api_request` with PUT/DELETE and better error bodies. **Also fixed a pre-existing auth
  bug**: `_api_request` posted to `/auth/login` (404) — the real route is `/login`; every API-backed
  MCP tool (read included) was failing auth until this fix. Verified live end-to-end: dry-run previews,
  `pm_compose_run` created run+2 jobs+behaviours, guard blocks a non-local target even with
  confirm=true, cleanup deletes. Orchestrator unit suite: 194 passed (3 pre-existing env-leak failures
  unrelated to MCP).
