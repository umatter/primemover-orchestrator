"""Queue starts in a lock are offsets from one anchor, so the same lock can be
uploaded on any day and every session keeps its spacing."""

from datetime import datetime, timedelta

import pytest

from experiment_designer.lock.anchor import (
    UPLOAD_ANCHOR,
    AnchorError,
    derive_anchor,
    format_offset,
    parse_offset,
    resolve_anchor,
    resolve_start_at,
)
from experiment_designer.spec.time_utils import API_TIMEZONE


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("+5m", 300), ("+30s", 30), ("+2h", 7200), ("+1d", 86400), ("-10m", -600), ("+0s", 0)],
)
def test_parse_and_format_offset_round_trip(text, seconds):
    assert parse_offset(text) == timedelta(seconds=seconds)
    assert parse_offset(format_offset(timedelta(seconds=seconds))) == timedelta(seconds=seconds)


def test_format_offset_picks_the_largest_exact_unit():
    assert format_offset(timedelta(seconds=300)) == "+5m"
    assert format_offset(timedelta(seconds=90)) == "+90s"
    assert format_offset(timedelta(days=2)) == "+2d"


def test_relative_only_spec_anchors_at_upload():
    anchor, offsets = derive_anchor(["now", "+5m", "+9m"])
    assert anchor == UPLOAD_ANCHOR
    assert offsets == ["+0s", "+5m", "+9m"]


def test_absolute_only_spec_anchors_at_the_earliest():
    anchor, offsets = derive_anchor(["2026-10-01T12:00:00", "2026-10-01T10:00:00"])
    assert anchor == "2026-10-01T10:00:00"
    assert offsets == ["+2h", "+0s"]


def test_absolute_with_zone_is_converted_to_zurich_wall_clock():
    anchor, offsets = derive_anchor(["2026-07-15T03:00:00Z"])
    assert anchor == "2026-07-15T05:00:00"
    assert offsets == ["+0s"]


def test_mixed_relative_and_absolute_is_an_error():
    with pytest.raises(AnchorError, match="mixed"):
        derive_anchor(["+5m", "2026-10-01T10:00:00"])


def test_unparseable_start_is_an_error():
    with pytest.raises(AnchorError, match="tomorrow"):
        derive_anchor(["tomorrow"])


def test_override_wins_and_rebases_offsets():
    anchor, offsets = derive_anchor(["2026-10-01T10:00:00"], override="2026-10-01T09:00:00")
    assert anchor == "2026-10-01T09:00:00"
    assert offsets == ["+1h"]


def test_override_upload_keeps_spacing_of_absolute_starts():
    anchor, offsets = derive_anchor(
        ["2026-10-01T12:00:00", "2026-10-01T10:00:00"], override=UPLOAD_ANCHOR
    )
    assert anchor == UPLOAD_ANCHOR and offsets == ["+2h", "+0s"]


def test_resolve_anchor_upload_is_now_in_the_apis_zone():
    fixed = datetime(2026, 10, 1, 10, 0, tzinfo=API_TIMEZONE)
    assert resolve_anchor(UPLOAD_ANCHOR, now=fixed) == fixed


def test_resolve_anchor_fixed_is_naive_wall_clock_in_zurich():
    dt = resolve_anchor("2026-10-01T10:00:00")
    assert dt.tzinfo is not None and dt.utcoffset() == timedelta(hours=2)


def test_resolve_start_at_across_dst_keeps_wall_clock_arithmetic():
    # 2026-10-25 02:00 CEST -> 03:00 CET is the autumn change; a +1d offset must
    # land on the next day's same wall-clock hour, not 25 hours later.
    anchor = resolve_anchor("2026-10-24T10:00:00")
    assert resolve_start_at(anchor, "+1d") == "2026-10-25T10:00:00+01:00"
