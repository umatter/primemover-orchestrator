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

from experiment_designer.spec.file_loader import create_file_tag_loader, resolve_file_references
from experiment_designer.spec.job_registry import JobRegistry
from experiment_designer.spec.schema_gen import SCHEMA_PATH, build_schema
from experiment_designer.spec.validator import SpecValidator

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
            ("crawler", "_KEYS_CRAWLER"),
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

    def test_root_properties_match_keys_root(self):
        assert self.SCHEMA["additionalProperties"] is False
        assert set(self.SCHEMA["properties"].keys()) == SpecValidator._KEYS_ROOT

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
        reads. This is the *only* violation; every other construct in the
        file (data_pools with a documentary-only `type`/`description`,
        lambda_select's own documentary-only `description`, from_pool proxy
        assignment, unresolved `{param}` placeholders in configuration.params)
        is deliberately schema-permissive (see schema_gen.py's `_params_schema`
        and the `dataPool`/shorthand-options `additionalProperties: true`)."""
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
