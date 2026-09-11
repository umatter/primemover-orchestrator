# Experiment Designer

The Experiment Designer provides a YAML-based Domain-Specific Language (DSL) for defining PrimeMover browser automation experiments. It supports factorial experimental designs, reusable templates, and automatic expansion into API-compatible JSON.

## Workflow: From Plain English to Running Experiments

The recommended workflow for creating experiments is:

### Step 1: Describe Your Experiment in Plain English

Start with a plain English description of your experimental design, similar to what you'd write in an R Markdown file or research proposal:

```
Study: Web Search Polarization
- 100 crawlers with different political orientations
- Usage types: 25% visits only, 25% searches only, 50% both
- Pi (political orientation) sampled uniformly from -1 to 1
- Kappa (engagement level) sampled from discrete distribution
- Media outlets selected based on proximity to crawler's pi value
- Search terms matched to crawler's political orientation
```

### Step 2: Draft YAML Specification with Claude

Ask Claude to translate your plain English description into a YAML specification:

```
"Based on my research design above, please draft a YAML specification
using the PrimeMover experiment designer DSL. Use:
- Proportional factor levels for usage types (25/25/50 split)
- Uniform distribution for pi
- Data pools for outlets and search terms with scoring"
```

Claude will generate a YAML spec using the DSL features documented below.

### Step 3: Compile to a lock

```bash
pmspec compile my-study.yaml --seed 42        # writes my-study.lock.yaml beside it
git add my-study.yaml my-study.lock.yaml && git commit -m "study: lock v1"
```

The **lock** is the reproducibility artifact: every crawler expanded, every random
draw fixed by the one seed, every data pool inlined, secrets left as
`${secret:…}` placeholders, and session starts expressed as offsets from a
`schedule.anchor`. It is committable by construction — `compile` refuses a literal
credential — and byte-deterministic: the same spec and seed always produce the same
file. Its header records the spec's sha256, the git commit it was compiled from
(`-dirty` if the tree was not clean), the generator, the pmspec version and the seed.

### Step 4: Upload the lock

```bash
pmspec secrets check my-study.lock.yaml       # which ${secret:…} keys it needs, none printed
pmspec upload my-study.lock.yaml --mode test  # VM runner, name prefixed test-…, dirty commit ok
pmspec upload my-study.lock.yaml --mode real  # as in the lock; refuses a dirty/unknown commit
```

`--mode` is required. Upload resolves `${secret:key}` from `~/.config/primemover/secrets.toml`
(mode 600; or `$PMSPEC_SECRETS_FILE`), resolves the anchor (`upload` = now, Europe/Zurich),
creates the experiment with a **provenance record** as experiment preferences
(`pmspec.lock_sha256`, `pmspec.seed`, `pmspec.anchor`, `pmspec.git_commit`, `pmspec.mode`, …),
uploads the crawlers, then immediately runs `verify`. `--record ledger.txt` appends a one-line
record (`<time> exp=<id> mode=… lock=… commit=… api=…`) you can commit next to the lock.

### Step 5: Verify, any time later

```bash
pmspec verify 181 my-study.lock.yaml          # BOUND/UNBOUND + structural diff
pmspec verify 181 my-study.lock.yaml --deep   # also jobs and behaviours (via the export)
```

`BOUND` means the server's `pmspec.lock_sha256` matches this file. The structural diff
compares params, configuration preferences, agent location, proxy host/port/type, and
each queue's start time (credentials are never compared or printed). `verify` says nothing
about execution — use `GET /experiments/{id}/health` for that.

### The seed

There is exactly one: `--seed N` (or `design.seed` when no flag is given). `compile`
refuses to produce an unseeded lock. Pools, file selections and design expansion all
derive from it, so the same seed reproduces every draw.

---

## Overview

Instead of manually creating complex JSON structures, you can define experiments in human-readable YAML:

```yaml
experiment:
  name: "My Study"

design:
  factors:
    condition:
      levels: [control, treatment]
  replications: 5

crawlers:
  - from_design: true
    name: "bot_{condition}_{replica:02d}"
    # ... configuration
```

This automatically generates 10 crawlers (2 conditions × 5 replications) with proper naming and parameter mapping.

### Runner assignment

The `experiment:` mapping accepts an optional runner choice:

- `runner_user_id` (optional, integer): which runner executes the study. `1` = ex63
  (the default when omitted), `3` = the runner-only VM.

### Locks, provenance and verify — for generators

