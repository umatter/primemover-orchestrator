---
name: primemover-compose
description: "Use when the user wants to create or build up a PrimeMover experiment/bot/run via the orchestrator MCP write tools — e.g. \"compose a run on bot 14 that opens youtube then waits\", \"create an experiment with a bot\", \"add a wait job to run 40\", \"schedule a run\". Drives the pm_* write tools with a dry-run → confirm discipline against the local API only."
---

# /primemover-compose

Compose and manage PrimeMover experiments through the **orchestrator MCP write tools** (`pm_*`).
Every write is **dry-run by default**; you apply it only after the user confirms. Writes are
refused unless the API target is the local dev server (`localhost:8090`).

## Prerequisites

- The `primemover` MCP server is connected. If the `pm_*` tools (e.g. `pm_compose_run`) are not in
  the available tools, tell the user to start it (`primemover-orchestrator/scripts/mcp-server.sh`,
  or `--http --port 8100` + `claude mcp add`) and stop.
- The local API is up on `:8090` (`orchestrator status` shows API RUNNING). If not, tell the user
  to run `orchestrator up` first.

## Entity model (the hierarchy you're building)

```
Experiment  →  Bot (crawler = agent + proxy + configuration)  →  Run (queue)  →  Job  →  Behaviour
```

- A **Bot** is composed from **Library** blocks: an agent (browser identity), a proxy (connection),
  and a configuration (profile). Creating a *new* bot needs those ids.
- A **Run** is a scheduled session (name + start_at + timezone).
- A **Job** is one ordered step with a `type`; its parameters are **Behaviours** (`name → value`).

## The tools

Read (orient first): `list_experiments`, `list_crawlers`, `list_queues`, `list_library`
(agents/proxies/configurations with ids), `get_experiment`, `get_crawler`, `get_queue`, `get_job`,
`list_job_types`, `get_job_type_schema`.

Write (all take `confirm` — omit for a preview, pass `confirm=true` to apply):
`pm_create_experiment`, `pm_add_bot`, `pm_create_run`, `pm_add_job`, `pm_set_behaviour`,
`pm_activate_run`, `pm_delete`, and the composite **`pm_compose_run`**.

## Playbook

### Step 1 — Orient
Run `list_experiments` (and `list_crawlers` if the target is an existing bot). Confirm the ids you'll
act on. Never guess an id — read it.

### Step 2 — Pick the path
- **Compose a run on an existing bot** (most common, cleanest): you need a `crawler_id` from
  `list_crawlers`. Go to Step 3.
- **Create a whole new experiment + bot**: call `pm_create_experiment(name, contact, ...)` —
  **`contact` is required** (an email address; omitting it fails with a 422) — (confirm), then get
  the Library block ids with **`list_library`** (agents / proxies / configurations), and call
  `pm_add_bot(experiment_id, name, agent_id, proxy_id, configuration_id)` (dry-run → confirm). Then Step 3.
- **Factorial / multi-arm design**: don't hand-build every cell. Write the YAML DSL and use the spec
  tools — `get_spec_template` → `validate_experiment_spec` → `preview_factorial_design` →
  `upload_experiment(dry_run=true)` then `dry_run=false`. The DSL, and the treatment-param traps
  (pi/psi/kappa/tau are **0/1 switches**), are in the `doc://experiment-designer` and
  `doc://treatment-params` MCP resources.

### Step 3 — Compose the run (preferred: one call)
Prefer `pm_compose_run` — it builds the run + ordered jobs + each job's behaviours in one go:

```
pm_compose_run(
  crawler_id=14,
  run={"name": "Week 1 — morning", "start_at": "2026-08-01T09:00"},
  jobs=[
    {"type": "visitjob", "name": "Open YouTube", "behaviours": [{"name": "url", "value": "https://youtube.com"}]},
    {"type": "waitjob",  "name": "Watch",        "behaviours": [{"name": "seconds", "value": "30"}]}
  ]
)
```

Call it **without `confirm`** first and show the user the returned plan. Only after they approve,
call again with `confirm=true`. Report the created `run_id` and job ids.

Granular alternative (when editing an existing run): `pm_create_run` → `pm_add_job` (repeat, it
auto-orders and chains) → `pm_set_behaviour` per parameter.

### Step 4 — Get the job parameters right
Each job type needs specific behaviours. Check `get_job_type_schema(<type>)` when unsure — it accepts
**either** the API type (`visitjob`, what the `pm_` write tools take) **or** the DSL yaml_name
(`visit`). `list_job_types` returns both names side by side; the write tools below use the **API
type**. Common ones:

| Job type | Required behaviours | Notes |
|---|---|---|
| `visitjob` | `url` | + optional `captchaMode` (default `never`), `consent` (`accept`\|`reject`\|`ignore`) |
| `waitjob` | `seconds` | |
| `clickjob` / `singleselectjob` | `selector` | + `selectionType` (default `XPATH`) |
| `entertextfieldjob` | `text`, `selector` | + `typingMode`, `appendReturn` |
| `scrollbydurationjob` | `duration` | + `scrollDirection` (default `DOWN`) |
| `scrollbylengthjob` | `length` | |
| `handlealertjob` | `action` | `accept` or `dismiss` |

### Step 5 — Verify, then (optionally) activate
`get_queue(run_id)` to read the composed run back and show the user. A new run is created
**inactive** (`active=0`). Only when the user says to run it, `pm_activate_run(run_id)` (dry-run →
confirm) so the runner will pick it up.

## Safety rules
- **Always dry-run first.** Show the plan; get explicit approval before `confirm=true`.
- Deletes (`pm_delete(entity, id)`) show a cascade warning in dry-run — surface it before confirming.
- If a tool returns `{"error": "MCP writes are restricted to the local dev API…"}`, the API target
  isn't local — do not try to work around it; tell the user.
- Report ids and outcomes plainly; if a write returns `applied: false`, show the error (usually a
  422 body pinpointing the bad field).

Reference: `docs/specs/mcp-control-surface.md` and `docs/specs/runs-and-bots.md` (monorepo root).
