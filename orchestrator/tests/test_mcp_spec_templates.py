"""`get_spec_template` hands a template to whoever is composing a study next,
so each one must be a spec pmspec accepts -- same net as `pmspec init`'s
templates, over the MCP server's own (separate) copies.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.mcp_server.server import MCP_SPEC_TEMPLATES, get_spec_template
from pmspec.lock.build import load_and_prepare
from pmspec.spec.validator import SpecValidator

TEMPLATE_NAMES = sorted(MCP_SPEC_TEMPLATES)


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_template_validates_with_no_drops(name, tmp_path):
    text = json.loads(get_spec_template(name))["template"]

    prepared = load_and_prepare(text, base_dir=tmp_path, seed=1)
    assert prepared.errors == [], f"{name}: {prepared.errors}"

    result = SpecValidator(base_dir=tmp_path).validate(prepared.raw)
    assert result.errors == [], f"{name}: {result.errors}"
    assert result.valid, name
    assert result.dropped == 0, f"{name}: {result.dropped} dropped construct(s)"


def test_the_youtube_template_treats_pi_as_a_switch():
    """The CLI template's old bug -- levels of -0.5/0/0.5, every one of which
    rounds to a control arm -- must not appear here either."""
    import yaml

    text = MCP_SPEC_TEMPLATES["youtube"]
    assert "youtube_video_select" in text

    spec = yaml.safe_load(text)
    for factor in spec["design"]["factors"].values():
        for path, level_map in factor["maps_to"].items():
            if path.rsplit(".", 1)[-1] in {"pi", "psi", "kappa", "tau"}:
                for level, value in level_map.items():
                    assert value in (0, 1), f"{path}[{level}]: {value!r} is not a 0/1 switch"


def test_an_unknown_type_falls_back_to_default():
    assert json.loads(get_spec_template("nope"))["template"] == MCP_SPEC_TEMPLATES["default"]
