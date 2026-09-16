"""Seeded timing: `wait: {min, max}` ranges and `start_at: {from, jitter}`.

Both are drawn once, in the converter, from the lock's one seed -- so the lock
records a concrete number and everything downstream (verify, audit, the runner)
sees an ordinary wait and an ordinary start.
"""

from pathlib import Path

import pytest
import yaml

from pmspec.lock.build import compile_spec
from pmspec.lock.io import dump_lock
from pmspec.spec.converter import SpecConverter
from pmspec.spec.parser import SpecParser
from pmspec.spec.timing import WAIT_TOKEN_RE, parse_duration, wait_token
from pmspec.spec.validator import SpecValidator

WAIT_SPEC = """
version: "1.0"
experiment: {name: T}
design:
  seed: 7
  factors:
    arm:
      levels: [a, b, c]
  replications: 1
crawlers:
  - name: bot_{arm}
    from_design: true
    session:
      start: "+1m"
      workflow:
        - wait: {min: 30s, max: 60s}
"""

JITTER_SPEC = """
version: "1.0"
experiment: {name: J}
design:
  seed: 7
  factors:
    arm:
      levels: [a, b, c]
  replications: 1
crawlers:
  - name: bot_{arm}
    from_design: true
    session:
      start_at: {from: "+1h", jitter: "6h"}
      workflow:
        - wait: 5s
"""


def _compile(tmp_path: Path, text: str, seed: int | None = None):
    return compile_spec(text, base_dir=tmp_path, spec_path="s.yaml", seed=seed)


def _waits(lock) -> list[int]:
    return [
        b["value"]
        for c in lock.crawlers
        for q in c["queues"]
        for j in q["jobs"]
        for b in j["behaviors"]
        if b["name"] == "seconds"
    ]


def _offsets(lock) -> list[str]:
    return [q["start_offset"] for c in lock.crawlers for q in c["queues"]]


# --- the token ---------------------------------------------------------------


def test_wait_token_round_trip():
    token = wait_token(30, 60)
    match = WAIT_TOKEN_RE.fullmatch(token)
    assert match and match.groups() == ("30", "60")


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("5m", 300), ("6h", 21600), ("1d", 86400), (45, 45), ("0s", 0)],
)
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["-5m", "soon", "", None, {"a": 1}])
def test_parse_duration_refuses_what_is_not_a_duration(text):
    assert parse_duration(text) is None


# --- wait ranges -------------------------------------------------------------


def test_wait_range_draws_one_integer_per_job(tmp_path):
    result = _compile(tmp_path, WAIT_SPEC)
    assert result.lock is not None, result.errors
    waits = _waits(result.lock)
    assert len(waits) == 3
    assert all(isinstance(w, int) and 30 <= w <= 60 for w in waits), waits
    assert len(set(waits)) > 1, waits
    assert "%%pmspec-wait" not in dump_lock(result.lock)


def test_wait_range_is_pinned_to_the_seed(tmp_path):
    assert _waits(_compile(tmp_path, WAIT_SPEC).lock) == [42, 39, 52]


def test_wait_range_is_reproducible_and_seed_dependent(tmp_path):
    same = _waits(_compile(tmp_path, WAIT_SPEC).lock)
    assert same == _waits(_compile(tmp_path, WAIT_SPEC).lock)
    assert same != _waits(_compile(tmp_path, WAIT_SPEC, seed=8).lock)


def test_min_above_max_is_an_error(tmp_path):
    text = WAIT_SPEC.replace("{min: 30s, max: 60s}", "{min: 60s, max: 30s}")
    result = _compile(tmp_path, text)
    assert result.lock is None
    assert any("wait: min (60s) exceeds max (30s)" in e for e in result.errors), result.errors


def test_wait_range_needs_both_bounds(tmp_path):
    text = WAIT_SPEC.replace("{min: 30s, max: 60s}", "{min: 5}")
    result = _compile(tmp_path, text)
    assert result.lock is None
    assert any("needs both min and max" in e for e in result.errors), result.errors


def test_equal_bounds_are_fine(tmp_path):
    result = _compile(tmp_path, WAIT_SPEC.replace("{min: 30s, max: 60s}", "{min: 45s, max: 45s}"))
    assert result.lock is not None, result.errors
    assert _waits(result.lock) == [45, 45, 45]


def test_a_surviving_wait_token_is_a_drop(tmp_path):
    # Unseeded -- `pmspec generate`, or any caller that built a converter
    # without one. `compile_spec` refuses an unseeded lock before it gets
    # here, so this is the converter's own guarantee: the draw cannot be made,
    # the token stays, and the strict scan refuses to ship it to a bot.
    text = WAIT_SPEC.replace("  seed: 7\n", "")
    parsed = SpecParser(base_dir=tmp_path).parse(text)
    assert not parsed.errors, parsed.errors
    result = SpecConverter().convert(parsed)
    assert any("wait range needs a seed" in e for e in result.errors), result.errors
    assert any("unresolved wait range" in e for e in result.errors), result.errors
    assert result.dropped >= 1


