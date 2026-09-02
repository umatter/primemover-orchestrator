# Standing up PrimeMover on a new server

The ordered checklist for a fresh deployment. `orchestrator setup` automates the
mechanical part (clone → build → start); everything else here is provisioning
that cannot be automated because it involves external accounts and secrets.

Run `orchestrator doctor` at the end (and any time after) — it now preflights the
account-specific pieces (secrets set, decision symlink, job-data store
reachable), not just Docker/Java/ports.

## 0. Two kinds of "new"

- **Same account, new box** (e.g. rebuilding ex63): easiest. Reuse the existing
  Multilogin account, MinIO bucket, and folder id; you mainly re-provision
  secrets and re-run setup.
- **New account entirely**: additionally requires a new Multilogin workspace, its
  **folder id** (`multilogin.defaultFolderId`, see §4), a MinIO/S3 bucket, and an
  OpenAI key. Nothing needs a code change for this any more.

## 1. Host prerequisites

- Docker + docker-compose plugin, Java 25 (the runner runs on 25; poms compile at
  17), Python 3.10+.
- For real bot runs: a VNC display (`vncserver@1`, `DISPLAY=:1`) and Multilogin X
  desktop, which must be **logged in** — the runner inherits `DISPLAY=:1`.
- `orchestrator doctor` checks Docker/compose/Java/ports.

## 2. Clone + build

```bash
orchestrator setup            # clones the sibling repos, builds, starts
# or, if the repos already exist:
orchestrator setup --skip-clone
```

Sibling layout (all under one parent): `primemover-orchestrator`,
`primemover_api`, `primemover_runner`, `bot-decision-yt`, `primemover_gui`.

## 3. The decision symlink (local Lambda server)

The local Lambda server imports the decision function through a symlink; it
refuses to start without it. Recreate it **relative**, from the repo root:

```bash
ln -s ../../../bot-decision-yt/bot-decision-EN orchestrator/lambda_service/decision
```

Point it at `bot-decision-EN` specifically — `bot-decision-EN-arm` and
`bot-decision-DE` are stale forks.

## 4. Configuration & secrets

Copy `.env.example` to `.env` and fill in. Secrets with **no default** that block
the thing named:

| variable | needed for |
|---|---|
| `PM_API_EMAIL`, `PM_API_PASSWORD` | API login |
| `GRAFANA_ADMIN_PASSWORD` | Grafana won't start without it |
| `PM_MYSQL_PASSWORD`, `PM_REDIS_PASSWORD` | API datastores |
| `JOBDATA_S3_ACCESS_KEY`, `JOBDATA_S3_SECRET_KEY` | `debug job` / `lambda invocations` (see §5) |
| `E2E_PROXY_*` | e2e tests only |

Runner-side, in `primemover_runner/.../config/secrets.properties`:

- `multilogin.apiUser` / `multilogin.apiPassword` — the Multilogin account.
- `awsS3.endpoint` — the MinIO endpoint (overrides the empty value in
  `application.properties`). **This is not AWS** despite the `awsS3.*` names; it
  is the local/edge MinIO. Default on ex63 is `https://s3.qamelab.org`.

Runner `application.properties`:

- `multilogin.defaultFolderId` — the **account-specific** Multilogin workspace
  folder. A new account has a different id; set it here (no code change needed).
- `lambda.mode=local` to use the local Lambda server (default since runner PR #53).

Decision function: `OPENAI_API_KEY` in the Lambda service's environment (the
`primemover-lambda` systemd unit's `EnvironmentFile`, or the shell for a manual
run). Without it the LLM filters are skipped and every treated arm degrades.

## 5. The job-data (MinIO) reader key

Job data (HAR, page source, lambda payloads) goes to MinIO, and the orchestrator
reads it back through `JOBDATA_S3_*`. Mint a **scoped, read-only** key (never the
root credentials) — in the MinIO console, Access Keys → Create, restricted to
`GetObject`/`ListBucket` on the runner's bucket (`primemoverrunner`). Put it in
`.env`. Without it, `orchestrator debug job` and `lambda invocations` return
nothing silently rather than failing. See CLAUDE.md "Where job data goes".

## 6. Proxies (real runs)

A study needs ~one stable exit IP per bot. `scripts/mlx-proxy-gen.py --count N
--verify --country <cc>` mints N sticky Multilogin residential sessions (24h TTL;
they flap as they age — re-verify before a run). They drop into the API's
`proxies` table. Reads the Multilogin credential from the runner config.

## 7. systemd (VM deployments)

```bash
sudo scripts/install-systemd-services.sh
```

Caveats:

- The units and the install script reference **`/home/primemover`** absolute
  paths. A different user/home needs those edited.
- `primemover-runner` is installed but **not auto-enabled** (it needs a built jar
  and a logged-in Multilogin X). Enable once both are in place:
  `sudo systemctl enable --now primemover-runner`.
- `primemover-lambda` sets `LimitNOFILE=65536`. After any unit change:
  `sudo systemctl daemon-reload && sudo systemctl restart primemover-lambda`,
  and confirm the running process shows that soft limit.
- The lambda is served by **waitress**, not werkzeug's dev server (see CLAUDE.md
  "Local Lambda Server"). `waitress` is a declared dependency; a fresh venv gets
  it via `pip install -e ".[dev]"`.

## 8. Verify

```bash
orchestrator doctor        # machine + config/credentials + systemd preflight
orchestrator status -v
```

`doctor` failing on the decision symlink or an unreachable job-data store is
real — resolve it before a run. A missing secret is a warning; resolve the ones
the workload needs.

## The ex63-specific pieces (for reference)

