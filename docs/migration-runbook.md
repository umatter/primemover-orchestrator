# PrimeMover migration runbook — Hetzner VM → new bare-metal server

Migrate the full PrimeMover setup from the current cloud VM (`hetzner-primemover`,
"OLD") to the new bare-metal server ("NEW"), then decommission OLD.

**Chosen strategy (2026-08-12):**
- **Storage: staged.** Migrate pointing at the existing **AWS S3** first; prove the
  whole setup works on NEW hardware; **then** flip to MinIO as a separate step.
- **Database: mysqldump → restore, with a short downtime window.**
- **Networking: move the Tailscale MagicDNS name** — `hetzner-primemover` is a tailnet
  machine name, not a public DNS record (verified 2026-08-12: no A record anywhere, no
  config file references it; everything on-box uses `localhost`). Cutover = rename the
  machines in the Tailscale admin console.
- **Execution:** operator (you) runs every step; this runbook is the source of truth.

Legend: `[OLD]` run on the current server, `[NEW]` on the new server, `[LAPTOP]`
on your machine, `[TS]` in the Tailscale admin console (login.tailscale.com).

---

## What is in git vs. what must be hand-carried

**In git (just `git clone` on NEW):** all code in five repos, `application.properties`
(now secret-free), `mainstream_media_channels.csv`, the systemd units, the spool-and-drain
sidecar, docs. Branches deployed on OLD, **pinned 2026-08-12 after pre-flight** (phase 0
done: all repos clean, everything pushed — incl. the API login fix, the Telescope
migration, the GUI :8090 dev pointer, and bot-decision-yt's YouTube XPath fix):

| repo | branch | commit |
|------|--------|--------|
| primemover-orchestrator | `feature/har-spool-drain` | `f6507c4` |
| primemover_api | `integration/gui-redesign` | `2adf76e` |
| primemover_gui | `integration/gui-redesign` | `b7c9eed` |
| primemover_runner | `feature/local-lambda-server` | `cadb15e` |
| bot-decision-yt | `feature/local-lambda-server` | `fef78b8` |

None of this is on `master`/`main` — cloning defaults gives a pre-redesign stack without
the MCP write tools. The orchestrator branch fully contains `integration/gui-redesign`;
do not check that branch out separately.

**NOT in git — must be transferred (the whole point of the secrets externalization):**
- `primemover-orchestrator/.env`
- `primemover_api/.env`  (contains `PASSPORT_PASSWORD_CLIENT_ID/_SECRET`, DB creds, Grafana pw)
- `primemover_runner/primemover-runner/config/secrets.properties`  (backend/multilogin/recorder/geosurf/anticaptcha/awsS3 creds)
- `~/.aws/credentials` (+ `~/.aws/config`)
- the MySQL database (dump/restore)
- recorder cert `primemover_runner/primemover-runner/cert/2024/ca-cert.p12` **only if it turns out to be gitignored** (verify in step 4)

**Regenerated on NEW, never copied:** the SSH key.

---

## Phase 0 — Pre-flight `[OLD]` / `[TS]`

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

0.3 `[NEW]` / `[TS]` Join NEW to the tailnet **now** (`tailscale up` with the same account)
and, in the admin console, disable key expiry on it. No TTL to lower — MagicDNS renames
propagate to tailnet clients within seconds, but NEW must already be on the tailnet before
the phase-10 rename can happen. Give it a distinct name for now (e.g. `pm-new`).

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

3.1 Clone all five repos, each on the pinned branch. **Chosen layout on NEW (2026-08-12):
repos live in `/opt/primemover/`, symlinked into `/home/primemover/`** — the systemd units
and ops scripts hardcode `/home/primemover/...`, and the sibling layout is load-bearing
(`.env` uses relative `../primemover_*` paths):
```bash
sudo mkdir -p /opt/primemover && sudo chown primemover:primemover /opt/primemover
cd /opt/primemover   # clone the five repos here, then:
for r in primemover-orchestrator primemover_api primemover_gui primemover_runner bot-decision-yt; do
  ln -s /opt/primemover/$r /home/primemover/$r
done
```
All later `~/...` commands in this runbook then work unchanged (Docker bind mounts follow
symlinks). Original layout, if preferred, was:
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
(Confirm each `rev-parse --short HEAD` matches the pinned-commit table at the top of this
runbook. A *newer* commit on the same branch is fine if it was pushed deliberately since
2026-08-12 — anything else, stop and investigate.)

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

3.5b API PHP deps — `vendor/` is gitignored, so a fresh clone can't run artisan until
Composer has run **inside the php container** (needs the API compose up first, phase 5.4;
discovered the hard way during the 2026-08-13 trial restore):
```bash
docker exec primemover-php8.1 composer install
docker exec primemover-php8.1 chown -R $(id -u):$(id -g) /var/www/html/vendor
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
~/primemover_api/storage/oauth-private.key   (Passport signing keys, gitignored —
~/primemover_api/storage/oauth-public.key     without them every login 500s)
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

7.1 Install the Multilogin X **desktop app** deb (`sudo apt install -y ./desktop-multiloginx-*.deb`).
The 24.04 build installs fine on Ubuntu 26. Since app v12 the agent is **embedded in
`desktop.bin`** — there is no separate `mlx` agent package and `/opt/mlx/agent.bin` never
appears (on OLD, app v11 had auto-installed it as a separate dpkg).
7.2 `[LAPTOP]` VNC-tunnel in (`ssh -L 5901:127.0.0.1:5901 ...`), launch `/opt/mlxapp/desktop.bin`
in the XFCE session, log in interactively with the account from step 0.4, then quit the app.
7.3 Enable **only the desktop unit** (the legacy `multilogin-agent.service` points at the
nonexistent `/opt/mlx/agent.bin` and must stay disabled on a v12 install):
```bash
sudo systemctl enable --now multilogin-desktop
```
7.4 Confirm launcher ports 45000–45003 are listening, owned by `desktop.bin`
(`ss -tlnp | grep -E ':4500[0-3]'`). They bind all interfaces — the SSH-only firewall is
what keeps them private.

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
The change below is **backward compatible**: with `awsS3.endpoint` blank it behaves exactly
as today (real AWS), so you can commit it without changing OLD's behaviour, and MinIO is
enabled purely by setting the endpoint.

### 9.1 Code — `primemover_runner/primemover-runner/src/main/java/primemover/service/AwsS3StorageService.java`

**Add two `@Value` fields** next to the existing `accessKey`/`secretKey` (around line 34):
```java
	@Value("${awsS3.accessKey}")
	private String accessKey;
	@Value("${awsS3.secretKey}")
	private String secretKey;
	// New: blank => real AWS (eu-central-1) exactly as before; set => S3-compatible store (MinIO).
	@Value("${awsS3.endpoint:}")
	private String endpoint;
	@Value("${awsS3.region:eu-central-1}")
	private String region;
```

**Replace the client build in `init()`** (the `try` block around lines 52-55):
```java
		// BEFORE
		AwsBasicCredentials awsCreds = AwsBasicCredentials.create(accessKey, secretKey);
		client = S3Client.builder().region(Region.EU_CENTRAL_1)
				.credentialsProvider(StaticCredentialsProvider.create(awsCreds)).build();
```
```java
		// AFTER
		AwsBasicCredentials awsCreds = AwsBasicCredentials.create(accessKey, secretKey);
		software.amazon.awssdk.services.s3.S3ClientBuilder builder = S3Client.builder()
				.region(Region.of(region))
				.credentialsProvider(StaticCredentialsProvider.create(awsCreds));
		if (endpoint != null && !endpoint.isBlank()) {
			// MinIO / self-hosted: custom endpoint + path-style addressing
			// (virtual-host style needs per-bucket DNS, which MinIO doesn't provide).
			builder = builder.endpointOverride(java.net.URI.create(endpoint)).forcePathStyle(true);
		}
		client = builder.build();
```
No new import lines are required (`Region` and `S3Client` are already imported;
`URI` and `S3ClientBuilder` are fully-qualified inline above).

### 9.2 Config
- `application.properties` (tracked, non-secret): add
  ```
  awsS3.endpoint=http://127.0.0.1:9000
  awsS3.region=us-east-1
  ```
  (MinIO ignores region but the SDK requires a valid one; `us-east-1` is the conventional
  default. Leave `awsS3.endpoint` blank/absent to keep using AWS.)
- `secrets.properties` (gitignored): replace `awsS3.accessKey` / `awsS3.secretKey` with the
  **MinIO** access key / secret from phase 1.1.

### 9.3–9.6 Build, verify, commit
```bash
cd ~/primemover_runner && mvn -q -pl primemover-runner -am -DskipTests package   # rebuild
cd ~/primemover-orchestrator && ./venv/bin/orchestrator restart --runner
./venv/bin/orchestrator e2e --chrome                                             # re-run
mc ls local/primemoverrunner/reports/ | tail                                    # objects now in MinIO
```
Confirm the run's jobdata/finalsource keys appear in **MinIO**, not AWS. Then commit the
Java + `application.properties` change on `feature/local-lambda-server` and push (it's
backward compatible, so safe to land regardless of which server uses which backend).

> Keep AWS creds around read-only until you've confirmed MinIO holds everything you need; the
> historical HAR objects still live in the AWS bucket unless you separately migrate them
> (`mc mirror` / `rclone sync` AWS→MinIO if you need the back-catalogue).

---

## Phase 10 — Final cutover `[OLD]` → `[NEW]`

**Decision 2026-08-19: skip the MagicDNS rename.** Grepped `primemover_api`,
`primemover_runner`, `primemover-orchestrator`, `bot-decision-yt`, all `.env` files, the
`oauth_clients` table, and Laravel's `cors.php`/`session.php` — nothing references
`hetzner-primemover` anywhere (`APP_URL`, both oauth redirect URIs, `SESSION_DOMAIN` are
all `localhost`-based; confirms phase 0's original finding). The hostname is purely an
access convenience for whoever/whatever connects in — retiring OLD and switching to
NEW's own tailnet identity costs nothing at the application layer, just some SSH
configs/bookmarks to update by hand.

Also note: `ex63` (NEW) and OLD currently can't see each other as tailnet peers at all
(`tailscale ping` → "no matching peer" from NEW's side) — worth resolving for general
tailnet hygiene, but no longer a blocker for cutover, since this approach doesn't
require them to see each other.

10.1 Final quiescence on OLD: stop the runner, take a **final** DB dump (5.2) as the
authoritative copy, restore onto NEW (5.4–5.6). An interim (non-quiescent) dump/restore
was already done 2026-08-19 as a warm-up (36 experiments/36 crawlers/1167 jobs landed on
NEW) — this step is just the small delta since then.
**Gate: do not do this until OLD's current testing has wrapped up** — interrupting it is
explicitly out of scope right now.
10.2 Switch access: update your own SSH config/bookmarks/muscle memory (and tell anyone
else who connects this way) from `hetzner-primemover` to `ex63` /
`ex63.tailec055e.ts.net`. No app config, DNS, or Tailscale admin-console change needed.
10.3 Verify from LAPTOP: ssh to the new name, confirm login/e2e work end to end against NEW.

---

## Phase 11 — Decommission OLD `[OLD]`

Only after NEW has run clean for a burn-in period (a day+ of real use):
11.1 Keep the final DB dump + the copied secret files archived somewhere safe (off both servers).
11.2 Revoke anything OLD-specific: its SSH deploy key on GitHub; and the still-outstanding PAT
`ghp_YOIz…` if not already revoked.
11.3 Stop services, confirm nothing points at OLD anymore, then release the Hetzner VM.

---

## Rollback

Until phase 10, OLD is untouched and authoritative — rollback is "keep using OLD." After
cutover, rollback = go back to connecting to OLD by its existing name/IP (nothing was
renamed, so this is just reverting your own access habits) and re-dump NEW→OLD if any
data was written to NEW. Do not release OLD until you're certain.

## Gotchas carried over from this deployment
- Reaching the GUI needs **both** tunnels: `-L 4200:127.0.0.1:4200 -L 8090:127.0.0.1:8090`.
- `storage.local.enabled=false` means job data is S3/MinIO-only (no `out/har` growth) — intended.
- `recorder.enabled=true` produces HAR; its cert is valid to 2034.
- Always run `docker compose` from a repo root (relative bind-mount trap in CLAUDE.md).
- Systemd `enable` only takes effect at next boot — start units manually if you need them before rebooting.
