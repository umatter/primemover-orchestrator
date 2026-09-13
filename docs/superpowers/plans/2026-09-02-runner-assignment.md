# Runner Assignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let researchers create experiments under their own logins that actually run, by giving each experiment an explicit runner assignment instead of using ownership to partition work between the ex63 and VM runners.

**Architecture:** One nullable `runner_user_id` column on `experiments` (NULL = default runner, config `PM_DEFAULT_RUNNER_USER_ID`). Both runner accounts become service accounts again; the two work-fetching queries (`crawlers-unprocessed`, `crawlers-testing`) gain a single shared Eloquent scope that filters by assignment regardless of isolation bypass. Researchers may set the field on create/update, validated against the configured runner accounts. The orchestrator's MCP create tool and YAML spec pass the field through.

**Tech Stack:** Laravel 10 / PHP 8.1+ (PHPUnit 10, Pint) in `~/tools/primemover_api`; Python 3.10+ (pytest, black, ruff, mypy) in `~/tools/primemover-orchestrator`.

**Spec:** `docs/superpowers/specs/2026-09-02-runner-assignment-design.md` (in `primemover-orchestrator`).

## Global Constraints

- API branch: `feat/runner-assignment` in `~/tools/primemover_api`, cut from `origin/chore/pint-baseline` (commit `06903e2`, what ex63 runs). PR targets `master`.
- Orchestrator branch: `feat/runner-assignment` in `~/tools/primemover-orchestrator` (exists; carries the spec commit). Leave the pre-existing uncommitted changes to `CLAUDE.md`, `docs/runner-vm-migration-handoff.md`, `scripts/multilogin-login.py` alone — they belong to the user's separate commit.
- `NULL` in `runner_user_id` means "the default runner" — never backfill it.
- Valid assignment targets are exactly `config('primemover.isolation.service_account_ids')`. No second list.
- Never scope any endpoint other than `crawlers-unprocessed` and `crawlers-testing` (see the warning block in `config/primemover.php`).
- No local PHP. Run every API command through Docker from the API repo root with this helper (paste into the shell once per session):

```bash
apitest() {  # usage: apitest [phpunit args...]   e.g. apitest tests/Feature/Isolation
  docker run --rm -v "$PWD":/app -w /app php:8.3-cli sh -c '
    [ -f .env ] || cp .env.example .env
    [ -f storage/oauth-public.key ] || cp docker/oauth-public.key storage/oauth-public.key
    grep -q "^APP_KEY=base64" .env || php artisan key:generate --quiet
    chmod -R 777 storage bootstrap/cache; mkdir -p database; touch database/database.sqlite
    DB_CONNECTION=sqlite DB_DATABASE=database/database.sqlite vendor/bin/phpunit '"$*"
}
apipint() { docker run --rm -v "$PWD":/app -w /app php:8.3-cli vendor/bin/pint "$@"; }
```
  `vendor/` is already installed (`composer:2` image, `--ignore-platform-reqs`). `core.fileMode=false` is set so the chmod does not dirty the tree. After a run, `git status` must show only your files; if `storage/api-docs/*` or `bootstrap/cache/.gitignore` show up, `git checkout -- storage/api-docs bootstrap/cache/.gitignore`.
- Orchestrator checks: `make check` (black → ruff → mypy → pytest) from the orchestrator root, using its venv.
- Commit messages: imperative, one line + body when useful, and the attribution trailer:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01X6ZG2SdXdEezCxfT33LaSg
  ```

---

## File structure

**`primemover_api`**
- Create `database/migrations/2026_09_02_000000_add_runner_user_id_to_experiments_table.php` — the column.
- Modify `config/primemover.php` — new `runner.default_user_id`.
- Modify `app/Access/Isolation.php` — `defaultRunnerUserId()`, `isRunner()` helpers (single home for "who is a runner").
- Modify `app/Modules/Experiment/Model/Experiment.php` — `runner()` relation.
- Modify `app/Modules/Crawler/Model/Crawler.php` — `scopeAssignedToCallingRunner()`.
- Modify `app/Modules/Crawler/Repository/CrawlerRepository.php` — apply the scope in `unprocessed()` and `testing()`.
- Modify `app/Modules/Experiment/Http/Request/ExperimentStoreRequest.php` — validate `runner_user_id`.
- Create `tests/Feature/RunnerAssignment/RunnerAssignmentTestCase.php` — fixtures: default runner, second runner, researcher, assigned/unassigned trees.
- Create `tests/Feature/RunnerAssignment/WorkPartitionTest.php` — the two queries partition correctly.
- Create `tests/Feature/RunnerAssignment/ExperimentRequestTest.php` — request validation.

**`primemover-orchestrator`**
- Modify `orchestrator/mcp_server/server.py` — `pm_create_experiment(runner_user_id=...)`, `upload_experiment` create payload.
- Modify `pmspec/spec/converter.py` — `ConversionResult.experiment_runner_user_id`.
- Modify `pmspec/cli.py` — create payload in `upload`.
- Modify `orchestrator/tests/test_mcp_write_tools.py`, `pmspec/tests/test_spec.py` — tests.
- Modify `docs/integration-contract.md`, `docs/new-server-setup.md`, `docs/runner-vm-migration-handoff.md` — contract + rollout docs.

---

### Task 1: Config, migration, model relation, Isolation helpers

**Files:**
- Create: `database/migrations/2026_09_02_000000_add_runner_user_id_to_experiments_table.php`
- Modify: `config/primemover.php` (append a `runner` key after `isolation`)
- Modify: `app/Access/Isolation.php`
- Modify: `app/Modules/Experiment/Model/Experiment.php`
- Test: `tests/Feature/RunnerAssignment/RunnerAssignmentTestCase.php`, `tests/Feature/RunnerAssignment/SchemaTest.php`

**Interfaces:**
- Produces: `Isolation::defaultRunnerUserId(): int`, `Isolation::isRunner(?User $user = null): bool` (true iff the id is in `serviceAccountIds()`), `Experiment::runner(): BelongsTo`, column `experiments.runner_user_id` (nullable FK → users, `onDelete('restrict')`, indexed), config `primemover.runner.default_user_id` (env `PM_DEFAULT_RUNNER_USER_ID`, default `1`).
- Consumes: existing `Isolation::serviceAccountIds()`, `IsolationTestCase::treeFor()` shape (experiment → crawler → queue with `processed=0`, `testing=0`, `active=1`).

- [ ] **Step 1: Write the shared fixture base class**

`tests/Feature/RunnerAssignment/RunnerAssignmentTestCase.php` (not `*Test.php`, so phpunit does not collect it):

```php
<?php

