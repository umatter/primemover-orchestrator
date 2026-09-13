"""SpecConverter options the lock compiler relies on: an explicit seed that beats
design.seed, and a way to get the queue start strings back *unresolved* so the
lock can turn them into offsets from an anchor."""

from pmspec.spec.converter import SpecConverter
from pmspec.spec.parser import SpecParser

SPEC = """
version: "1.0"
experiment:
  name: Seeded
design:
  seed: 1
  factors:
    arm:
      levels: [control, treated]
      maps_to:
        params.pi: {control: 0, treated: 1}
  parameters:
    alpha:
      distribution: uniform
      min: 0
      max: 100
  replications: 1
crawlers:
  - from_design: true
    name: "bot_{arm}"
    session:
      start: "+5m"
      workflow:
        - visit: https://example.com
        - wait: 2s
"""


def _convert(**kwargs):
    parsed = SpecParser(seed=kwargs.pop("parser_seed", None)).parse(SPEC)
    assert not parsed.errors, parsed.errors
    result = SpecConverter(**kwargs).convert(parsed)
    assert not result.errors, result.errors
    return result


def test_explicit_seed_overrides_design_seed():
    by_design = _convert()
    explicit = _convert(seed=99)
    again = _convert(seed=99)
    draw = lambda r: [c["configuration"]["params"].get("alpha") for c in r.json_data]  # noqa: E731
    assert draw(explicit) == draw(again)
    assert draw(explicit) != draw(by_design)


def test_resolve_times_false_keeps_the_raw_start():
    result = _convert(resolve_times=False)
    starts = {q["start_at"] for c in result.json_data for q in c["queues"]}
    assert starts == {"+5m"}


def test_resolve_times_default_still_resolves():
    result = _convert()
    start = result.json_data[0]["queues"][0]["start_at"]
    assert start.startswith("20") and "+" in start[10:]  # ISO with an offset


def test_missing_start_is_now_when_unresolved():
    spec = SPEC.replace('      start: "+5m"\n', "")
    parsed = SpecParser().parse(spec)
    result = SpecConverter(resolve_times=False).convert(parsed)
    assert result.json_data[0]["queues"][0]["start_at"] == "now"