These are hostname/account values, expected to differ per deployment: the MinIO
endpoint (`s3.qamelab.org`), the Multilogin folder id, the Traefik edge configs
(`*.qamelab.org`, tailnet IPs) under `infrastructure/traefik/` and the ex63
compose overlays. None require code changes — they are configuration.

## Adding a runner-only box (horizontal scaling)

If ex63's compute is the limit but the rest of the stack is fine, you can add a
second (tailnet) server that runs **only** the runner + Multilogin X + a local
Lambda, sharing ex63's API, database, MinIO and monitoring. You do **not** need
to redeploy the control plane.

### Topology

| component | where | how |
|---|---|---|
| runner (Java) | new box | `backend.endpoint.api` → ex63's API over the tailnet |
| Multilogin X (`mlxapp`) | new box | its own `DISPLAY=:1`, logged in |
| local Lambda + `bot-decision-yt` | new box | own checkout, symlink (§3), `OPENAI_API_KEY` — keeps decisions off the network |
| API + MySQL + Grafana/Loki | **shared (ex63)** | reachable over the tailnet |
| MinIO job data | **shared (ex63)** | `awsS3.endpoint = s3.qamelab.org`; the key namespace is flat and job-id-keyed, so two runners never collide |

So you install §1, §3, §4 (runner-side config + the local Lambda), §5 (the MinIO
key), §6 (proxies) and the `primemover-runner` / `primemover-lambda` /
`multilogin-*` systemd units on the new box — and skip the API/DB/monitoring.

### The hard requirement: partition the work

`crawlers-unprocessed` is a plain `WHERE processed = 0` select with **no atomic
claim**. Two runners pointed at the same API with the same visibility will both
fetch the same queues and both run every job — doubling and corrupting the data.
Before adding a second runner you must partition the work. The built-in
mechanism is ownership isolation:

- That query is wrapped in `visibleTo()`, which scopes results to the
  authenticated user's own experiments — **unless the account bypasses
  isolation** (a service account, or `Isolation` disabled), which sees
  everything.
- **Safe pattern:** run each runner as a **distinct regular user account** with
  isolation **enabled**, and assign each experiment to the owner whose runner
  should execute it. Each runner then sees only its own slice — no double
  processing, no code change.
- **Check first:** confirm ex63's current runner is not on a service account (or
  with isolation off) that sees everything — otherwise a second runner collides
  with it. See `primemover_api` `app/Access/Isolation.php`
  (`isServiceAccount` / `enabled`).

**Since 2026-09-02 partitioning is explicit, not by owner.** Experiments carry
`runner_user_id` (NULL = the default runner, `PM_DEFAULT_RUNNER_USER_ID`, 1 on
ex63). Every runner account is listed in `PM_SERVICE_ACCOUNT_IDS` (`1,3` on ex63),
so runners see everything for status reporting, but `crawlers-unprocessed` and
`crawlers-testing` only hand each runner the experiments assigned to it. To add a
runner-only box: create (or reuse) an API user for it, add its id to
`PM_SERVICE_ACCOUNT_IDS`, `config:clear`, and assign experiments to it with
`runner_user_id`. Researchers keep creating experiments under their own logins;
those run on the default runner unless assigned elsewhere. See
`docs/integration-contract.md` "Runner assignment".

### Multilogin limits are per workspace, not per box

If both runners use the **same** Multilogin account they share the 50
operations/minute profile-create limit and the ~1000-profile quota, and the
profile-create batcher is per process — two batchers can collectively exceed the
rate limit even though each clamps at 10. Options: give the second box its **own
Multilogin workspace** (its different folder id is pure config —
`multilogin.defaultFolderId`, §4), or stagger launches across the two boxes.

### If the new box is a VM: GPU decode does not travel

The Chrome-renderer-death fix has a hardware-dependent half and a
hardware-independent half (`docs/chrome-browser-death-fix.txt`). Only the second
half ships in the runner jar; the first does **not** exist on a VM:

- **Software decode is back.** ex63's VA-API hardware decode (iHD driver +
  render/video groups) is what makes each browser "mostly idle-waiting". A VM has
  no iGPU passthrough, so YouTube's VP9/AV1 is decoded on CPU again — the exact
  Feb-2026 condition that killed renderers after 19-95 jobs.
- **The H.264 override extension goes from redundant to load-bearing.** On ex63 it
  "no longer buys performance" because hardware decodes everything; on a VM,
  forcing H.264 is what keeps software decode affordable instead of pinning the
  renderer main thread. It is loaded by absolute path, so on a VM whose home
  differs from `/home/primemover` you **must** set `multilogin.h264ExtensionPath`
  in `application.properties` to where the extension actually lives. A wrong or
  absent path makes Chrome drop the extension **silently** — no log line — and
  the renderer dies under VP9/AV1. (The path was hardcoded until 2026-09-02;
  it is now a property, ex63 path as the fallback.)
- **The V8 heap cap + renderer recycle are hardware-independent** and already in
  the jar — nothing to do.
- **Do not** bother with render/video group membership (no `/dev/dri`) and do
  **not** add GL flags (Chrome 151 allows only `gl=egl-angle, angle=default`;
  anything else kills the GPU process).

**Capacity is CPU-bound on a VM, not RAM-bound.** The `~15 instant / ~30-40
staggered` figures in CLAUDE.md were measured on ex63 with hardware decode. With
software decode, per-bot CPU is far higher, so CPU binds long before RAM or
exit-IP supply — the reverse of ex63's ordering. Treat the VM's concurrency
ceiling as unknown and measure it (`scripts/monitor-memory.sh` for renderer RSS,
plus plain CPU) before trusting any number.

### Verify on the new box

`orchestrator doctor` still helps even without the full stack: it checks Docker,
Java, the decision symlink, the job-data store reachability and the systemd
units. The API/port checks will show the API as remote (not local) — that is
expected for a runner-only box.
