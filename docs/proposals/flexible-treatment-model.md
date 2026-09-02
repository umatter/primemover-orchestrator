# Plan: a generic, strategy-dispatched treatment model

**Status:** decisions made 2026-09-02; **implementation plan below, not yet
built**. Track D of the experiment-design review. Tracks A–C (the enablers) are
merged.

## Problem

"Treatment" is currently modeled as **one study's six numeric switches**
(`pi, psi, kappa, tau, beta, alpha`) plus two lists (`search_terms`,
`media_outlet_urls`), typed identically at every layer. That was right for the
YouTube personalization study, but it is not a general framework: a new
experiment with different treatment logic cannot express it without code changes
in several repos, and the decision function is a monolith of hardwired
`if preference_x == 1` branches with LLM-vs-random chosen as a side effect.

## Goal

An experiment declares its treatment as **arbitrary config carried on the
`lambda_select` job's behaviours**, which flows to the decision function, where a
**per-experiment strategy** (named explicitly) decides. Three strategy kinds are
first-class: **LLM-driven**, **parameters + decision-function**, and
**random/control**. The framework never knows any study's parameter *names*; it
carries config and routes to a strategy, and the strategy owns the meaning.

## Decisions (settled 2026-09-02)

1. **Channel = behaviours.** Treatment config rides on the `lambda_select` job's
   behaviours (a generic `name`→`value` store that already flows DSL→API→runner),
   **not** a new `treatment_config` DB column. This avoids a production DB
   migration. Cost: treatment is per-job rather than per-crawler, so the DSL is
   responsible for applying it consistently across a session's select jobs.
2. **Explicit `strategy` behaviour**, e.g. `strategy = youtube_v1`. Defaulted to
   `youtube_v1` when absent, so deployed experiments are unchanged. No inference
   from which keys are present (that is how the fragile `selection_mode`
   side-effect happened).
3. **Strategies live in `bot-decision-yt`** as a registry, one module per study.
4. **Validation via a shared per-strategy JSON schema file** shipped in
   `bot-decision-yt`, read by both `validate_experiment_spec` (offline/CI) and the
   strategy at runtime — single source, offline-capable.
5. **Phased, decision-function-first**, ~two repos (runner + lambda; the API is
   untouched). Sequence below never lets a mixed version record false data.

## Why behaviours reach nothing today (the two real gaps)

Behaviours are generic in the DSL (`parser.py:732`, `converter.py:420`) and the
API DB (`create_behaviors_table.php`, unguarded `Behavior.php`), and are
serialized API→runner. But:

- The runner flattens them to a `Map` and copies only known keys (`task`,
  `linkValue`, …) into a **typed** internal `LambdaSelectJob`, dropping the rest
  (`PrimemoverApiConverter.java:169`, `:138`).
- The payload it POSTs (`LambdaSelectJobParam`) carries that typed job, which has
  **no behaviours field**, so `lambda_function.py` never sees them (it reads only
  `crawler.config.selectionConfiguration` + `pagesource`, `:656`).

So the plan's runner work is: carry the behaviours through to the payload. The
lambda work is: read + dispatch on them.

## Config shape on the wire

Behaviours are string `name`→`value`. A strategy reads:

- `strategy` — the strategy name (string).
- its own named behaviours for simple scalar config (e.g. `pi = 1`), **and/or**
- a single `treatment_config` behaviour whose value is a JSON string, for
  structured config.

