---
name: primemover
description: "Use when the user wants to start, stop, or check the PrimeMover stack, is getting oriented / \"getting back into it\", or asks how the ecosystem fits together (components, ports, where specs live, what the MCP can do) — e.g. \"start the primemover stack\", \"is everything running?\", \"how does primemover work again?\", \"where do I schedule runs?\". Orientation + lifecycle; hands off to primemover-compose (build) and primemover-debug (diagnose)."
---

# /primemover

Orientation and lifecycle control for the **PrimeMover** ecosystem (BFH algorithm/recommender
studies: automated browser "bots" act as simulated users to observe what a platform surfaces).

## The pieces (monorepo at `~/Code/BFH/Primemover/`)
- **primemover-orchestrator** (Python) — the control plane. CLI `orchestrator` + experiment DSL
  `pmspec`, and the consolidated **MCP server** (read + `pm_*` write tools). Run commands from here
  with the venv active.
- **primemover_api** (Laravel) — the API + DB (Docker). Serves `:8090`.
- **primemover_gui** (Angular) — the new researcher UI. `npm start` → `:4200` (also served at :8090).
- **primemover_runner** (Java/Spring Boot) — executes runs in real browsers via Multilogin X.
  Runs natively (not Docker); needs Multilogin for real automation.

## Entity model
```
Experiment  →  Bot (crawler = agent + proxy + configuration)  →  Run (queue)  →  Job  →  Behaviour
```
UI terms: Bot = crawler, Run = queue, a Job's parameters = its Behaviours. Full contract in
`docs/specs/runs-and-bots.md` (monorepo root).

## Start / stop / check
Always from `primemover-orchestrator/` with the venv active:
```
cd ~/Code/BFH/Primemover/primemover-orchestrator && source venv/bin/activate
orchestrator status      # health of API / Runner / Monitoring
orchestrator up          # start components
orchestrator down        # stop components
orchestrator doctor      # diagnose setup issues
```
Docker must be running first (macOS: `open -a Docker`); the API + monitoring are Docker containers
and usually auto-start with it.

Frontend dev server: `cd ~/Code/BFH/Primemover/primemover_gui && npm start` → http://localhost:4200.

## Where things live
| Service | URL |
|---|---|
| GUI (Angular dev) | http://localhost:4200 |
| API + docs | http://localhost:8090 · /api/documentation |
| Grafana | http://localhost:3000 |
| Prometheus / Loki | :9090 / :3100 |

## What the MCP server can do
Read/observe: `list_experiments`, `list_crawlers`, `list_queues`, `list_library`
(agents/proxies/configs), `get_*`, `trace_*`, `find_failed_executions`, `query_logs`,
`list_job_types`. Write/control (`pm_*`, dry-run→confirm, local-only): create
experiments/bots/runs/jobs/behaviours, `pm_compose_run`, `pm_delete`.
Start it: `primemover-orchestrator/scripts/mcp-server.sh` (stdio) or `--http --port 8100`.

## Hand-offs
- Building/scheduling experiments via the MCP tools → use **primemover-compose**.
- Diagnosing a failing run/job/bot → use **primemover-debug**.

## Gotchas (learned the hard way)
- Run `docker compose` **only** from the orchestrator repo root (relative bind-mounts).
- If the `orchestrator` CLI errors with a stale path, the venv was made before the repo moved —
  recreate it: `rm -rf venv && python3 -m venv venv && venv/bin/pip install -e ".[dev]"`.
- API code is bind-mounted into the php container, so PHP edits are live without a rebuild.
- MCP writes only touch the **local** API (`:8090`) by design.
