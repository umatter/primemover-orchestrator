# Runner assignment: decouple who owns an experiment from where it runs

**Date:** 2026-09-02 · **Status:** approved design, not yet implemented
**Repos touched:** `primemover_api` (the change), `primemover-orchestrator` (MCP tool +
YAML spec field + docs). GUI follow-up is out of scope.

## Problem

Since 2026-09-02 there are two runners: ex63 (API user 1, `admin@primemover.example`) and the
runner-only VM `primemover-switch-roland` (API user 3, `runner@primemover.example`). The API has no
atomic queue claim, so the two runners must never see the same unprocessed queue.
The only partitioning mechanism today is per-user isolation: each runner is a plain
isolated user and sees only experiments it *owns*.

That makes ownership do two jobs at once — *who may see an experiment* and *which runner
executes it* — and the second job breaks the researchers' workflow: an experiment Ulrich
(users 5/7) or Matthias (user 6) creates under their own login is owned by them, no runner
authenticates as them, so it never runs. To work around it today, experiments 42 and 143
were moved to user 1, which hides them from their authors.

## Decision

Add an explicit **runner assignment** to experiments. Ownership goes back to meaning
visibility only. Both runners become service accounts again (see everything, needed for
their status reporting), and the two work-fetching endpoints filter by assignment
instead of by owner. ex63 is the default runner; the VM is opt-in per experiment.

Rejected alternatives: an atomic queue claim (automatic load balancing, but needs claim
expiry for crashed runners, and the VM's capacity is unknown — a later step if wanted);
manual ownership transfer per experiment (what exists today; not usable).

## Design

### 1. Data model (`primemover_api`)

- Migration: `experiments.runner_user_id`, `foreignId()->nullable()->constrained('users')`
  with `onDelete('restrict')`, indexed. No backfill: `NULL` means "the default runner".
- Config `config/primemover.php`:
  ```php
  'runner' => [
      // The runner that executes experiments with runner_user_id = NULL.
      'default_user_id' => (int) env('PM_DEFAULT_RUNNER_USER_ID', 1),
  ],
  ```
  Runner accounts are the existing `isolation.service_account_ids` list; no second list.
- `Experiment` model: `runner()` BelongsTo. `$guarded = []` already, so the column is
  mass-assignable. The default JSON serialisation includes it (there is no dedicated
  experiment resource class; `JsonResource::make($model)` serialises all attributes).

### 2. Runner partitioning

The Java runner fetches work from exactly two endpoints (verified by grepping the runner
source): `GET /crawlers-unprocessed` → `CrawlerRepository::unprocessed()` and
`GET /crawlers-testing` → `CrawlerRepository::testing()`. Both gain the same constraint, applied
**after** `visibleTo()` and **regardless of** `Isolation::bypasses()`:

```php
->whereHas('experiment', function ($q): void {
    $me = Isolation::userId();
    $q->where('runner_user_id', $me);
    if ($me === (int) config('primemover.runner.default_user_id')) {
        $q->orWhereNull('runner_user_id');
    }
})
```

Implemented once as a scope on `Crawler` (`scopeAssignedToCallingRunner`) and used by both
repository methods, so the two endpoints cannot drift. Every other endpoint — including
the runner's status PUTs on `runner/agents/{agent}`, `runner/jobs/{job}`,
`runner/queues/{queue}` — is untouched; `config/primemover.php` documents why those must
never be scoped.

Consequence for non-runner callers of these two endpoints: a human user who is neither
the default runner nor assigned anything gets an empty list. The orchestrator CLI/MCP do
not call these endpoints (verified), so nothing else changes.

### 3. Users and requests

- `ExperimentStoreRequest`, which serves both POST `/experiments` and PUT/PATCH
  `/experiments/{experiment}` (the controller's `update()` takes the same request class):
  accept optional `runner_user_id`,
  rule `['nullable','integer', Rule::in(Isolation::serviceAccountIds())]`. Unknown or
  non-runner ids are a 422, not silently accepted.
- `StripClientOwnership` middleware keeps stripping `user_id` only; `runner_user_id` is
  exactly the field a researcher is meant to set.
- Nothing changes for the `/experiments/upload` and `/experiments/{experiment}/upload`
  crawler-upload pipeline; it does not create experiments' runner assignment.

### 4. Clients (`primemover-orchestrator`)

- MCP `pm_create_experiment`: new optional `runner_user_id: int | None` passed through.
  Docstring: `1` = ex63 (default when omitted), `3` = the VM.
- Experiment designer YAML: optional `experiment.runner_user_id`, carried into the
  experiment payload by the converter and `orchestrator experiment upload`.
- `docs/integration-contract.md`: new short section "Runner assignment" stating the
  field, the NULL-means-default rule, the two filtered endpoints, and that
  `PM_SERVICE_ACCOUNT_IDS` must list every runner account.

Out of scope, recorded as follow-ups: a `GET /runners` endpoint returning
`{id, name, email}` for the runner accounts so clients can offer names instead of ids;
a GUI dropdown on the experiment form.

### 5. Tests

PHPUnit feature tests next to `tests/Feature/Isolation/` (reuse `IsolationTestCase`):

1. Default runner (user = `default_user_id`, service account) calling
   `/crawlers-unprocessed` gets crawlers of experiments with `runner_user_id` NULL and
   `= itself`, and not those assigned to another runner.
2. Second runner (service account, not default) gets only its assignments, including
   experiments owned by other users.
3. Same two cases for `/crawlers-testing`.
4. Store request: a plain isolated user may set `runner_user_id` to a runner account;
   setting it to a non-runner id is a 422; omitting it stores NULL.
5. Isolation disabled: responses for the two endpoints are unchanged except for the
   runner filter (documented deviation from "byte-identical when disabled" — the filter is
   a partitioning rule, not an isolation rule, and applies in both modes).

Orchestrator: unit tests for the converter field and the MCP tool payload.

### 6. Rollout on ex63 (in this order)

1. Merge the API PR. The branch is cut from `chore/pint-baseline` (what ex63 runs:
   master plus two formatting commits) and the PR targets `master`; if
   `chore/pint-baseline` has not been merged by then, the PR simply carries those two
   commits along.
2. On ex63: `git pull` in `/opt/primemover/primemover_api`, then inside the PHP container
   `php artisan migrate`.
3. API `.env`: `PM_SERVICE_ACCOUNT_IDS=1,3`, `PM_DEFAULT_RUNNER_USER_ID=1`;
   `php artisan config:clear`. Verify with tinker that both values are effective.
4. Give experiments 42 and 143 back to users 5 and 6 (`UPDATE experiments SET user_id`),
   leaving `runner_user_id` NULL so they keep running on ex63.
5. Verify: VM runner log shows it still polls and sees nothing; ex63 runner unchanged;
   create a test experiment as Matthias/Ulrich via the GUI and confirm ex63 picks it up;
   assign one to `runner_user_id=3` and confirm only the VM picks it up.
6. Update `docs/new-server-setup.md` ("The hard requirement") and
   `docs/runner-vm-migration-handoff.md` to describe assignment instead of owner
   partitioning.

Between steps 2 and 3 there is a window where the filter is live but user 3 is not yet a
service account; the VM then sees nothing for a few seconds. Harmless.

Rollback: restore `.env.bak-*`, `config:clear`, `php artisan migrate:rollback --step=1`,
redeploy the previous commit.