def test_plain_wait_is_unchanged(tmp_path):
    result = _compile(tmp_path, WAIT_SPEC.replace("{min: 30s, max: 60s}", "45s"))
    assert result.lock is not None, result.errors
    assert _waits(result.lock) == [45, 45, 45]


# --- start jitter ------------------------------------------------------------


def test_relative_jitter_draws_an_offset_per_session(tmp_path):
    result = _compile(tmp_path, JITTER_SPEC)
    assert result.lock is not None, result.errors
    assert result.lock.schedule["anchor"] == "upload"
    offsets = _offsets(result.lock)
    assert len(offsets) == 3
    for text in offsets:
        assert text.startswith("+")
    seconds = [_seconds(t) for t in offsets]
    assert all(3600 <= s <= 3600 + 21600 for s in seconds), offsets
    assert len(set(seconds)) > 1, offsets


def _seconds(offset: str) -> int:
    from pmspec.lock.anchor import parse_offset

    return int(parse_offset(offset).total_seconds())


def test_relative_jitter_is_pinned_to_the_seed(tmp_path):
    assert _offsets(_compile(tmp_path, JITTER_SPEC).lock) == [
        "+19170s",
        "+9467s",
        "+5757s",
    ]


def test_relative_jitter_is_reproducible_and_seed_dependent(tmp_path):
    same = _offsets(_compile(tmp_path, JITTER_SPEC).lock)
    assert same == _offsets(_compile(tmp_path, JITTER_SPEC).lock)
    assert same != _offsets(_compile(tmp_path, JITTER_SPEC, seed=8).lock)


def test_now_jitters_from_zero(tmp_path):
    result = _compile(tmp_path, JITTER_SPEC.replace('from: "+1h"', 'from: "now"'))
    assert result.lock is not None, result.errors
    assert all(0 <= _seconds(o) <= 21600 for o in _offsets(result.lock))


def test_absolute_jitter_anchors_at_the_earliest_resolved_start(tmp_path):
    text = JITTER_SPEC.replace('from: "+1h"', 'from: "2026-10-01T10:00:00"')
    result = _compile(tmp_path, text)
    assert result.lock is not None, result.errors
    anchor = result.lock.schedule["anchor"]
    assert anchor.startswith("2026-10-01T1")
    offsets = _offsets(result.lock)
    assert all(_seconds(o) >= 0 for o in offsets), offsets
    assert min(_seconds(o) for o in offsets) == 0
    assert max(_seconds(o) for o in offsets) <= 21600


def test_zero_jitter_is_a_no_op(tmp_path):
    result = _compile(tmp_path, JITTER_SPEC.replace('jitter: "6h"', 'jitter: "0s"'))
    assert result.lock is not None, result.errors
    assert _offsets(result.lock) == ["+1h", "+1h", "+1h"]


def test_additional_sessions_jitter_uses_its_own_session_index(tmp_path):
    text = JITTER_SPEC.replace(
        "    session:\n",
        "    additional_sessions:\n"
        '      - start_at: {from: "+1h", jitter: "6h"}\n'
        "        workflow: [{wait: 5s}]\n"
        "    session:\n",
    )
    result = _compile(tmp_path, text)
    assert result.lock is not None, result.errors
    offsets = _offsets(result.lock)
    assert len(offsets) == 6
    # Session 0 of each crawler keeps the values the single-session spec drew;
    # session 1 draws from its own (i, 1) stream, so the pair differs.
    first = offsets[0::2]
    second = offsets[1::2]
    assert first == ["+19170s", "+9467s", "+5757s"]
    assert first != second


def test_generate_refuses_jitter(tmp_path):
    parsed = SpecParser(base_dir=tmp_path, seed=8).parse(JITTER_SPEC)
    assert not parsed.errors, parsed.errors
    result = SpecConverter(seed=7, resolve_times=True).convert(parsed)
    assert any(
        "start_at jitter needs a seed: use pmspec compile" in e for e in result.errors
    ), result.errors


# --- validation --------------------------------------------------------------


def test_validator_accepts_both_forms():
    for text in (WAIT_SPEC, JITTER_SPEC):
        result = SpecValidator().validate(yaml.safe_load(text))
        assert result.valid, result.errors
        assert result.dropped == 0, result.errors