namespace Tests\Feature\RunnerAssignment;

use App\Models\User;
use Illuminate\Database\Eloquent\Model;
use Tests\Feature\Isolation\IsolationTestCase;

/**
 * Fixtures for runner assignment: two runner accounts (both service accounts,
 * one of them the configured default) and a researcher who owns experiments
 * assigned to each runner and one left unassigned (NULL = default runner).
 *
 * Reuses IsolationTestCase's tree builder so the crawler/queue flags match what
 * the runner's discovery queries select on.
 */
abstract class RunnerAssignmentTestCase extends IsolationTestCase
{
    protected User $defaultRunner;

    protected User $secondRunner;

    /** @var array<string, Model> researcher-owned, runner_user_id = NULL */
    protected array $unassigned = [];

    /** @var array<string, Model> researcher-owned, runner_user_id = defaultRunner */
    protected array $forDefault = [];

    /** @var array<string, Model> researcher-owned, runner_user_id = secondRunner */
    protected array $forSecond = [];

    public function setUp(): void
    {
        parent::setUp();

        $this->defaultRunner = $this->serviceAccount;
        $this->secondRunner = User::factory()->create();

        config([
            'primemover.isolation.service_account_ids' => [
                $this->defaultRunner->id,
                $this->secondRunner->id,
            ],
            'primemover.runner.default_user_id' => $this->defaultRunner->id,
        ]);

        // $this->mine (from the parent) is the researcher's unassigned tree.
        $this->unassigned = $this->mine;

        $this->forDefault = $this->treeFor($this->researcher);
        $this->forDefault['experiment']->update(['runner_user_id' => $this->defaultRunner->id]);

        $this->forSecond = $this->treeFor($this->researcher);
        $this->forSecond['experiment']->update(['runner_user_id' => $this->secondRunner->id]);
    }
}
```

- [ ] **Step 2: Write the failing schema/config test**

`tests/Feature/RunnerAssignment/SchemaTest.php`:

```php
<?php

namespace Tests\Feature\RunnerAssignment;

use App\Access\Isolation;
use App\Modules\Experiment\Model\Experiment;
use Illuminate\Support\Facades\Schema;

/**
 * @internal
 *
 * @coversDefaultClass \App\Access\Isolation
 */
class SchemaTest extends RunnerAssignmentTestCase
{
    public function test_experiments_have_a_nullable_runner_assignment(): void
    {
        $this->assertTrue(Schema::hasColumn('experiments', 'runner_user_id'));
        $this->assertNull($this->unassigned['experiment']->fresh()->runner_user_id);
        $this->assertTrue($this->forSecond['experiment']->fresh()->runner->is($this->secondRunner));
    }

    /**
     * @covers ::defaultRunnerUserId
     * @covers ::isRunner
     */
    public function test_isolation_knows_the_default_runner_and_the_runner_accounts(): void
    {
        $this->assertSame($this->defaultRunner->id, Isolation::defaultRunnerUserId());
        $this->assertTrue(Isolation::isRunner($this->defaultRunner));
        $this->assertTrue(Isolation::isRunner($this->secondRunner));
        $this->assertFalse(Isolation::isRunner($this->researcher));
    }

    public function test_default_runner_falls_back_to_user_one_when_unset(): void
    {
        config(['primemover.runner.default_user_id' => null]);
        $this->assertSame(1, Isolation::defaultRunnerUserId());
    }
}
```

- [ ] **Step 3: Run it to verify it fails**

Run (API repo root): `apitest tests/Feature/RunnerAssignment`
Expected: errors — `runner_user_id` column missing (`no such column` from SQLite), `Isolation::defaultRunnerUserId` undefined.

- [ ] **Step 4: Migration**

`database/migrations/2026_09_02_000000_add_runner_user_id_to_experiments_table.php`:

```php
<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class() extends Migration
{
    public function up(): void
    {
        Schema::table('experiments', function (Blueprint $table): void {
            // Which runner account executes this experiment's queues.
            // NULL means the default runner (config primemover.runner.default_user_id).
            // Deliberately separate from user_id: ownership is visibility,
            // this is execution.
            $table->foreignId('runner_user_id')
                ->nullable()
                ->after('user_id')
                ->constrained('users')
                ->onDelete('restrict');
        });
    }

    public function down(): void
    {
        Schema::table('experiments', function (Blueprint $table): void {
            $table->dropConstrainedForeignId('runner_user_id');
        });
    }
};
```

(`foreignId()->constrained()` creates the index; SQLite in tests accepts `after()` as a no-op.)

- [ ] **Step 5: Config key**

In `config/primemover.php`, after the closing `],` of the `'isolation' => [ ... ]` entry and before the final `];`, add:

```php
    /*
    |--------------------------------------------------------------------------
    | Runner assignment
    |--------------------------------------------------------------------------
    |
    | experiments.runner_user_id names the runner account that executes an
    | experiment. NULL means this default. Runner accounts are the
    | service_account_ids above -- they see everything (status reporting needs
    | that) but the two work-fetching queries only hand them experiments
    | assigned to them.
    |
    */
    'runner' => [
        'default_user_id' => (int) (env('PM_DEFAULT_RUNNER_USER_ID') ?: 1),
    ],
