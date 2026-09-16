"""`assign:` -- one pool row per crawler, exposed as `{name.column}` variables.

The generalisation of `proxy: {from_pool: ...}`: a crawler names one or more
assignments, each takes a row of a pool without replacement across the whole
crawler list, and every column of the drawn row is substitutable anywhere in
that crawler. The two features share the taken-row bookkeeping, so one pool
serving both can never hand the same row out twice.
"""

from pathlib import Path

import pytest

from pmspec.spec.converter import SpecConverter
from pmspec.spec.parser import SpecParser
from pmspec.spec.validator import SpecValidator

PANELISTS = """person_id,region,zip
510121845,US-IL-CHICAGO,60601
510121846,US-IL-CHICAGO,60602
510121847,US-GA-ATLANTA,30301
"""

SPEC = """
version: "1.0"
experiment:
  name: Assign
data_pools:
  panelists:
    type: csv
    source: panelists.csv
crawlers:
  - name: "bot_1_{panelist.person_id}"
    agent: {location: US-IL-CHICAGO}
    assign:
      panelist: {from_pool: panelists, select: first}
    configuration:
      preferences:
        - {name: comscore_person_id, value: "{panelist.person_id}"}
    session: {start: "+1m", workflow: [{wait: 1s}]}
  - name: "bot_2_{panelist.person_id}"
    agent: {location: US-IL-CHICAGO}
    assign:
      panelist: {from_pool: panelists, select: first}
    configuration:
      preferences:
        - {name: comscore_person_id, value: "{panelist.person_id}"}
    session: {start: "+1m", workflow: [{wait: 1s}]}
"""


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    (tmp_path / "panelists.csv").write_text(PANELISTS)
    return tmp_path


def _convert(spec_dir: Path, text: str = SPEC, *, seed: int | None = 1):
    parsed = SpecParser(base_dir=spec_dir).parse(text)
    assert not parsed.errors, parsed.errors
    return SpecConverter(seed=seed).convert(parsed)


def _preference(crawler, name):
    for p in crawler["configuration"]["preferences"]:
        if p["name"] == name:
            return p["value"]
    raise AssertionError(f"no preference {name!r}")


def _validate(spec_dir: Path, text: str = SPEC, **kw):
    import yaml

    return SpecValidator(base_dir=spec_dir, **kw).validate(yaml.safe_load(text))


# --- drawing ---------------------------------------------------------------


def test_select_first_hands_out_rows_in_pool_order(spec_dir):
    r = _convert(spec_dir)
    assert r.errors == [], r.errors
    assert [c["name"] for c in r.json_data] == ["bot_1_510121845", "bot_2_510121846"]
    assert _preference(r.json_data[0], "comscore_person_id") == "510121845"
    assert _preference(r.json_data[1], "comscore_person_id") == "510121846"


def test_the_assign_block_never_reaches_the_api_json(spec_dir):
    r = _convert(spec_dir)
    assert all("assign" not in c for c in r.json_data)


def test_select_random_is_deterministic_per_seed_and_differs_across_seeds(spec_dir):
    text = SPEC.replace("select: first", "select: random")
    once = [c["name"] for c in _convert(spec_dir, text, seed=1).json_data]
    twice = [c["name"] for c in _convert(spec_dir, text, seed=1).json_data]
    other = [c["name"] for c in _convert(spec_dir, text, seed=4).json_data]
    assert once == twice
    assert once != other


def test_a_draw_without_a_seed_is_an_error(spec_dir):
    r = _convert(spec_dir, seed=None)
    assert any(
        e == "crawlers[0].assign.panelist: assignments need a seed" for e in r.errors
    ), r.errors


def test_match_field_matches_the_agents_location(spec_dir):
    text = SPEC.replace("select: first", "match_field: region, select: first").replace(
        '  - name: "bot_2_{panelist.person_id}"\n    agent: {location: US-IL-CHICAGO}',
        '  - name: "bot_2_{panelist.person_id}"\n    agent: {location: US-GA-ATLANTA}',
    )
    r = _convert(spec_dir, text)
    assert r.errors == [], r.errors
    assert [c["name"] for c in r.json_data] == ["bot_1_510121845", "bot_2_510121847"]


