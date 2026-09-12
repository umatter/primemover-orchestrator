"""§4 of the lock design: whatever would be dropped, ignored or left unresolved is
an error; --lax turns it into a warning and a count. One row per rule."""

from __future__ import annotations

import pytest

from experiment_designer.spec.converter import SpecConverter
from experiment_designer.spec.drops import Drops
from experiment_designer.spec.parser import SpecParser
from experiment_designer.spec.validator import SpecValidator


class TestDrops:
    def test_strict_puts_messages_in_errors(self):
        d = Drops()
        d.report("crawlers[0].configuration.params.foo", "unknown parameter", "remove it")
        errors: list[str] = []
        warnings: list[str] = []
        d.merge_into(errors, warnings)
        assert errors == ["crawlers[0].configuration.params.foo: unknown parameter; remove it"]
        assert warnings == [] and d.count == 1

    def test_lax_puts_messages_in_warnings_with_a_count(self):
        d = Drops(lax=True)
        d.report("a", "x", "y")
        d.report("b", "x", "y")
        errors: list[str] = []
        warnings: list[str] = []
        d.merge_into(errors, warnings)
        assert errors == []
        assert warnings[:2] == ["a: x; y", "b: x; y"]
        assert warnings[2] == "2 construct(s) would have been dropped (lax mode)"

    def test_no_drops_no_count_line(self):
        warnings: list[str] = []
        Drops(lax=True).merge_into([], warnings)
        assert warnings == []


class TestComponentsCarryLax:
    def test_validator_result_has_dropped(self):
        result = SpecValidator().validate(
            {"experiment": {"name": "x"}, "crawlers": [{"name": "b"}]}
        )
        assert hasattr(result, "dropped") and result.dropped == 0

    def test_parser_and_converter_accept_lax(self):
        parsed = SpecParser(lax=True).parse("experiment: {name: x}\ncrawlers:\n  - name: b\n")
        assert parsed.dropped == 0
        assert SpecConverter(lax=True).convert(parsed).dropped == 0


BASE = """
version: "1.0"
experiment:
  name: Strict
crawlers:
  - name: bot
    session:
      start: "+1m"
      workflow:
        - visit: https://example.com
"""


def _validate(yaml_text: str, lax: bool = False):
    import yaml

    return SpecValidator(lax=lax).validate(yaml.safe_load(yaml_text))


class TestValidatorDropRules:
    """One row per §4 rule: strict -> error, lax -> warning + count."""

    ROWS = [
        pytest.param(
            BASE.replace(
                "  - name: bot\n",
                "  - name: bot\n    configuration:\n      params: {reliability: high}\n",
            ),
            "crawler[0].configuration.params.reliability",
            id="unknown-params-key",
        ),
        pytest.param(
            BASE + "design:\n  factors:\n    arm:\n      levels: [a, b]\n      maps_to:\n"
            "        params.lean: {a: L, b: R}\n  replications: 1\n",
            "design.factors.arm.maps_to.params.lean",
            id="unknown-maps_to-param",
        ),
        pytest.param(
            BASE.replace(
                "        - visit: https://example.com\n",
                "        - lambda_select:\n            task: t\n            function: f\n"
                "            treatment: {reliability: high}\n",
            ),
            "lambda_select",
            id="treatment-without-strategy",
        ),
        pytest.param(
            BASE.replace(
                "        - visit: https://example.com\n",
                "        - teleport: https://example.com\n",
            ),
            "unknown job type 'teleport'",
            id="unknown-job-type",
        ),
        pytest.param(
            BASE.replace('      start: "+1m"\n', ""),
            "crawler[0].session: no start time",
            id="missing-session-start",
        ),
        pytest.param(BASE + "sceduled: true\n", "sceduled", id="unknown-root-key"),
        pytest.param(
            BASE + "defaults:\n  proxy: {hostnme: h, port: 1}\n",
            "defaults.proxy.hostnme",
            id="unknown-defaults-proxy-key",
        ),
        pytest.param(
            BASE.replace("  - name: bot\n", "  - name: bot\n    proxxy: {hostname: h}\n"),
            "crawler[0].proxxy",
            id="unknown-crawler-key",
        ),
        pytest.param(
            BASE.replace('      start: "+1m"\n', '      start: "+1m"\n      begin: now\n'),
            "crawler[0].session.begin",
            id="unknown-session-key",
        ),
    ]

    @pytest.mark.parametrize("yaml_text,needle", ROWS)
    def test_strict_is_an_error(self, yaml_text, needle):
        r = _validate(yaml_text)
        assert not r.valid
        assert any(needle in e for e in r.errors), r.errors
        assert r.dropped >= 1

    @pytest.mark.parametrize("yaml_text,needle", ROWS)
    def test_lax_is_a_warning_with_a_count(self, yaml_text, needle):
        r = _validate(yaml_text, lax=True)
        assert r.valid, r.errors
        assert any(needle in w for w in r.warnings), r.warnings
        assert any("would have been dropped (lax mode)" in w for w in r.warnings)
        assert r.dropped >= 1


