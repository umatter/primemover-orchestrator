"""Format 2: every distinct queue and agent body once, crawlers by reference."""

import copy

import pytest

from experiment_designer.lock.io import dump_lock, load_lock, validate_lock_dict
from experiment_designer.lock.model import LOCK_FORMAT_VERSION, Lock
from experiment_designer.lock.parts import PartCollision, factor_crawlers, materialise, part_key

from .test_lock_io import make_lock


def _fleet(n: int) -> Lock:
    base = make_lock()
    crawlers = []
    for i in range(n):
        c = copy.deepcopy(base.crawlers[0])
        c["name"] = f"bot_{i:03d}"
        c["agent"]["name"] = f"Agent {i:03d}"
        # params differ per crawler but live in `configuration`, so queues stay
        # identical -- one queue part, not two arms' worth.
        c["configuration"]["params"]["pi"] = i % 2
        crawlers.append(c)
    return Lock(
        provenance=base.provenance,
        experiment=base.experiment,
        schedule=base.schedule,
        crawlers=crawlers,
    )


def test_part_key_is_content_addressed_and_order_independent():
    a = {"x": 1, "y": [1, 2]}
    b = {"y": [1, 2], "x": 1}
    assert part_key(a, "q") == part_key(b, "q")
    assert part_key(a, "q").startswith("q:") and len(part_key(a, "q")) == 2 + 16
    assert part_key(a, "q") != part_key({"x": 2, "y": [1, 2]}, "q")


def test_factor_and_materialise_round_trip():
    lock = _fleet(5)
    parts, refs = factor_crawlers(lock.crawlers)
    assert len(parts["queues"]) == 1 and len(parts["agents"]) == 1
    assert all(
        "ref" in q and "start_offset" in q and "jobs" not in q for c in refs for q in c["queues"]
    )
    assert all(set(c["agent"]) == {"ref", "name"} for c in refs)
    assert materialise(parts, refs) == lock.crawlers


def test_dump_writes_format_2_and_load_materialises():
    lock = _fleet(3)
    text = dump_lock(lock)
    assert text.startswith(f"pmspec_lock: {LOCK_FORMAT_VERSION}\n") and LOCK_FORMAT_VERSION == 2
    assert "\nparts:\n" in text and text.index("parts:") < text.index("crawlers:")
    assert load_lock(text) == lock


def test_dump_is_deterministic():
    assert dump_lock(_fleet(4)) == dump_lock(_fleet(4))


def test_size_collapses_for_a_fleet():
    # configuration/proxy stay inline per crawler by design (genuinely unique
    # per real bot), so the per-crawler growth here is configuration + proxy +
    # two small refs (~600 bytes measured against this fixture's config/proxy
    # bodies), not configuration + proxy + a full queue body (jobs/behaviors)
    # + a full agent body (which would run well over 800/crawler). 700 leaves
    # headroom above the measured ~603 while still catching a regression to
    # per-crawler full-body duplication.
    small = len(dump_lock(_fleet(1)))
    big = len(dump_lock(_fleet(200)))
    assert big < small + 200 * 700, (small, big)


def test_format_1_still_loads(tmp_path):
    lock = make_lock()
    d = lock.to_dict()
    # hand-build a format-1 document: crawlers inline, no parts
    d1 = {k: v for k, v in d.items() if k != "parts"}
    d1["pmspec_lock"] = 1
    d1["crawlers"] = lock.crawlers
    import yaml

    text = yaml.safe_dump(d1, sort_keys=False, allow_unicode=True)
    assert load_lock(text) == lock


def test_unknown_format_is_refused():
    text = dump_lock(make_lock()).replace("pmspec_lock: 2", "pmspec_lock: 3", 1)
    errors = validate_lock_dict(__import__("yaml").safe_load(text))
    assert any("pmspec_lock" in e for e in errors)


def test_dangling_ref_is_an_error():
    parts, refs = factor_crawlers(_fleet(1).crawlers)
    refs[0]["queues"][0]["ref"] = "q:0000000000000000"
    with pytest.raises(KeyError, match="q:0000000000000000"):
        materialise(parts, refs)


def test_collision_is_an_error(monkeypatch):
    from experiment_designer.lock import parts as parts_mod

    monkeypatch.setattr(parts_mod, "part_key", lambda obj, prefix: f"{prefix}:same")
    lock = _fleet(2)
    lock.crawlers[1]["queues"][0]["name"] = "different"
    with pytest.raises(PartCollision):
        factor_crawlers(lock.crawlers)