A lock can also be *written* directly by a generator (R, Python) instead of compiled from a
spec; `pmspec validate my.lock.yaml` checks it against
`experiment_designer/schema/lock-1.schema.json`. Shape:

```yaml
pmspec_lock: 1
provenance: {spec_path, spec_sha256, generator, git_commit, pmspec_version, compiled_at, seed, lax}
experiment: {name, description, runner_user_id}
schedule: {anchor: upload | "YYYY-MM-DDTHH:MM:SS"}   # Europe/Zurich wall clock
crawlers:            # the API upload shape, except:
  - …
    proxy: {…, username: "${secret:k.u}", password: "${secret:k.p}"}   # placeholders, never literals
    queues:
      - start_offset: "+5m"                                            # not start_at
```

Through the MCP: `compile_experiment_spec(yaml_content, files={"pool.csv": "<content>"}, seed=…)`
returns the lock text; `upload_experiment(lock_content, mode=…)` and
`verify_experiment(experiment_id, lock_content)` take it from there. A lock compiled through the
MCP carries no git commit (the repo is not on that host), so a `real` upload needs the CLI in the
committed repo.

## Installation

```bash
# Install Python dependencies
pip install -r requirements.txt
```

## Quick Start

### 1. Create a Specification

Create a file `my-study.yaml`:

```yaml
version: "1.0"

experiment:
  name: "Google Search Personalization Study"
  description: "Investigating search result personalization"

defaults:
  location: US-IL-CHICAGO
  browser: mlx
  timezone: America/Chicago

crawlers:
  - name: "search_bot_01"
    description: "Search crawler"

    configuration:
      params:
        pi: 0
        alpha: 1

    proxy:
      hostname: "proxy.example.com"
      port: 8080
      type: http

    agent:
      identification: mlx
      location: US-IL-CHICAGO

    session:
      name: "Search Session"
      start: "2025-02-01T10:00:00-06:00"

      workflow:
        - visit: https://google.com
        - wait: 3s
        - type:
            text: "climate change"
            selector: "//input[@name='q']"
            submit: true
        - wait: 5s
        - scroll: 30s
```

### 2. Validate

```bash
python3 -m experiment_designer.cli validate my-study.yaml
```

### 3. Generate JSON

```bash
python3 -m experiment_designer.cli generate my-study.yaml -o output.json
```

### 4. Upload to API

```bash
# Set credentials
export PM_API_BASE_URL=http://localhost:8090/api/v1
export PM_API_EMAIL=admin@primemover.example
export PM_API_PASSWORD=password

# Dry run first
python3 -m experiment_designer.cli upload my-study.yaml --dry-run

# Upload
python3 -m experiment_designer.cli upload my-study.yaml
```

## Use Case: YouTube Recommendation Study

This example demonstrates a factorial design studying how political orientation and content type affect YouTube recommendations.

### Research Design

- **Factor 1**: Political Orientation (3 levels: left, center, right)
- **Factor 2**: Starting Content (2 levels: news, entertainment)
- **Replications**: 5 per condition
- **Total Crawlers**: 3 × 2 × 5 = 30

### YAML Specification

