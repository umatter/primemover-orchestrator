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


def _crawler_with_flags(flags: str) -> dict:
    spec = yaml.safe_load(
        """
version: "1.0"
experiment:
  name: MLX flags
crawlers:
  - name: bot
    agent:
      multilogin_profile:
        parameters:
          flags:
"""
        + flags
        + """
    session:
      start: "+1m"
      workflow:
        - visit: https://example.com
"""
    )
    assert isinstance(spec, dict)
    return spec


class TestFlagValuesAreCheckedAgainstTheApisVocabulary:
    """docs/multilogin-profile-flags.md §2: the API validates `parameters.flags`
    *values* -- a wrong one is a 400 at profile creation, i.e. every bot in the
    spec fails setup on the VM. pmspec checked only the keys, so the whole table
    the spike measured is now a compile-time check."""

    def test_the_constant_covers_exactly_the_profiles_own_flags(self):
        from pmspec.spec.multilogin import MLX_FLAG_VALUES

        assert set(MLX_FLAG_VALUES) == set(_DEFAULT_FLAGS)

    def test_the_default_profile_is_a_profile_multilogin_would_accept(self):
        from pmspec.spec.multilogin import MLX_FLAG_VALUES

        for flag, value in _DEFAULT_FLAGS.items():
            assert value in MLX_FLAG_VALUES[flag], (flag, value)

    def test_canvas_blocking_is_accepted(self):
        """The one value that expresses the canvas-blocking privacy arm."""
        result = SpecValidator().validate(_crawler_with_flags("            canvas_noise: disabled"))
        assert result.valid, result.errors
        assert result.dropped == 0

    def test_geolocation_blocking_is_accepted(self):
        result = SpecValidator().validate(
            _crawler_with_flags("            geolocation_popup: block")
        )
        assert result.valid, result.errors

    def test_a_value_the_api_rejects_is_an_error(self):
        """`block` is the geolocation vocabulary, not the canvas one: the API
        answers 400 "wrong canvas noise flag"."""
        result = SpecValidator().validate(_crawler_with_flags("            canvas_noise: block"))
        assert not result.valid
        assert any(
            "flags.canvas_noise: 'block' is not accepted by Multilogin for canvas_noise" in e
            for e in result.errors
        ), result.errors
        assert any("disabled" in e and "mask" in e for e in result.errors), result.errors

    def test_startup_behavior_is_pinned_although_the_api_validates_nothing(self):
        """The API takes any string here and stores it verbatim, so a typo is
        silent -- which is exactly why pmspec pins it."""
        result = SpecValidator().validate(
            _crawler_with_flags("            startup_behavior: whatever")
        )
        assert not result.valid
        assert any("startup_behavior" in e and "whatever" in e for e in result.errors)

    def test_a_bad_maps_to_level_value_is_an_error(self):
        text = SPEC.replace(
            "agent.multilogin_profile.parameters.flags.geolocation_masking: "
            "{none: mask, geo: custom}",
            "agent.multilogin_profile.parameters.flags.canvas_noise: {none: mask, geo: block}",
        )
        result = SpecValidator().validate(yaml.safe_load(text))
        assert not result.valid
        assert any(
            "'block' is not accepted by Multilogin for canvas_noise" in e for e in result.errors
        ), result.errors

    def test_a_good_maps_to_level_value_is_clean(self):
        text = SPEC.replace(
            "agent.multilogin_profile.parameters.flags.geolocation_masking: "
            "{none: mask, geo: custom}",
            "agent.multilogin_profile.parameters.flags.canvas_noise: {none: mask, geo: disabled}",
        )
        result = SpecValidator().validate(yaml.safe_load(text))
        assert result.valid, result.errors
        assert result.dropped == 0

    def test_an_unknown_key_is_still_a_drop_not_a_value_error(self):
        result = SpecValidator().validate(_crawler_with_flags("            do_not_track: enabled"))
        assert not result.valid
        assert any("unknown Multilogin flag" in e for e in result.errors), result.errors


TEMPLATED = """
version: "1.0"
experiment:
  name: MLX flags
design:
  factors:
    privacy:
      levels: [mask, LEVEL_TWO]
  replications: 1
crawlers:
  - from_design: true
    name: "bot_{privacy}"
    agent:
      multilogin_profile:
        parameters:
          flags:
            canvas_noise: "{privacy}"
    session:
      start: "+1m"
      workflow:
        - visit: https://example.com
"""


