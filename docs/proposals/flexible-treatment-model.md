# Proposal: a generic, lambda-driven treatment model

**Status:** draft for review (track D of the 2026-09-02 experiment-design review).
Not implemented. This proposes a direction and a sequencing; it asks for
decisions before any code.

## Problem

"Treatment" is currently modeled as **one study's six numeric switches**
(`pi, psi, kappa, tau, beta, alpha`) plus two lists (`search_terms`,
`media_outlet_urls`), typed identically at every layer. That was right for the
YouTube personalization study, but it is not a general framework: a new
experiment with a different treatment cannot express it without editing code in
several repos.

The transport is already generic — the runner ships the whole page source, the
full crawler and the raw configuration to the lambda as **opaque JSON**
(`LambdaSelectJobHandler.java:398`, `LambdaCaller.java:193`). But arbitrary
treatment config cannot reach the decision function today because of **four typed
chokepoints**, each of which silently drops anything outside the fixed keys:

1. `experiment_designer/spec/converter.py` — rebuilt `params` from a fixed
   whitelist. *(Track B has opened this to pass unknown keys through; kept here
   for the full picture.)*
2. `primemover_api` `params` table — one typed column per param
   (`…create_params_table.php`).
3. `primemover_runner` `ConfigurationParam` — typed fields +
   `@JsonIgnoreProperties(ignoreUnknown=true)` (`ConfigurationParam.java:13`),
   copied into a typed `SelectionConfiguration`.
4. `bot-decision-yt` `lambda_function.py` — reads the six names directly
   (`:656`), runs hardwired `if preference_x == 1` branches (`:1078`, `:1162`, …),
   and picks LLM-vs-random as a *side effect* of which fixed param built a rating
   column (`:1356`) rather than as a per-experiment choice.

The DSL spine above the chokepoints is already generic: `maps_to` sets arbitrary
dotted paths (`design_expander.py:614`), distributions and data pools are
treatment-agnostic, and `lambda_select` already carries an arbitrary
`task`/`function`.

## Goal

An experiment declares its treatment as **arbitrary config**, which flows
unmodified to the lambda, where a **per-experiment strategy** decides. Three
strategy kinds must be first-class:

