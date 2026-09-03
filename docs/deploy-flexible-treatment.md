# Deploying the flexible-treatment (D) chain

Ordered rollout for the strategy/behaviours treatment model (tracks A–D of the
2026-09-02 experiment-design review). Design + rationale:
`docs/proposals/flexible-treatment-model.md`. **All code is merged; nothing is
deployed.** This runbook is the deploy.

## The golden rule

The behaviours channel activates **only** when a `lambda_select` job carries an
explicit `strategy` marker. So **every intermediate deploy state is safe** and
**no existing experiment changes behaviour** until someone authors a spec with a
`strategy:`. That is what lets this roll out without a maintenance window. Deploy
order is **lambda → runner**; the orchestrator/DSL needs no runtime deploy.

## ⚠️ Read before you restart anything

**Restarting the lambda ships the entire `bot-decision-yt` checkout, not just D.**
The local Lambda server loads the decision code at process start (via the
`orchestrator/lambda_service/decision` symlink → `bot-decision-yt/bot-decision-EN`).
The running process therefore has the code as of its **last restart**, and a
restart picks up **everything currently in the checkout** — every commit merged
since the running version, not only the D work. Before restarting, diff what you
are about to ship and make sure you intend all of it (e.g. the error/warning
overhaul may also be merged-but-undeployed on this box).

## Deployed 2026-09-03 (both boxes) — canary result

Deployed per this runbook on **ex63** (lambda restarted 10:46 on `461ab5f`, runner jar
rebuilt from `1826600`, MCP restarted) and on the **VM** `primemover-switch-roland`
(lambda 10:31, runner 10:33). Old jars kept at `/var/tmp/runner-jar-rollback.jar` on
each box. Every deploy of this chain must be repeated on both boxes.

Canary (experiment 165 on the VM, deleted afterwards): a treatment bot with
`strategy: youtube_v1, treatment: {pi: 1}` and a control bot without a marker.
- The runner sends `job.behaviors` on every `lambda_select` (both bots).
- The gate holds: the control's payload carries behaviours but no `strategy`, and it
  decided on the legacy path, not degraded.
- The treatment bot's second call applied the treatment (not degraded). Its **first**
  call lost it: the mainstream-media aversion filter raised `IndexError: list index out
  of range` at `lambda_function.py:983` (`...xpath(xpath_recommendations_channel)[0]`
  on a recommendation card with no channel node), the decision fell back to random and
  the job was reported as `warning` / `treatment_lost` — i.e. the new degradation
  reporting works, and it exposed a pre-existing fragility in that filter's DOM path.
  Follow-up in `bot-decision-yt`: guard the per-card xpath lookups instead of `[0]`.

## Current state (as of 2026-09-02, superseded by the section above — re-derive before deploying)

