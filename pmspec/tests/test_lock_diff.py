"""diff_locks compares two locks structurally, at a fixed reference anchor, and
never diffs a crawler that exists on only one side."""

from pmspec.lock.diff import diff_locks
from pmspec.lock.io import dump_lock

from .test_lock_io import make_lock


def test_identical_locks():
    text = dump_lock(make_lock())
    d = diff_locks(text, text)
    assert d.identical and "identical" in d.render()


def test_param_change_is_a_difference():
    a = make_lock()
    b = make_lock()
    b.crawlers[0]["configuration"]["params"]["pi"] = 0
    d = diff_locks(dump_lock(a), dump_lock(b))
    assert not d.identical
    assert any(
        x.path == "crawlers.bot_01.params.pi" and x.expected == 1.0 and x.actual == 0.0
        for x in d.differences
    )


def test_anchor_change_is_one_line_not_per_queue():
    a = make_lock()
    b = make_lock(schedule={"anchor": "2026-10-01T10:00:00"})
    d = diff_locks(dump_lock(a), dump_lock(b))
    assert d.anchor_a == "upload" and d.anchor_b == "2026-10-01T10:00:00"
    assert not any("start_at" in x.path for x in d.differences)
    assert not d.identical and "anchor" in d.render()


def test_job_behaviour_change_is_seen_by_default():
    a = make_lock()
    b = make_lock()
    b.crawlers[0]["queues"][0]["jobs"][0]["behaviors"][0]["value"] = "https://other"
    d = diff_locks(dump_lock(a), dump_lock(b))
    assert any(x.path.endswith("jobs[0].behaviors.url") for x in d.differences)


def test_crawlers_only_on_one_side_are_listed_not_diffed():
    a = make_lock()
    b = make_lock()
    extra = dict(a.crawlers[0])
    extra = __import__("copy").deepcopy(extra)
    extra["name"] = "bot_02"
    b.crawlers.append(extra)
    d = diff_locks(dump_lock(a), dump_lock(b))
    assert d.only_in_b == ["bot_02"] and d.only_in_a == [] and not d.identical
    assert not any("bot_02" in x.path for x in d.differences)


def test_provenance_side_by_side_in_render():
    a = make_lock()
    b = make_lock()
    b.provenance.git_commit = "deadbee"
    text = diff_locks(dump_lock(a), dump_lock(b)).render()
    assert "a2024c9" in text and "deadbee" in text
    # The provenance lines above differ; the verdict line must say what is identical.
    assert "structure identical" in text


def test_pool_sha_change_is_named_and_is_not_structural():
    a = make_lock()
    a.provenance.pools = {"outlets": "a" * 64}
    b = make_lock()
    b.provenance.pools = {"outlets": "b" * 64}
    d = diff_locks(dump_lock(a), dump_lock(b))
    assert d.pools_changed == ["outlets"]
    assert d.identical  # provenance is not structure
    text = d.render()
    assert "pools: outlets" in text
    assert ("a" * 12) in text and ("b" * 12) in text


def test_pool_only_on_one_side_is_named():
    a = make_lock()
    a.provenance.pools = {}
    b = make_lock()
    b.provenance.pools = {"new_pool": "c" * 64}
    d = diff_locks(dump_lock(a), dump_lock(b))
    assert d.pools_changed == ["new_pool"]
    assert "pools: new_pool" in d.render()
    assert "only in b" in d.render()


# --- runs locks ------------------------------------------------------------


def test_two_identical_runs_locks_are_identical():
    from .test_lock_runs import make_runs_lock

    text = dump_lock(make_runs_lock())
    d = diff_locks(text, text)
    assert d.identical and "identical" in d.render()


def test_a_changed_runs_queue_is_a_difference():
    from .test_lock_runs import make_runs_lock

    a = make_runs_lock()
    b = make_runs_lock()
    b.crawlers[0]["queues"][0]["start_offset"] = "+3h"
    d = diff_locks(dump_lock(a), dump_lock(b))
    assert not d.identical
    assert any("queues" in x.path for x in d.differences)


def test_a_changed_runs_job_changes_the_queue_part():
    from .test_lock_runs import make_runs_lock

    a = make_runs_lock()
    b = make_runs_lock()
    b.crawlers[0]["queues"][0]["jobs"][0]["behaviors"][0]["value"] = "https://other"
    d = diff_locks(dump_lock(a), dump_lock(b))
    assert not d.identical and any("queues" in x.path for x in d.differences)


def test_changed_updates_are_a_difference():
    from .test_lock_runs import make_runs_lock

    a = make_runs_lock()
    b = make_runs_lock()
    b.crawlers[0]["updates"] = {"preferences": {"ideology": "0.99"}}
    d = diff_locks(dump_lock(a), dump_lock(b))
    assert any(x.path.endswith("updates.preferences.ideology") for x in d.differences)


def test_a_runs_crawler_only_on_one_side_is_listed():
    import copy

    from .test_lock_runs import make_runs_lock

    a = make_runs_lock()
    b = make_runs_lock()
    extra = copy.deepcopy(b.crawlers[0])
    extra["name"] = "bot_02"
    b.crawlers.append(extra)
    d = diff_locks(dump_lock(a), dump_lock(b))
    assert d.only_in_b == ["bot_02"] and not d.identical


def test_a_runs_lock_against_a_fleet_lock_is_refused():
    import pytest

    from pmspec.lock.model import LockError

    from .test_lock_runs import make_runs_lock

    with pytest.raises(LockError) as exc:
        diff_locks(dump_lock(make_runs_lock()), dump_lock(make_lock()))
    assert exc.value.errors == ["cannot diff a runs lock against a fleet lock"]