```yaml
version: "1.0"

experiment:
  name: "YouTube Recommendation Pathways Study"
  description: >
    Investigating how political orientation and starting content type
    affect YouTube recommendation algorithm trajectories.

defaults:
  location: US-IL-CHICAGO
  browser: mlx
  timezone: America/Chicago
  captcha_mode: always

# Reusable workflow templates
templates:
  cookie_consent:
    - try_click: "//button[contains(text(),'Accept all')]"
    - wait: 2s

  youtube_search:
    - type:
        text: "${search_term}"
        selector: "//*[@id='search']"
        submit: true
    - wait: 5s

  watch_and_recommend:
    - wait: 10s
    - lambda_select:
        task: youtube_video_select
        function: bot-decision-yt
        link_type: FUNCTION_NAME
    - wait: 10s

# Factorial Design Definition
#
# ⚠️ Treatment params are 0/1 SWITCHES, not intensities.
# `configuration.params.pi`, `.psi`, `.kappa` and `.tau` are read by the decision
# function as `int(round(x)) == 1`, so ONLY 0 or 1 are meaningful. A fractional
# value such as 0.5 rounds to 0 (Python rounds half-to-even) and *silently*
# disables the arm it looks like it configures -- there is no error. A factor that
# toggles such a param must therefore have 0/1 levels; a multi-level factor cannot
# map to a single switch (map it to preferences or search terms instead). The
# validator now rejects non-0/1 values for these four params.
design:
  type: factorial

  factors:
    # A 2-level treatment factor toggling a 0/1 switch param (the correct shape).
    mainstream_media_aversion:
      levels: [control, treated]
      maps_to:
        configuration.params.pi:
          control: 0
          treated: 1

    # A multi-level factor drives non-switch levers (preferences, search terms),
    # NOT a 0/1 param.
    political_orientation:
      levels: [left, center, right]
      maps_to:
        # Map to configuration preferences
        configuration.preferences.preference_political_videos:
          left: "1"
          center: "0"
          right: "-1"
        # Map to search terms (for variable substitution)
        initial_search:
          left: "progressive politics news"
          center: "today breaking news"
          right: "conservative news today"

    starting_content:
      levels: [news, entertainment]
      maps_to:
        starting_channel:
          news: "@WashingtonPost"
          entertainment: "@MrBeast"
        configuration.preferences.preference_entertainment:
          news: "0"
          entertainment: "1"

  replications: 5

# Crawler Template (expanded by design)
crawlers:
  - from_design: true
    name: "yt_{mainstream_media_aversion}_{political_orientation}_{starting_content}_{replica:02d}"
    description: "YouTube bot studying recommendation pathways"
    active: true
    testing: false

    configuration:
      name: "Config {political_orientation}_{starting_content}"
      params:
        # pi is set by design.factors.political_orientation.maps_to
        psi: 0
        alpha: 1
        tau: 0
        kappa: 0
        beta: 0
      preferences:
        - name: location
          value: "US-IL-CHICAGO"
        - name: cookie_pref
          value: '{"accept_all": true}'   # consumed by the runner: accept_all true -> "Accept all", false -> "Reject all"
        - name: usage_type
          value: "only_search"
        - name: order
          value: "{replica}"

    proxy:
      name: "Proxy {political_orientation}_{starting_content}"
      hostname: "proxy.example.com"
      port: 8080
      username: "user"
      password: "pass"
      type: http

    agent:
      name: "Agent {political_orientation}_{starting_content}"
      identification: mlx
      location: US-IL-CHICAGO

    session:
      name: "Daily Session"
      start: "2025-02-01T10:00:00-06:00"

      workflow:
        # Phase 1: Navigate to YouTube
        - visit: https://youtube.com
        - wait: 5s
        - template: cookie_consent

        # Phase 2: Initial search based on political orientation
        - template: youtube_search
          with:
            search_term: "{initial_search}"

        # Phase 3: Visit starting channel based on content type
        - visit: "https://youtube.com/{starting_channel}/videos"
        - wait: 5s

        # Phase 4: Click first video
        - try_click: '(//*[@id="video-title"])[1]'
        - wait: 10s

        # Phase 5: Follow recommendations (10 iterations)
        - repeat: 10
          template: watch_and_recommend

        # Phase 6: Final state capture
        - visit: https://youtube.com/feed/trending
        - scroll: 30s
        - wait: 5s
```

### Running the Study

```bash
# Preview the design
python3 -m experiment_designer.cli preview youtube-study.yaml

# Output:
# Experiment: YouTube Recommendation Pathways Study
#
# Factorial Design
# Design Type: factorial
# Factors:
#   political_orientation: ['left', 'center', 'right']
#     -> maps to: configuration.preferences.preference_political_videos, ...
#   starting_content: ['news', 'entertainment']
#     -> maps to: starting_channel, configuration.preferences.preference_entertainment
#
# Replications: 5
# political_orientation(3) × starting_content(2) × 5 replications = 30 crawlers

# Validate
python3 -m experiment_designer.cli validate youtube-study.yaml

# Generate JSON
python3 -m experiment_designer.cli generate youtube-study.yaml -o youtube-study.json

# Upload (dry run)
python3 -m experiment_designer.cli upload youtube-study.yaml --dry-run

# Upload to API
python3 -m experiment_designer.cli upload youtube-study.yaml
```

### Generated Crawlers

The design expands to 30 crawlers with names like:
- `yt_left_news_01` through `yt_left_news_05`
- `yt_left_entertainment_01` through `yt_left_entertainment_05`
- `yt_center_news_01` through `yt_center_news_05`
- ... and so on