class TestValidatorKeepsWarnings:
    def test_self_configuring_proxy_without_host_is_clean(self):
        # oxylabs: the converter deliberately emits no host/port (the runner
        # supplies them), so a hostname warning would ask for what must not be there.
        r = _validate(
            BASE.replace("  - name: bot\n", "  - name: bot\n    proxy: {type: oxylabs}\n")
        )
        assert r.valid and r.dropped == 0
        assert not any("hostname" in w or "port" in w for w in r.warnings), r.warnings

        # A plain http proxy with no hostname keeps its warning. A warning, not a
        # drop: the converter substitutes 127.0.0.1:8080, which upload will refuse
        # anyway at the credential check.
        r = _validate(BASE.replace("  - name: bot\n", "  - name: bot\n    proxy: {type: http}\n"))
        assert r.valid and r.dropped == 0
        assert any("no hostname" in w for w in r.warnings), r.warnings

    def test_every_example_spec_and_research_spec_uses_only_known_keys(self):
        """The key sets are a whitelist; this pins them to the specs that exist."""
        import glob
        from pathlib import Path

        import yaml

        from experiment_designer.spec.file_loader import create_file_tag_loader

        paths = glob.glob("experiment_designer/examples/*.yaml") + [
            p
            for p in [
                "/home/umatter/projects/autocompleted_misinformation/design/experiment_spec.yaml",
            ]
            if Path(p).exists()
        ]
        for p in paths:
            loader, _ = create_file_tag_loader(base_dir=Path(p).parent)
            raw = yaml.load(Path(p).read_text(), Loader=loader)
            r = SpecValidator().validate(raw)
            unknown = [e for e in r.errors if "unknown key" in e]
            assert unknown == [], (p, unknown)


class TestUnresolvedVariables:
    def _convert(self, yaml_text, lax=False):
        parsed = SpecParser(lax=lax).parse(yaml_text)
        assert not parsed.errors, parsed.errors
        return SpecConverter(lax=lax).convert(parsed)

    def test_template_variable_not_supplied_is_a_drop(self):
        text = (
            BASE.replace(
                '    session:\n      start: "+1m"\n      workflow:\n        - visit: https://example.com\n',
                '    session:\n      start: "+1m"\n      workflow:\n        - template: t\n',
            )
            + "templates:\n  t:\n    - type: {text: '${reliability}', selector: '//x'}\n"
        )
        r = self._convert(text)
        assert any(
            "${reliability}" in e and "crawlers[0].queues[0].jobs[0]" in e for e in r.errors
        ), r.errors
        lax = self._convert(text, lax=True)
        assert not lax.errors and lax.dropped == 1

    def test_design_variable_not_defined_is_a_drop(self):
        text = (
            BASE.replace("  - name: bot\n", "  - name: bot_{arm}_{colour}\n    from_design: true\n")
            + "design:\n  factors:\n    arm:\n      levels: [a, b]\n  replications: 1\n"
        )
        r = self._convert(text)
        assert any("{colour}" in e for e in r.errors), r.errors
        assert not any("{arm}" in e for e in r.errors)

    def test_supplied_with_variable_is_fine(self):
        text = (
            BASE.replace(
                "        - visit: https://example.com\n",
                "        - template: t\n          with: {reliability: high}\n",
            )
            + "templates:\n  t:\n    - type: {text: '${reliability}', selector: '//x'}\n"
        )
        assert self._convert(text).errors == []

    def test_multilogin_profile_json_is_not_scanned(self):
        text = BASE.replace(
            "  - name: bot\n",
            "  - name: bot\n    agent:\n      multilogin_profile: {notes: 'keep {this} and ${that}'}\n",
        )
        assert self._convert(text).errors == []


class TestTimestampScalars:
    def test_unquoted_iso_timestamp_does_not_crash(self):
        text = BASE.replace('start: "+1m"', "start: 2026-10-01T10:00:00")
        parsed = SpecParser().parse(text)
        assert not parsed.errors, parsed.errors
        r = SpecConverter().convert(parsed)
        assert r.json_data[0]["queues"][0]["start_at"].startswith("2026-10-01T10:00:00")

    def test_unquoted_date_becomes_midnight(self):
        text = BASE.replace('start: "+1m"', "start: 2026-10-01")
        r = SpecConverter().convert(SpecParser().parse(text))
        assert r.json_data[0]["queues"][0]["start_at"].startswith("2026-10-01T00:00:00")


class TestUnknownWorkflowItemDrop:
    def test_strict_names_the_construct(self):
        text = BASE.replace("        - visit: https://example.com\n", "        - teleport: x\n")
        parsed = SpecParser().parse(text)
        assert any("teleport" in e for e in parsed.errors), parsed.errors

    def test_lax_warns_and_counts_a_drop(self):
        text = BASE.replace("        - visit: https://example.com\n", "        - teleport: x\n")
        parsed = SpecParser(lax=True).parse(text)
        assert not parsed.errors, parsed.errors
        assert any("teleport" in w for w in parsed.warnings), parsed.warnings
        assert parsed.dropped == 1