| piece | running | target (main/master) |
|---|---|---|
| lambda (`primemover-lambda`) | started 08:59 CEST, **pre-D** | checkout `bot-decision-yt` main `461ab5f` (Phase 1 #19 + gate #20) |
| runner (`primemover-runner`) | jar `4c66c4c0…` (**pre-Phase-2**, the #80 folder-id jar) | runner master `1826600` (#82) |
| orchestrator DSL | n/a (authoring-time) | master `5d9b04e` (#54) |

Re-derive current running vs target before each step (commands in each step).

## Step 0 — pre-deploy checks

```bash
# lambda: what is running vs what a restart would ship
readlink /proc/$(systemctl show -p MainPID --value primemover-lambda)/cwd   # must end in /decision
systemctl show -p ExecMainStartTimestamp --value primemover-lambda          # running code is as of here
cd /opt/primemover/bot-decision-yt && git status --short && git log --oneline -5   # what the restart ships
# CSV + symlink integrity (existing gotchas)
cd bot-decision-EN && pytest tests/test_mainstream_media_csv.py -q
# runner: current jar identity for rollback
sha256sum /opt/primemover/primemover_runner/primemover-runner/target/*.jar | cut -c1-16
```
Record the running lambda commit (or service start time) and the running jar sha —
those are your rollback points.

## Step 1 — Lambda (bot-decision-yt)

The deploy is: make the checkout the target commit, then restart.

```bash
cd /opt/primemover/bot-decision-yt
git fetch && git checkout main && git pull        # -> 461ab5f (or later)
sudo systemctl restart primemover-lambda
```

Verify:
```bash
# health: status ok, cwd ends in /decision, fds not near limit
curl -s http://127.0.0.1:5001/health | python3 -m json.tool
# legacy payload still decides (no strategy -> selectionConfiguration, unchanged)
# strategy payload dispatches; watch for the "unknown strategy" warning NOT firing on legacy
journalctl -u primemover-lambda -n 50 --no-pager
```
A legacy `/invoke` (a payload with `crawler.config.selectionConfiguration`, no job
behaviours) must return the same shape it did before. This is a **no-op deploy**:
until the runner sends behaviours, nothing reaches the new channel.

Rollback: `git checkout <recorded commit> && sudo systemctl restart primemover-lambda`.

## Step 2 — Runner (primemover_runner)

Build in a **throwaway clone** (never build in the live checkout — the runner
executes from `target/`, and rebuilding under it throws `EOFException`). JDK 25.

```bash
# 1) build in a throwaway copy
SB=$(mktemp -d); rsync -a --exclude target/ --exclude .git/ /opt/primemover/primemover_runner/ "$SB"/
cd "$SB" && git -C /opt/primemover/primemover_runner rev-parse --short HEAD   # confirm master has #82 (1826600)
mvn -q -pl primemover-runner -am package -DskipTests=false   # or -DskipTests + rely on CI
NEWJAR=$(ls "$SB"/primemover-runner/target/primemover-runner-*.jar); sha256sum "$NEWJAR" | cut -c1-16

# 2) swap when the runner is idle (SIGTERM does not drain; prefer nothing in flight)
sudo systemctl stop primemover-runner        # TimeoutStopSec=180
cp /opt/primemover/primemover_runner/primemover-runner/target/*.jar /var/tmp/runner-jar-rollback.jar   # keep old
cp "$NEWJAR" /opt/primemover/primemover_runner/primemover-runner/target/
# ensure exactly ONE jar in target/ (ExecStart globs *.jar)
ls /opt/primemover/primemover_runner/primemover-runner/target/*.jar
sudo systemctl start primemover-runner
```

Verify:
```bash
# JVM holds the new jar by inode; 0 startup errors
PID=$(systemctl show -p MainPID --value primemover-runner)
sudo ls -l /proc/$PID/root/... # (or compare the loaded jar path); check sha matches NEWJAR
journalctl -u primemover-runner -n 80 --no-pager | grep -iE "error|exception" | head
```
After this, a `lambda_select` job's payload includes `job.behaviors`. Confirm on
the next real select by reading a captured payload (MinIO) or the runner log — but
remember only a `strategy`-marked spec activates the channel.

Rollback: `sudo systemctl stop primemover-runner; cp /var/tmp/runner-jar-rollback.jar
<target>/…; sudo systemctl start primemover-runner`.

## Step 3 — Orchestrator (no runtime deploy)

The DSL parser/validator run at **authoring time** (`pmspec`, or the MCP tools),
so nothing needs restarting for experiments to use the channel. Two optional
touches:
- To serve the new `doc://treatment-params` text and strategy-treatment validation
  to MCP clients, `sudo systemctl restart primemover-mcp`.
- `validate_experiment_spec` reads the strategy schema through the decision
  symlink, so Step 1 (pulling `bot-decision-yt`) already makes
  `strategies/youtube_v1.schema.json` reachable.

## End-to-end canary (do this watching a soak)

1. Author a tiny test experiment owned by a **non-shared test user** (see
   `docs/new-server-setup.md` / the personal-MCP note), one bot, with:
   ```yaml
   - lambda_select: {task: youtube_video_select, function: bot-decision-yt,
       strategy: youtube_v1, treatment: {pi: 1}}
   ```
2. Run it; then use `orchestrator debug job <id>` / the lambda payload to confirm
   the decision saw `strategy=youtube_v1` and applied the treatment (degradation
   report), and that a **control** crawler (no `strategy`) still behaves exactly as
   before.
3. Only after the canary looks right, treat the channel as live for real studies.

## Rollback summary

| step | rollback |
|---|---|
| lambda | `git checkout <old commit>` in bot-decision-yt + restart `primemover-lambda` |
| runner | restore `/var/tmp/runner-jar-rollback.jar` to `target/` + restart `primemover-runner` |
| orchestrator | revert is authoring-time only; no running state to roll back |

Because of the marker gate, rolling back the runner alone is safe (the new lambda
falls back to `selectionConfiguration` when no behaviours arrive), and rolling
back the lambda alone is safe (an old lambda ignores the new `job.behaviors`
field). The chain has no ordering trap — lambda-first is the recommendation, not a
hard requirement.