Each crawler has:
- Correct `pi` parameter based on political orientation
- Correct search term substituted in the workflow
- Correct starting channel URL
- Unique name with replica number

---

## External Data Integration (Phase 4)

The experiment designer supports loading external data files, parameter distributions, and data pools with scoring expressions.

### External File References (`!file` tag)

Load data from CSV, JSON, or YAML files directly in your specification:

```yaml
crawlers:
  - name: "Bot"
    configuration:
      params:
        # Load all search terms from CSV
        search_terms: !file ./data/searchterms.csv

        # Load with selection options
        media_outlets: !file
          path: ./data/outlets.csv
          select: random
          count: 10
          filter:
            region: US
```

**Selection strategies:**
- `all` - Load all records (default)
- `random` - Random selection
- `first` - First N records
- `filter` - Filter by field values

### Parameter Distributions

Sample parameter values from probability distributions:

```yaml
design:
  seed: 42  # For reproducibility

  parameters:
    # Uniform distribution
    pi:
      distribution: uniform
      min: -1
      max: 1

    # Discrete distribution with proportions
    kappa:
      distribution: discrete
      values:
        0: 0.25   # 25% probability
        1: 0.25   # 25% probability
        2: 0.50   # 50% probability

    # Normal (Gaussian) distribution
    alpha:
      distribution: normal
      mean: 0.35
      std: 0.1

    # Gumbel distribution (for discrete choice models)
    epsilon:
      distribution: gumbel
      mu: -0.4557735
      beta: 0.0793006

    # Truncated normal (bounded)
    beta:
      distribution: truncated_normal
      mean: 0.25
      std: 0.1
      min: 0
      max: 0.5

    # Beta distribution
    engagement:
      distribution: beta
      alpha: 2
      beta: 5

    # Fixed value
    tau:
      value: 1.0
```

**Available distributions:**
| Distribution | Parameters | Use Case |
|--------------|------------|----------|
| `uniform` | `min`, `max` | Political orientation, random values |
| `discrete` | `values` (dict) | Categorical with proportions |
| `normal` | `mean`, `std` | Continuous with center tendency |
| `gumbel` | `mu`, `beta` | Error terms in choice models |
| `truncated_normal` | `mean`, `std`, `min`, `max` | Bounded continuous |
| `beta` | `alpha`, `beta` | Probabilities, proportions |
| `fixed` / `value` | `value` | Constants |

### Proportional Factor Levels (Stratified Sampling)

Create stratified designs with specific proportions:

```yaml
design:
  factors:
    usage_type:
      levels:
        - id: only_visits
          proportion: 0.25      # 25% of crawlers
        - id: only_searches
          proportion: 0.25      # 25% of crawlers
        - id: searches_and_visits
          proportion: 0.50      # 50% of crawlers

  total_crawlers: 100  # Generate exactly 100 crawlers
```

This creates:
- 25 crawlers with `usage_type: only_visits`
- 25 crawlers with `usage_type: only_searches`
- 50 crawlers with `usage_type: searches_and_visits`

### Data Pools with Scoring

Define data pools and select records based on scoring expressions:

```yaml
data_pools:
  outlets:
    source: ./data/outlets_pool.csv
  search_terms:
    source: ./data/searchterms_pool.csv

crawlers:
  - from_design: true
    configuration:
      params:
        # Select top 10 outlets closest to crawler's pi
        media_outlet_urls:
          from_pool: outlets
          select: top
          count: 10
          score: "{alpha} - {tau} * pow(abs({crawler.pi} - {item.pi}), 2)"

        # Select top 5 search terms closest to crawler's pi
        search_terms:
          from_pool: search_terms
          select: top
          count: 5
          score: "{alpha} - {tau} * pow(abs({crawler.pi} - {item.pi}), 2)"
```

**Pool selection strategies:**
- `all` - All records
- `random` - Random N records
- `first` - First N records
- `top` - Top N by score expression

**Scoring expression variables:**
- `{crawler.pi}`, `{crawler.alpha}`, etc. - Crawler parameters
- `{item.pi}`, `{item.domain}`, etc. - Pool record fields
- `{alpha}`, `{tau}`, etc. - Design parameters

**Safe math functions:**
- `abs()`, `pow()`, `sqrt()`, `min()`, `max()`
- `log()`, `log10()`, `exp()`
- `ceil()`, `floor()`, `round()`

### Variable Substitution (Extended)

With Phase 4, additional variables are available:

