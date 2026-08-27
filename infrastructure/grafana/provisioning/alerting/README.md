# Alerting

Grafana **unified alerting**, provisioned from files. This directory is the only
working alerting in the repository.

| file | what it does |
|---|---|
| `rules-primemover.yaml` | 7 alert rules, all over the **Loki** datasource (uid `loki`) |
| `contact-points.yaml` | one contact point, `primemover-default`, inert until SMTP is configured |
| `notification-policies.yaml` | the org's root notification policy (it **replaces** the default one) |

Grafana logs one benign warning about this file on every start --
`file has invalid suffix 'README.md' (.yaml,.yml,.json accepted), skipping` --
because it scans the directory for provisioning files. It is skipped, nothing
else is affected, and it is not worth chasing.

It is picked up with **no `docker-compose.yml` change**: the compose file already
mounts the whole provisioning tree
(`./infrastructure/grafana/provisioning:/etc/grafana/provisioning:ro`), and
Grafana reads every subdirectory it knows about, `alerting/` included.

**Provisioning is read at startup only.** Nothing here takes effect until
Grafana restarts, which is a deliberate operator decision, not something a
tooling change should do on its own:

```bash
cd /path/to/primemover-orchestrator      # the CWD rule applies here too
docker compose restart grafana
```

## What is alerted, and why those things

| uid | severity | fires on |
|---|---|---|
| `pm-lambda-treatment-lost` | critical | `"treatment_lost":true` in a response payload, or the runner's `Lambda decision LOST THE TREATMENT` line |
| `pm-lambda-mainstream-media-inert` | critical | `Aversion to mainstream media channels ignored` in a response |
| `pm-lambda-degraded` | warning | `degraded=true`, or a non-empty `warnings[]` |
| `pm-job-failures` | warning | >5 runner lines reporting a job ending in failure, in 15m |
| `pm-job-warnings` | warning | any job finishing with the new `warning` status |
| `pm-runner-error-rate` | warning | >10 runner ERROR lines in 10m |
| `pm-api-error-rate` | warning | >10 Laravel ERROR lines in 10m |

The three `pm-lambda-*` rules are the point of the exercise. A degraded decision
returns **HTTP 200**, the job is recorded as a success, and the run finishes
green — so a bot that was configured to be treated quietly produces control-arm
data and *nothing* in the system says so. They are the only signal that this
happened.

### `treatment_lost`, not `selection_mode` — the correction

`pm-lambda-treatment-lost` replaces a rule that was keyed on
`selection_mode == "random"`, uid `pm-lambda-random-selection`. That gate was
wrong, and it was wrong in the direction that would have destroyed the rule's
credibility on day one.

`selection_mode` is a **neutral description** of how the video was picked.
`"random"` means no LLM rating informed the choice — which is the *correct and
expected* answer for a genuine control crawler (`pi=psi=kappa=tau=0`), because
no LLM filter was ever requested. Measured on this host before the rewrite:
**295 of the 358** lambda payloads in the rotated runner logs carry no rating
column at all (a plain `grep -c rating` over the same 358 says 350), and
**236 of the 243** `params` rows in the live database are control-arm. Keying
critical on the mode alone would have marked ~82% of every `lambdaselect` job as
a treatment failure and pinned a `severity=critical` alert on permanently.

The contract now computes the two-term gate once, at the source, and ships it as
its own boolean:

| question | field to gate on |
|---|---|
| a human should look at this decision | `degraded == true` → `pm-lambda-degraded` (warning) |
| this session's data is scientifically void | `treatment_lost == true` → `pm-lambda-treatment-lost` (critical) |
| how was the video chosen | `selection_mode` — description only, **never** alert on it |

`filters: {name: "skipped"}` means *not requested*. It is not a failure, and no
rule here counts it as one. Full contract: `docs/integration-contract.md` §7.3.

### Which log lines the rules read, and why those

Everything is read from the **runner's** log file, because that is the only
stream promtail ships. Two traps behind that sentence:

- **The local Lambda server's own logs are not in Loki.** Under systemd it logs
  to the journal, and promtail scrapes no journal unit — `component` in Loki is
  only `api`, `runner`, `apache`, `mysql`, `php`, `redis`, `system`. Its
  degradation WARNING/ERROR lines are real and worth reading with
  `journalctl -u primemover-lambda`, but nothing can alert on them today. The
  runner echoes the whole decision payload anyway, which is what the rules use.