```

- [ ] **Step 6: Isolation helpers**

In `app/Access/Isolation.php`, add after `serviceAccountIds()`:

```php
    /**
     * The runner that executes experiments whose runner_user_id is NULL.
     */
    public static function defaultRunnerUserId(): int
    {
        return (int) (config('primemover.runner.default_user_id') ?: 1);
    }

    /**
     * Runner accounts are the service accounts: valid targets for
     * experiments.runner_user_id.
     */
    public static function isRunner(?User $user = null): bool
    {
        return self::isServiceAccount($user);
    }
```

- [ ] **Step 7: Model relation**

In `app/Modules/Experiment/Model/Experiment.php`, after `user()`:

```php
    /**
     * The runner account assigned to execute this experiment; null = default.
     */
    public function runner(): BelongsTo
    {
        return $this->belongsTo(User::class, 'runner_user_id');
    }
```

- [ ] **Step 8: Run the tests**

Run: `apitest tests/Feature/RunnerAssignment`
Expected: `OK (3 tests, ...)`.

- [ ] **Step 9: Pint, then commit**

Run: `apipint --test` — must PASS (if not, `apipint` to fix, re-run tests).

```bash
git add config/primemover.php app/Access/Isolation.php app/Modules/Experiment/Model/Experiment.php \
  database/migrations/2026_09_02_000000_add_runner_user_id_to_experiments_table.php tests/Feature/RunnerAssignment
git commit -m "feat: experiments carry an explicit runner assignment

runner_user_id (nullable FK to users, NULL = the default runner from
PM_DEFAULT_RUNNER_USER_ID) separates who owns an experiment from which
runner executes it. Column, config and helpers only; nothing reads it yet."
```

---

### Task 2: The partition scope on Crawler

**Files:**
- Modify: `app/Modules/Crawler/Model/Crawler.php` (add a scope near `queuesNotProcessed()`; `Builder` is already imported)
- Test: `tests/Feature/RunnerAssignment/WorkPartitionTest.php`

**Interfaces:**
- Produces: `Crawler::scopeAssignedToCallingRunner(Builder $query): Builder` — crawlers whose experiment has `runner_user_id = Isolation::userId()`, plus `runner_user_id IS NULL` when the caller is the default runner. Applies even when `Isolation::bypasses()` is true.
- Consumes: `Isolation::userId()`, `Isolation::defaultRunnerUserId()` from Task 1.

- [ ] **Step 1: Write the failing scope tests**

`tests/Feature/RunnerAssignment/WorkPartitionTest.php` (repository tests are added in Task 3; start with the scope):

```php
<?php

namespace Tests\Feature\RunnerAssignment;

use App\Modules\Crawler\Model\Crawler;

/**
 * Two runners, one API: each must fetch only the experiments assigned to it.
 * crawlers-unprocessed has no atomic claim, so an overlap means every queue
 * runs twice. NULL assignment belongs to the default runner.
 *
 * @internal
 *
 * @coversDefaultClass \App\Modules\Crawler\Model\Crawler
 */
class WorkPartitionTest extends RunnerAssignmentTestCase
{
    /**
     * @covers ::scopeAssignedToCallingRunner
     */
    public function test_default_runner_gets_unassigned_and_own_assignments_only(): void
    {
        $this->enableIsolation();
        $this->actingAs($this->defaultRunner);

        $ids = $this->scopedCrawlerIds();

        $this->assertContains((int) $this->unassigned['crawler']->id, $ids);
        $this->assertContains((int) $this->forDefault['crawler']->id, $ids);
        $this->assertNotContains((int) $this->forSecond['crawler']->id, $ids);
    }

    /**
     * @covers ::scopeAssignedToCallingRunner
     */
    public function test_second_runner_gets_only_its_assignments(): void
    {
        $this->enableIsolation();
        $this->actingAs($this->secondRunner);

        $ids = $this->scopedCrawlerIds();

        $this->assertSame([(int) $this->forSecond['crawler']->id], $ids);
    }

    /**
     * The scope is a partitioning rule, not an isolation rule: it applies with
     * isolation off too, or a second runner on an un-isolated API would double-run.
     *
     * @covers ::scopeAssignedToCallingRunner
     */
    public function test_partition_applies_with_isolation_disabled(): void
    {
        $this->disableIsolation();
        $this->actingAs($this->secondRunner);

        $this->assertSame([(int) $this->forSecond['crawler']->id], $this->scopedCrawlerIds());
    }

    /**
     * @covers ::scopeAssignedToCallingRunner
     */
    public function test_a_non_runner_caller_gets_nothing(): void
    {
        $this->enableIsolation();
        $this->actingAs($this->researcher);

        $this->assertSame([], $this->scopedCrawlerIds());
    }

    /**
     * @covers ::scopeAssignedToCallingRunner
     */
    public function test_a_guest_gets_nothing(): void
    {
        $this->enableIsolation();
        auth()->logout();

        $this->assertSame([], $this->scopedCrawlerIds());
    }

