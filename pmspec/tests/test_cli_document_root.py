"""A spec file whose YAML root is not a mapping must be refused, not crashed on.

`pmspec validate` handed whatever YAML produced straight to
`SpecValidator.validate`, which starts with `spec.get(...)`: an empty file, a
bare list or a bare scalar came back as an uncaught
`AttributeError: 'list' object has no attribute 'get'` -- a stack trace where a
one-line refusal belongs. `compile_spec` and the MCP tool had guarded with
`isinstance(raw, dict)` all along.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from pmspec.cli import cli

ROOTS = [
    pytest.param("", "NoneType", id="empty-file"),
    pytest.param("- a\n- b\n", "list", id="bare-list"),
    pytest.param("hello\n", "str", id="bare-scalar"),
    pytest.param("42\n", "int", id="bare-number"),
]


def _spec(tmp_path: Path, text: str) -> str:
    p = tmp_path / "spec.yaml"
    p.write_text(text)
    return str(p)


@pytest.mark.parametrize("command", ["validate", "preview"])
@pytest.mark.parametrize("text,typename", ROOTS)
def test_a_non_mapping_root_is_refused_without_a_traceback(tmp_path, command, text, typename):
    out = CliRunner().invoke(cli, [command, _spec(tmp_path, text)], catch_exceptions=False)
    assert out.exit_code == 1, out.output
    assert f"spec must be a YAML mapping (got {typename})" in out.output


@pytest.mark.parametrize("text,typename", ROOTS)
def test_validate_json_format_reports_it_as_an_error(tmp_path, text, typename):
    out = CliRunner().invoke(
        cli,
        ["validate", _spec(tmp_path, text), "--format", "json"],
        catch_exceptions=False,
    )
    assert out.exit_code == 1
    payload = json.loads(out.output)
    assert payload["valid"] is False
    assert payload["errors"] == [f"spec must be a YAML mapping (got {typename})"]


def test_a_mapping_root_still_validates(tmp_path):
    text = (
        'version: "1.0"\nexperiment:\n  name: Root\ncrawlers:\n  - name: bot\n'
        '    session:\n      start: "+1m"\n      workflow:\n        - visit: https://x.com\n'
    )
    out = CliRunner().invoke(cli, ["validate", _spec(tmp_path, text)], catch_exceptions=False)
    assert out.exit_code == 0, out.output