class TestASubstitutedValueIsJudgedAfterExpansion:
    """A `{placeholder}` is not a value yet, so the raw spec cannot say whether
    it is accepted. The expanded crawlers can, and that is where it is caught --
    the same place `validate_expanded` already judges treatments."""

    def test_the_placeholder_itself_is_not_reported(self):
        raw = yaml.safe_load(TEMPLATED.replace("LEVEL_TWO", "disabled"))
        result = SpecValidator().validate(raw)
        assert result.valid, result.errors

    def test_a_good_level_survives_expansion(self, spec_dir: Path):
        result = _compile(spec_dir, TEMPLATED.replace("LEVEL_TWO", "disabled"))
        assert result.lock is not None, result.errors
        flags = _flags_by_crawler_name(result.lock.crawlers)
        assert flags["bot_disabled"]["canvas_noise"] == "disabled"

    def test_a_bad_level_is_caught_after_expansion(self, spec_dir: Path):
        result = _compile(spec_dir, TEMPLATED.replace("LEVEL_TWO", "blorp"))
        assert result.lock is None, "a profile Multilogin would 400 must not compile"
        assert any(
            "'blorp' is not accepted by Multilogin for canvas_noise" in e for e in result.errors
        ), result.errors

    def test_the_same_bad_value_is_reported_once_per_spec_not_once_per_bot(self, spec_dir: Path):
        text = TEMPLATED.replace("levels: [mask, LEVEL_TWO]", "levels: [blorp]").replace(
            "replications: 1", "replications: 3"
        )
        result = _compile(spec_dir, text)
        assert result.lock is None
        hits = [e for e in result.errors if "is not accepted by Multilogin" in e]
        assert len(hits) == 1, hits
        assert "3 place(s)" in hits[0], hits


def test_the_docs_say_values_are_checked():
    docs = (Path(__file__).resolve().parents[2] / "docs" / "pmspec.md").read_text()
    section = docs.split("### Multilogin profile flags", 1)[1].split("###", 1)[0]
    assert "multilogin-profile-flags.md" in section
    assert "not checked" not in section


class TestANonStringValueIsStillAValue:
    """A `maps_to`-shaped mapping written in the crawler's own agent block is
    the easy version of this mistake: `canvas_noise: {control: mask, treated:
    disabled}` looks like a factor mapping but sits where a value belongs, and
    the whole mapping is what reaches `/profile/create`. Skipping it as though
    it were a `{placeholder}` would compile a profile Multilogin 400s on for
    every bot."""

    def test_a_mapping_where_a_value_belongs_is_an_error(self):
        result = SpecValidator().validate(
            _crawler_with_flags(
                "            canvas_noise:\n"
                "              control: mask\n"
                "              treated: disabled"
            )
        )
        assert not result.valid, "a flags mapping must not pass as a placeholder"
        assert any(
            "is not accepted by Multilogin for canvas_noise" in e for e in result.errors
        ), result.errors

    def test_the_message_shows_what_the_author_wrote(self):
        result = SpecValidator().validate(
            _crawler_with_flags(
                "            canvas_noise:\n"
                "              control: mask\n"
                "              treated: disabled"
            )
        )
        message = next(e for e in result.errors if "canvas_noise" in e)
        assert '{"control": "mask", "treated": "disabled"}' in message, message
        assert "'{'control'" not in message, "a Python repr is not what the author wrote"

    def test_a_yaml_boolean_reads_back_as_yaml(self):
        result = SpecValidator().validate(_crawler_with_flags("            quic_mode: true"))
        assert not result.valid
        message = next(e for e in result.errors if "quic_mode" in e)
        assert "'true' is not accepted by Multilogin for quic_mode" in message, message

    def test_a_list_is_an_error_too(self):
        result = SpecValidator().validate(
            _crawler_with_flags("            canvas_noise: [mask, disabled]")
        )
        assert not result.valid
        assert any("canvas_noise" in e for e in result.errors), result.errors

    def test_a_string_placeholder_is_still_skipped(self):
        result = SpecValidator().validate(
            _crawler_with_flags('            canvas_noise: "{privacy}"')
        )
        assert result.valid, result.errors
