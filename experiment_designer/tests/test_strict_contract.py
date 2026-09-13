"""§4 of the lock design: whatever would be dropped, ignored or left unresolved is
an error; --lax turns it into a warning and a count. One row per rule."""

from __future__ import annotations

import re

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
            BASE.replace(
                "        - visit: https://example.com\n",
                "        - template: t\n          weight: 0.7\n",
            )
            + "templates:\n  t:\n    - wait: 1s\n",
            "crawler[0].session.workflow[0].weight",
            id="unknown-key-on-template-item",
        ),
        pytest.param(
            BASE.replace(
                "        - visit: https://example.com\n",
                "        - repeat: 2\n          jobs: [{wait: 1s}]\n          times: 3\n",
            ),
            "crawler[0].session.workflow[0].times",
            id="unknown-key-on-repeat-item",
        ),
        pytest.param(
            BASE.replace(
                "        - visit: https://example.com\n",
                "        - repeat: 2\n          jobs: [{teleport: x}]\n",
            ),
            "crawler[0].session.workflow[0].jobs[0]: unknown job type 'teleport'",
            id="unknown-job-type-nested-in-repeat",
        ),
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
        pytest.param(
            BASE.replace(
                "        - visit: https://example.com\n",
                "        - visit: https://example.com\n          weight: 0.7\n",
            ),
            "crawler[0].session.workflow[0].weight",
            id="stray-workflow-item-key",
        ),
        pytest.param(
            BASE.replace(
                "        - visit: https://example.com\n",
                "        - click: {selector: x, sped: fast}\n",
            ),
            "crawler[0].session.workflow[0].click.sped",
            id="unknown-job-option",
        ),
        pytest.param(
            BASE + "design:\n  factors:\n    arm:\n      levels: [a, b]\n      mapsto:\n"
            "        params.pi: {a: 0, b: 1}\n  replications: 1\n",
            "design.factors.arm.mapsto",
            id="unknown-factor-key",
        ),
        pytest.param(
            BASE.replace("  - name: bot\n", "  - name: bot_{arm}\n    from_design: true\n")
            + "design:\n  factors:\n    arm:\n      levels: [a, b]\n"
            "  parameters:\n    foo:\n      distribution: uniform\n      min: 0\n      max: 1\n"
            "  replications: 1\n",
            "design.parameters.foo",
            id="non-standard-sampled-param-unused",
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

    def test_template_and_repeat_items_with_only_their_own_keys_are_clean(self):
        text = (
            BASE.replace(
                "        - visit: https://example.com\n",
                "        - template: t\n          with: {x: 1}\n"
                "        - repeat: 2\n          template: t\n"
                "        - repeat: 2\n          jobs: [{wait: 1s}]\n",
            )
            + "templates:\n  t:\n    - wait: 1s\n"
        )
        r = _validate(text)
        assert r.valid and r.dropped == 0, r.errors

    def test_every_example_spec_and_research_spec_uses_only_known_keys(self):
        """The structural key sets (_KEYS_ROOT ... _KEYS_DESIGN) are a whitelist;
        this pins them to the specs that exist. Only the structural
        "unknown key; remove it or fix the spelling" report counts: the
        template/repeat-item check is a different rule (it caught `weight: 0.7`
        on the research spec's template calls -- a real drop for that repo to
        fix, not a hole in these whitelists)."""
        import glob
        from pathlib import Path

        import yaml

        from experiment_designer.spec.file_loader import create_file_tag_loader

        examples = Path(__file__).resolve().parents[1] / "examples"
        paths = glob.glob(str(examples / "*.yaml")) + [
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
            unknown = [e for e in r.errors if "unknown key; remove it or fix" in e]
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

    def test_same_variable_in_many_places_is_one_grouped_message(self):
        """One `${q}` in a template used by every crawler and job used to produce
        one message per occurrence -- 1200 of them for 300 bots x 4 jobs."""
        text = (
            BASE.replace(
                "        - visit: https://example.com\n",
                "        - template: t\n        - template: t\n",
            ).replace(
                "  - name: bot\n",
                "  - name: bot_{arm}\n    from_design: true\n",
            )
            + "design:\n  factors:\n    arm:\n      levels: [a, b]\n  replications: 1\n"
            + "templates:\n  t:\n    - type: {text: '${q}', selector: '//x'}\n"
        )
        r = self._convert(text, lax=True)
        about_q = [w for w in r.warnings if "${q}" in w]
        assert len(about_q) == 1 and r.dropped == 1, r.warnings
        assert "4 place(s)" in about_q[0] and "first: crawlers[0].queues[0].jobs[0]" in about_q[0]
        assert about_q[0].startswith("crawlers[*].queues[*].jobs[*].behaviors.text:"), about_q[0]

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


class TestFullJobWorkflowItem:
    """`type` is overloaded: it is both the key of a full job definition
    (`type: visitjob`, a registered API type string) and the shorthand key of
    the `type:` (enter text) job (`type: {text: ..., selector: ...}`, a
    dict). `_expand_workflow_item`'s shorthand loop used to check `type` as a
    shorthand key first, so a full job's `type: visitjob` was swallowed by
    that shorthand and errored ("'type' job requires dict with 'text' and
    'selector'") instead of being recognised as a full job -- full jobs in
    `workflow:` have been broken since shorthand support was added.
    `JobRegistry.is_api_type` now gates full-job detection ahead of the
    shorthand loop in both the parser and the validator."""

    FULL_JOB = BASE.replace(
        "        - visit: https://example.com\n",
        "        - type: visitjob\n"
        "          name: My Visit\n"
        "          description: go there\n"
        "          behaviors:\n"
        "            - {name: url, value: 'https://example.com', description: '', active: true}\n",
    )

    def test_full_job_parses_to_one_job_with_its_behaviours(self):
        parsed = SpecParser().parse(self.FULL_JOB)
        assert not parsed.errors, parsed.errors
        jobs = parsed.crawlers[0]["queues"][0]["jobs"]
        assert len(jobs) == 1
        job = jobs[0]
        assert job["type"] == "visitjob"
        assert job["name"] == "My Visit"
        assert job["behaviors"] == [
            {
                "name": "url",
                "value": "https://example.com",
                "description": "",
                "active": True,
            }
        ]

    def test_full_job_validates_clean(self):
        r = _validate(self.FULL_JOB)
        assert r.valid and r.dropped == 0, (r.errors, r.warnings)

    def test_full_job_with_a_typo_key_is_a_drop(self):
        text = self.FULL_JOB.replace("          behaviors:\n", "          behaviours:\n")
        r = _validate(text)
        assert not r.valid
        assert any("behaviours" in e for e in r.errors), r.errors
        assert r.dropped >= 1

    def test_full_job_with_a_stray_key_is_a_drop(self):
        text = self.FULL_JOB.replace(
            "          description: go there\n",
            "          description: go there\n          weight: 0.7\n",
        )
        r = _validate(text)
        assert not r.valid
        assert any("weight" in e for e in r.errors), r.errors
        assert r.dropped >= 1

    def test_type_shorthand_with_a_dict_still_works(self):
        text = BASE.replace(
            "        - visit: https://example.com\n",
            "        - type: {text: hello, selector: '//input'}\n",
        )
        parsed = SpecParser().parse(text)
        assert not parsed.errors, parsed.errors
        jobs = parsed.crawlers[0]["queues"][0]["jobs"]
        assert len(jobs) == 1 and jobs[0]["type"] == "entertextfieldjob"

        r = _validate(text)
        assert r.valid and r.dropped == 0, (r.errors, r.warnings)


class TestFormatSpecs:
    """design_expander must resolve {name:spec} (e.g. {pi:.2f}), not just the
    handful of bare {name} forms -- our own shipped examples use it."""

    def _convert(self, yaml_text, lax=False):
        parsed = SpecParser(lax=lax).parse(yaml_text)
        assert not parsed.errors, parsed.errors
        return SpecConverter(lax=lax).convert(parsed)

    def test_numeric_format_spec_on_sampled_and_replica_resolves(self):
        text = (
            BASE.replace(
                "  - name: bot\n",
                "  - name: bot_{arm}\n"
                '    description: "A={alpha:.3f} R={replica:02d} arm={arm}"\n'
                "    from_design: true\n",
            )
            + "design:\n  factors:\n    arm:\n      levels: [a, b]\n"
            "  parameters:\n    alpha:\n      distribution: uniform\n      min: 0\n      max: 1\n"
            "  replications: 1\n"
        )
        r = self._convert(text)
        assert r.errors == [], r.errors
        descriptions = [c["description"] for c in r.json_data]
        assert any(
            re.match(r"A=\d\.\d{3} R=\d{2} arm=(a|b)", d) for d in descriptions
        ), descriptions

    def test_format_spec_on_string_value_is_left_unresolved(self):
        text = (
            BASE.replace(
                "  - name: bot\n",
                "  - name: bot_{arm}\n"
                '    description: "bad={arm:.2f}"\n'
                "    from_design: true\n",
            )
            + "design:\n  factors:\n    arm:\n      levels: [a, b]\n  replications: 1\n"
        )
        r = self._convert(text)
        assert any("{arm:.2f}" in e for e in r.errors), r.errors


class TestSampledSubstitutionParameter:
    """A `design.parameters` name outside _KNOWN_PARAMS is a drop when nothing
    references it (the expander writes it into configuration.params, which the
    API discards) -- but a spec that uses `{name}` somewhere under crawlers is
    using it as a substitution variable, which is legitimate. The converter
    then strips it from params, since the value has already landed where it was
    referenced and the API would drop the key anyway."""

    SPEC = (
        BASE.replace(
            "  - name: bot\n",
            "  - name: bot_{arm}\n    from_design: true\n"
            "    configuration:\n      preferences:\n"
            "        - {name: alpha_draw, value: '{alpha_draw}'}\n",
        )
        + "design:\n  seed: 7\n  factors:\n    arm:\n      levels: [a, b]\n"
        "  parameters:\n    alpha_draw:\n      distribution: uniform\n      min: 0\n      max: 1\n"
        "  replications: 1\n"
    )

    def test_converter_and_validator_agree_on_the_standard_params(self):
        # The strip keys on the converter's copy of the set; the drop rule on
        # the validator's. They must be the same set or a name could be
        # stripped without having been checked for a reference (or vice versa).
        from experiment_designer.spec import converter

        assert converter._STANDARD_PARAM_KEYS == SpecValidator._KNOWN_PARAMS

    def test_referenced_parameter_validates_clean(self):
        r = _validate(self.SPEC)
        assert r.valid and r.dropped == 0, (r.errors, r.warnings)

    def test_format_spec_reference_counts_as_a_reference(self):
        text = self.SPEC.replace("'{alpha_draw}'", "'{alpha_draw:.2f}'")
        r = _validate(text)
        assert r.valid and r.dropped == 0, (r.errors, r.warnings)

    def test_reference_inside_a_template_counts(self):
        text = (
            self.SPEC.replace(
                "        - {name: alpha_draw, value: '{alpha_draw}'}\n",
                "        - {name: fixed, value: 'x'}\n",
            ).replace(
                "        - visit: https://example.com\n",
                "        - template: t\n",
            )
            + "templates:\n  t:\n    - type: {text: 'a={alpha_draw}', selector: '//x'}\n"
        )
        r = _validate(text)
        assert r.valid and r.dropped == 0, (r.errors, r.warnings)

    def test_converter_strips_it_from_params_and_preference_carries_the_number(self):
        parsed = SpecParser().parse(self.SPEC)
        assert not parsed.errors, parsed.errors
        r = SpecConverter().convert(parsed)
        assert r.errors == [] and r.dropped == 0, (r.errors, r.warnings)
        assert len(r.json_data) == 2
        for crawler in r.json_data:
            params = crawler["configuration"]["params"]
            assert "alpha_draw" not in params, params
            assert set(params) == SpecValidator._KNOWN_PARAMS, params
            pref = {p["name"]: p["value"] for p in crawler["configuration"]["preferences"]}
            assert 0.0 <= float(pref["alpha_draw"]) <= 1.0, pref

    def test_standard_sampled_parameter_still_lands_in_params(self):
        text = self.SPEC.replace("alpha_draw", "alpha")
        parsed = SpecParser().parse(text)
        r = SpecConverter().convert(parsed)
        assert r.errors == [], r.errors
        for crawler in r.json_data:
            alpha = crawler["configuration"]["params"]["alpha"]
            assert 0.0 <= alpha <= 1.0 and alpha != 1, alpha  # sampled, not the default

    def test_converter_strips_an_unreferenced_one_too(self):
        """The validator is what refuses the spec; in lax mode (or a caller
        that skips validation) the converter still does not emit a key the
        API would drop."""
        text = self.SPEC.replace(
            "        - {name: alpha_draw, value: '{alpha_draw}'}\n",
            "        - {name: fixed, value: 'x'}\n",
        )
        parsed = SpecParser().parse(text)
        r = SpecConverter().convert(parsed)
        assert r.errors == [], r.errors
        assert all("alpha_draw" not in c["configuration"]["params"] for c in r.json_data)


class TestJobOptionKeys:
    """Every dict-form option key SpecParser._expand_shorthand reads stays
    clean; visit/wait have no dict form and are already an error there, so
    the option check must not add a second message."""

    @pytest.mark.parametrize(
        "item",
        [
            "click: {selector: x, selection_type: CSS, captcha_mode: never, task: t, flag: f}",
            "try_click: {selector: x, task: t}",
            "type: {text: hi, selector: x, selection_type: CSS, typing_mode: FAST, submit: true, "
            "captcha_mode: never}",
            "scroll: {duration: 5, direction: up}",
            "scroll_length: {pixels: 100, direction: down}",
            "try_scroll_length: {length: 100}",
            "lambda_select: {task: t, function: f, link_type: FUNCTION_NAME, captcha_mode: never}",
            "single_select: {target: x, selection_type: CSS}",
            "switch: {selector: x, captcha_mode: never}",
            "handle_alert: {action: dismiss}",
        ],
    )
    def test_known_option_keys_are_clean(self, item):
        r = _validate(BASE.replace("        - visit: https://example.com\n", f"        - {item}\n"))
        assert r.dropped == 0, (r.errors, r.warnings)

    def test_visit_dict_is_an_error_not_a_drop(self):
        r = _validate(
            BASE.replace("        - visit: https://example.com\n", "        - visit: {url: x}\n")
        )
        assert not r.valid and r.dropped == 0, (r.errors, r.warnings)
        assert not any("unknown option" in e for e in r.errors), r.errors

    def test_lambda_select_treatment_keys_are_not_options(self):
        text = BASE.replace(
            "        - visit: https://example.com\n",
            "        - lambda_select: {task: t, function: f, strategy: s, "
            "treatment: {reliability: high}}\n",
        ) + ("strategy_schemas:\n  s: {type: object, properties: {reliability: {type: string}}}\n")
        r = _validate(text)
        assert not any("unknown option" in e for e in r.errors + r.warnings), (r.errors, r.warnings)


class TestCompileIsStrict:
    SPEC = BASE.replace(
        "  - name: bot\n", "  - name: bot\n    configuration:\n      params: {reliability: high}\n"
    )

    def test_compile_refuses_a_drop(self, tmp_path):
        from experiment_designer.lock.build import compile_spec

        r = compile_spec(self.SPEC, base_dir=tmp_path, spec_path="s.yaml", seed=1)
        assert r.lock is None and any("params.reliability" in e for e in r.errors)

    def test_lax_compile_records_it(self, tmp_path):
        from experiment_designer.lock.build import compile_spec

        r = compile_spec(self.SPEC, base_dir=tmp_path, spec_path="s.yaml", seed=1, lax=True)
        assert r.lock is not None and r.lock.provenance.lax is True and r.dropped == 1
        assert any("1 construct(s) would have been dropped" in w for w in r.warnings)

    def test_strict_compile_has_no_lax_count_line(self, tmp_path):
        """In strict mode the drops are already in `errors`; the "would have been
        dropped (lax mode)" line belongs to lax compiles only."""
        from experiment_designer.lock.build import compile_spec

        strict = compile_spec(self.SPEC, base_dir=tmp_path, spec_path="s.yaml", seed=1)
        assert strict.lock is None and strict.dropped == 1
        assert not any("would have been dropped" in w for w in strict.warnings), strict.warnings
        lax = compile_spec(self.SPEC, base_dir=tmp_path, spec_path="s.yaml", seed=1, lax=True)
        assert sum("would have been dropped" in w for w in lax.warnings) == 1, lax.warnings

    def test_unknown_top_level_workflow_item_is_one_drop_not_two(self, tmp_path):
        """The validator (`unknown job type`) and the parser (`unknown workflow
        item`) both see a top-level `- teleport: x`; compile_spec reports it once."""
        from experiment_designer.lock.build import compile_spec

        text = BASE.replace("        - visit: https://example.com\n", "        - teleport: x\n")
        r = compile_spec(text, base_dir=tmp_path, spec_path="s.yaml", seed=1, lax=True)
        assert r.dropped == 1, r.warnings
        assert len([w for w in r.warnings if "teleport" in w]) == 1, r.warnings
        strict = compile_spec(text, base_dir=tmp_path, spec_path="s.yaml", seed=1)
        assert len([e for e in strict.errors if "teleport" in e]) == 1, strict.errors

    def test_cli_compile_lax_prints_the_count(self, tmp_path):
        from click.testing import CliRunner

        from experiment_designer.cli import cli

        (tmp_path / "s.yaml").write_text(self.SPEC)
        strict = CliRunner().invoke(cli, ["compile", str(tmp_path / "s.yaml"), "--seed", "1"])
        assert strict.exit_code == 1 and "params.reliability" in strict.output
        lax = CliRunner().invoke(cli, ["compile", str(tmp_path / "s.yaml"), "--seed", "1", "--lax"])
        assert lax.exit_code == 0 and "would have been dropped" in lax.output

    def test_cli_compile_lax_prints_the_count_exactly_once(self, tmp_path):
        """The dropped-construct count used to be printed twice: once from the merged
        warnings, once from an extra hand-written summary line."""
        from click.testing import CliRunner

        from experiment_designer.cli import cli

        (tmp_path / "s.yaml").write_text(self.SPEC)
        lax = CliRunner().invoke(cli, ["compile", str(tmp_path / "s.yaml"), "--seed", "1", "--lax"])
        assert lax.exit_code == 0, lax.output
        assert lax.output.count("would have been dropped") == 1, lax.output

    def test_mcp_compile_reports_dropped(self):
        import json

        from orchestrator.mcp_server import server as s

        strict = json.loads(s.compile_experiment_spec(self.SPEC, seed=1))
        assert "errors" in strict
        lax = json.loads(s.compile_experiment_spec(self.SPEC, seed=1, lax=True))
        assert lax["dropped"] == 1 and "lock" in lax

    def test_hard_error_plus_two_drop_sources_prints_one_correct_count_line(self, tmp_path):
        """Two validator drops (unknown params key; a bad job type nested in
        repeat/jobs) plus a hard error (missing experiment.name, unconditional in
        both) must take the ``if parsed.errors:`` early return -- and still print
        exactly one count line, for the true total, not one stale line per
        component. The parser sees the nested teleport too, but compile_spec
        suppresses its duplicate report, so the total is 2, not 3."""
        from experiment_designer.lock.build import compile_spec

        text = """
version: "1.0"
experiment: {}
crawlers:
  - name: bot
    configuration:
      params: {reliability: high}
    session:
      start: "+1m"
      workflow:
        - repeat: 1
          jobs:
            - teleport: x
"""
        r = compile_spec(text, base_dir=tmp_path, spec_path="s.yaml", seed=1, lax=True)
        assert r.lock is None
        assert r.dropped == 2
        count_lines = [
            w for w in r.warnings if re.match(r"^\d+ construct\(s\) would have been dropped", w)
        ]
        assert count_lines == ["2 construct(s) would have been dropped (lax mode)"], r.warnings


class TestValidateLaxIsFullyWired:
    """`--lax` on `validate` (CLI and MCP) reached only the validator; the parser
    and converter it also runs were still strict, so a parser- or converter-only
    drop stayed an error under --lax."""

    PARSER_DROP = BASE.replace("        - visit: https://example.com\n", "        - teleport: x\n")
    CONVERTER_DROP = (
        BASE.replace(
            "        - visit: https://example.com\n",
            "        - template: t\n",
        )
        + "templates:\n  t:\n    - type: {text: '${reliability}', selector: '//x'}\n"
    )

    def test_cli_validate_lax_downgrades_a_parser_drop(self, tmp_path):
        from click.testing import CliRunner

        from experiment_designer.cli import cli

        (tmp_path / "s.yaml").write_text(self.PARSER_DROP)
        strict = CliRunner().invoke(cli, ["validate", str(tmp_path / "s.yaml")])
        assert strict.exit_code == 1 and "teleport" in strict.output
        lax = CliRunner().invoke(cli, ["validate", str(tmp_path / "s.yaml"), "--lax"])
        assert lax.exit_code == 0, lax.output
        assert "WARNING" in lax.output and "teleport" in lax.output, lax.output
        # One report for the construct, not one from the validator and one from the parser.
        assert lax.output.count("teleport") == 1, lax.output

    def test_cli_validate_lax_downgrades_a_converter_drop(self, tmp_path):
        from click.testing import CliRunner

        from experiment_designer.cli import cli

        (tmp_path / "s.yaml").write_text(self.CONVERTER_DROP)
        strict = CliRunner().invoke(cli, ["validate", str(tmp_path / "s.yaml")])
        assert strict.exit_code == 1 and "${reliability}" in strict.output
        lax = CliRunner().invoke(cli, ["validate", str(tmp_path / "s.yaml"), "--lax"])
        assert lax.exit_code == 0 and "${reliability}" in lax.output, lax.output

    def test_mcp_validate_lax_downgrades_a_converter_drop(self):
        import json

        from orchestrator.mcp_server import server as s

        strict = json.loads(s.validate_experiment_spec(self.CONVERTER_DROP))
        assert strict["valid"] is False
        assert any("${reliability}" in e for e in strict["errors"]), strict
        lax = json.loads(s.validate_experiment_spec(self.CONVERTER_DROP, lax=True))
        assert lax["valid"] is True, lax
        assert any("${reliability}" in w for w in lax["warnings"]), lax

    def test_mcp_validate_reports_a_parser_drop_once(self):
        import json

        from orchestrator.mcp_server import server as s

        out = json.loads(s.validate_experiment_spec(self.PARSER_DROP, lax=True))
        assert out["valid"] is True, out
        assert len([w for w in out["warnings"] if "teleport" in w]) == 1, out

    def test_cli_validate_json_reports_dropped_count(self, tmp_path):
        import json

        from click.testing import CliRunner

        from experiment_designer.cli import cli

        (tmp_path / "s.yaml").write_text(self.PARSER_DROP)
        out = CliRunner().invoke(
            cli, ["validate", str(tmp_path / "s.yaml"), "--lax", "--format", "json"]
        )
        assert out.exit_code == 0, out.output
        payload = json.loads(out.output)
        assert payload["dropped"] == 1, payload

    def test_mcp_validate_reports_dropped_count(self):
        import json

        from orchestrator.mcp_server import server as s

        out = json.loads(s.validate_experiment_spec(self.PARSER_DROP, lax=True))
        assert out["dropped"] == 1, out

    def test_merge_reports_preserves_order_across_components(self):
        """`_merge` (used by merge_reports) concatenates and de-duplicates without
        reordering -- a `validate` JSON caller sees errors in the order its
        components produced them, not sorted or hashed."""
        from experiment_designer.lock.build import merge_reports

        errors, warnings = merge_reports([["b issue"], ["a issue"]], [[], []], 0, False)
        assert errors == ["b issue", "a issue"]


class TestRepeatPassesWith:
    """`repeat: N` + `template:` ignored `with:` -- the template's ${var} survived
    to design expansion, which replaced only the `{var}` part and handed the
    decision function `$green`. A real study's treatment vehicle."""

    SPEC = """
version: "1.0"
experiment: {name: R}
templates:
  visit_arm:
    - lambda_select:
        task: t
        function: f
        strategy: s
        treatment: {reliability: "${reliability}"}
crawlers:
  - name: bot
    session:
      start: "+1m"
      workflow:
        - repeat: 2
          template: visit_arm
          with: {reliability: green}
"""

    def test_with_is_applied_on_every_repetition(self):
        parsed = SpecParser().parse(self.SPEC)
        assert not parsed.errors, parsed.errors
        jobs = parsed.crawlers[0]["queues"][0]["jobs"]
        assert len(jobs) == 2
        values = [b["value"] for j in jobs for b in j["behaviors"] if b["name"] == "reliability"]
        assert values == ["green", "green"]

    def test_repeat_without_with_leaves_the_placeholder_for_the_scan(self):
        text = self.SPEC.replace("          with: {reliability: green}\n", "")
        parsed = SpecParser().parse(text)
        r = SpecConverter().convert(parsed)
        assert any("${reliability}" in e for e in r.errors), r.errors
