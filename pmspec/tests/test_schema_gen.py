"""The checked-in experiment-spec schema must equal `build_schema()`'s output,
and every shipped example spec must validate against it (modulo
websearch-polarization-study.yaml's one known pre-existing construct the
parser has never actually supported: a dict-form `wait:`)."""

from __future__ import annotations

import glob
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from pmspec.spec.file_loader import create_file_tag_loader, resolve_file_references
from pmspec.spec.job_registry import JobRegistry
from pmspec.spec.schema_gen import SCHEMA_PATH, build_schema
from pmspec.spec.validator import SpecValidator

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"


def _load_example(path: Path):
    loader, file_loader = create_file_tag_loader(base_dir=path.parent)
    raw = yaml.load(path.read_text(), Loader=loader)
    return resolve_file_references(raw, file_loader)


def test_checked_in_schema_is_generated():
    """Drift check: if this fails, someone edited the schema file by hand, or
    changed a validator `_KEYS_*`/`_OPTION_KEYS` table without `make schema`."""
    on_disk = json.loads(SCHEMA_PATH.read_text())
    assert on_disk == build_schema()


def test_generated_file_ends_with_a_newline():
    assert SCHEMA_PATH.read_text().endswith("\n")


def test_schema_is_valid_draft7():
    jsonschema.Draft7Validator.check_schema(build_schema())


class TestKeySetsProduceTheContract:
    """Each `_KEYS_*` whitelist becomes an `additionalProperties: false` object
    definition with exactly that property set -- the two cannot disagree."""

    SCHEMA = build_schema()

    @pytest.mark.parametrize(
        "definition,keys_attr",
        [
            ("experiment", "_KEYS_EXPERIMENT"),
            ("defaults", "_KEYS_DEFAULTS"),
            ("design", "_KEYS_DESIGN"),
            ("factor", "_KEYS_FACTOR"),
            ("configuration", "_KEYS_CONFIGURATION"),
            ("proxy", "_KEYS_PROXY"),
            ("agent", "_KEYS_AGENT"),
            ("session", "_KEYS_SESSION"),
        ],
    )
    def test_definition_properties_match_key_set(self, definition, keys_attr):
        d = self.SCHEMA["definitions"][definition]
        assert d["additionalProperties"] is False
        assert set(d["properties"].keys()) == getattr(SpecValidator, keys_attr)

    def test_root_and_crawler_are_the_union_of_both_spec_kinds(self):
        # One schema document describes a fleet spec and a runs spec, so these
        # two property sets are the union of the two whitelists; which of them
        # applies is `_validate_unknown_keys`'s decision, made per spec kind.
        assert self.SCHEMA["additionalProperties"] is False
        assert set(self.SCHEMA["properties"].keys()) == (
            SpecValidator._KEYS_ROOT | SpecValidator._KEYS_ROOT_RUNS
        )
        crawler = self.SCHEMA["definitions"]["crawler"]
        assert crawler["additionalProperties"] is False
        assert set(crawler["properties"].keys()) == (
            SpecValidator._KEYS_CRAWLER | SpecValidator._KEYS_CRAWLER_RUNS
        )

    def test_the_runs_only_keys_are_absent_from_the_fleet_whitelists(self):
        # The schema is permissive by construction above; the whitelists are
        # what actually refuse `updates:` on a fleet crawler (a drop) and
        # `agent:` on a runs crawler (an error).
        assert {"from_base", "updates"}.isdisjoint(SpecValidator._KEYS_CRAWLER)
        assert "runs_for" not in SpecValidator._KEYS_ROOT
        assert {"agent", "proxy", "configuration", "design"}.isdisjoint(
            SpecValidator._KEYS_CRAWLER_RUNS | SpecValidator._KEYS_ROOT_RUNS
        )

    def test_workflow_item_has_one_property_per_job_plus_control_keys(self):
        props = set(self.SCHEMA["definitions"]["workflowItem"]["properties"].keys())
        expected = {j.yaml_name for j in JobRegistry.list_all()} | {
            "template",
            "repeat",
            "with",
            "jobs",
        }
        assert props == expected
        assert self.SCHEMA["definitions"]["workflowItem"]["additionalProperties"] is False

    def test_job_type_enum_has_every_api_type(self):
        job = self.SCHEMA["definitions"]["job"]
        assert set(job["properties"]["type"]["enum"]) == {
            j.api_type for j in JobRegistry.list_all()
        }

    def test_proxy_type_matches_the_apis_proxy_types(self):
        """primemover_api/app/Modules/Proxy/Model/Proxy.php: TYPES = ['http',
        'brightdata', 'oxylabs']. The old hand-written schema also listed
        'socks5', which the API has never accepted."""
        enum = set(self.SCHEMA["definitions"]["proxy"]["properties"]["type"]["enum"])
        assert enum == {"http", "brightdata", "oxylabs"}
        assert "socks5" not in enum

    def test_params_keys_match_known_params_and_stay_permissive(self):
        params = self.SCHEMA["definitions"]["params"]
        assert set(params["properties"].keys()) == SpecValidator._KNOWN_PARAMS
        assert params["additionalProperties"] is True


