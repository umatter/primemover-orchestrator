# Handoff: stand up a runner-only box on primemover-switch-roland

**Written 2026-09-02** by the ex63 session, for a fresh Claude instance run from
Ulrich's laptop (which can reach *both* ex63 and the new VM; ex63 itself cannot
reach the VM — see "Access" below). This captures session state that is not in
git. Read the repo's `CLAUDE.md` and `docs/new-server-setup.md` ("Adding a
runner-only box") first — this file is the delta on top of them, not a
replacement.

## Goal

ex63 (bare metal, the live system) may not have the compute for all planned
experiments. Add a **second runner that offloads browser work** and shares ex63's
control plane. The new box already has an **old, dormant full stack** that is to
be **fully replaced** by this runner-only setup.

**Decisions already made with Ulrich (do not re-litigate):**
- **Architecture: runner-only, sharing ex63.** Runner + Multilogin X + local
  Lambda on the VM; API, MySQL, MinIO, monitoring stay on ex63, reached over the
  tailnet. Tear down the VM's local API/MySQL/GUI/monitoring stack. Chosen because
  a full standalone redeploy is "not optimal" and the VM has no GPU + only 16
  cores — its CPU should go to browsers, not a redundant control plane.
- **Old data: preserve before teardown.** The VM's local DB is a *different*,
  completed study ("Google AI Detection", dormant since 2026-02-12). Not
  throwaway — `mysqldump` it before removing anything (see "Pending").

## The two machines

| | ex63 | new VM |
|---|---|---|
| tailnet name / IP | `ex63` / `<ex63-tailnet-ip>` (FQDN `ex63.tailec055e.ts.net`) | `primemover-switch-roland` / `100.71.54.93` |
| role | **live system of record** — API, MySQL, MinIO (`s3.qamelab.org`), monitoring, one runner | to become **runner-only** |
| ops user | `primemover` (home `/home/primemover`) | **`primemover` just created** (home `/home/primemover`, currently empty); old stack lives under `/home/ubuntu` |
| GPU | Arrow Lake iGPU, VA-API decode working | **none** (VM) — software decode |
| specs | 20 cores / 62 GB | 16 cores / 31 GB / 62 GB free |

`primemover` on the VM was created `--disabled-password` and given **passwordless
sudo** (or a password — confirm which; if `sudo` prompts and fails, that step was
not completed). Its `authorized_keys` was copied from `ubuntu`.

## Access (why this handoff exists)

ex63 is a tagged/server node and the **tailnet ACL blocks ex63 → any device on
port 22**, so the ex63 session could not SSH into the VM. `tailscale ping` works;
TCP 22 times out. The laptop is a user device and should reach both boxes.

