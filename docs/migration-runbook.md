# PrimeMover migration runbook — Hetzner VM → new bare-metal server

Migrate the full PrimeMover setup from the current cloud VM (`hetzner-primemover`,
"OLD") to the new bare-metal server ("NEW"), then decommission OLD.

**Chosen strategy (2026-08-12):**
- **Storage: staged.** Migrate pointing at the existing **AWS S3** first; prove the
  whole setup works on NEW hardware; **then** flip to MinIO as a separate step.
- **Database: mysqldump → restore, with a short downtime window.**
- **Networking: move DNS** — repoint `hetzner-primemover` to NEW's IP at cutover.
- **Execution:** operator (you) runs every step; this runbook is the source of truth.

Legend: `[OLD]` run on the current server, `[NEW]` on the new server, `[LAPTOP]`
on your machine, `[DNS]` at your DNS provider.

---

## What is in git vs. what must be hand-carried

**In git (just `git clone` on NEW):** all code in five repos, `application.properties`
(now secret-free), `mainstream_media_channels.csv`, the systemd units, the spool-and-drain
sidecar, docs. Branches currently deployed on OLD (verify in step 0):

| repo | branch |
|------|--------|
| primemover-orchestrator | `feature/har-spool-drain` |
| primemover_api | `integration/gui-redesign` |
| primemover_gui | `integration/gui-redesign` |
| primemover_runner | `feature/local-lambda-server` |
| bot-decision-yt | `feature/local-lambda-server` |

**NOT in git — must be transferred (the whole point of the secrets externalization):**
- `primemover-orchestrator/.env`
- `primemover_api/.env`  (contains `PASSPORT_PASSWORD_CLIENT_ID/_SECRET`, DB creds, Grafana pw)
- `primemover_runner/primemover-runner/config/secrets.properties`  (backend/multilogin/recorder/geosurf/anticaptcha/awsS3 creds)
- `~/.aws/credentials` (+ `~/.aws/config`)
- the MySQL database (dump/restore)
- recorder cert `primemover_runner/primemover-runner/cert/2024/ca-cert.p12` **only if it turns out to be gitignored** (verify in step 4)

**Regenerated on NEW, never copied:** the SSH key.

---

## Phase 0 — Pre-flight `[OLD]` / `[DNS]`

0.1 Record the exact deployed branch + commit of each repo (paste into a note):
```bash
for d in primemover-orchestrator primemover_api primemover_gui primemover_runner bot-decision-yt; do
  echo "$d: $(git -C ~/$d branch --show-current) @ $(git -C ~/$d rev-parse --short HEAD)"
done
```

0.2 Confirm all commits are pushed (nothing stranded on OLD):
```bash
for d in primemover-orchestrator primemover_api primemover_gui primemover_runner bot-decision-yt; do
  echo "== $d =="; git -C ~/$d status --short; git -C ~/$d log --oneline @{u}..HEAD 2>/dev/null
done
```
Anything listed as unpushed or modified must be committed+pushed first (e.g. the API auth
fix `AuthController.php` + `config/services.php`, if not yet done), or it will be lost.

0.3 `[DNS]` Lower the TTL on the `hetzner-primemover` record to 300s (or less) **now**, so
the later cutover propagates fast. (DNS changes take one TTL to take effect, so do this early.)

0.4 Note the MLX/Multilogin account login (email + the password in `secrets.properties`
`multilogin.apiPassword`) — you'll re-login interactively on NEW.

---

## Phase 1 — MinIO + object store, standalone `[NEW]`

Do this first and **prove it in isolation** — it's independent of PrimeMover and de-risks
phase 5. (You said MinIO + other services are the AWS replacement.)

1.1 Install MinIO (container or binary). Create a service account / access key. Bring it up
behind the firewall; expose only where needed.

1.2 Create the bucket and prefixes PrimeMover expects (names are fixed in the runner config):
```bash
mc alias set local http://127.0.0.1:9000 <ACCESS_KEY> <SECRET_KEY>
mc mb local/primemoverrunner
# prefixes are created lazily by the first object; optional to pre-create:
mc cp /etc/hostname local/primemoverrunner/reports/.keep
mc cp /etc/hostname local/primemoverrunner/finalsource/.keep
```