| Variable | Description |
|----------|-------------|
| `{factor_name}` | Factor level ID |
| `{factor_name:value}` | Factor level value |
| `{param_name}` | Mapped parameter or sampled value |
| `{pi}`, `{kappa}`, `{alpha}` | Sampled parameter values |
| `{replica}` | Replication number |
| `{replica:02d}` | Zero-padded replica |
| `{cell_index}` | Cell index in design |
| `{cell_index:03d}` | Zero-padded cell index |

### Complete Phase 4 Example

```yaml
version: "1.0"

experiment:
  name: "Web Search Polarization Study"

data_pools:
  outlets:
    source: ./data/outlets_pool.csv
  terms:
    source: ./data/searchterms_pool.csv

design:
  seed: 42

  parameters:
    pi:
      distribution: uniform
      min: -1
      max: 1
    kappa:
      distribution: discrete
      values:
        0: 0.25
        1: 0.25
        2: 0.50
    alpha:
      distribution: normal
      mean: 0.35
      std: 0.1
    tau:
      value: 1.0

  factors:
    usage_type:
      levels:
        - id: only_visits
          proportion: 0.25
        - id: only_searches
          proportion: 0.25
        - id: searches_and_visits
          proportion: 0.50

  total_crawlers: 100

crawlers:
  - from_design: true
    name: "ws_{usage_type}_{cell_index:03d}"
    description: "PI={pi:.3f}, Kappa={kappa}"

    configuration:
      params:
        search_terms: !file
          path: ./data/searchterms_pool.csv
          select: first
          count: 10

    # ... rest of crawler config
```

---

## CLI Commands

| Command | Description |
|---------|-------------|
| `validate <file> [--seed N]` | Validate a spec or a lock |
| `compile <file> [-o out] --seed N [--anchor] [--generator] [--lax]` | Compile a spec into a committable lock |
| `upload <lock> --mode real\|test [-e ID] [--dry-run] [--record] [--allow-dirty] [--allow-past] [--deep] [--secrets] [--seed N]` | Upload a lock (or compile a spec, then upload) and verify |
| `verify <experiment_id> <lock> [--deep]` | Check a live experiment is bound to, and identical to, a lock |
| `secrets check <lock> [--secrets]` | List the secrets a lock references and which are missing |
| `generate <file> [-o output.json] [--seed N] [--resolve-secrets]` | Convert a spec or lock to API JSON (GUI upload only; carries no provenance) |
| `preview <file> [--seed N]` | Preview factorial design |
| `schema jobs` | List all job types |
| `schema job <type>` | Show job type details |
| `init [-t template]` | Create from template |

**Options:**
- `--seed N` / `-s N` - Random seed for reproducible randomization (distributions, file selections)

## Laravel Commands

```bash
# Validate
php artisan experiment:spec validate path/to/spec.yaml

# Generate JSON
php artisan experiment:spec generate spec.yaml --output=output.json

# Preview design
php artisan experiment:spec preview spec.yaml

# Upload
php artisan experiment:spec upload spec.yaml --dry-run
php artisan experiment:spec upload spec.yaml --experiment-id=5
```

## HTTP API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/experiments/spec/validate` | POST | Validate YAML |
| `/api/v1/experiments/spec/preview` | POST | Preview design |
| `/api/v1/experiments/spec/convert` | POST | Convert to JSON |
| `/api/v1/experiments/spec/upload` | POST | Upload experiment |
| `/api/v1/experiments/spec/job-types` | GET | List job types |
| `/api/v1/experiments/spec/template/{type}` | GET | Get template |

### Example API Call

```bash
curl -X POST http://localhost:8080/api/v1/experiments/spec/validate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "yaml": "experiment:\n  name: Test\ncrawlers:\n  - name: Bot",
    "strict": false
  }'
```

## Relative Timestamps

Session start times support relative timestamps for easier experiment scheduling:

| Format | Description | Example |
|--------|-------------|---------|
| `now` | Current time when spec is converted | `start: now` |
| `+Ns` | N seconds from now | `start: +30s` |
| `+Nm` | N minutes from now | `start: +5m` |
| `+Nh` | N hours from now | `start: +2h` |
| `+Nd` | N days from now | `start: +1d` |
| ISO 8601 | Fixed timestamp | `start: "2025-02-01T10:00:00-06:00"` |

**Example: Staggered bot starts**