**Operating model for the laptop instance:** run Claude Code on the laptop inside
a `primemover-orchestrator` checkout, and drive each box over SSH:
- `ssh primemover@<ex63-tailnet-ip>` (ex63) — should already work.
- `ssh primemover@100.71.54.93` (VM) — works if the laptop's key is in the VM's
  `authorized_keys` (it inherited `ubuntu`'s). Else `ssh ubuntu@100.71.54.93` and
  `sudo -iu primemover`.

**Verify both before starting.** If the VM's 22 is still unreachable from the
laptop too, the fix is on the VM: `sudo ufw` is inactive and sshd listens on
`0.0.0.0:22`, so it is purely the ACL — allow the laptop (or `*`) → VM:22 in the
Tailscale admin console.

The VM will also need, over the tailnet, **HTTPS to ex63's edge** for the shared
services (these are the runner's real dependencies, not SSH):
- `https://primemover.qamelab.org/api` — ex63's API (runner `backend.endpoint.api`)
- `https://s3.qamelab.org` — ex63's MinIO (runner `awsS3.endpoint`; job data)

These resolve (Cloudflare DNS-only) to ex63's tailscale IP and are tailnet-only.
Confirm `curl -sSI https://primemover.qamelab.org/api` and `curl -sSI
https://s3.qamelab.org` from the VM early — if they hang, the ACL also needs
VM → ex63:443, or DNS isn't resolving on the VM.

## What was already shipped this session (all merged to master)

- **runner #81** (`multilogin.h264ExtensionPath`) — the H.264 override extension
  path is now configurable. **Directly relevant here:** on a GPU-less VM the
  override reverts from redundant to load-bearing; if the repo is cloned to
  `/home/primemover/primemover_runner`, the default path resolves and no config is
  needed. If the home/path differs, set `multilogin.h264ExtensionPath`.
- **orchestrator #45** — docs: the "If the new box is a VM" section in
  `new-server-setup.md` (software decode returns; capacity is CPU-bound; don't add
  render/video groups or GL flags).
- ex63's runner jar was deployed to `4c66c4c0` (has the configurable folder id).
  **The VM does not need that jar** — it will build its own from master.

## New VM inventory (2026-09-02, under /home/ubuntu)

Old **full standalone stack**, all versions predating this session's fixes:
- **Running in Docker (~2 weeks up):** grafana, loki, promtail, prometheus, tempo.
- **Running in Docker (~13 days up):** primemover-apache, primemover-php8.1,
  mysql:8.0.25 (`127.0.0.1:3307`→3306), redis, memcached. API on `127.0.0.1:8090`.
- **Running (python, 13 days):** old local lambda server — **holds port 5001**
  (the new lambda will need it; stop the old one when cutting over).
- **VNC** `:1` via systemd `vncserver@1` (active).
- **NOT running:** the Java runner; any Multilogin/mlx process.
- **No MinIO** on this box at all.
- Checkouts (old): orchestrator `b07312f`, runner `b19723c`, api `a53e0f5`,
  gui `810a4bf`, bot-decision-yt `5409e7c`.
- **Toolchain: Java 21** (runner needs **25** — must upgrade), Python 3.12.3,
  git 2.43, **no Node**. Multilogin at `/opt/mlx` (verify version + login).
- `/home/primemover` is empty.

## Old DB findings (dormant — back up, don't discard)

DB `primemover` (user `primemover`), in container `primemover-mysql`:
- 3 users: `admin@primemover.example` (id 1), `researcher@primemover.example` (2), `runner@primemover.example` (3).
- 33 experiments, 77 crawlers, 671 queues, 2,375 jobs, 5,515 reports.
- All recent experiments are **"Google AI Detection"** (Feb 2026) — a different
  study from ex63's YouTube personalization work.
- **Newest job activity 2026-02-12** (~7 months stale). Nothing active depends on it.
- The 5,515 `reports` are mostly pointers; the actual HAR/page bytes are **not**
  on MinIO (there is none here) — locate them before wiping (see Pending #2).

## Pending immediately (start here)

1. **Dump the old DB** (non-destructive; do regardless):
   ```bash
   cd /home/ubuntu/primemover_api
   P=$(awk -F= '/^DB_PASSWORD=/{gsub(/["\r]/,"",$2);print $2}' .env)
   TS=$(date +%Y%m%d)
   sudo docker exec -e MYSQL_PWD="$P" primemover-mysql \
     mysqldump -uprimemover --databases primemover --single-transaction \
     > ~/primemover-switch-roland-db-$TS.sql
   ls -lh ~/primemover-switch-roland-db-$TS.sql
   ```
   Consider copying the dump off-box (e.g. to ex63) for safety.
2. **Locate the old job-data bytes** (the report pointers) so they are not lost on
   teardown:
   ```bash
   cd /home/ubuntu/primemover_api
   P=$(awk -F= '/^DB_PASSWORD=/{gsub(/["\r]/,"",$2);print $2}' .env)
   sudo docker exec -e MYSQL_PWD="$P" -i primemover-mysql mysql -uprimemover primemover -N \
     -e "SELECT mimetype, COUNT(*), COALESCE(SUM(LENGTH(filecontent)),0) FROM reports GROUP BY mimetype;"
   du -sh /home/ubuntu/primemover_runner/out 2>/dev/null
   find /home/ubuntu -maxdepth 3 -type d \( -iname 'har' -o -iname 'out' -o -iname 'reports' -o -iname 'finalsource' \) 2>/dev/null
   ```
   If `filecontent` holds the bytes, the mysqldump already captured them. If they
   are files on disk, archive that directory before teardown. If neither, they may
   already be gone — report and let Ulrich decide.

## The runner-only build plan (under /home/primemover)

Follow `docs/new-server-setup.md` "Adding a runner-only box" for the canonical
checklist. Build the new setup **alongside** the old (different home) — do not
tear anything down until the new runner is proven. Order:

1. **Prereqs:** install **Java 25** (JDK; box has 21). Python 3.12 is fine. VNC is
   up. Verify Multilogin X is installed and **logged in** (it must be, for the
   runner). No render/video groups (no GPU).
2. **Checkouts** at current master under `/home/primemover`:
   `primemover-orchestrator`, `primemover_runner`, `bot-decision-yt`. (No GUI/API.)
   Clone the runner to exactly `/home/primemover/primemover_runner` so the h264
   extension default path resolves.
3. **Decision symlink** (relative): from the orchestrator root,
   `ln -s ../../../bot-decision-yt/bot-decision-EN orchestrator/lambda_service/decision`.
4. **Config:**
   - orchestrator `.env`: `JOBDATA_S3_*` (scoped read-only MinIO key on ex63),
     `JOBDATA_S3_ENDPOINT=https://s3.qamelab.org`, API creds, `GRAFANA_*` not
     needed (no local Grafana).
   - runner `application.properties`: `backend.endpoint.api=https://primemover.qamelab.org/api`,
     `lambda.mode=local`, `multilogin.defaultFolderId=<workspace folder>`,
     `multilogin.h264ExtensionPath` (defaults correctly if under /home/primemover).
   - runner `secrets.properties`: `multilogin.apiUser/apiPassword`,
     `awsS3.endpoint=https://s3.qamelab.org` + its key/secret for the runner's
     write path (bucket `primemoverrunner`).
   - decision function: `OPENAI_API_KEY` in the lambda unit's `EnvironmentFile`.
5. **Build the jar** with Java 25 in a throwaway clone (never build in the live
   working copy). One jar in `target/`.
6. **Work partitioning (hard requirement) -- READ THE CORRECTED STATUS.**
   `crawlers-unprocessed` has no atomic claim; two runners with overlapping
   visibility double-process every queue. **ex63's runner authenticates as
   `admin@primemover.example` (user 1), which IS a service account** (`service_account_ids
   = [1]`, re-verified 2026-09-02 -- an earlier note in this file's plan and in
   `new-server-setup.md` wrongly said "no service accounts"). It **bypasses
   isolation and sees every user's crawlers**. So adding the VM runner as a
   different regular user does **not** partition cleanly on its own -- ex63 would
   also run that user's queues. Before the VM runner goes live, pick one:
   - **(a)** remove `1` from `PM_SERVICE_ACCOUNT_IDS` on ex63 so its runner (user
     1) sees only user-1 experiments, then run the VM runner as a *distinct
     regular user* with its own experiments. Mind the shared-account consequences
     (CLI/MCP/e2e stop seeing everything; experiments owned by users 2/5/6 stop
     running unless a runner authenticates as them -- migrate ownership first).
     Full detail in `new-server-setup.md` "The hard requirement".
   - **(b)** add an atomic claim in the API (`UPDATE ... WHERE processing=0`
     returning affected rows, or `SELECT ... FOR UPDATE`). Does not exist today.

   Also decide **same vs separate Multilogin workspace** (same account shares the
   50 ops/min + ~1000-profile quota; separate workspace = its own folder id).
7. **systemd** (`sudo scripts/install-systemd-services.sh`) — units already assume
   `/home/primemover`. Enable `primemover-runner`, `primemover-lambda`,
   `multilogin-*` only (no api/gui/monitoring). Free **port 5001** from the old
   lambda first.
8. **Verify:** `orchestrator doctor` (expects API as *remote*, not local — that is
   correct here), then a small smoke run. Capacity is **CPU-bound** on this
   software-decode VM — do not reuse ex63's ~15/~30-40 numbers; measure with
   `scripts/monitor-memory.sh` + CPU.
9. **Decommission the old /home/ubuntu stack** (only after the new runner is
   proven and the data is preserved): `docker compose down` the old api + monitoring
   stacks, stop the old lambda, retire the old checkouts. Keep VNC/Multilogin if the
   new setup reuses them, else migrate cleanly.

## Watch-outs specific to this box

- **No GPU** → software decode → the H.264 override extension is load-bearing;
  the renderer-death mitigations (heap cap + recycle) ship in the jar and travel.
- **Java 21 is too old** for the current runner — upgrade to 25 before building.
- **Port 5001** is held by the old lambda — free it at cutover.
- **The old stack is still serving** (API up 13 days) — nothing depends on it
  (runner not running, DB stale 7 months), but confirm before teardown.
- The old checkouts are under `/home/ubuntu`; the new ones go under
  `/home/primemover`. Don't mix them.

## Status 2026-09-02 (laptop session) — build done, smoke test in progress

Everything up to the smoke test is done; the old `/home/ubuntu` stack is still
present (not torn down). Access: laptop SSH aliases `pm-vm` (primemover), `pm-vm-ubuntu`,
`ex63` — tailnet only. `primemover` on the VM has passwordless sudo now.

**Preserved before anything was touched** (on ex63, `~/backups/primemover-switch-roland/`,
sha256 verified): `primemover-switch-roland-db-20260902.sql` (12 MB, 25 tables; the
"PROCESS privilege" warning is tablespace metadata only) and
`primemover-switch-roland-jobdata-20260902.tar` (641 MB, 5,514 files = the 5,514
`primemover/s3` report pointers, which live in
`/home/ubuntu/primemover_runner/primemover-runner/out/har/<job_id>/`, not in the DB).

**Built under `/home/primemover`:** Java 25 (apt, default), the three checkouts at
ex63's commits (orchestrator `73ec587`, runner `1cea95f`, bot-decision-yt `93304d7`),
decision symlink, venv (+ `langdetect`, `lxml`; the decision function's pinned
`requirements.txt` does not install on Python 3.12 and is not needed), runner jar
2.2.1 built in `~/build/` and copied to `target/`, Multilogin X **12.10.1** installed
from ex63's own deb (`/opt/mlxapp`). Config: `secrets.properties` carries the
host-specific overrides (edge endpoints, `awsS3.endpoint`, `runner.MaxParallelExecution=4`
to start) plus the copied secrets; orchestrator `.env` points at the edge with the
read-only MinIO key. `E2E_PROXY_PORT` must not be blank (pydantic int) — delete the
line rather than leave it empty.

**systemd:** `vncserver@1` switched from `ubuntu` to `primemover` (localhost-only, tunnel
in with `ssh -L 5901:127.0.0.1:5901 pm-vm`; password given to Ulrich in-session),
`multilogin-desktop`, `primemover-lambda` (old ubuntu lambda on 5001 killed first),
`primemover-runner` all enabled and active. `multilogin-agent` and
`primemover-health-check` deliberately disabled (v12 has no agent; the health check
probes local Grafana/Loki that do not exist here). Multilogin login was done with
`scripts/multilogin-login.py --env` (fixed this session to read `secrets.properties`
too) — no VNC login was needed.

**Partitioning (step 6) — done, option (a):**
- Experiments 42 (user 5) and 143 (user 6) reassigned to user 1 on ex63
  (`UPDATE experiments SET user_id=1`); only the seed experiment 1 (user 2) stays stranded.
- `PM_SERVICE_ACCOUNT_IDS=` (empty) in ex63's API `.env` (backup `.env.bak-*`),
  `config:clear` run; effective config verified `{"enabled":true,"service_account_ids":[]}`.
- VM runner identity: **existing ex63 user 3 `runner@primemover.example` ("Primemover Runner")**,
  password reset in-session; also the VM orchestrator's `PM_API_EMAIL`. So anything the
  VM `orchestrator e2e` creates is owned by user 3 and runs only on the VM.
- Verified: VM runner picked up queue 960 (YouTube e2e); ex63's runner log has zero
  mentions of it and keeps polling.

*Superseded — see "Partitioning, second iteration" below.*

**Smoke test passed (2026-09-02 14:48):** `orchestrator e2e --youtube` run from the VM
(experiment 156, queue 960, owner user 3): 20/20 jobs `success` incl. 3
`lambdasingleselectjob` via the VM's local lambda, 328 s; HAR + lambda jobdata for job
33947 readable back from ex63's MinIO (`orchestrator debug job`). ex63's runner never
saw the queue. Old `/home/ubuntu` stack decommissioned the same afternoon: both compose
projects (`primemover`, `primemover-orchestrator`) `down` **without** `--volumes`; the
old checkouts, MySQL bind-mount data, monitoring volumes, DB dump and job-data tar are
still on the VM's disk under `/home/ubuntu`.

**Partitioning, second iteration (2026-09-02, after the smoke test):** owner-based
partitioning made researchers' own experiments unrunnable, so it was replaced by an
explicit `experiments.runner_user_id` (API PR "Runner assignment"). After that
rollout: `PM_SERVICE_ACCOUNT_IDS=1,3`, `PM_DEFAULT_RUNNER_USER_ID=1`, experiments 42
and 143 returned to users 5 and 6. Assign an experiment to the VM with
`runner_user_id=3`; everything else runs on ex63.

**Logs (2026-09-02 late):** the VM's runner + lambda logs now ship to ex63's Loki with
`host=primemover-switch-roland` (Promtail container on the VM, Loki bound on ex63's tailnet
address via a gitignored compose override; see `new-server-setup.md` "Logs from a
runner-only box"). The MinIO->Nextcloud archiver is NOT scheduled on ex63 and its
`infrastructure/har-drain/rclone.conf` still holds template placeholders; MinIO is the only
copy of all job data until that is done.

**GUI (2026-09-03):** the experiment wizard at `/experiments/create` (the only creation
path the GUI routes to; the old standalone create component is unrouted) has a "Runs on"
select on its "The study" step, fed by `GET /api/v1/runners` (API #60) and preselecting the
default. Verified end to end under a researcher's own login: EXP-163 stored runner 1
(ex63), EXP-164 stored runner 3 (VM). ex63 pins the GUI image in
`/opt/ex63-infra/compose/apps.yml` (`sha-593a027` as of this note). Runner display names:
user 1 "Administrator (ex63 runner, default)", user 3 "Switch VM (Roland) runner".
VM `runner.MaxParallelExecution` is 10 (soak: 4 concurrent bots used ~19% CPU avg, 61%
peak, 1.85 GB RAM per browser; ~10-12 is the likely ceiling). Test experiments 157-164
were deleted on 2026-09-03; their MinIO objects remain (nothing references them).

**Still open:** capacity measurement (single bot: ~1.6 GB PSS browser, CPU is the
limit — measure with several bots before raising `runner.MaxParallelExecution` from 4);
optional read-only GitHub deploy key for `primemover@primemover-switch-roland`
(key in `~/.ssh/id_ed25519.pub` on the VM; until then resync checkouts from the laptop
with rsync); Multilogin workspace is shared with ex63 (same folder id) — stagger
launches; delete `/home/ubuntu`'s old checkouts/volumes when Ulrich decides.