def test_match_value_overrides_the_location_on_a_design_crawler(spec_dir):
    text = """
version: "1.0"
experiment:
  name: Assign
data_pools:
  panelists:
    type: csv
    source: panelists.csv
design:
  factors:
    city:
      levels: [US-IL-CHICAGO, US-GA-ATLANTA]
crawlers:
  - from_design: true
    name: "bot_{city}_{panelist.person_id}"
    agent: {location: US-IL-CHICAGO}
    assign:
      panelist: {from_pool: panelists, match_field: region, match_value: "{city}", select: first}
    session: {start: "+1m", workflow: [{wait: 1s}]}
"""
    r = _convert(spec_dir, text)
    assert r.errors == [], r.errors
    assert [c["name"] for c in r.json_data] == [
        "bot_US-IL-CHICAGO_510121845",
        "bot_US-GA-ATLANTA_510121847",
    ]


def test_match_field_without_a_location_or_match_value_is_an_error(spec_dir):
    text = SPEC.replace("select: first", "match_field: region, select: first").replace(
        "    agent: {location: US-IL-CHICAGO}\n", "", 1
    )
    r = _convert(spec_dir, text)
    assert any(
        e == "crawlers[0].assign.panelist: match_field needs agent.location or match_value"
        for e in r.errors
    ), r.errors


# --- running out -----------------------------------------------------------


def test_an_exhausted_pool_names_the_field_value_and_count(spec_dir):
    text = (
        SPEC.replace("select: first", "match_field: region, select: first")
        + """  - name: "bot_3_{panelist.person_id}"
    agent: {location: US-IL-CHICAGO}
    assign:
      panelist: {from_pool: panelists, match_field: region, select: first}
    session: {start: "+1m", workflow: [{wait: 1s}]}
"""
    )
    r = _convert(spec_dir, text)
    assert (
        "crawlers[2].assign.panelist: pool 'panelists' has no free row with "
        "region == 'US-IL-CHICAGO' (2 taken)" in r.errors
    ), r.errors


def test_no_matching_row_at_all_names_the_field_and_value(spec_dir):
    text = SPEC.replace("select: first", "match_field: region, select: first").replace(
        "US-IL-CHICAGO", "US-TX-AUSTIN"
    )
    r = _convert(spec_dir, text)
    assert (
        "crawlers[0].assign.panelist: pool 'panelists' has no row with "
        "region == 'US-TX-AUSTIN'" in r.errors
    ), r.errors


def test_an_unknown_pool_is_an_error_and_the_placeholder_becomes_a_drop(spec_dir):
    text = SPEC.replace("from_pool: panelists", "from_pool: nope")
    r = _convert(spec_dir, text)
    assert (
        "crawlers[0].assign.panelist: unknown pool 'nope' (define it under data_pools)" in r.errors
    ), r.errors
    assert any("{panelist.person_id}" in e and "literal" in e for e in r.errors), r.errors
    assert r.dropped >= 1


def test_a_failed_draw_still_removes_the_assign_block(spec_dir):
    r = _convert(spec_dir, SPEC.replace("from_pool: panelists", "from_pool: nope"))
    assert all("assign" not in c for c in r.json_data)


# --- formatting ------------------------------------------------------------


def test_a_format_spec_is_applied_to_the_drawn_value(spec_dir):
    text = SPEC.replace('value: "{panelist.person_id}"', 'value: "{panelist.zip:>10}"')
    r = _convert(spec_dir, text)
    assert r.errors == [], r.errors
    assert _preference(r.json_data[0], "comscore_person_id") == "     60601"


def test_a_format_spec_the_value_cannot_satisfy_is_an_error(spec_dir):
    text = SPEC.replace('value: "{panelist.person_id}"', 'value: "{panelist.region:.2f}"')
    r = _convert(spec_dir, text)
    assert "crawlers[0]: {panelist.region:.2f} cannot format 'US-IL-CHICAGO'" in r.errors, r.errors
    # ...and the token survives, so the strict scan refuses the compile too.
    assert any("{panelist.region:.2f}" in e and "literal" in e for e in r.errors), r.errors


