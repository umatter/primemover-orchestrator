# Deploying the status-surfacing change set

Ordered rollout for the four PRs that make the 2026-08 error/warning overhaul
visible in the GUI. Design and rationale:
`docs/proposals/gui-error-warning-surfacing.md`. The normative order and the
reason it matters: `docs/integration-contract.md` **§0**.

| repo | PR | what it ships |
|---|---|---|
| `primemover_api` | #62 | migration + rollups, filters, health endpoint, persisted verdict |
| `primemover_gui` | #55 | every UI surface that reads them |
| `primemover_runner` | #84 | sends the verdict on the job PUT |
| `primemover-orchestrator` | #66 | `status.py`, the contract, this runbook |

## The one rule

**Migrate before the runner. Never the runner first.** The verdict rides the
same PUT as `status_code`, so a runner ahead of the schema loses the job's
terminal state as well as its verdict, and a job stuck at `running` takes its
run's rollup — and every analysis that joins on `status_code` — with it.

Every other intermediate state is safe, because a missing column and a NULL
column both read as **unknown** and no layer ever asserts a treatment applied
when nobody measured it.

## The API is two events, not one

`/opt/primemover/primemover_api` is **bind-mounted** into the running php
container, so API code is live the instant it lands on disk, while the migration
is a separate later command. Following "deploy the API" as a single step is what
caused the **107-minute outage of 2026-09-07/08** (22:44 to 00:31), in which
`GET /crawlers-unprocessed` — the runner's only work feed — returned 500 on
every poll.

The code now carries `Job::hasDecisionColumns()`, so that mistake degrades to
omitted counts rather than an outage. The migration still cannot run before the
code, because it calls `Job::forgetDecisionColumnSupport()`, which only exists
in the new code. So the order below is **code, then migration, immediately**,
with the guard covering the gap. Keep the gap short anyway.

## Pre-flight

Deploy when nothing is in flight. Stopping the runner mid-job leaves queues
flagged `processing=1, processed=0`.

```bash
docker exec primemover-mysql sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" primemover -e "
  select count(*) running_queues from queues where status_code=\"running\";
  select count(*) running_jobs   from jobs   where status_code=\"running\";
  select count(*) scheduled      from queues where processed=0 and start_at > now();"'
```

All three must be `0`. Also note the current state so you can compare after:

```bash
git -C /opt/primemover/primemover_api log --oneline -1     # expect 5d115cf before deploy
ls -l /opt/primemover/primemover_runner/primemover-runner/target/primemover-runner-2.2.1.jar
docker ps --format '{{.Image}}' | grep primemover_gui
```

## Step 1 — API code, then migration

```bash
cd /opt/primemover/primemover_api
git checkout master && git pull                 # code is LIVE from this instant
docker exec primemover-php8.1 sh -c 'cd /var/www/html && php artisan migrate --force'
docker restart primemover-php8.1                # clear any cached "no decision columns"
```

The container restart matters: the capability check is memoised per process, so
a worker that answered "no columns" before the migration would keep omitting the
counts until it recycled.

**Verify before going further.** The first command is the one that matters —
it is the endpoint the runner polls every 60 seconds.

```bash
API=http://localhost:8090/api/v1
TOKEN=$(curl -s -X POST $API/login -H 'Content-Type: application/json' \
  -d '{"email":"admin@primemover.example","password":"<from .env>"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
for e in crawlers-unprocessed crawlers queues jobs experiments; do
  printf '%-22s ' $e
  curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" "$API/$e?per_page=1"
done
```

All five must be `200`. Then confirm the columns exist and every existing row is
**NULL**, not `false` — NULL is "we never measured this", `false` would claim the
treatment applied:

```bash
docker exec primemover-mysql sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" primemover -e "
  select count(*) decision_columns from information_schema.columns
    where table_schema=\"primemover\" and column_name like \"decision%\";
  select count(*) total,
         sum(decision_treatment_lost is null)  as unknown,
         sum(decision_treatment_lost is true)  as lost,
         sum(decision_treatment_lost is false) as clean
    from jobs;"'
```

Expect 4 columns, and `unknown` equal to `total` (~31,600) with `lost` and
`clean` both 0.

**If the endpoints are not 200:** `git checkout <previous sha>` in the API tree
restores service immediately. That is how the 2026-09-07/08 outage was ended.
Leave the migration in place; the columns are harmless to the old code.

## Step 2 — GUI image

Needs write access to `/opt/ex63-infra` (owned by `umatter`).

Merging #55 makes CI publish `ghcr.io/umatter/primemover_gui:sha-<short>`; there
is deliberately no `latest`. Take the tag from the publish workflow run, then:

```bash
# in ex63-infra
$EDITOR compose/apps.yml      # set image: ghcr.io/umatter/primemover_gui:sha-<new>
make up
```

