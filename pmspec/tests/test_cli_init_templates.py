"""Every `pmspec init -t <name>` template must be a spec pmspec accepts.

A template is the first spec a new user runs, so one that does not parse -- or
that parses and then loses a treatment arm to a drop -- costs more than a bad
example: it teaches the wrong shape. The net is the real validator, over every
template the CLI can emit, enumerated from the CLI's own map so a fourth
template cannot be added without one.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from pmspec.cli import cli, spec_templates
from pmspec.lock.build import load_and_prepare
from pmspec.spec.validator import SpecValidator

TEMPLATE_NAMES = sorted(spec_templates())


def _assert_spec_is_clean(text: str, base_dir, label: str) -> None:
    prepared = load_and_prepare(text, base_dir=base_dir, seed=1)
    assert prepared.errors == [], f"{label}: {prepared.errors}"
    result = SpecValidator(base_dir=base_dir).validate(prepared.raw)
    assert result.errors == [], f"{label}: {result.errors}"
    assert result.valid, label
    assert result.dropped == 0, f"{label}: {result.dropped} dropped construct(s)"


class TestInitTemplates:
    def test_the_cli_offers_exactly_the_templates_in_the_map(self):
        choice = cli.commands["init"].params[0].type
        assert sorted(choice.choices) == TEMPLATE_NAMES

    @pytest.mark.parametrize("name", TEMPLATE_NAMES)
    def test_template_validates_with_no_drops(self, name, tmp_path):
        path = tmp_path / f"{name}.yaml"
        init = CliRunner().invoke(cli, ["init", "-t", name, "-o", str(path)])
        assert init.exit_code == 0, init.output

        _assert_spec_is_clean(path.read_text(), tmp_path, name)

        validate = CliRunner().invoke(cli, ["validate", str(path)])
        assert validate.exit_code == 0, validate.output
        assert "Validation PASSED" in validate.output

    @pytest.mark.parametrize("name", TEMPLATE_NAMES)
    def test_template_compiles_to_a_lock(self, name, tmp_path):
        path = tmp_path / f"{name}.yaml"
        CliRunner().invoke(cli, ["init", "-t", name, "-o", str(path)])
        lock = tmp_path / f"{name}.lock.yaml"
        out = CliRunner().invoke(cli, ["compile", str(path), "--seed", "1", "-o", str(lock)])
        assert out.exit_code == 0, out.output
        assert lock.exists()
        assert "pmspec_lock:" in lock.read_text()


class TestYoutubeTemplateIsAYoutubeStudy:
    """The youtube template is the one that carries a treatment arm, and the
    arm is where the silent failures live: 0/1 switches, not intensities."""

    @pytest.fixture
    def spec(self):
        import yaml

        return yaml.safe_load(spec_templates()["youtube"])

    def test_the_condition_factor_toggles_switch_params(self, spec):
        factor = spec["design"]["factors"]["condition"]
        assert factor["levels"] == ["control", "treated"]
        for path in ("configuration.params.pi", "configuration.params.kappa"):
            assert factor["maps_to"][path] == {"control": 0, "treated": 1}

    def test_the_workflow_calls_the_decision_function(self, spec):
        text = spec_templates()["youtube"]
        assert "youtube_video_select" in text
        assert "lambda_select" in text

    def test_it_asks_for_a_cookie_preference(self, spec):
        prefs = spec["crawlers"][0]["configuration"]["preferences"]
        cookie = [p for p in prefs if p["name"] == "cookie_pref"]
        assert cookie and '"accept_all": true' in cookie[0]["value"]

    def test_its_session_starts_in_the_future(self, spec):
        assert spec["crawlers"][0]["session"]["start_at"] == "+1h"