# --- sharing the taken set with proxy.from_pool ----------------------------


SHARED = """
version: "1.0"
experiment:
  name: Shared
data_pools:
  endpoints:
    type: csv
    source: endpoints.csv
crawlers:
  - name: bot_1
    agent: {location: US-IL-CHICAGO}
    proxy: {from_pool: true, pool: endpoints, match_field: location}
    assign:
      slot: {from_pool: endpoints, match_field: location, select: first}
    configuration:
      preferences:
        - {name: slot, value: "{slot.hostname}"}
    session: {start: "+1m", workflow: [{wait: 1s}]}
  - name: bot_2
    agent: {location: US-IL-CHICAGO}
    proxy: {from_pool: true, pool: endpoints, match_field: location}
    assign:
      slot: {from_pool: endpoints, match_field: location, select: first}
    configuration:
      preferences:
        - {name: slot, value: "{slot.hostname}"}
    session: {start: "+1m", workflow: [{wait: 1s}]}
"""

ENDPOINTS = """hostname,port,location
e1.example,7777,US-IL-CHICAGO
e2.example,7777,US-IL-CHICAGO
e3.example,7777,US-IL-CHICAGO
e4.example,7777,US-IL-CHICAGO
"""


def test_an_assign_and_a_proxy_on_one_pool_never_share_a_row(tmp_path):
    (tmp_path / "endpoints.csv").write_text(ENDPOINTS)
    r = _convert(tmp_path, SHARED)
    assert r.errors == [], r.errors
    assigned = [_preference(c, "slot") for c in r.json_data]
    proxied = [c["proxy"]["hostname"] for c in r.json_data]
    assert assigned == ["e1.example", "e2.example"]
    assert proxied == ["e3.example", "e4.example"]
    assert set(assigned) & set(proxied) == set()


# --- validator -------------------------------------------------------------


def test_an_undeclared_pool_is_a_validation_error(spec_dir):
    result = _validate(spec_dir, SPEC.replace("from_pool: panelists", "from_pool: nope"))
    assert any("crawler[0].assign.panelist" in e and "nope" in e for e in result.errors)


def test_a_match_field_that_is_not_a_column_is_a_validation_error(spec_dir):
    result = _validate(spec_dir, SPEC.replace("select: first", "match_field: county"))
    assert any(
        "crawler[0].assign.panelist" in e and "county" in e and "person_id" in e
        for e in result.errors
    ), result.errors


def test_a_bad_select_is_a_validation_error(spec_dir):
    result = _validate(spec_dir, SPEC.replace("select: first", "select: randm"))
    assert any("crawler[0].assign.panelist" in e and "randm" in e for e in result.errors)


def test_from_pool_is_required(spec_dir):
    result = _validate(spec_dir, SPEC.replace("from_pool: panelists, ", ""))
    assert any("crawler[0].assign.panelist" in e and "from_pool" in e for e in result.errors)


@pytest.mark.parametrize("name", ["city", "ideology", "base"])
def test_a_name_colliding_with_a_design_variable_is_an_error(spec_dir, name):
    text = """
version: "1.0"
experiment:
  name: Assign
data_pools:
  panelists:
    type: csv
    source: panelists.csv
design:
  factors:
    city:
      levels: [a, b]
  parameters:
    ideology: {distribution: uniform, min: 0, max: 1}
crawlers:
  - from_design: true
    name: "bot_{cell_index}"
    assign:
      NAME: {from_pool: panelists, select: first}
    session: {start: "+1m", workflow: [{wait: 1s}]}
""".replace("NAME", name)
    result = _validate(spec_dir, text)
    assert f"crawler[0].assign.{name}: collides with a design variable" in result.errors