    /** @return array<int, int> */
    private function scopedCrawlerIds(): array
    {
        return Crawler::query()
            ->assignedToCallingRunner()
            ->orderBy('id')
            ->pluck('id')
            ->map(fn ($id): int => (int) $id)
            ->all();
    }
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `apitest tests/Feature/RunnerAssignment/WorkPartitionTest.php`
Expected: `BadMethodCallException: Call to undefined method ... assignedToCallingRunner()`.

- [ ] **Step 3: Implement the scope**

In `app/Modules/Crawler/Model/Crawler.php`, add `use App\Access\Isolation;` to the imports, and add this method (e.g. right after `queuesNotProcessed()`):

```php
    /**
     * Crawlers this caller, as a runner, is assigned to execute.
     *
     * Partition rule for the two work-fetching endpoints (crawlers-unprocessed,
     * crawlers-testing). Unlike visibleTo() it does NOT honour
     * Isolation::bypasses(): runners are service accounts precisely so they can
     * see everything for status reporting, yet they must never fetch each
     * other's work, because there is no atomic queue claim.
     */
    public function scopeAssignedToCallingRunner(Builder $query): Builder
    {
        $me = Isolation::userId();

        if (null === $me) {
            // No caller, no work. (where(col, null) would compile to IS NULL and
            // hand every unassigned experiment to a guest.)
            return $query->whereRaw('0 = 1');
        }

        return $query->whereHas('experiment', function (Builder $q) use ($me): void {
            $q->where('experiments.runner_user_id', $me);
            if ($me === Isolation::defaultRunnerUserId()) {
                $q->orWhereNull('experiments.runner_user_id');
            }
        });
    }
```

- [ ] **Step 4: Run the tests**

Run: `apitest tests/Feature/RunnerAssignment`
Expected: all pass (`OK (8 tests, ...)`).

- [ ] **Step 5: Pint + commit**

```bash
apipint --test
git add app/Modules/Crawler/Model/Crawler.php tests/Feature/RunnerAssignment/WorkPartitionTest.php
git commit -m "feat: Crawler::assignedToCallingRunner scope

The partition rule for the runner's work-fetching queries: assigned to me,
or unassigned when I am the default runner. Ignores isolation bypass on
purpose -- see the docblock."
```

---

### Task 3: Apply the scope to `crawlers-unprocessed` and `crawlers-testing`

**Files:**
- Modify: `app/Modules/Crawler/Repository/CrawlerRepository.php` — `unprocessed()` (line ~91) and `testing()` (line ~110)
- Test: `tests/Feature/RunnerAssignment/WorkPartitionTest.php` (append tests)

**Interfaces:**
- Consumes: `Crawler::scopeAssignedToCallingRunner()` (Task 2).
- Produces: `CrawlerRepository::unprocessed()` and `::testing()` return only the calling runner's slice. Endpoint routes are unchanged: `GET /api/v1/crawlers-unprocessed`, `GET /api/v1/crawlers-testing`.

- [ ] **Step 1: Append failing repository + endpoint tests**

Add to `WorkPartitionTest` (and add `use App\Modules\Crawler\Repository\CrawlerRepository;` to its imports):

```php
    /**
     * @covers \App\Modules\Crawler\Repository\CrawlerRepository::unprocessed
     */
    public function test_unprocessed_is_partitioned_per_runner(): void
    {
        $this->enableIsolation();
        $repo = new CrawlerRepository(new Crawler());

        $this->actingAs($this->defaultRunner);
        $forDefault = $this->idsOf($repo->unprocessed());
        $this->assertContains((int) $this->unassigned['crawler']->id, $forDefault);
        $this->assertContains((int) $this->forDefault['crawler']->id, $forDefault);
        $this->assertNotContains((int) $this->forSecond['crawler']->id, $forDefault);

        $this->actingAs($this->secondRunner);
        $this->assertSame([(int) $this->forSecond['crawler']->id], $this->idsOf($repo->unprocessed()));
    }

    /**
     * @covers \App\Modules\Crawler\Repository\CrawlerRepository::testing
     */
    public function test_testing_is_partitioned_per_runner(): void
    {
        $this->enableIsolation();
        foreach ([$this->unassigned, $this->forDefault, $this->forSecond] as $tree) {
            $tree['crawler']->update(['testing' => 1]);
        }
        $repo = new CrawlerRepository(new Crawler());

        $this->actingAs($this->secondRunner);
        $this->assertSame([(int) $this->forSecond['crawler']->id], $this->idsOf($repo->testing()->items()));

        $this->actingAs($this->defaultRunner);
        $forDefault = $this->idsOf($repo->testing()->items());
        $this->assertContains((int) $this->unassigned['crawler']->id, $forDefault);
        $this->assertNotContains((int) $this->forSecond['crawler']->id, $forDefault);
    }

    /**
     * Through HTTP, as the Java runner does it.
     */
    public function test_endpoints_serve_each_runner_its_own_slice(): void
    {
        $this->enableIsolation();

        $this->actingAs($this->secondRunner)
            ->getJson('/api/v1/crawlers-unprocessed')
            ->assertOk()
            ->assertJsonCount(1, 'data')
            ->assertJsonPath('data.0.id', $this->forSecond['crawler']->id);

        $response = $this->actingAs($this->defaultRunner)->getJson('/api/v1/crawlers-unprocessed')->assertOk();
        $ids = array_map('intval', array_column($response->json('data'), 'id'));
        $this->assertNotContains((int) $this->forSecond['crawler']->id, $ids);
        $this->assertContains((int) $this->unassigned['crawler']->id, $ids);
    }
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `apitest tests/Feature/RunnerAssignment/WorkPartitionTest.php`
Expected: the three new tests fail (second runner receives more than its own crawler).

- [ ] **Step 3: Apply the scope in the repository**

In `CrawlerRepository::unprocessed()` change the first line of the chain from

```php
        return $this->crawler->query()->visibleTo()->where('testing', 0)
```
to
```php
        return $this->crawler->query()->visibleTo()->assignedToCallingRunner()->where('testing', 0)
```

and in `testing()` from

```php
        return $this->crawler->query()->visibleTo()->where('testing', 1)
```
to
```php
        return $this->crawler->query()->visibleTo()->assignedToCallingRunner()->where('testing', 1)
```

Do not touch `reviewed()`, `processed()`, `inactive()` or anything else.

- [ ] **Step 4: Run the whole suite**

Run: `apitest`
Expected: everything green. Watch for `tests/Feature/Modules/Crawler/Repository/CrawlerRepositoryTest::test_unprocessed` and `::test_testing` — they iterate over results, so an empty result still passes; if any other existing test asserts a count from these two methods, that test's caller must now be a runner: fix by `config(['primemover.runner.default_user_id' => $this->user->id])` in that test's setUp, and say so in the commit body.

- [ ] **Step 5: Pint + commit**

```bash
apipint --test
git add app/Modules/Crawler/Repository/CrawlerRepository.php tests/Feature/RunnerAssignment/WorkPartitionTest.php
git commit -m "feat: partition crawlers-unprocessed and crawlers-testing by runner assignment

The only two endpoints the Java runner fetches work from. Everything else,
including the runner's status PUTs, is untouched."
```

---

### Task 4: Researchers may set `runner_user_id` on create/update

**Files:**
- Modify: `app/Modules/Experiment/Http/Request/ExperimentStoreRequest.php` (serves both `store()` and `update()`)
- Test: `tests/Feature/RunnerAssignment/ExperimentRequestTest.php`

**Interfaces:**
- Produces: `POST /api/v1/experiments` and `PUT /api/v1/experiments/{id}` accept optional `runner_user_id` (integer, must be in `Isolation::serviceAccountIds()`, or null). Response JSON includes `runner_user_id`.
- Consumes: `Isolation::serviceAccountIds()`.

- [ ] **Step 1: Write the failing request tests**

`tests/Feature/RunnerAssignment/ExperimentRequestTest.php`:

```php
<?php

namespace Tests\Feature\RunnerAssignment;

/**
 * A researcher chooses where an experiment runs; the API only accepts a
 * configured runner account as the target.
 *
 * @internal
 *
 * @coversDefaultClass \App\Modules\Experiment\Http\Request\ExperimentStoreRequest
 */
class ExperimentRequestTest extends RunnerAssignmentTestCase
{
    public function setUp(): void
    {
        parent::setUp();
        $this->enableIsolation();
        $this->withExceptionHandling();
        $this->actingAs($this->researcher);
    }

