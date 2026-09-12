"""§4 of the lock design: whatever would be dropped, ignored or left unresolved is
an error; --lax turns it into a warning and a count. One row per rule."""

from __future__ import annotations

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