The per-strategy JSON schema (decision #4) defines which of these the strategy
expects and validates them. `youtube_v1` reads the existing switch behaviours.

## Implementation plan

### Phase 1 — `bot-decision-yt` (deployable alone; zero behaviour change)

- Add `strategies/` registry. Each strategy declares: `name`, the config keys it
  reads, the filters it **requests**, a `schema.json`, and
  `select(candidates, config, page) -> decision`.
- Refactor the current monolith into a **`youtube_v1`** strategy — the existing
  pi/psi/kappa/tau logic, verbatim, so behaviour is identical.
- Dispatcher in `select_recommendation`:
  1. Read treatment config: prefer `event['job']['behaviors']` **if present**,
     else fall back to `crawler.config.selectionConfiguration` (legacy).
  2. Read `strategy`; default `youtube_v1` when absent.
  3. Route to the strategy; each strategy reports its own `requested`/`applied`
     filter sets.
- Keep `treatment_lost` computed **once, at the source** (degraded AND nothing
  requested applied), now per-strategy.
- Ship `strategies/<name>/schema.json` with importable `check_*` helpers (mirrors
  the `tests/test_mainstream_media_csv.py` importable-helper pattern).
- Tests: dispatch, **`youtube_v1` parity** (existing behaviour unchanged), the
  no-behaviours/no-strategy fallback, per-strategy degradation.
- **Deploy is a no-op behaviourally**: nothing sends behaviours yet, absence →
  `youtube_v1` → `selectionConfiguration`.

### Phase 2 — `primemover_runner` (makes behaviours reach the lambda)

- Give the internal job model a generic `Map<String,String>` (e.g. on `BaseJob`
  or `LambdaSelectJob`) to hold behaviours not consumed by typed fields.
- Populate it in `convertApiLambdaSelectJob` / `setBaseFields` from the behaviour
  map that `convertApiBehaviourList` already builds (`PrimemoverApiConverter.java`).
- Ensure it serializes into `LambdaSelectJobParam` so it reaches the payload
  (`LambdaSelectJobParam.java`, `LambdaSelectJobHandler.java:398`).
- Build in a throwaway clone (JDK 25, one jar). Test: a `lambda_select` job with
  an arbitrary behaviour serializes that behaviour into the payload.
- **Deploy only after Phase 1 is live.**

### Phase 3 — `primemover-orchestrator` (authoring + validation; no API change)

- `job_registry.py`: register `strategy` (and known treatment behaviours) as
  recognized optional behaviours on `lambda_select`.
- `validate_experiment_spec`: when a `lambda_select` job carries a `strategy`
  behaviour, load that strategy's `schema.json` from the `bot-decision-yt` sibling
  (like the decision symlink) and validate the treatment behaviours against it;
  if the sibling/schema is absent, **warn, do not hard-fail** (keep the
  orchestrator's dependency on the sibling soft).
- `get_spec_template("<strategy>")` per-strategy templates; update
  `docs/experiment-designer.md` and the `doc://treatment-params` resource to
  describe the strategy mechanism.
- Tests for registration, schema validation, and templates.

### Contract & docs

- `docs/integration-contract.md` §7.3: treatment now comes from the
  `lambda_select` job's behaviours (`strategy` + config); the degradation report
  is per-strategy (`requested`/`applied`). Update §0's deploy order to
  lambda → runner → DSL.
- `orchestrator/status.py` and the `pm-lambda-treatment-lost` alert are unchanged
  — `treatment_lost` stays a single source-computed boolean.

## Backward compatibility

- No API change; legacy typed columns and the six params keep working.
- A payload with no `strategy` behaviour and no job behaviours → `youtube_v1`
  reading `selectionConfiguration`, i.e. **today's exact behaviour**. Absence is
  never treated as degraded.

## Sequencing safety

Deploy **lambda → runner → DSL**. Before the runner change, no behaviours arrive,
so the lambda uses the legacy path unchanged. After it, behaviours arrive and
dispatch runs. An old spec (no `strategy`) always falls back to `youtube_v1`. At
no point does a mixed version record false data.

## Effort

Rough: Phase 1 ~1–2 days (registry refactor + parity tests), Phase 2 ~half a day
(runner map + payload), Phase 3 ~half–1 day (registry/validation/templates), plus
contract docs. Best scoped as one milestone.

## Remaining sub-decision (not blocking)

How the orchestrator reaches a strategy's `schema.json` — read the sibling
`bot-decision-yt` path if present (soft dependency, warn-and-skip otherwise), or
vendor/publish the schemas. Recommendation: soft sibling read, matching the
existing decision-symlink pattern. Decide at Phase 3.

## Status of the enablers (tracks A–C, merged)

A: 0/1 switch semantics enforced (schema + validator); footgun template/docs
fixed. B: converter passes unknown params through; validator warns; `try_scroll*`
reachable. C: skills refreshed; design docs served as MCP resources;
`get_job_type_schema` accepts either name.
