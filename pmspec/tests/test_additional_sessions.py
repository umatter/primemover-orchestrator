"""`additional_sessions` was in the shipped example and in a real study's spec, and
the parser ignored it -- the study's measurement sessions were silently dropped."""

import yaml

from pmspec.spec.converter import SpecConverter
from pmspec.spec.parser import SpecParser
from pmspec.spec.validator import SpecValidator

SPEC = """
version: "1.0"
experiment:
  name: Extra
crawlers:
  - name: bot
    session:
      name: Diet
      start: "+1m"
      workflow:
        - visit: https://a
    additional_sessions:
      - name: Measure
        start: "+2h"
        workflow:
          - visit: https://b
          - wait: 1s
"""


def test_additional_sessions_become_queues():
    parsed = SpecParser().parse(SPEC)
    assert not parsed.errors, parsed.errors
    queues = parsed.crawlers[0]["queues"]
    assert [q["name"] for q in queues] == ["Diet", "Measure"]
    assert len(queues[1]["jobs"]) == 2
    result = SpecConverter().convert(parsed)
    assert result.total_jobs == 3


def test_additional_sessions_without_session_is_an_error():
    text = SPEC.replace(
        '    session:\n      name: Diet\n      start: "+1m"\n      workflow:\n'
        "        - visit: https://a\n",
        "",
    )
    parsed = SpecParser().parse(text)
    assert any("additional_sessions needs a session" in e for e in parsed.errors), parsed.errors


def test_queues_and_session_together_is_an_error():
    text = SPEC + "    queues:\n      - name: q\n        start: now\n        jobs: []\n"
    parsed = SpecParser().parse(text)
    assert any("queues" in e and "session" in e for e in parsed.errors), parsed.errors


def test_additional_session_without_start_is_a_strict_validation_error():
    """The validator whitelists additional_sessions but, until now, never ran
    _validate_session over its entries -- a missing start was silent."""
    spec = yaml.safe_load(SPEC)
    del spec["crawlers"][0]["additional_sessions"][0]["start"]
    result = SpecValidator().validate(spec)
    assert not result.valid
    assert any("crawler[0].additional_sessions[0]" in e for e in result.errors), result.errors


def test_additional_session_name_gets_design_substitution():
    """The design expander walks the whole crawler dict, so an additional
    session's name template should already pick up factor substitution."""
    spec_text = """
version: "1.0"
experiment:
  name: Extra
design:
  factors:
    arm:
      levels: [control, treated]
      maps_to:
        configuration.params.pi: {control: 0, treated: 1}
  replications: 1
crawlers:
  - from_design: true
    name: "bot_{arm}"
    session:
      name: Diet
      start: "+1m"
      workflow:
        - visit: https://a
    additional_sessions:
      - name: "M {arm}"
        start: "+2h"
        workflow:
          - visit: https://b
"""
    parsed = SpecParser().parse(spec_text)
    assert not parsed.errors, parsed.errors
    result = SpecConverter().convert(parsed)
    assert not result.errors, result.errors
    names = {c["name"]: [q["name"] for q in c["queues"]] for c in result.json_data}
    assert names["bot_control"] == ["Diet", "M control"]
    assert names["bot_treated"] == ["Diet", "M treated"]