- **DEBUG lines cannot be alerted on.** `primemover.service.JobService` logs
  `sending JSON: {"status_code":…}` — the literal PUT body — at DEBUG, and
  nothing pins that logger, so it inherits root: DEBUG only under the
  `dev,staging,test,local` profiles, INFO under `production` and under no
  profile at all (`primemover-runner/config/logback-spring.xml`). The first
  version of `pm-job-failures` / `pm-job-warnings` matched exactly that line, so
  outside a dev profile they could never fire — and a quiet rule reads as "no
  failures", not as "not measured". They now match `TaskOperator`'s own WARN and
  ERROR outcome lines, which survive every profile:

| rule | line | level | class |
|---|---|---|---|
| `pm-lambda-treatment-lost` | `Full Lambda response payload: {…}` (carries `"treatment_lost":true`) | INFO | `LambdaSelectJobHandler` |
| `pm-lambda-treatment-lost` | `Lambda decision LOST THE TREATMENT for job …` | ERROR | `LambdaSelectJobHandler` |
| `pm-lambda-degraded` | the same payload line (`"degraded":true` / non-empty `warnings[]`) | INFO | `LambdaSelectJobHandler` |
| `pm-job-failures` | `Done running job with failure <id>` | WARN | `TaskOperator` |
| `pm-job-failures` | `Exception executing job <id>` | ERROR | `TaskOperator` |
| `pm-job-warnings` | `Done running job with warning …` | WARN | `TaskOperator` |

Note the runner is currently started with `--spring.profiles.active=local`
(`infrastructure/systemd/primemover-runner.service`), which is the only reason
the DEBUG version worked at all on this box. Do not rely on that: the fix is not
"keep the profile", it is "do not alert on DEBUG".

Renaming any line in that table silently zeroes a rule. It is on the breaking-
change checklist in `docs/integration-contract.md` §8 for that reason.

Both `level=~"(?i)error"` matchers are case-insensitive on purpose: promtail
lowercases the `level` label as of 2026-08-27, while every line older than that
is still uppercase `ERROR`. They are also the only two rules that depend on the
`level` label existing at all — every other rule matches line text, deliberately,
because 7374 of the `sending JSON` lines already in Loki carry **no** level label
(promtail's runner regex only started populating it on 2026-08-27, and promtail
has not been restarted since the config changed).

`noDataState: OK` on every rule. An idle box produces no logs, and that is not an
incident.

## What is NOT wired (and why it looks like it is)

**`prometheus/rules/primemover-alerts.yml` is dead code.** It has never been
loaded, by anything, and reading it will mislead you. Every one of these is
independently fatal:

1. `infrastructure/prometheus/prometheus.yml` has `rule_files: []` — the rules
   file is not referenced.
2. `docker-compose.yml` never mounts `prometheus/rules/` into the Prometheus
   container, so the file does not even exist in there.
3. `prometheus.yml` has `alertmanagers: []`, and there is no Alertmanager
   service in the compose file, so a firing rule would have nowhere to go.
4. The expressions reference metrics that **do not exist**: `loki_logs_total`
   is not a metric Loki exports, and there is no `aws_lambda_*` exporter.
5. The `primemover-api` and `primemover-runner` scrape jobs it depends on
   (`up{job="primemover-api"}`) are commented out — neither component exposes a
   Prometheus endpoint at all.

Nothing here fixes that file; it is left in place untouched. The reason this
directory uses Loki rather than Prometheus is (4) and (5): the only machine-readable
signal the ecosystem actually emits is its logs.

Also not wired: **Loki's own ruler** (`infrastructure/loki/loki-config.yaml` has
no `ruler` block). Grafana-managed rules were chosen over Loki ruler rules
because they need no extra component, no Alertmanager, and are visible and
silenceable in the same UI people already use.

## Arming notifications

Out of the box alerts **fire and are visible** under *Alerting → Alert rules*,
and nothing is delivered. Delivery is opt-in:

1. Forward SMTP settings into the Grafana container's `environment:` in
   `docker-compose.yml` — `GF_SMTP_ENABLED=true`, `GF_SMTP_HOST`, `GF_SMTP_USER`,
   `GF_SMTP_PASSWORD`, `GF_SMTP_FROM_ADDRESS`. They are documented in
   `.env.example`; compose does **not** pass them through today, so adding them
   is a required one-line change per variable.