def test_an_unknown_key_inside_an_assign_entry_is_a_drop(spec_dir):
    result = _validate(spec_dir, SPEC.replace("select: first", "selct: first"))
    assert result.dropped >= 1
    assert any("crawler[0].assign.panelist.selct" in e for e in result.errors), result.errors


def test_a_name_that_cannot_be_a_placeholder_is_an_error(spec_dir):
    result = _validate(spec_dir, SPEC.replace("panelist:", "pan-elist:"))
    assert any("crawler[0].assign.pan-elist" in e for e in result.errors), result.errors


# --- runs specs ------------------------------------------------------------


RUNS_FLEET = """
version: "1.0"
experiment:
  name: Fleet
  description: the randomization record
crawlers:
  - name: bot_a
    agent: {location: US-IL-CHICAGO}
    configuration:
      preferences:
        - {name: comscore_person_id, value: "510121845"}
    session: {start: "+5m", workflow: [{visit: "https://example.com"}]}
  - name: bot_b
    agent: {location: US-IL-CHICAGO}
    configuration:
      preferences:
        - {name: comscore_person_id, value: "510121846"}
    session: {start: "+5m", workflow: [{visit: "https://example.com"}]}
"""

SESSIONS = """person_id,session_id
510121845,s-aaa
510121846,s-bbb
510121845,s-ccc
"""

RUNS_SPEC_ASSIGN = """
version: "1.0"
runs_for: fleet.lock.yaml
seed: 5
data_pools:
  sessions:
    type: csv
    source: sessions.csv
crawlers:
  - from_base: true
    name: "{base.name}"
    assign:
      session:
        from_pool: sessions
        match_field: person_id
        match_value: "{base.comscore_person_id}"
        select: first
    session:
      start: "2026-10-01T10:00:00"
      workflow:
        - visit: "https://example.com/replay/{session.session_id}"
"""


@pytest.fixture
def runs_study(tmp_path):
    from pmspec.lock.build import compile_spec
    from pmspec.lock.io import dump_lock

    (tmp_path / "sessions.csv").write_text(SESSIONS)
    (tmp_path / "fleet.yaml").write_text(RUNS_FLEET)
    result = compile_spec(RUNS_FLEET, base_dir=tmp_path, spec_path="fleet.yaml", seed=7)
    assert result.lock is not None, result.errors
    (tmp_path / "fleet.lock.yaml").write_text(dump_lock(result.lock))
    return tmp_path


def test_a_runs_spec_assigns_a_row_matched_on_a_base_preference(runs_study):
    from pmspec.lock.build import compile_spec

    (runs_study / "day.yaml").write_text(RUNS_SPEC_ASSIGN)
    result = compile_spec(RUNS_SPEC_ASSIGN, base_dir=runs_study, spec_path="day.yaml", seed=5)
    assert result.errors == [], result.errors
    urls = [
        b["value"]
        for c in result.lock.crawlers
        for q in c["queues"]
        for j in q["jobs"]
        for b in j["behaviors"]
        if b["name"] == "url"
    ]
    assert urls == ["https://example.com/replay/s-aaa", "https://example.com/replay/s-bbb"]
    assert all("assign" not in c for c in result.lock.crawlers)


def test_a_runs_spec_match_field_without_match_value_has_no_agent_to_fall_back_on(runs_study):
    from pmspec.lock.build import compile_spec

    text = RUNS_SPEC_ASSIGN.replace('        match_value: "{base.comscore_person_id}"\n', "")
    (runs_study / "day.yaml").write_text(text)
    result = compile_spec(text, base_dir=runs_study, spec_path="day.yaml", seed=5)
    assert result.lock is None
    assert any(
        e == "crawlers[0].assign.session: match_field needs agent.location or match_value"
        for e in result.errors
    ), result.errors


# --- validate must not reject what compile accepts --------------------------


NO_SEED_SPEC = SPEC  # no design.seed, no root seed:
SEEDED_SPEC = SPEC.replace(
    "crawlers:\n", "design:\n  seed: 3\n  factors:\n    arm:\n      levels: [a]\ncrawlers:\n"
)