Verify from off the box — testing via `127.0.0.1` returns 403, because the
`tailnet-only` allow-list sees the docker bridge address:

```bash
curl -sS --resolve primemover.qamelab.org:443:<ex63-tailnet-ip> \
  https://primemover.qamelab.org -o /dev/null -w '%{http_code}\n'
```

Open an experiment and confirm the health strip renders. A bot whose runs all
ended `warning` should now read amber, not "Running".

**Rollback:** repin the previous tag and `make up`. The image is immutable, so
the old tag is still there.

## Step 3 — Runner jar

Needs `sudo`. **Never build in the live checkout** — the running runner executes
from `primemover-runner/target/` and rebuilding the fat jar underneath it throws
`EOFException` into in-flight jobs. Build in a throwaway clone, then swap while
stopped.

A jar built from this change set is already staged at
`/tmp/claude-1001/runner-build/primemover-runner/target/primemover-runner-2.2.1.jar`
(built 2026-09-08 07:03). Rebuild from merged `master` if you prefer:

```bash
rm -rf /tmp/runner-build && git clone -q --shared /opt/primemover/primemover_runner /tmp/runner-build
cd /tmp/runner-build && git checkout master && git pull && mvn -o -DskipTests package
```

Then swap and restart:

```bash
cd /opt/primemover/primemover_runner/primemover-runner/target
cp primemover-runner-2.2.1.jar primemover-runner-2.2.1.jar.bak-$(date +%Y%m%d)
sudo systemctl stop primemover-runner          # TimeoutStopSec=180; SIGTERM does not drain
cp /tmp/runner-build/primemover-runner/target/primemover-runner-2.2.1.jar .
sudo systemctl start primemover-runner
```

Verify it is polling cleanly, and that the tests really passed if you rebuilt —
`primemover-runner/pom.xml` sets `testFailureIgnore=true`, so **`BUILD SUCCESS`
prints alongside failing tests**. Read the `Tests run:` totals, not the build
result.

```bash
journalctl -u primemover-runner -n 20 --no-pager | grep -E 'jobs detected|Unexpected error'
```

`jobs detected: 0` on each poll is healthy when nothing is scheduled. Any
`Unexpected error occurred in scheduled task` means the API is refusing the work
feed — go back to step 1.

**Rollback:** stop, copy the `.bak` jar back, start.

## Step 4 — Orchestrator

```bash
cd /opt/primemover/primemover-orchestrator && git checkout master && git pull
```

The CLI picks this up immediately. `primemover-mcp` and `primemover-lambda` run
from this tree but only re-read on restart, and nothing in this change set
requires one. Restart `primemover-mcp` if you want its read tools to use the new
verdict helpers:

```bash
sudo systemctl restart primemover-mcp
```

## Step 5 — Prove it end to end

```bash
cd /opt/primemover/primemover-orchestrator && source venv/bin/activate
orchestrator status          # Runner should report RUNNING, not STOPPED
orchestrator e2e --youtube -v
```

The e2e takes about six minutes and exercises the whole chain: upload, pickup,
three lambda-select jobs, job data to MinIO. Then confirm the new fields are
actually populated end to end:

```bash
docker exec primemover-mysql sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" primemover -e "
  select id, status_code, decision_degraded, decision_treatment_lost, decision_selection_mode
    from jobs where type=\"lambdasingleselectjob\" order by id desc limit 5;"'
```

After step 3 those rows should carry a verdict rather than NULL. Before step 3
they stay NULL, which is correct and is why the runner goes last.

## Backfilling the history

Optional, and **not** part of the rollout. ~6,700 `lambda-jobdata` objects
predate the columns; `GET /api/v1/jobs/{id}/decision` already reads them without
a backfill. To populate the columns as well:

```bash
docker exec primemover-php8.1 sh -c 'cd /var/www/html && php artisan decisions:backfill --dry-run --limit=50'
```

Read the dry run before committing to it. Note that 419 of the archived objects
are empty zips (22-byte EOCD only, scattered 2026-08-27 to 09-01); those stay
unknown, which is the honest answer. The command never overwrites a non-null
value without `--force`.

## What deploying only part of this costs

| state | consequence |
|---|---|
| API code live, migration not yet run | Counts omitted, endpoints 200. Safe; the guard exists for exactly this. Close it quickly anyway. |
| API done, GUI old | Old GUI ignores the new fields. Safe. |
| API done, runner old | Columns stay NULL. Correct: nobody measured those jobs. |
| **Runner new, migration not run** | **The only unsafe state.** The verdict rides the status PUT; the API 500s, and the runner's strip-and-retry recovers the status at the cost of a wasted round trip per job. Older runners have no retry and leave the job at `running`. |
