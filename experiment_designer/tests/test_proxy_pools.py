"""proxy.from_pool: one row of a proxy CSV per bot, matched on the agent's location."""

from pathlib import Path

import pytest

from experiment_designer.spec.converter import SpecConverter
from experiment_designer.spec.parser import SpecParser

SPEC = """
version: "1.0"
experiment:
  name: Pools
data_pools:
  us_proxies:
    type: csv
    source: proxies.csv
crawlers:
  - name: chi_1
    agent: {location: US-IL-CHICAGO}
    proxy: {from_pool: true, pool: us_proxies, match_field: location}
    session: {start: "+1m", workflow: [{wait: 1s}]}
  - name: chi_2
    agent: {location: US-IL-CHICAGO}
    proxy: {from_pool: true, pool: us_proxies, match_field: location}
    session: {start: "+1m", workflow: [{wait: 1s}]}
  - name: atl_1
    agent: {location: US-GA-ATLANTA}
    proxy: {from_pool: true, pool: us_proxies, match_field: location}
    session: {start: "+1m", workflow: [{wait: 1s}]}
"""

CSV = """hostname,port,username,password,type,location
p1.example,7777,${secret:pool.u},${secret:pool.p},http,US-IL-CHICAGO
p2.example,7777,${secret:pool.u},${secret:pool.p},http,US-IL-CHICAGO
p3.example,7777,${secret:pool.u},${secret:pool.p},http,US-GA-ATLANTA
"""


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    (tmp_path / "proxies.csv").write_text(CSV)
    return tmp_path


def _convert(spec_dir: Path, text: str = SPEC):
    parsed = SpecParser(base_dir=spec_dir).parse(text)
    assert not parsed.errors, parsed.errors
    return SpecConverter().convert(parsed)


def test_each_bot_gets_its_own_row_for_its_location(spec_dir):
    r = _convert(spec_dir)
    assert r.errors == []
    hosts = [c["proxy"]["hostname"] for c in r.json_data]
    assert hosts == ["p1.example", "p2.example", "p3.example"]
    assert r.json_data[0]["proxy"]["port"] == 7777
    assert r.json_data[0]["proxy"]["username"] == "${secret:pool.u}"
    assert r.json_data[0]["proxy"]["type"] == "http"


def test_exhausted_pool_is_an_error(spec_dir):
    text = SPEC + """  - name: chi_3
    agent: {location: US-IL-CHICAGO}
    proxy: {from_pool: true, pool: us_proxies, match_field: location}
    session: {start: "+1m", workflow: [{wait: 1s}]}
"""
    r = _convert(spec_dir, text)
    assert any(
        "us_proxies" in e and "US-IL-CHICAGO" in e and "chi_3" in e for e in r.errors
    ), r.errors


def test_no_row_for_location_is_an_error(spec_dir):
    r = _convert(spec_dir, SPEC.replace("US-GA-ATLANTA", "US-TX-AUSTIN"))
    assert any("US-TX-AUSTIN" in e for e in r.errors), r.errors


def test_unknown_pool_is_an_error(spec_dir):
    r = _convert(spec_dir, SPEC.replace("pool: us_proxies", "pool: nope"))
    assert any("nope" in e for e in r.errors)


def test_missing_location_is_an_error(spec_dir):
    r = _convert(
        spec_dir, SPEC.replace("    agent: {location: US-IL-CHICAGO}\n    proxy", "    proxy", 1)
    )
    assert any("crawlers[0]" in e and "location" in e for e in r.errors), r.errors


def test_compile_inlines_the_proxy_and_keeps_placeholders(spec_dir):
    from experiment_designer.lock.build import compile_spec
    from experiment_designer.lock.io import dump_lock

    (spec_dir / "spec.yaml").write_text(SPEC)
    result = compile_spec(SPEC, base_dir=spec_dir, spec_path="spec.yaml", seed=1)
    assert result.lock is not None, result.errors
    text = dump_lock(result.lock)
    assert "from_pool" not in text and "p2.example" in text and "${secret:pool.p}" in text
