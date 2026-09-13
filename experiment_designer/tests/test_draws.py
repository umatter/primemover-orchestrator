"""with: {from_pool, field}: one draw per bot per key, without replacement within a bot."""

from pathlib import Path

import pytest

from experiment_designer.spec.converter import SpecConverter
from experiment_designer.spec.draws import DRAW_TOKEN_RE, is_draw, make_token, parse_token
from experiment_designer.spec.parser import SpecParser
from experiment_designer.spec.validator import SpecValidator

SPEC = """
version: "1.0"
experiment: {name: D}
data_pools:
  queries:
    type: csv
    source: queries.csv
templates:
  search:
    - type: {text: "${query}", selector: "//x", appendReturn: true}
design:
  factors:
    arm:
      levels: [a, b]
  replications: 3
crawlers:
  - name: bot_{arm}_{replica}
    from_design: true
    session:
      start: "+1m"
      workflow:
        - template: search
          with: {query: {from_pool: queries, field: term}}
        - template: search
          with: {query: {from_pool: queries, field: term}}
"""
CSV = "term,cat\nalpha,x\nbeta,x\ngamma,y\ndelta,y\nepsilon,z\nzeta,z\n"


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    (tmp_path / "queries.csv").write_text(CSV)
    return tmp_path


def test_token_round_trip():
    t = make_token("queries", "term", "random", 0)
    assert DRAW_TOKEN_RE.fullmatch(t) and parse_token(t) == ("queries", "term", "random", 0)
    assert parse_token("plain") is None


def test_parser_emits_tokens_with_distinct_ordinals(spec_dir):
    parsed = SpecParser(base_dir=spec_dir).parse(SPEC)
    assert not parsed.errors, parsed.errors
    jobs = parsed.crawlers[0]["queues"][0]["jobs"]
    texts = [b["value"] for j in jobs for b in j["behaviors"] if b["name"] == "text"]
    assert [parse_token(t)[3] for t in texts] == [0, 0]  # ordinal within each with: block
    assert all(parse_token(t)[:3] == ("queries", "term", "random") for t in texts)


def test_repeat_draws_per_repetition(spec_dir):
    text = SPEC.replace(
        "        - template: search\n          with: {query: {from_pool: queries, field: term}}\n"
        "        - template: search\n          with: {query: {from_pool: queries, field: term}}\n",
        "        - repeat: 3\n          template: search\n          with: {query: {from_pool: queries, field: term}}\n",
    )
    parsed = SpecParser(base_dir=spec_dir).parse(text)
    jobs = parsed.crawlers[0]["queues"][0]["jobs"]
    ordinals = [
        parse_token(b["value"])[3] for j in jobs for b in j["behaviors"] if b["name"] == "text"
    ]
    assert ordinals == [0, 1, 2]


def test_validator_accepts_a_draw_and_rejects_other_dicts():
    import yaml

    ok = SpecValidator().validate(yaml.safe_load(SPEC))
    assert ok.dropped == 0, ok.errors
    bad = SPEC.replace("{from_pool: queries, field: term}", "{from_pool: queries}")
    r = SpecValidator().validate(yaml.safe_load(bad))
    assert any("with.query" in e and "field" in e for e in r.errors), r.errors
    counted = SPEC.replace(
        "{from_pool: queries, field: term}", "{from_pool: queries, field: term, count: 2}"
    )
    r = SpecValidator().validate(yaml.safe_load(counted))
    assert any("count" in e for e in r.errors)


def test_is_draw_rejects_names_outside_the_token_charset():
    assert is_draw({"from_pool": "queries", "field": "term"})
    assert not is_draw({"from_pool": "qu/eries", "field": "term"})
    assert not is_draw({"from_pool": "queries", "field": "te rm"})


def test_validator_rejects_names_outside_the_token_charset():
    import yaml

    bad = SPEC.replace("field: term", "field: te/rm")
    r = SpecValidator().validate(yaml.safe_load(bad))
    assert any("with.query" in e and "letters, digits" in e for e in r.errors), r.errors


def _convert(spec_dir, text=SPEC, seed=7):
    parsed = SpecParser(base_dir=spec_dir, seed=seed).parse(text)
    assert not parsed.errors, parsed.errors
    return SpecConverter(seed=seed).convert(parsed)


def _typed(result):
    out = {}
    for c in result.json_data:
        out[c["name"]] = [
            b["value"]
            for q in c["queues"]
            for j in q["jobs"]
            for b in j["behaviors"]
            if b["name"] == "text"
        ]
    return out