    /**
     * @covers ::rules
     */
    public function test_researcher_can_assign_a_runner_on_create(): void
    {
        $response = $this->postJson('/api/v1/experiments', [
            'name' => 'VM study',
            'contact' => 'r@example.org',
            'runner_user_id' => $this->secondRunner->id,
        ])->assertCreated();

        $this->assertSame($this->secondRunner->id, (int) $response->json('data.runner_user_id'));
        $this->assertSame($this->researcher->id, (int) $response->json('data.user_id'), 'Ownership still comes from Auth.');
        $this->assertDatabaseHas('experiments', ['name' => 'VM study', 'runner_user_id' => $this->secondRunner->id]);
    }

    /**
     * @covers ::rules
     */
    public function test_omitting_the_runner_stores_null(): void
    {
        $response = $this->postJson('/api/v1/experiments', [
            'name' => 'Default study',
            'contact' => 'r@example.org',
        ])->assertCreated();

        $this->assertNull($response->json('data.runner_user_id'));
    }

    /**
     * @covers ::rules
     */
    public function test_a_non_runner_target_is_rejected(): void
    {
        $this->postJson('/api/v1/experiments', [
            'name' => 'Nope',
            'contact' => 'r@example.org',
            'runner_user_id' => $this->otherResearcher->id,
        ])->assertUnprocessable()->assertJsonValidationErrors('runner_user_id');

        $this->assertDatabaseMissing('experiments', ['name' => 'Nope']);
    }

