"""`{prefix.name}` / `{prefix.name:fmt}` substitution, shared by `from_base:`
(a runs spec) and `assign:` (per-crawler pool rows)."""

import pytest

from pmspec.spec.substitute import substitute_named


def test_a_known_column_is_replaced():
    assert substitute_named("bot {base.name}", "base", {"name": "b01"}) == "bot b01"


def test_an_unknown_column_is_left_untouched():
    assert substitute_named("{base.ghost}", "base", {"name": "b01"}) == "{base.ghost}"


def test_another_prefix_is_left_untouched():
    assert substitute_named("{other.name}", "base", {"name": "b01"}) == "{other.name}"


def test_a_bare_placeholder_is_left_untouched():
    # `{arm}` is a design variable, a different namespace.
    assert substitute_named("{arm}", "base", {"arm": "x"}) == "{arm}"


def test_a_format_spec_is_applied():
    assert substitute_named("{base.p:.2f}", "base", {"p": 0.3141}) == "0.31"


def test_every_occurrence_is_replaced():
    out = substitute_named("{base.name}-{base.name}", "base", {"name": "b"})
    assert out == "b-b"


def test_a_format_error_names_the_column():
    with pytest.raises(ValueError, match="base.p"):
        substitute_named("{base.p:.2f}", "base", {"p": "not a number"})


def test_a_hyphen_is_a_legal_column_character():
    assert substitute_named("{base.a-b}", "base", {"a-b": "v"}) == "v"