def test_each_bot_draws_its_own_values_without_replacement(spec_dir):
    r = _convert(spec_dir)
    assert r.errors == [], r.errors
    typed = _typed(r)
    assert len(typed) == 6
    for values in typed.values():
        assert (
            len(values) == 2
            and len(set(values)) == 2
            and set(values)
            <= set(CSV.split()[1:]) | {"alpha", "beta", "gamma", "delta", "epsilon", "zeta"}
        )
    assert len({tuple(v) for v in typed.values()}) > 1, "bots differ"
    assert not any(DRAW_TOKEN_RE.search(v) for vs in typed.values() for v in vs)


def test_draws_are_deterministic_per_seed(spec_dir):
    assert _typed(_convert(spec_dir, seed=7)) == _typed(_convert(spec_dir, seed=7))
    assert _typed(_convert(spec_dir, seed=7)) != _typed(_convert(spec_dir, seed=8))


def test_exhaustion_is_an_error(spec_dir):
    (spec_dir / "queries.csv").write_text("term\nonly\n")
    r = _convert(spec_dir)
    assert any("exhausted" in e and "queries" in e for e in r.errors), r.errors


def test_missing_field_column_is_an_error(spec_dir):
    r = _convert(spec_dir, SPEC.replace("field: term", "field: nope"))
    assert any("nope" in e and "term" in e for e in r.errors), r.errors


def test_first_select_is_stable(spec_dir):
    r = _convert(spec_dir, SPEC.replace("field: term}", "field: term, select: first}"))
    assert all(v == ["alpha", "beta"] for v in _typed(r).values())


def test_compile_never_writes_a_token(spec_dir):
    from experiment_designer.lock.build import compile_spec
    from experiment_designer.lock.io import dump_lock

    result = compile_spec(SPEC, base_dir=spec_dir, spec_path="s.yaml", seed=7)
    assert result.lock is not None, result.errors
    assert "%%pmspec-draw" not in dump_lock(result.lock)


def test_unknown_pool_draw_is_reported_by_the_strict_scan(spec_dir):
    r = _convert(spec_dir, SPEC.replace("from_pool: queries", "from_pool: missing"))
    assert any("unknown pool" in e and "missing" in e for e in r.errors), r.errors
    assert any("unresolved pool draw" in e for e in r.errors), r.errors


# --- an invalid select is refused on every path, not shipped as a token -------


def test_is_draw_refuses_an_invalid_select():
    assert is_draw({"from_pool": "queries", "field": "term", "select": "random"})
    assert is_draw({"from_pool": "queries", "field": "term", "select": "first"})
    assert not is_draw({"from_pool": "queries", "field": "term", "select": "randm"})
    assert not is_draw({"from_pool": "queries", "field": "term", "select": ""})


def test_parser_drops_an_invalid_select_and_leaves_the_variable_unsubstituted(spec_dir):
    # No validator on this path (`orchestrator experiment generate`, MCP
    # convert_spec_to_json): the parser itself must refuse, and the `${query}`
    # must survive so the strict scan reports it rather than a literal
    # "{'from_pool': ...}" or a token the regex cannot match reaching the bot.
    text = SPEC.replace(
        "{from_pool: queries, field: term}", "{from_pool: queries, field: term, select: randm}"
    )
    parsed = SpecParser(base_dir=spec_dir).parse(text)
    assert any(
        "select must be random or first" in e and "query" in e for e in parsed.errors
    ), parsed.errors
    jobs = parsed.crawlers[0]["queues"][0]["jobs"]
    texts = [b["value"] for j in jobs for b in j["behaviors"] if b["name"] == "text"]
    assert texts and all(t == "${query}" for t in texts), texts
    assert not any("pmspec-draw" in t for t in texts)


def test_validator_still_names_the_invalid_select(spec_dir):
    import yaml

    text = SPEC.replace(
        "{from_pool: queries, field: term}", "{from_pool: queries, field: term, select: randm}"
    )
    r = SpecValidator().validate(yaml.safe_load(text))
    assert any("with.query" in e and "randm" in e for e in r.errors), r.errors


def test_strict_scan_reports_any_malformed_draw_token():
    # A token DRAW_TOKEN_RE cannot match (bad select, bad ordinal, truncated)
    # is still a `%%pmspec-draw:...%%` that would reach the bot as a literal.
    conv = SpecConverter(seed=1)
    crawlers = [
        {
            "name": "bot",
            "queues": [
                {
                    "jobs": [
                        {
                            "behaviors": [
                                {"name": "text", "value": "%%pmspec-draw:queries:term:randm:0%%"},
                                {"name": "url", "value": "%%pmspec-draw:queries%%"},
                            ]
                        }
                    ]
                }
            ],
        }
    ]
    conv._scan_unresolved(crawlers)
    msgs = conv.drops.messages
    assert any("unresolved pool draw" in m and "randm" in m for m in msgs), msgs
    assert any("unresolved pool draw" in m and "%%pmspec-draw:queries%%" in m for m in msgs), msgs