2. Replace the placeholder recipient in `contact-points.yaml`, or forward
   `PM_ALERT_EMAIL_TO` and use `${PM_ALERT_EMAIL_TO}` there.
3. Restart Grafana.

For a webhook instead, set `PM_ALERT_WEBHOOK_URL`, uncomment the
`primemover-webhook` block, point the policy at it, restart.

Two Grafana behaviours worth knowing before editing these files, both verified
against `grafana/grafana:11.4.0`:

- **An invalid contact point aborts the ENTIRE alerting provisioning** — rules
  included — and Grafana **exits 1**. An empty `addresses`, or an uncommented
  webhook with an unset `url`, does exactly this. That is why the default
  recipient is a literal placeholder rather than an unset variable.
- **`${VAR:-default}` is not supported.** Grafana's provisioning expansion
  handles `${VAR}` and expands an unset variable to the empty string, which
  walks straight into the previous point.

The alert rules live in the folder **`PrimeMover Alerts`**, not `PrimeMover`.
That is not cosmetic: alert-rule provisioning creates the folder by *title* with
its own uid, and the dashboards provider then fails with *"a folder with the
same name already exists in the current location"* and provisions no dashboards
at all. Observed, then fixed by separating the titles — do not merge them back.

## Deploy order (this alerting is the LAST step)

Nothing about the 2026-08-27 error-handling change was deployed when these rules
were written. Grafana provisioning is read at startup only, so restarting Grafana
is what arms them — do it **after** the things they read exist, in the order in
`docs/integration-contract.md` §0:

1. `bot-decision-yt` → restart `primemover-lambda` (the source of `degraded` /
   `treatment_lost`);
2. restart promtail (runner-line regex + level label);
3. rebuild + restart the runner jar (the WARN/ERROR outcome lines, and
   `status_code=warning`);
4. rebuild the GUI image;
5. `docker compose restart grafana` — last.

Arming Grafana first is not harmless: every rule would evaluate against lines
that cannot exist yet, `noDataState: OK` would report green, and a green board
over an unmeasured period is a **wrong answer**, not a missing one. Check the
rules against live Loki (below) before believing a quiet one.

## Verifying a change without touching the live stack

Never test provisioning by restarting the running Grafana. Run a throwaway one
against the same files; it needs no ports and does not share the data volume:

```bash
cd /path/to/primemover-orchestrator
docker run -d --name pm-alert-check --network pm-orchestrator-network \
  -e GF_SECURITY_ADMIN_PASSWORD=probeonly \
  -v "$PWD/infrastructure/grafana/provisioning:/etc/grafana/provisioning:ro" \
  grafana/grafana:11.4.0

sleep 20
docker logs pm-alert-check 2>&1 | grep -i "provision"        # want "finished to provision alerting"
docker ps -a --filter name=pm-alert-check --format '{{.Status}}'   # want Up, not Exited (1)

sleep 75   # let one evaluation interval pass
docker exec pm-alert-check wget -qO- \
  --header="Authorization: Basic $(printf 'admin:probeonly' | base64)" \
  http://localhost:3000/api/prometheus/grafana/api/v1/rules   # want health=ok, lastError=null

docker rm -f pm-alert-check
```

Attaching it to `pm-orchestrator-network` is what makes the rules evaluate
against the **real** Loki, which is the only way to learn that an expression is
wrong rather than merely well-formed.

Two cheaper checks worth running on every edit, neither of which needs a
container at all:

```bash
# 1. every file here parses, and the datasource uid the rules name exists
python3 -c "import yaml,glob;[yaml.safe_load(open(f)) for f in glob.glob('infrastructure/grafana/provisioning/alerting/*.yaml')];print('yaml ok')"
grep -n 'uid:' infrastructure/grafana/provisioning/datasources/datasources.yaml   # want `loki`

# 2. every expression actually runs against the live Loki (a bad matcher
#    returns success + empty, a bad EXPRESSION returns an error -- read the status)
curl -s -G http://localhost:3100/loki/api/v1/query \
  --data-urlencode 'query=sum(count_over_time({component="runner"} |~ `"treatment_lost"\s*:\s*true` [15m]))'
```

A rule naming a datasource uid that does not exist provisions **without error**
and then fails at evaluation time with `datasource not found`, which surfaces
only in the rule's health — so check the uid, do not assume it.