def test_validator_refuses_a_bad_wait_range():
    bad = yaml.safe_load(WAIT_SPEC.replace("{min: 30s, max: 60s}", "{min: 60s, max: 30s}"))
    assert any("exceeds max" in e for e in SpecValidator().validate(bad).errors)
    missing = yaml.safe_load(WAIT_SPEC.replace("{min: 30s, max: 60s}", "{max: 30s}"))
    assert any("needs both min and max" in e for e in SpecValidator().validate(missing).errors)
    unparsable = yaml.safe_load(WAIT_SPEC.replace("min: 30s", "min: soon"))
    assert any("duration" in e for e in SpecValidator().validate(unparsable).errors)


def test_validator_reports_an_unknown_wait_option_as_a_drop():
    text = WAIT_SPEC.replace("{min: 30s, max: 60s}", "{min: 30s, max: 60s, mean: 40s}")
    result = SpecValidator().validate(yaml.safe_load(text))
    assert result.dropped >= 1
    assert any("mean" in e for e in result.errors), result.errors


def test_validator_refuses_a_bad_start_at():
    missing = yaml.safe_load(JITTER_SPEC.replace('{from: "+1h", jitter: "6h"}', '{from: "+1h"}'))
    assert any(
        "start_at: needs both from and jitter" in e
        for e in SpecValidator().validate(missing).errors
    )
    bad = yaml.safe_load(JITTER_SPEC.replace('jitter: "6h"', 'jitter: "-6h"'))
    assert any("jitter" in e for e in SpecValidator().validate(bad).errors)
    unknown = yaml.safe_load(
        JITTER_SPEC.replace('{from: "+1h", jitter: "6h"}', '{from: "+1h", jitter: "6h", spread: 2}')
    )
    result = SpecValidator().validate(unknown)
    assert any("spread" in e for e in result.errors), result.errors


def test_runs_spec_sessions_accept_both_forms(tmp_path):
    runs = """
version: "1.0"
runs_for: fleet.lock.yaml
seed: 7
crawlers:
  - name: bot_a
    session:
      start_at: {from: "+1h", jitter: "6h"}
      workflow:
        - wait: {min: 30s, max: 60s}
"""
    result = SpecValidator(runs=True).validate(yaml.safe_load(runs))
    assert result.valid, result.errors
    assert result.dropped == 0, result.errors


# --- an unresolvable jitter is an error, never a traceback -------------------


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ('from: "+1h", jitter: "soon"', "is not a duration"),
        ('from: "+1h", jitter: "-5s"', "is not a duration"),
        ('jitter: "6h"', "needs both from and jitter"),
        ('from: "tomorrow", jitter: "6h"', "cannot parse start time"),
    ],
)
def test_an_unresolvable_jitter_is_reported_not_raised(tmp_path, bad, message):
    # `resolve_start_jitter` leaves a *string* behind on every error path and
    # compile_spec returns on the errors before deriving an anchor, so neither
    # `derive_anchor` nor `is_relative_time` is ever handed a dict. Both halves
    # matter: either one alone turns a bad jitter into an AttributeError
    # traceback out of `pmspec compile`.
    text = JITTER_SPEC.replace('from: "+1h", jitter: "6h"', bad)
    result = _compile(tmp_path, text)
    assert result.lock is None
    assert any(message in e for e in result.errors), result.errors


def test_the_cli_reports_a_bad_jitter_instead_of_crashing(tmp_path):
    from click.testing import CliRunner

    from pmspec.cli import cli

    spec = tmp_path / "study.yaml"
    spec.write_text(JITTER_SPEC.replace('jitter: "6h"', 'jitter: "soon"'))
    out = CliRunner().invoke(cli, ["compile", str(spec), "--seed", "7"])
    assert out.exit_code == 1, out.output
    assert out.exception is None or isinstance(out.exception, SystemExit), out.exception
    assert "is not a duration" in out.output, out.output


def test_the_unseeded_jitter_error_is_reported_once(tmp_path):
    # Three crawlers, one message -- not one per session.
    parsed = SpecParser(base_dir=tmp_path).parse(JITTER_SPEC.replace("  seed: 7\n", ""))
    result = SpecConverter(resolve_times=False).convert(parsed)
    assert [e for e in result.errors if "start jitter needs a seed" in e] == [
        "start jitter needs a seed: use pmspec compile, or set design.seed"
    ], result.errors


def test_the_generate_jitter_refusal_is_reported_once(tmp_path):
    parsed = SpecParser(base_dir=tmp_path, seed=8).parse(JITTER_SPEC)
    result = SpecConverter(seed=7, resolve_times=True).convert(parsed)
    assert [e for e in result.errors if "start_at jitter" in e] == [
        "start_at jitter needs a seed: use pmspec compile"
    ], result.errors