class TestExampleSpecsValidate:
    CLEAN = [
        "simple-distribution-study.yaml",
        "websearch-study.yaml",
        "trump-search-mini-study.yaml",
    ]

    @pytest.mark.parametrize("name", CLEAN)
    def test_clean_examples_validate(self, name):
        spec = _load_example(EXAMPLES_DIR / name)
        jsonschema.validate(spec, build_schema())

    def test_polarization_example_fails_only_on_its_known_dict_form_wait(self):
        """`wait: {duration: ..., random_range: ...}` appears twice in this
        spec's templates. SpecParser._expand_shorthand's `wait` branch only
        ever accepts int/float/str (`_parse_duration`); a dict silently
        becomes duration 0 -- it was never a supported construct, so the
        schema correctly rejects it rather than modelling a shape nothing
        reads. This is the *only* schema violation; every other construct in
        the file (data_pools with a documentary-only `type`/`description`,
        lambda_select's `description`, from_pool proxy assignment, unresolved
        `{param}` placeholders in configuration.params) is deliberately
        schema-permissive (see schema_gen.py's `_params_schema` and the
        `dataPool`/shorthand-options `additionalProperties: true`). Permissive
        in the *schema* only: `lambda_select.description` is not in
        `_OPTION_KEYS["lambda_select"]`, so `SpecValidator` reports it as a
        drop ("unknown option; the runner never sees it") -- the schema leaves
        it to the validator precisely so that `--lax` can downgrade it."""
        spec = _load_example(EXAMPLES_DIR / "websearch-polarization-study.yaml")
        validator = jsonschema.Draft7Validator(build_schema())
        errors = sorted(validator.iter_errors(spec), key=lambda e: list(e.absolute_path))
        assert len(errors) == 2, [e.message for e in errors]
        for e in errors:
            path = ".".join(str(p) for p in e.absolute_path)
            assert path.endswith(".wait"), (path, e.message)
            assert "random_range" in e.message or "duration" in e.message

    def test_all_example_yaml_files_are_covered(self):
        """Pin the example directory's contents so a new example silently
        skips this test's parametrization instead of being noticed."""
        names = {Path(p).name for p in glob.glob(str(EXAMPLES_DIR / "*.yaml"))}
        assert names == set(self.CLEAN) | {"websearch-polarization-study.yaml"}


class TestWaitScrollScalarValues:
    """`oneOf` with both `{type: integer}` and `{type: number}` as separate
    branches rejects a plain int -- "integer" is a subset of "number", so the
    value is valid under two branches, and `oneOf` demands exactly one. Fixed
    by a single `{"type": ["integer", "number"]}` branch for `wait` (matching
    how `pixels`/`duration` already declare a multi-type property); `scroll`/
    `try_scroll` drop the `number` branch entirely instead, since a bare
    float there is not merely unmodelled but crashes the parser
    (`_expand_shorthand`'s scroll branch does `value.get(...)` on anything
    that is not `str`/`int`, and a `float` has no `.get`)."""

    BASE = """
version: "1.0"
experiment:
  name: X
crawlers:
  - name: bot
    session:
      start: "+1m"
      workflow:
        - {item}
"""

    @pytest.mark.parametrize(
        "item",
        ["wait: 5", "scroll: 10", "try_scroll: 10"],
    )
    def test_plain_integer_scalars_are_valid(self, item):
        spec = yaml.safe_load(self.BASE.format(item=item))
        result = SpecValidator().validate(spec)
        assert result.valid, result.errors

    def test_float_scroll_is_invalid(self):
        """`scroll: 2.5` was never a supported construct -- the parser would
        crash on it (see class docstring) -- so the schema must reject it."""
        spec = yaml.safe_load(self.BASE.format(item="scroll: 2.5"))
        result = SpecValidator().validate(spec)
        assert not result.valid

    def test_float_wait_is_valid(self):
        """Unlike scroll, `_parse_duration` explicitly accepts float (and
        int(value) truncates it), so wait has no analogous crash risk."""
        spec = yaml.safe_load(self.BASE.format(item="wait: 2.5"))
        result = SpecValidator().validate(spec)
        assert result.valid, result.errors