def test_a_spec_with_design_seed_validates_and_compiles_alike(spec_dir):
    """The divergence this closes: `assign` refuses to draw unseeded, and
    validate used to hand its converter only `--seed`."""
    from click.testing import CliRunner

    from pmspec.cli import cli

    (spec_dir / "s.yaml").write_text(SEEDED_SPEC)
    result = CliRunner().invoke(cli, ["validate", str(spec_dir / "s.yaml")])
    assert result.exit_code == 0, result.output
    assert "need a seed" not in result.output


def test_validate_and_compile_agree_that_an_unseeded_assign_is_an_error(spec_dir):
    from click.testing import CliRunner

    from pmspec.cli import cli
    from pmspec.lock.build import compile_spec

    (spec_dir / "s.yaml").write_text(NO_SEED_SPEC)
    result = CliRunner().invoke(cli, ["validate", str(spec_dir / "s.yaml")])
    assert result.exit_code == 1
    assert "assignments need a seed" in result.output

    # compile refuses first, and harder: a *lock* is never unseeded at all, so
    # it never reaches the draw. Both refuse; neither accepts.
    compiled = compile_spec(NO_SEED_SPEC, base_dir=spec_dir, spec_path="s.yaml", seed=None)
    assert compiled.lock is None
    assert any("seed" in e for e in compiled.errors), compiled.errors


def test_validate_seeds_from_the_command_line_too(spec_dir):
    from click.testing import CliRunner

    from pmspec.cli import cli

    (spec_dir / "s.yaml").write_text(NO_SEED_SPEC)
    result = CliRunner().invoke(cli, ["validate", str(spec_dir / "s.yaml"), "--seed", "3"])
    assert result.exit_code == 0, result.output


def test_validate_accepts_a_runs_spec_seeded_by_its_root_seed(runs_study):
    from click.testing import CliRunner

    from pmspec.cli import cli

    (runs_study / "day.yaml").write_text(RUNS_SPEC_ASSIGN)
    result = CliRunner().invoke(cli, ["validate", str(runs_study / "day.yaml")])
    assert result.exit_code == 0, result.output


def test_the_mcp_validate_tool_seeds_from_design_seed(spec_dir):
    import json

    from orchestrator.mcp_server.server import validate_experiment_spec

    files = {"panelists.csv": PANELISTS}
    out = json.loads(validate_experiment_spec(SEEDED_SPEC, files=files))
    assert out["valid"] is True, out["errors"]


def test_the_mcp_validate_tool_reports_the_seed_error_compile_would(spec_dir):
    import json

    from orchestrator.mcp_server.server import validate_experiment_spec

    out = json.loads(validate_experiment_spec(NO_SEED_SPEC, files={"panelists.csv": PANELISTS}))
    assert out["valid"] is False
    assert any("assignments need a seed" in e for e in out["errors"]), out["errors"]


def test_the_orchestrator_validate_command_seeds_its_converter(spec_dir):
    from click.testing import CliRunner

    from orchestrator.cli import cli

    (spec_dir / "s.yaml").write_text(NO_SEED_SPEC)
    runner = CliRunner()
    unseeded = runner.invoke(cli, ["experiment", "validate", str(spec_dir / "s.yaml")])
    assert "assignments need a seed" in unseeded.output
    seeded = runner.invoke(cli, ["experiment", "validate", str(spec_dir / "s.yaml"), "--seed", "3"])
    assert "assignments need a seed" not in seeded.output


# --- a blank cell is empty, not "None" --------------------------------------


def test_a_blank_pool_cell_substitutes_as_an_empty_string(spec_dir):
    (spec_dir / "panelists.csv").write_text(
        PANELISTS.replace("510121845,US-IL-CHICAGO,60601", "510121845,US-IL-CHICAGO,")
    )
    text = SPEC.replace('value: "{panelist.person_id}"', 'value: "zip=[{panelist.zip}]"')
    r = _convert(spec_dir, text)
    assert r.errors == [], r.errors
    assert _preference(r.json_data[0], "comscore_person_id") == "zip=[]"