    /**
     * @covers ::rules
     */
    public function test_runner_can_be_changed_and_cleared_on_update(): void
    {
        $experiment = $this->unassigned['experiment'];

        $this->putJson('/api/v1/experiments/'.$experiment->id, [
            'name' => $experiment->name,
            'runner_user_id' => $this->secondRunner->id,
        ])->assertOk();
        $this->assertSame($this->secondRunner->id, (int) $experiment->fresh()->runner_user_id);

        $this->putJson('/api/v1/experiments/'.$experiment->id, [
            'name' => $experiment->name,
            'runner_user_id' => null,
        ])->assertOk();
        $this->assertNull($experiment->fresh()->runner_user_id);
    }
}
```

If `assertCreated()` fails because the controller returns 200, replace it with `->assertSuccessful()` — the status code is pre-existing behaviour, not part of this change.

- [ ] **Step 2: Run to verify they fail**

Run: `apitest tests/Feature/RunnerAssignment/ExperimentRequestTest.php`
Expected: the first and fourth tests fail (`runner_user_id` is not validated, so `validated()` drops it and the column stays NULL); the third fails (no 422).

- [ ] **Step 3: Validate the field**

In `ExperimentStoreRequest.php` add `use App\Access\Isolation;` and `use Illuminate\Validation\Rule;` to the imports, and change `rules()` to:

```php
    public function rules(): array
    {
        return [
            'name' => ['required'],
            'description' => ['nullable'],
            'user_id' => ['required'],
            'contact' => ['nullable'],
            // Which runner executes this experiment. Only a configured runner
            // account is a valid target; null/absent = the default runner.
            'runner_user_id' => ['nullable', 'integer', Rule::in(Isolation::serviceAccountIds())],
        ];
    }
```

`prepareForValidation()` stays as it is: `user_id` still comes from `Auth::id()`, and `runner_user_id` passes straight through. If Laravel's `validated()` omits a key that was sent as `null` and the fourth test's "cleared" half fails, add to `prepareForValidation()`:

```php
        if ($this->exists('runner_user_id') && '' === (string) $this->input('runner_user_id')) {
            $this->merge(['runner_user_id' => null]);
        }
```

- [ ] **Step 4: Run the suite**

Run: `apitest`
Expected: all green.

- [ ] **Step 5: Pint + commit**

```bash
apipint --test
git add app/Modules/Experiment/Http/Request/ExperimentStoreRequest.php tests/Feature/RunnerAssignment/ExperimentRequestTest.php
git commit -m "feat: researchers can set runner_user_id on experiment create/update

Validated against the configured runner accounts; ownership is still
derived from the authenticated user."
```

---

### Task 5: API docs, push, PR

**Files:**
- Modify: `config/primemover.php` comment on `service_account_ids` (it still says "has to stay [1] until the runner is moved onto its own identity" — now both runners are listed)
- Modify: `README.md` or `.env.example` if they list `PM_SERVICE_ACCOUNT_IDS` (check with `grep -rn PM_SERVICE_ACCOUNT_IDS --include=*.md --include=.env.example .`) — add `PM_DEFAULT_RUNNER_USER_ID` next to it.

- [ ] **Step 1: Update the config comment**

Replace the `service_account_ids` comment block in `config/primemover.php` with:

```php
        /*
         | Users who see everything. Machine callers, not people: every runner
         | account (ex63 = admin@primemover.example, user 1; the runner-only VM =
         | runner@primemover.example, user 3), which the orchestrator CLI/MCP/e2e also use.
         | They are also the only valid targets for experiments.runner_user_id;
         | the two work-fetching queries partition them by that column.
         */
```

- [ ] **Step 2: `.env.example`**

If `grep -n PM_SERVICE_ACCOUNT_IDS .env.example` finds a line, add directly below it:

```
# Runner that executes experiments with runner_user_id = NULL (default 1).
PM_DEFAULT_RUNNER_USER_ID=1
```

If it finds nothing, add both lines at the end of `.env.example` under a `# Per-user isolation / runner partitioning` heading.

- [ ] **Step 3: Full check, commit, push, PR**

```bash
apitest && apipint --test
git add -A config/primemover.php .env.example README.md 2>/dev/null
git commit -m "docs: runner assignment env and config notes"
git push -u origin feat/runner-assignment
gh pr create --base master --title "Runner assignment: decouple experiment ownership from execution" --body "$(cat <<'EOF'
## Why
Two runners (ex63, the runner-only VM) share this API and there is no atomic queue claim. Partitioning by *owner* made experiments created under researchers' own logins unrunnable. This adds an explicit `experiments.runner_user_id`.

## What
- `runner_user_id` (nullable FK → users; NULL = default runner, `PM_DEFAULT_RUNNER_USER_ID`, default 1)
- `Crawler::assignedToCallingRunner()` scope applied to `crawlers-unprocessed` and `crawlers-testing` only — the two endpoints the Java runner fetches work from. Applies regardless of isolation bypass.
- `ExperimentStoreRequest` accepts `runner_user_id`, validated against `PM_SERVICE_ACCOUNT_IDS`.
- Tests in `tests/Feature/RunnerAssignment/`.

## Rollout (ex63)
`git pull` → `php artisan migrate` → `.env`: `PM_SERVICE_ACCOUNT_IDS=1,3`, `PM_DEFAULT_RUNNER_USER_ID=1` → `php artisan config:clear`.

Design: primemover-orchestrator `docs/superpowers/specs/2026-09-02-runner-assignment-design.md`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01X6ZG2SdXdEezCxfT33LaSg
EOF
)"
```

Note the branch is cut from `chore/pint-baseline`; the PR will show its two formatting commits too unless that branch is merged first. That is expected.

---

### Task 6: Orchestrator MCP — `pm_create_experiment(runner_user_id=…)` and `upload_experiment`

**Files:**
- Modify: `orchestrator/mcp_server/server.py` — `pm_create_experiment` (line ~670) and the experiment-create payload inside `upload_experiment` (line ~2123, the `requests.post(f"{api_url}/experiments", json={...})` block)
- Test: `orchestrator/tests/test_mcp_write_tools.py` (class `TestCreateExperiment`)

**Interfaces:**
- Produces: `pm_create_experiment(name, contact, description="", testing=False, runner_user_id: int | None = None, confirm=False)`; payload contains `"runner_user_id"` only when given. `upload_experiment(yaml_content, experiment_id=None, dry_run=True, runner_user_id: int | None = None)` — same rule for its create call.

- [ ] **Step 1: Write the failing tests**

Append to `class TestCreateExperiment` in `orchestrator/tests/test_mcp_write_tools.py`:

```python
    def test_runner_assignment_is_passed_through_when_given(self, api: RecordingAPI) -> None:
        out = json.loads(
            s.pm_create_experiment(name="Study A", contact="ada@example.org", runner_user_id=3)
        )
        assert out["payload"]["runner_user_id"] == 3
        assert api.calls == []

    def test_runner_assignment_is_absent_by_default(self, api: RecordingAPI) -> None:
        out = json.loads(s.pm_create_experiment(name="Study A", contact="ada@example.org"))
        assert "runner_user_id" not in out["payload"]  # NULL = default runner, API-side
```

- [ ] **Step 2: Run to verify they fail**

Run (orchestrator root): `venv/bin/pytest orchestrator/tests/test_mcp_write_tools.py -k runner_assignment -v` (use `.venv` or `python -m pytest` if the venv lives elsewhere: `ls venv .venv`).
Expected: `TypeError: pm_create_experiment() got an unexpected keyword argument 'runner_user_id'`.

- [ ] **Step 3: Implement**

Replace `pm_create_experiment` with:

```python
@mcp.tool()
def pm_create_experiment(
    name: str,
    contact: str,
    description: str = "",
    testing: bool = False,
    runner_user_id: int | None = None,
    confirm: bool = False,
) -> str:
    """Create an experiment (study). Dry-run unless confirm=true. Local API only.

    Args:
        name: Study name.
        contact: Contact person (required by the API).
        description: Optional description.
        testing: Mark as a testing experiment (runner uses testing endpoints).
        runner_user_id: Which runner executes it: 1 = ex63 (the default when omitted),
            3 = the runner-only VM. Must be a configured runner account on the API.
        confirm: Apply the change; without it, returns a preview only.
    """
    payload: dict[str, Any] = {
        "name": name,
        "contact": contact,
        "description": description or name,
        "testing": testing,
    }
    if runner_user_id is not None:
        payload["runner_user_id"] = runner_user_id
    return _write("create experiment", "POST", "experiments", payload, confirm=confirm)
```

(Keep the existing `@mcp.tool()` decorator line — do not duplicate it.)

In `upload_experiment`, add the parameter `runner_user_id: int | None = None` after `dry_run: bool = True`, document it in the docstring's Args with the same 1/3 sentence, and change the create call's body to:

```python
            create_payload: dict[str, Any] = {
                "name": exp_name,
                "description": result.experiment_description or "Created via MCP",
                "contact": "PrimeMover Orchestrator MCP",
                "active": 1,
            }
            if runner_user_id is not None:
                create_payload["runner_user_id"] = runner_user_id
            elif getattr(result, "experiment_runner_user_id", None) is not None:
                create_payload["runner_user_id"] = result.experiment_runner_user_id

            create_response = requests.post(
                f"{api_url}/experiments",
                json=create_payload,
                headers={
```

(`experiment_runner_user_id` is added to `ConversionResult` in Task 7; `getattr` keeps this task green on its own.)

- [ ] **Step 4: Run the tests**

Run: `venv/bin/pytest orchestrator/tests/test_mcp_write_tools.py orchestrator/tests/test_mcp_write_guard.py -v`
Expected: all pass.

- [ ] **Step 5: Lint + commit**

```bash
make check    # black, ruff, mypy, pytest
git add orchestrator/mcp_server/server.py orchestrator/tests/test_mcp_write_tools.py
git commit -m "mcp: pm_create_experiment and upload_experiment take runner_user_id"
```

---

### Task 7: Experiment designer — `experiment.runner_user_id` in the YAML spec

**Files:**
- Modify: `pmspec/spec/converter.py` — `ConversionResult` (line ~21) and both `ConversionResult(...)` constructions in `convert()` (lines ~119 and ~161)
- Modify: `pmspec/cli.py` — create payload in `upload` (line ~336)
- Test: `pmspec/tests/test_spec.py` (class `TestSpecConverter`)

**Interfaces:**
- Produces: `ConversionResult.experiment_runner_user_id: int | None` (from `parsed.experiment.get("runner_user_id")`); a non-integer value is a conversion error `"experiment.runner_user_id must be an integer runner account id (1 = ex63, 3 = VM)"`.
- Consumes: `ParsedSpec.experiment` (the parser already passes the whole `experiment:` mapping through; the JSON schema declares no experiment properties, so the key is not rejected).

- [ ] **Step 1: Write the failing tests**

Append to `class TestSpecConverter` in `pmspec/tests/test_spec.py`:

```python
    def test_convert_carries_runner_assignment(self):
        yaml_content = """
experiment:
  name: VM study
  runner_user_id: 3

crawlers:
  - name: Test Crawler
    proxy:
      hostname: proxy.example.com
      port: 8080
    session:
      workflow:
        - visit: https://example.com
"""
        result = self.converter.convert(self.parser.parse(yaml_content))
        self.assertEqual(result.errors, [])
        self.assertEqual(result.experiment_runner_user_id, 3)

    def test_convert_defaults_runner_assignment_to_none(self):
        yaml_content = """
experiment:
  name: Default study

crawlers:
  - name: Test Crawler
    proxy:
      hostname: proxy.example.com
      port: 8080
    session:
      workflow:
        - visit: https://example.com
"""
        result = self.converter.convert(self.parser.parse(yaml_content))
        self.assertIsNone(result.experiment_runner_user_id)

    def test_convert_rejects_non_integer_runner_assignment(self):
        yaml_content = """
experiment:
  name: Bad
  runner_user_id: vm

crawlers:
  - name: Test Crawler
    proxy:
      hostname: proxy.example.com
      port: 8080
    session:
      workflow:
        - visit: https://example.com
"""
        result = self.converter.convert(self.parser.parse(yaml_content))
        self.assertTrue(any("runner_user_id" in e for e in result.errors))
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/pytest pmspec/tests/test_spec.py -k runner_assignment -v`
Expected: `AttributeError: 'ConversionResult' object has no attribute 'experiment_runner_user_id'`.

- [ ] **Step 3: Implement**

In `converter.py`, add the field to the dataclass after `experiment_description`:

```python
    experiment_runner_user_id: int | None = None  # None = the API's default runner
```

Because dataclass fields with defaults must follow fields without, place it *after* `total_jobs` and before `errors`:

```python
    crawler_count: int
    total_jobs: int
    experiment_runner_user_id: int | None = None  # None = the API's default runner
    errors: list[str] = field(default_factory=list)
```

Add a helper method to `SpecConverter` (next to `__init__`):

```python
    def _runner_user_id(self, experiment: dict[str, Any]) -> int | None:
        """experiment.runner_user_id, validated; None when absent."""
        value = experiment.get("runner_user_id")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            self.errors.append(
                "experiment.runner_user_id must be an integer runner account id (1 = ex63, 3 = VM)"
            )
            return None
        return value
```

In `convert()`, compute `runner_user_id = self._runner_user_id(parsed.experiment)` right after `self.warnings = list(parsed.warnings)`, and pass `experiment_runner_user_id=runner_user_id` in **both** `ConversionResult(...)` constructions. The early-return branch checks `parsed.errors` before your helper runs, so call the helper first and change that condition to `if self.errors:` so a bad runner id also short-circuits.

In `pmspec/cli.py` `upload`, change the create call to build the payload the same way as Task 6:

```python
            create_payload = {
                "name": exp_name,
                "description": result.experiment_description
                or "Created via experiment designer CLI",
                "contact": "Experiment Designer CLI",
                "active": 1,
            }
            if result.experiment_runner_user_id is not None:
                create_payload["runner_user_id"] = result.experiment_runner_user_id

            create_response = requests.post(
                f"{api_url}/experiments",
                json=create_payload,
```

and print it: after `click.echo(f"Experiment: {result.experiment_name}")` (line ~286) add
`click.echo(f"Runner: {result.experiment_runner_user_id or 'default'}")`.

- [ ] **Step 4: Run the tests**

Run: `venv/bin/pytest pmspec/tests -v`
Expected: all pass, including the three new ones.

- [ ] **Step 5: Lint + commit**

```bash
make check
git add pmspec/spec/converter.py pmspec/cli.py pmspec/tests/test_spec.py
git commit -m "designer: experiment.runner_user_id chooses the runner for a spec"
```

---

### Task 8: Docs — contract, setup guide, handoff; push, PR

**Files:**
- Modify: `docs/integration-contract.md` — new section "Runner assignment" (after the section that documents `crawlers-unprocessed`; find it with `grep -n "crawlers-unprocessed" docs/integration-contract.md`)
- Modify: `docs/new-server-setup.md` — the "The hard requirement: partition the work" subsection
- Modify: `docs/runner-vm-migration-handoff.md` — append to the status section
- Modify: `docs/pmspec.md` — the experiment-section field list (find with `grep -n "experiment:" docs/pmspec.md | head -3`)

- [ ] **Step 1: integration-contract.md**

Add:

```markdown
### Runner assignment (2026-09-02)

`experiments.runner_user_id` (nullable, FK → users) names the runner account that
executes an experiment. `NULL` means the default runner, `PM_DEFAULT_RUNNER_USER_ID`
on the API (1 = ex63). Valid targets are exactly the accounts in
`PM_SERVICE_ACCOUNT_IDS`; on ex63 that is `1,3` (ex63's runner, the VM's runner).

Only two endpoints are partitioned by it, the two the Java runner fetches work from:
`GET /crawlers-unprocessed` and `GET /crawlers-testing` return crawlers whose
experiment is assigned to the calling runner (plus unassigned ones for the default
runner). The filter applies regardless of isolation bypass. Every other endpoint —
including the runner's status PUTs — is unaffected.

Ownership (`user_id`) is visibility only. Researchers create experiments under their
own login and pick the runner with `runner_user_id` (create/update requests; the
MCP `pm_create_experiment` and the YAML `experiment.runner_user_id`).
```

- [ ] **Step 2: new-server-setup.md**

Replace the paragraph starting `**Status on ex63 (re-verified 2026-09-02 -- corrects an earlier wrong note` through the end of that subsection (it ends just before `### Multilogin limits are per workspace`) with:

```markdown
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
```

- [ ] **Step 3: handoff + designer docs**

Append to the status section of `docs/runner-vm-migration-handoff.md`:

```markdown
**Partitioning, second iteration (2026-09-02, after the smoke test):** owner-based
partitioning made researchers' own experiments unrunnable, so it was replaced by an
explicit `experiments.runner_user_id` (API PR "Runner assignment"). After that
rollout: `PM_SERVICE_ACCOUNT_IDS=1,3`, `PM_DEFAULT_RUNNER_USER_ID=1`, experiments 42
and 143 returned to users 5 and 6. Assign an experiment to the VM with
`runner_user_id=3`; everything else runs on ex63.
```

In `docs/pmspec.md`, in the list of `experiment:` fields, add:

```markdown
- `runner_user_id` (optional, integer): which runner executes the study. `1` = ex63
  (the default when omitted), `3` = the runner-only VM.
```

- [ ] **Step 4: Commit, push, PR**

```bash
git add docs/integration-contract.md docs/new-server-setup.md docs/runner-vm-migration-handoff.md docs/pmspec.md
git commit -m "docs: runner assignment replaces owner-based partitioning"
git push -u origin feat/runner-assignment
gh pr create --base master --title "Runner assignment: MCP + designer support, docs" --body "$(cat <<'EOF'
Companion to primemover_api PR "Runner assignment". `pm_create_experiment` / `upload_experiment` and the YAML `experiment.runner_user_id` pass the new field through; docs describe the contract and rollout.

Spec: `docs/superpowers/specs/2026-09-02-runner-assignment-design.md`, plan: `docs/superpowers/plans/2026-09-02-runner-assignment.md`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01X6ZG2SdXdEezCxfT33LaSg
EOF
)"
```

The three pre-existing uncommitted files (`CLAUDE.md`, `docs/runner-vm-migration-handoff.md` status section, `scripts/multilogin-login.py`) are the user's; the handoff edit in Step 3 lands in the same file — commit only the hunk you added if `git add -p` is practical, otherwise commit the whole file and say so in the PR body.

---

### Task 9: Rollout on ex63 and verification

Operational, not code. Some steps edit ex63's API `.env`, which the auto-mode classifier blocks — hand those to the user as the exact commands below and continue with the read-only verification once they confirm.

**Preconditions:** API PR merged to master; orchestrator PR merged.

- [ ] **Step 1: Deploy the API on ex63** (user runs; `ssh ex63`):

```bash
cd ~/primemover_api && git fetch -q && git checkout master && git pull --ff-only
docker exec primemover-php8.1 php artisan migrate --force
cp -p .env .env.bak-$(date +%Y%m%d-%H%M%S)
sed -i 's/^PM_SERVICE_ACCOUNT_IDS=.*/PM_SERVICE_ACCOUNT_IDS=1,3/' .env
grep -q '^PM_DEFAULT_RUNNER_USER_ID=' .env || echo 'PM_DEFAULT_RUNNER_USER_ID=1' >> .env
docker exec primemover-php8.1 php artisan config:clear
docker exec primemover-php8.1 php artisan tinker --execute='echo json_encode(["service_account_ids"=>config("primemover.isolation.service_account_ids"),"default_runner"=>config("primemover.runner.default_user_id")]), PHP_EOL;'
```
Expected last line: `{"service_account_ids":[1,3],"default_runner":1}`.

- [ ] **Step 2: Give 42 and 143 back** (Claude, via tinker — the same kind of update that worked earlier):

```php
DB::table('experiments')->where('id', 42)->update(['user_id' => 5]);
DB::table('experiments')->where('id', 143)->update(['user_id' => 6]);
```
Verify `runner_user_id` is NULL on both (they keep running on ex63).

- [ ] **Step 3: Verify partitioning end to end**

1. `ssh pm-vm 'sudo journalctl -u primemover-runner --since "-3min" -o cat | grep -c "consider joblists"'` ≥ 2 — the VM still polls (it now bypasses isolation and sees everything, but fetches nothing).
2. ex63 runner log: unchanged polling, no errors (`grep -c "consider joblists"` on `~/primemover_runner/primemover-runner/log/primemover-runner.log` for the last minutes).
3. From the VM, run the smoke test assigned to the VM: `orchestrator e2e --chrome` is not enough (no assignment field there) — instead create an experiment via the MCP with `runner_user_id=3` and a testing bot, or via `pmspec upload` with `experiment.runner_user_id: 3`, and confirm in the VM runner log that it picks the queue up and ex63's log never mentions it.
4. Create an experiment as Matthias or Ulrich through the GUI with no assignment, schedule a short run, and confirm ex63's runner picks it up.

- [ ] **Step 4: Update the orchestrator MCP note in `CLAUDE.md`** of this repo if it states that the MCP sees only user-1 experiments (grep `service account`); it sees everything again.