class TestDesignParameterForms:
    """`DistributionFactory.create` accepts a bare scalar (`parameters: {tau: 1.0}`
    -> `FixedValue`) and the expander writes it into `params.tau` -- so the
    schema must accept the scalar form beside the `{distribution: ...}` /
    `{value: ...}` object forms, or a spec the pipeline honours is rejected."""

    BASE = """
version: "1.0"
experiment:
  name: X
design:
  factors:
    arm:
      levels: [a, b]
  parameters:
    tau: {value}
  replications: 1
crawlers:
  - name: bot_{{arm}}
    from_design: true
    session:
      start: "+1m"
      workflow:
        - visit: https://example.com
"""

    @pytest.mark.parametrize("value", ["1.0", "1", "true", "'text'"])
    def test_scalar_parameter_validates(self, value):
        spec = yaml.safe_load(self.BASE.format(value=value))
        jsonschema.validate(spec, build_schema())
        result = SpecValidator().validate(spec)
        assert result.valid, result.errors

    @pytest.mark.parametrize("value", ["{distribution: uniform, min: 0, max: 1}", "{value: 1.0}"])
    def test_object_parameter_still_validates(self, value):
        spec = yaml.safe_load(self.BASE.format(value=value))
        jsonschema.validate(spec, build_schema())

    def test_distribution_enum_is_the_factorys_own_list(self):
        from pmspec.spec.distributions import DistributionFactory

        param = build_schema()["definitions"]["designParameter"]
        obj = next(b for b in param["oneOf"] if b.get("type") == "object")
        assert obj["properties"]["distribution"]["enum"] == DistributionFactory.list_types()


class TestEnumsMatchWhatTheRunnerAccepts:
    """The parser passes `typing_mode` through verbatim and upper-cases
    `direction`; the runner then does `TypingModeType.valueOf(...)` /
    `ScrollDirection.valueOf(...)` (primemover_runner
    `PrimemoverApiConverter.java`), which is exact and case-sensitive. So the
    schema's enums must be exactly the runner's enum constants for
    typing_mode, and both cases of the runner's two scroll directions."""

    # primemover_runner/.../primemover/model/TypingModeType.java, verified 2026-09-13.
    RUNNER_TYPING_MODES = [
        "DIRECT",
        "SIMULATED_KEEPINGTYPOS",
        "SIMULATED_FIXINGTYPOS",
        "SIMULATED_NOTYPOS",
    ]

    def test_typing_mode_enum_is_the_runners(self):
        schema = build_schema()
        assert schema["definitions"]["defaults"]["properties"]["typing_mode"]["enum"] == (
            self.RUNNER_TYPING_MODES
        )

    BASE = """
version: "1.0"
experiment:
  name: X
crawlers:
  - name: bot
    session:
      start: "+1m"
      workflow:
        - {item}
"""

    @pytest.mark.parametrize(
        "item",
        [
            "scroll: {duration: 5, direction: up}",
            "scroll: {duration: 5, direction: DOWN}",
            "try_scroll: {duration: 5, direction: down}",
            "scroll_length: {pixels: 100, direction: up}",
            "try_scroll_length: {length: 100, direction: UP}",
            "type: {text: hi, selector: x, typing_mode: DIRECT}",
            "try_type: {text: hi, selector: x, typing_mode: SIMULATED_NOTYPOS}",
        ],
    )
    def test_values_the_parser_honours_validate(self, item):
        spec = yaml.safe_load(self.BASE.format(item=item))
        result = SpecValidator().validate(spec)
        assert result.valid and result.dropped == 0, result.errors

    @pytest.mark.parametrize(
        "item",
        [
            "scroll: {duration: 5, direction: left}",
            "type: {text: hi, selector: x, typing_mode: FAST}",
            "type: {text: hi, selector: x, typing_mode: direct}",
        ],
    )
    def test_values_the_runner_rejects_are_invalid(self, item):
        spec = yaml.safe_load(self.BASE.format(item=item))
        result = SpecValidator().validate(spec)
        assert not result.valid


def test_pool_source_does_not_claim_s3_support():
    """FileLoader reads local files only; the schema used to advertise an s3:// URI."""
    source = build_schema()["definitions"]["dataPool"]["properties"]["source"]
    assert "s3" not in source["description"].lower()