1.3 Verify put/get/list works with `mc`. **Stop here** — PrimeMover will not point at MinIO
until phase 5. Leave AWS creds untouched for now.

---

## Phase 2 — Provision NEW `[NEW]`

Match the OLD environment: same user (`primemover`), same home layout (`/home/primemover/...`),
so the absolute paths in `.env` (`PM_API_PATH`, `PM_*_LOG_PATH`) transfer unchanged.

2.1 Base packages: Docker + compose plugin, `git`, `python3.10+` + venv, JDK + Maven,
`node` 18.19+ + `npm`, TigerVNC (`vncserver@1`), `rclone` + `zstd` (for the sidecar),
`mysql-client` (for restore), `awscli` optional.

2.2 Firewall: SSH-only inbound + whatever MinIO/monitoring you deliberately expose. This is
the post-incident hardening — do **not** skip it. MySQL/Redis/Grafana stay localhost-bound.

2.3 Generate a fresh SSH key and add it to GitHub (account key), then verify:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N '' -C "primemover-$(hostname)"
# add ~/.ssh/id_ed25519.pub to GitHub, then:
ssh -T git@github.com   # expect "Hi umatter!"
```

2.4 Start the VNC display the runner/MLX need:
```bash
sudo systemctl enable --now vncserver@1
```

---

## Phase 3 — Clone + build `[NEW]`

3.1 Clone all five repos into `/home/primemover/`, each on the branch from step 0.1:
```bash
cd ~
for r in primemover-orchestrator primemover_api primemover_gui primemover_runner bot-decision-yt; do
  git clone git@github.com:umatter/$r.git
done
git -C ~/primemover-orchestrator checkout feature/har-spool-drain
git -C ~/primemover_api          checkout integration/gui-redesign
git -C ~/primemover_gui          checkout integration/gui-redesign
git -C ~/primemover_runner       checkout feature/local-lambda-server
git -C ~/bot-decision-yt         checkout feature/local-lambda-server
```
(Confirm each `rev-parse --short HEAD` matches step 0.1.)

3.2 Recreate the local-Lambda symlink (a hard prerequisite):
```bash
ln -s /home/primemover/bot-decision-yt/bot-decision-EN \
      /home/primemover/primemover-orchestrator/orchestrator/lambda_service/decision
```

3.3 Orchestrator Python env:
```bash
cd ~/primemover-orchestrator && python3 -m venv venv && ./venv/bin/pip install -e ".[dev]"
```

3.4 Build the runner:
```bash
cd ~/primemover_runner && mvn -q -pl primemover-runner -am -DskipTests package
```

3.5 GUI deps:
```bash
cd ~/primemover_gui && npm install
```

3.6 **Before any `docker compose up`**, verify the API compose is the hardened one (localhost
binds, no empty root pw) — same check CLAUDE.md mandates:
```bash
grep -nE '127\.0\.0\.1:.*(3307|8889)|ALLOW_EMPTY' ~/primemover_api/docker-compose.yml
```
Expect MySQL/Redis bound to `127.0.0.1` and **no** `ALLOW_EMPTY_PASSWORD`.

---

## Phase 4 — Transfer secrets `[LAPTOP]` / `[NEW]`

Copy over SSH (scp/rsync); never paste secrets into chat/tickets. From LAPTOP, hop OLD→NEW,
or pull each file. The five targets:

```
~/primemover-orchestrator/.env
~/primemover_api/.env
~/primemover_runner/primemover-runner/config/secrets.properties
~/.aws/credentials   (and ~/.aws/config)
```

4.1 After copying, lock down perms:
```bash
chmod 600 ~/primemover-orchestrator/.env ~/primemover_api/.env \
          ~/primemover_runner/primemover-runner/config/secrets.properties \
          ~/.aws/credentials
