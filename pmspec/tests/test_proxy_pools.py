"""proxy.from_pool: one row of a proxy CSV per bot, matched on the agent's location."""

from pathlib import Path

import pytest

from pmspec.spec.converter import SpecConverter
from pmspec.spec.parser import SpecParser

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
    from pmspec.lock.build import compile_spec
    from pmspec.lock.io import dump_lock

    (spec_dir / "spec.yaml").write_text(SPEC)
    result = compile_spec(SPEC, base_dir=spec_dir, spec_path="spec.yaml", seed=1)
    assert result.lock is not None, result.errors
    text = dump_lock(result.lock)
    assert "from_pool" not in text and "p2.example" in text and "${secret:pool.p}" in text


def test_pool_without_a_hostname_column_is_an_error_naming_the_columns(spec_dir):
    """An empty hostname is the runner's "no proxy" signal, so a pool whose rows
    carry `ip` instead of `hostname` must fail the compile rather than hand every
    bot a direct connection."""
    from pmspec.lock.build import compile_spec

    (spec_dir / "proxies.csv").write_text(CSV.replace("hostname,", "ip,"))
    r = _convert(spec_dir)
    bad = [e for e in r.errors if "us_proxies" in e and "hostname" in e]
    assert len(bad) == 1, r.errors
    assert "ip" in bad[0] and "location" in bad[0], bad[0]
    assert not any(c.get("proxy", {}).get("hostname") == "" for c in r.json_data)

    result = compile_spec(SPEC, base_dir=spec_dir, spec_path="spec.yaml", seed=1)
    assert result.lock is None and any("hostname" in e for e in result.errors), result.errors
    assert not (spec_dir / "spec.lock.yaml").exists()


def test_pool_row_without_a_host_or_port_is_an_error_for_that_crawler(spec_dir):
    """Column present, value blank: the row that would become a direct connection
    is refused, the other rows still assign."""
    (spec_dir / "proxies.csv").write_text(
        CSV.replace("p2.example,7777", ",7777").replace("p3.example,7777", "p3.example,")
    )
    r = _convert(spec_dir)
    assert any("crawlers[1].proxy" in e and "row 1" in e and "hostname/port" in e for e in r.errors)
    assert any("crawlers[2].proxy" in e and "row 2" in e for e in r.errors), r.errors
    assert [c["proxy"]["hostname"] for c in r.json_data] == ["p1.example"]


def test_defaults_proxy_from_pool_assigns_rows_like_the_per_crawler_form(spec_dir):
    text = SPEC.replace(
        "    proxy: {from_pool: true, pool: us_proxies, match_field: location}\n", ""
    ).replace(
        "crawlers:\n",
        "defaults:\n  proxy: {from_pool: true, pool: us_proxies, match_field: location}\ncrawlers:\n",
    )
    assert "from_pool" in text and text.count("from_pool") == 1
    r = _convert(spec_dir, text)
    assert r.errors == [], r.errors
    assert [c["proxy"]["hostname"] for c in r.json_data] == [
        "p1.example",
        "p2.example",
        "p3.example",
    ]
    assert all(c["proxy"]["port"] == 7777 for c in r.json_data)
