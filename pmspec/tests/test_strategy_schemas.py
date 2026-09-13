import json
from pathlib import Path

import yaml

from pmspec.spec.validator import SpecValidator

SCHEMA = {
    "type": "object",
    "required": ["strategy", "reliability"],
    "properties": {
        "strategy": {"const": "autocomplete_v1"},
        "reliability": {"enum": ["high", "low"]},
    },
}

SPEC = """
version: "1.0"
experiment: {name: S}
crawlers:
  - name: bot
    session:
      start: "+1m"
      workflow:
        - lambda_select:
            task: t
            function: f
            strategy: autocomplete_v1
            treatment: {reliability: high}
"""


def _validate(text, base_dir=None, lax=False, monkeypatch=None):
    return SpecValidator(base_dir=base_dir, lax=lax).validate(yaml.safe_load(text))


def test_missing_schema_is_a_drop():
    # An impossible strategy name: a future autocomplete_v1.schema.json in the
    # bot-decision-yt sibling must not turn this into a pass.
    text = SPEC.replace("strategy: autocomplete_v1", "strategy: no_such_strategy_zz")
    r = _validate(text)
    assert not r.valid and any(
        "no schema for strategy 'no_such_strategy_zz'" in e for e in r.errors
    ), r.errors
    assert _validate(text, lax=True).valid


def test_declared_path_that_is_missing_is_named_not_no_schema(tmp_path: Path):
    text = SPEC + "strategy_schemas:\n  autocomplete_v1: nope.schema.json\n"
    r = _validate(text, base_dir=tmp_path)
    assert not r.valid and r.dropped == 1
    (msg,) = [e for e in r.errors if "strategy_schemas.autocomplete_v1" in e]
    assert "declared path" in msg and "nope.schema.json" in msg and "missing or not JSON" in msg
    assert not any("no schema for strategy" in e for e in r.errors), r.errors
    assert _validate(text, base_dir=tmp_path, lax=True).valid


def test_declared_path_that_is_not_json_is_named(tmp_path: Path):
    (tmp_path / "broken.schema.json").write_text("{not json")
    text = SPEC + "strategy_schemas:\n  autocomplete_v1: broken.schema.json\n"
    r = _validate(text, base_dir=tmp_path)
    assert any(
        "strategy_schemas.autocomplete_v1" in e and "broken.schema.json" in e for e in r.errors
    ), r.errors
    assert not any("no schema for strategy" in e for e in r.errors), r.errors


def test_inline_schema_validates_the_treatment():
    text = SPEC + "strategy_schemas:\n  autocomplete_v1: " + json.dumps(SCHEMA) + "\n"
    assert _validate(text).valid
    bad = text.replace("reliability: high", "reliability: medium")
    r = _validate(bad)
    assert any("invalid for strategy 'autocomplete_v1'" in e for e in r.errors), r.errors


def test_schema_path_relative_to_the_spec(tmp_path: Path):
    (tmp_path / "s.schema.json").write_text(json.dumps(SCHEMA))
    text = SPEC + "strategy_schemas:\n  autocomplete_v1: s.schema.json\n"
    assert _validate(text, base_dir=tmp_path).valid
    r = _validate(text)  # no base_dir -> resolved against cwd, not found
    assert not r.valid


def test_env_dirs(tmp_path: Path, monkeypatch):
    (tmp_path / "autocomplete_v1.schema.json").write_text(json.dumps(SCHEMA))
    monkeypatch.setenv("PMSPEC_STRATEGY_SCHEMA_DIRS", str(tmp_path))
    assert _validate(SPEC).valid


def test_compile_with_files_carrying_the_schema_via_mcp():
    from orchestrator.mcp_server import server as s

    out = json.loads(
        s.compile_experiment_spec(
            SPEC + "strategy_schemas:\n  autocomplete_v1: s.schema.json\n",
            files={"s.schema.json": json.dumps(SCHEMA)},
            seed=1,
        )
    )
    assert "lock" in out, out


# --- treatment values are only concrete after expansion ------------------------

TEMPLATED = """
version: "1.0"
experiment: {name: S}
strategy_schemas:
  autocomplete_v1:
    type: object
    required: [strategy, reliability]
    properties:
      strategy: {const: autocomplete_v1}
      reliability: {enum: [high, low]}
    additionalProperties: false
templates:
  diet:
    - lambda_select:
        task: t
        function: f
        strategy: autocomplete_v1
        treatment: {reliability: "${reliability}"}
  neutral:
    - lambda_select:
        task: t
        function: f
        strategy: autocomplete_v1
crawlers:
  - name: bot
    session:
      start: "+1m"
      workflow:
        - template: diet
          with: {reliability: high}
        - template: neutral
"""


def test_template_placeholders_are_not_validated_against_the_schema():
    """`${reliability}` is a template parameter; its value exists only after `with:`."""
    r = _validate(TEMPLATED)
    assert r.valid, r.errors


def test_marker_only_job_is_valid():
    """A `strategy` with no `treatment` activates the behaviours channel for a job
    that carries no arm (the neutral bank); the schema's `required` is about the
    treatment, not about the marker."""
    text = TEMPLATED.replace("        - template: diet\n          with: {reliability: high}\n", "")
    assert _validate(text).valid


def test_expanded_values_are_validated_and_grouped():
    from pmspec.lock.build import compile_spec

    bad = TEMPLATED.replace("with: {reliability: high}", "with: {reliability: medium}")
    r = compile_spec(bad, base_dir=Path("."), spec_path="s.yaml", seed=1)
    assert r.lock is None
    hits = [e for e in r.errors if "invalid for strategy 'autocomplete_v1'" in e and "medium" in e]
    assert len(hits) == 1, r.errors
    assert "crawlers[0].queues[0].jobs[0]" in hits[0]
    good = compile_spec(TEMPLATED, base_dir=Path("."), spec_path="s.yaml", seed=1)
    assert good.lock is not None, good.errors


def test_expanded_validation_ignores_job_control_behaviours():
    """task/linkType/linkValue live next to the treatment keys as behaviours; a
    schema with additionalProperties: false must not see them."""
    from pmspec.lock.build import compile_spec

    r = compile_spec(TEMPLATED, base_dir=Path("."), spec_path="s.yaml", seed=1)
    assert r.lock is not None, r.errors


def test_expanded_errors_are_grouped_across_crawlers():
    from pmspec.lock.build import compile_spec

    many = TEMPLATED.replace("with: {reliability: high}", "with: {reliability: medium}").replace(
        "crawlers:\n  - name: bot\n",
        "design:\n  factors:\n    arm:\n      levels: [a, b, c]\n  replications: 2\ncrawlers:\n  - name: bot_{arm}_{replica}\n    from_design: true\n",
    )
    r = compile_spec(many, base_dir=Path("."), spec_path="s.yaml", seed=1)
    hits = [e for e in r.errors if "medium" in e]
    assert len(hits) == 1 and "6 place(s)" in hits[0], r.errors