```

4.2 Check the recorder cert came with the clone; if absent, it's gitignored — copy it too:
```bash
ls -l ~/primemover_runner/primemover-runner/cert/2024/ca-cert.p12 || echo "MISSING — scp from OLD"
```

4.3 Sanity-check paths inside the copied `.env` files still match NEW's filesystem (they
should, if the user/home match). Grep for any `hetzner-primemover` hostnames that need
updating — but if you're moving DNS (below), the hostname stays valid, so leave them.

---

## Phase 5 — Migrate the database (downtime window) `[OLD]` → `[NEW]`

Do this last-minute, close to cutover, so little new data accrues on OLD afterward.

5.1 `[OLD]` Stop experiment execution so the DB is quiescent:
```bash
cd ~/primemover-orchestrator && ./venv/bin/orchestrator restart --runner   # or stop the runner
# confirm no active/overdue queues will fire; deactivate if needed
```

5.2 `[OLD]` Dump (the DB runs in the `primemover-mysql` container):
```bash
DBU=$(grep ^DB_USERNAME ~/primemover_api/.env|cut -d= -f2); DBP=$(grep ^DB_PASSWORD ~/primemover_api/.env|cut -d= -f2); DBN=$(grep ^DB_DATABASE ~/primemover_api/.env|cut -d= -f2)
docker exec primemover-mysql mysqldump -u"$DBU" -p"$DBP" --single-transaction --routines "$DBN" > ~/pm-dump-$(date +%Y%m%d).sql
```

5.3 Copy the dump to NEW (scp).

5.4 `[NEW]` Bring up the DB container first (monitoring stack / API compose), then restore:
```bash
cd ~/primemover_api && docker compose up -d   # or via orchestrator; brings up mysql
DBU=...; DBP=...; DBN=...   # same values from NEW's copied .env
docker exec -i primemover-mysql mysql -u"$DBU" -p"$DBP" "$DBN" < ~/pm-dump-YYYYMMDD.sql
```

5.5 `[NEW]` Verify row counts match OLD (experiments/crawlers/jobs/oauth_clients):
```bash
docker exec primemover-mysql mysql -u"$DBU" -p"$DBP" "$DBN" -e \
 "SELECT 'experiments',COUNT(*) FROM experiments UNION SELECT 'crawlers',COUNT(*) FROM crawlers UNION SELECT 'jobs',COUNT(*) FROM jobs UNION SELECT 'oauth_clients',COUNT(*) FROM oauth_clients;"
```
`oauth_clients` must include the password-grant client (id 2) — that's what makes the GUI
login work with the `.env` `PASSPORT_PASSWORD_CLIENT_SECRET` you copied.

5.6 `[NEW]` Run pending migrations (the merge added an index migration; idempotent if already in the dump):
```bash
docker exec primemover-php8.1 php artisan migrate --force
docker exec primemover-php8.1 php artisan config:clear
```

---

## Phase 6 — Bring up the stack + verify, still on AWS `[NEW]`

6.1 Start everything:
```bash
cd ~/primemover-orchestrator && ./venv/bin/orchestrator up
./venv/bin/orchestrator status -v
```

6.2 Install systemd units (lambda, health-check, + optionally har-drain, gui):
```bash
sudo ~/primemover-orchestrator/scripts/install-systemd-services.sh
# on NEW, `enable` takes effect at next boot; start now if wanted.
```

6.3 Verify API + MCP + login work (still against AWS S3):
```bash
./venv/bin/orchestrator test           # e2e without Multilogin, no browser
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8090/api/v1/login \
  -H 'Content-Type: application/json' -d '{"email":"admin@primemover.example","password":"<PM_API_PASSWORD>"}'   # expect 200
