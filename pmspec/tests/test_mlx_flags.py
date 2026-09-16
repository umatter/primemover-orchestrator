"""§4 of the phase-6 design: `maps_to: {agent.multilogin_profile.parameters.flags.<flag>: {...}}`
reaches the API agent JSON through DesignExpander._set_nested_value and the converter's
deep-merge over DEFAULT_MULTILOGIN_PROFILE. This proves the full spec -> lock -> API JSON
path for one flag, and that an unknown flag key is a drop."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pmspec.lock.build import compile_spec
from pmspec.lock.resolve import resolve_for_upload
from pmspec.spec.converter import DEFAULT_MULTILOGIN_PROFILE
from pmspec.spec.validator import SpecValidator

SPEC = """
version: "1.0"
experiment:
  name: MLX flags
design:
  factors:
    privacy:
      levels: [none, geo]
      maps_to:
        agent.multilogin_profile.parameters.flags.geolocation_masking: {none: mask, geo: custom}
  replications: 1
crawlers:
  - from_design: true
    name: "bot_{privacy}"
    session:
      start: "+1m"
      workflow:
        - visit: https://example.com
"""


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    (tmp_path / "spec.yaml").write_text(SPEC)
    return tmp_path


def _compile(spec_dir: Path, text: str = SPEC, **kw):
    kw.setdefault("seed", 42)
    return compile_spec(text, base_dir=spec_dir, spec_path="spec.yaml", **kw)


_DEFAULT_FLAGS = DEFAULT_MULTILOGIN_PROFILE["parameters"]["flags"]


def _flags_by_crawler_name(crawlers: list[dict]) -> dict[str, dict]:
    return {c["name"]: c["agent"]["multilogin_profile"]["parameters"]["flags"] for c in crawlers}


class TestNestedMultiloginFlagReachesTheLock:
    def test_flag_value_lands_in_the_lock_per_level(self, spec_dir: Path):
        result = _compile(spec_dir)
        assert result.lock is not None, result.errors
        flags = _flags_by_crawler_name(result.lock.crawlers)
        assert flags["bot_none"]["geolocation_masking"] == "mask"
        assert flags["bot_geo"]["geolocation_masking"] == "custom"

    def test_other_sixteen_flags_keep_their_defaults(self, spec_dir: Path):
        result = _compile(spec_dir)
        assert result.lock is not None, result.errors
        flags = _flags_by_crawler_name(result.lock.crawlers)
        for name in ("bot_none", "bot_geo"):
            for key, value in _DEFAULT_FLAGS.items():
                if key == "geolocation_masking":
                    continue
                assert flags[name][key] == value, (name, key)
            assert set(flags[name]) == set(_DEFAULT_FLAGS)

    def test_api_json_carries_the_same_flag_values(self, spec_dir: Path):
        result = _compile(spec_dir)
        assert result.lock is not None, result.errors
        crawlers, _anchor = resolve_for_upload(result.lock, store=None)
        flags = _flags_by_crawler_name(crawlers)
        assert flags["bot_none"]["geolocation_masking"] == "mask"
        assert flags["bot_geo"]["geolocation_masking"] == "custom"


class TestUnknownMultiloginFlagIsADrop:
    def test_typo_in_maps_to_flag_key_is_a_drop(self, spec_dir: Path):
        text = SPEC.replace("geolocation_masking", "geolocaton_masking")
        result = _compile(spec_dir, text)
        assert result.lock is None
        assert any("geolocaton_masking" in e for e in result.errors), result.errors
        assert any("unknown Multilogin flag" in e for e in result.errors), result.errors
        assert any("one of:" in e and "geolocation_masking" in e for e in result.errors)

    def test_explicit_crawler_flag_typo_is_a_drop(self):
        raw = yaml.safe_load("""
version: "1.0"
experiment:
  name: MLX flags
crawlers:
  - name: bot
    agent:
      multilogin_profile:
        parameters:
          flags:
            geolocaton_masking: custom
    session:
      start: "+1m"
      workflow:
        - visit: https://example.com
""")
        result = SpecValidator().validate(raw)
        assert not result.valid
        assert any("geolocaton_masking" in e for e in result.errors), result.errors
        assert any("unknown Multilogin flag" in e for e in result.errors), result.errors

    def test_known_explicit_crawler_flag_is_clean(self):
        raw = yaml.safe_load("""
version: "1.0"
experiment:
  name: MLX flags
crawlers:
  - name: bot
    agent:
      multilogin_profile:
        parameters:
          flags:
            geolocation_masking: custom
    session:
      start: "+1m"
      workflow:
        - visit: https://example.com
""")
        result = SpecValidator().validate(raw)
        assert result.valid, result.errors
        assert result.dropped == 0

    def test_lax_typo_is_a_warning_with_a_count(self, spec_dir: Path):
        text = SPEC.replace("geolocation_masking", "geolocaton_masking")
        result = _compile(spec_dir, text, lax=True)
        assert result.lock is not None, result.errors
        assert any("geolocaton_masking" in w for w in result.warnings), result.warnings
        assert any("would have been dropped (lax mode)" in w for w in result.warnings)