```yaml
crawlers:
  - name: "bot_01"
    session:
      start: now  # Starts immediately

  - name: "bot_02"
    session:
      start: +2m  # Starts 2 minutes after upload

  - name: "bot_03"
    session:
      start: +5m  # Starts 5 minutes after upload
```

Relative timestamps are resolved to ISO 8601 format with local timezone offset during JSON conversion.

---

## Workflow Shorthand Reference

| Shorthand | Example | Description |
|-----------|---------|-------------|
| `visit` | `visit: https://example.com` | Navigate to URL |
| `wait` | `wait: 10s` | Wait (supports s/m/h) |
| `click` | `click: "//button"` | Click element |
| `try_click` | `try_click: "//button"` | Click, continue on fail |
| `type` | `type: {text: "...", selector: "..."}` | Enter text |
| `scroll` | `scroll: 30s` | Scroll by duration |
| `lambda_select` | `lambda_select: {task: "...", function: "..."}` | AWS Lambda selection |
| `template` | `template: my_template` | Use template |
| `repeat` | `repeat: 10` | Repeat N times |

## Treatment via the decision strategy (behaviours channel)

Treatment can be carried on the `lambda_select` job itself, rather than only in
`configuration.params`. Set a **`strategy`** (which decision logic runs) and a
**`treatment`** block (its config); both become job behaviours the decision
function reads:

```yaml
- lambda_select:
    task: youtube_video_select
    function: bot-decision-yt
    strategy: youtube_v1        # activates the behaviours channel
    treatment:
      pi: 1                     # strategy-specific config (0/1 switch here)
      tau: 0
```

- The channel activates **only** when `strategy` is present; a `treatment` block
  without a `strategy` is ignored by the decision function (the validator warns).
- The `treatment` values are validated against that strategy's schema shipped in
  `bot-decision-yt` (`strategies/<strategy>.schema.json`) when it can be found;
  e.g. `youtube_v1` still enforces the 0/1 switch rule on `pi/psi/kappa/tau`.
- Job-control keys (`task`, `link_type`, `function`) always win over `treatment`.
- Legacy specs that set treatment in `configuration.params` are unaffected — no
  `strategy`, so the decision function keeps reading `selectionConfiguration`.

## Variable Substitution

In crawler templates with `from_design: true`:

| Variable | Description |
|----------|-------------|
| `{factor_name}` | Level ID (e.g., "left") |
| `{factor_name:value}` | Level value |
| `{param_name}` | Mapped parameter from `maps_to` |
| `{replica}` | Replication number |
| `{replica:02d}` | Zero-padded replica |

## Best Practices

1. **Start with templates**: Use `pmspec init -t youtube` to get started
2. **Validate early**: Run `validate` before `generate`
3. **Use dry-run**: Always `upload --dry-run` first
4. **Use templates**: Define reusable workflow patterns
5. **Name systematically**: Use `{factor}_{replica:02d}` for clear naming

## Troubleshooting

**YAML parse error**: Check indentation, use spaces not tabs

**Variable not substituted**: Ensure variable is defined in `maps_to` or is a factor name

**Template not found**: Check template name matches exactly

**Upload fails**: Verify API credentials and endpoint URL

## Files

```
experiment_designer/
├── cli.py                  # CLI tool (pmspec)
├── README.md               # Quick reference
├── spec/
│   ├── parser.py           # YAML parsing, !file tag support
│   ├── converter.py        # JSON conversion
│   ├── design_expander.py  # Factorial/stratified expansion
│   ├── job_registry.py     # Job schemas
│   ├── validator.py        # Validation
│   ├── time_utils.py       # Relative timestamp parsing (now, +5m, +1h)
│   ├── file_loader.py      # External file loading (Phase 4)
│   ├── distributions.py    # Parameter distributions (Phase 4)
│   ├── expression.py       # Safe expression evaluator (Phase 4)
│   └── data_pools.py       # Data pool management (Phase 4)
├── schema/
│   └── experiment-spec.schema.json
├── examples/
│   ├── data/
│   │   ├── outlets_pool.csv      # Example media outlets
│   │   └── searchterms_pool.csv  # Example search terms
│   ├── simple-distribution-study.yaml
│   ├── trump-search-mini-study.yaml  # Relative timestamps example
│   └── websearch-study.yaml      # Full Phase 4 example
└── tests/
    ├── test_spec.py              # Core tests (24 tests)
    └── test_external_data.py     # Phase 4 tests (38 tests)

docs/
└── experiment-designer.md  # This documentation
```
