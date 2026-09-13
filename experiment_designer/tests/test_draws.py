"""with: {from_pool, field}: one draw per bot per key, without replacement within a bot."""

from pathlib import Path

import pytest

from experiment_designer.spec.draws import DRAW_TOKEN_RE, make_token, parse_token

# NOTE: SpecConverter is imported by the Task 3 tests appended to this file
# (draw-token resolution in the converter); not used by the parser/validator
# tests here.
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
