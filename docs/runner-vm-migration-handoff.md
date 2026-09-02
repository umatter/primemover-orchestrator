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
6. **Work partitioning (hard requirement).** `crawlers-unprocessed` has no atomic
   claim; two runners on the same API+visibility double-process every queue. ex63's
   runner authenticates as `admin@primemover.example` (user id 1) with isolation **on**, so
   it sees only user 1's experiments. **The VM runner must authenticate as a
   different ex63 user** with its own experiments (create one via the API, or reuse
   `ps@`/`runner@primemover.example` if appropriate). Confirm on ex63 that isolation is still
   enabled and no service account sees everything. Decide **same vs separate
   Multilogin workspace** (same account shares the 50 ops/min + ~1000-profile
   quota; separate workspace = its own folder id).
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