```

---

## Phase 7 — Multilogin `[NEW]` (via VNC)

7.1 Install Multilogin X to match OLD (`/opt/mlx`, `/opt/mlxapp`, `~/mlx`).
7.2 `[LAPTOP]` VNC-tunnel in (`ssh -L 5901:127.0.0.1:5901 ...`), open the MLX desktop, log in
interactively with the account from step 0.4.
7.3 Enable the units (they auto-start on the **next** boot — unlike the enable-after-boot gap
seen on OLD):
```bash
sudo systemctl enable --now multilogin-agent multilogin-desktop
```
7.4 Confirm launcher ports 45000–45003 are listening and `agent_YYYYMMDD.log` is fresh.

---

## Phase 8 — Full end-to-end on AWS `[NEW]`

The go/no-go gate before touching storage or DNS.
```bash
cd ~/primemover-orchestrator
./venv/bin/orchestrator e2e --chrome     # recorder + MITM + storage, no MLX
./venv/bin/orchestrator e2e --youtube    # MLX + residential proxy + Lambda video selection
```
Expect jobs `success`, HAR/jobdata in `s3://primemoverrunner/{reports,finalsource}/`, and
Lambda selections succeeding. If this passes, NEW is a faithful clone on known-good storage.

---

## Phase 9 — Flip storage AWS → MinIO `[NEW]`

Only after phase 8 is green. This is the one code change we scoped but never made:
`AwsS3StorageService.init()` hardcodes AWS (`Region.EU_CENTRAL_1`, no endpoint override).

9.1 In `primemover_runner/.../service/AwsS3StorageService.java`, add to the `S3Client.builder()`:
```java
.endpointOverride(java.net.URI.create(endpoint))   // new: awsS3.endpoint
.forcePathStyle(true)                              // required for MinIO
.region(Region.of(regionName))                     // make region configurable too
```
Read `endpoint`/`regionName` from new `@Value("${awsS3.endpoint:}")` / `${awsS3.region:eu-central-1}`.
9.2 Add `awsS3.endpoint=http://127.0.0.1:9000` to `application.properties`, and put the MinIO
`awsS3.accessKey`/`secretKey` into `secrets.properties` (replacing the AWS ones).
9.3 Rebuild the runner (phase 3.4), restart, re-run `orchestrator e2e --chrome`.
9.4 Verify the new objects land in **MinIO** (`mc ls local/primemoverrunner/reports/`), not AWS.
9.5 Commit the endpoint-override change on `feature/local-lambda-server` and push.

> Keep AWS creds around read-only until you've confirmed MinIO holds everything you need; the
> historical HAR objects still live in the AWS bucket unless you separately migrate them.

---

## Phase 10 — DNS cutover `[DNS]`

10.1 Final quiescence on OLD: stop the runner, take a **final** DB dump (5.2) as the
authoritative copy, restore onto NEW (5.4–5.6) so no data is lost since the trial restore.
10.2 `[DNS]` Repoint `hetzner-primemover` A record to NEW's IP. With the 300s TTL from step
0.3, clients follow within minutes.
10.3 Verify from LAPTOP that the hostname now resolves to NEW and login/e2e work end to end.

---

## Phase 11 — Decommission OLD `[OLD]`

Only after NEW has run clean for a burn-in period (a day+ of real use):
11.1 Keep the final DB dump + the copied secret files archived somewhere safe (off both servers).
11.2 Revoke anything OLD-specific: its SSH deploy key on GitHub; and the still-outstanding PAT
`ghp_YOIz…` if not already revoked.
11.3 Stop services, confirm nothing points at OLD anymore, then release the Hetzner VM.

---

## Rollback

Until phase 10, OLD is untouched and authoritative — rollback is "keep using OLD." After DNS
cutover, rollback = repoint DNS back to OLD (still fast with the low TTL) and re-dump NEW→OLD
if any data was written to NEW. Do not release OLD until you're certain.

## Gotchas carried over from this deployment
- Reaching the GUI needs **both** tunnels: `-L 4200:127.0.0.1:4200 -L 8090:127.0.0.1:8090`.
- `storage.local.enabled=false` means job data is S3/MinIO-only (no `out/har` growth) — intended.
- `recorder.enabled=true` produces HAR; its cert is valid to 2034.
- Always run `docker compose` from a repo root (relative bind-mount trap in CLAUDE.md).
- Systemd `enable` only takes effect at next boot — start units manually if you need them before rebooting.