- **LLM-driven** — an LLM rates/filters candidates (today's `llm` mode).
- **Parameter + decision-function** — deterministic rules over declared params
  (today's `if pi == 1` branches, but registered per study).
- **Random / control** — no treatment applied (today's `random` mode).

The framework should not know the *names* of any study's params. It should carry
config and route to a strategy; the strategy owns the meaning.

## Design

### 1. A generic config channel (remove the three remaining chokepoints)

Carry an opaque treatment blob end to end, **alongside** the legacy typed columns
(which stay, for back-compat — legacy rows are real, per the `failed`/`failure`
precedent in CLAUDE.md).

- **API** (`primemover_api`): add a JSON/`longText` column to `params` (e.g.
  `treatment_config`) or a key-value child table; the crawler-config serializer
  that emits `selectionConfiguration` includes it. Migration only adds a nullable
  column.
- **Runner** (`primemover_runner`): add a `Map<String,Object>` (or raw
  `JsonNode`) field to `ConfigurationParam` / `SelectionConfiguration` so unknown
  keys survive `@JsonIgnoreProperties`; `PrimemoverApiConverter` copies the blob
  into `selectionConfiguration`. `LambdaCaller` / the job envelope need **no**
  change — transport is already opaque.
- **Converter** (`experiment_designer`): already passes unknown `params` keys
  through (track B). Optionally emit the blob under a dedicated `treatment_config`
  key rather than loose in `params`, to keep the wire shape explicit.

### 2. A strategy dispatcher in the decision function

Replace the monolithic body of `lambda_function.py` with a **registry**:

- Read a `strategy` (or `study`) key from `selectionConfiguration`. If absent,
  default to a `youtube_v1` strategy that reproduces today's exact behavior — so
  the currently deployed experiments are unchanged (absence == legacy, per the
  "absence reads as unknown, never degraded" rule).
- Each strategy is a small object that declares: the config keys it reads, the
  filters it *requests*, and a `select(candidates, config, page) -> decision`
  method. The current six-param logic becomes the `youtube_v1` strategy verbatim.
- `selection_mode` becomes something the **strategy** reports (`llm` / `random` /
  strategy-specific), not an inference from column counts.

### 3. Generalize the degradation contract (build on it, don't replace it)

The response envelope (`degraded` / `treatment_lost` / `selection_mode` /
`filters` / `llm`, `docs/integration-contract.md` §7.3) is already
treatment-agnostic in shape and is the right foundation. The one change:
`treatment_lost` must stay computed **once, at the source** as "degraded AND
nothing requested applied", where each strategy reports its own
`requested` vs `applied` filter sets (rather than the global `FILTER_NAMES`
constant). `orchestrator/status.py` and the alert (`pm-lambda-treatment-lost`)
then work unchanged for any strategy.

### 4. DSL surface

No structural DSL change needed — `maps_to` already targets arbitrary paths. Add:
- a documented `treatment_config` (or `strategy`) block in the schema, open
  (`additionalProperties: true`) so studies carry their own keys;
- validation that is **strategy-aware where it can be** (e.g. the 0/1 switch rule
  from track A applies to the `youtube_v1` strategy's params, not universally);
- `get_spec_template("<strategy>")` templates per strategy.

## Backward compatibility

- Legacy typed columns and the six params keep working; `youtube_v1` is the
  default strategy, so **already-deployed experiments and the running decision
  function behave identically**.
- A payload with no `strategy` and no `treatment_config` is a legacy payload →
  `youtube_v1`. Absence is never treated as degraded.

## Sequencing (this spans four repos; order matters)

Like `docs/integration-contract.md` §0, the danger is mixed versions recording
*false* data rather than merely incomplete data. Proposed order:

1. **Decision function** first: add the strategy registry with `youtube_v1` as
   default **and** keep reading the legacy params. Deployable alone; no behavior
   change. (Reads the blob if present, ignores it otherwise.)
2. **API**: add the nullable `treatment_config` column + serializer. Deployable
   alone; nothing writes it yet.
3. **Runner**: carry the blob through `ConfigurationParam` → `selectionConfiguration`.
4. **experiment_designer / MCP**: emit `treatment_config`, add strategy templates
   and validation, doc resources.

At every step the system is runnable and legacy experiments are unaffected,
because the decision function tolerates the blob's absence from step 1.

## Risks & open questions (need decisions)

1. **Blob location on the wire** — a dedicated `treatment_config` object vs loose
   keys in `params`. Recommendation: dedicated, so legacy vs generic is explicit.
2. **Strategy identity** — a `strategy` enum the decision function registers, vs
   inferring from which config keys are present. Recommendation: explicit
   `strategy` key; inference is how we got the fragile `selection_mode`
   side-effect.
3. **Where strategies live** — all in `bot-decision-yt`, or a plugin dir. Start
   in-repo; revisit if studies proliferate.
4. **Validation ownership** — can the orchestrator validate a study's config
   without importing the decision function? Options: a JSON-schema per strategy
   shipped in `bot-decision-yt` and read by `validate_experiment_spec`, or a
   lambda `POST /validate` endpoint. Decision needed.
5. **Effort** — realistically a multi-day change across four repos with a
   migration and a decision-function refactor. Worth scoping as its own milestone
   rather than a single PR.

## What is already done (tracks A–C)

- A: 0/1 switch semantics enforced (schema + validator), the footgun template/docs
  fixed. B: converter passes unknown params through; validator warns they are not
  yet delivered; `try_scroll*` reachable. C: skills refreshed; design docs served
  as MCP resources; `get_job_type_schema` accepts either name.

These are the enablers; D is the architecture. **Decisions on the open questions
above are the next step before implementation.**
