"""The runs lock: a second lock *kind* that appends queues to an existing
fleet's crawlers, matched by name.

Two properties carry the whole feature. A runs lock must survive
dump/load byte-identically like any other lock, and a *fleet* lock's bytes must
not move by a single character -- the fleet lock is the randomization record
every study already committed, and `pmspec verify` binds it by sha256.
"""

import copy
from typing import Any

import pytest

from pmspec.lock.io import dump_lock, load_lock, validate_lock_dict
from pmspec.lock.model import LOCK_FORMAT_VERSION, Lock, LockError, Provenance

from .test_lock_io import make_lock


def make_runs_lock(**overrides) -> Lock:
    crawler = {
        "name": "bot_01",
        "queues": [
            {
                "name": "2026-10-01",
                "description": "d",
                "start_offset": "+2h",
                "timezone": "Europe/Zurich",
                "timezone_utc_offset": "+02:00",
                "active": True,
                "jobs": [
                    {
                        "name": "visit",
                        "type": "visitjob",
                        "description": "d",
                        "active": True,
                        "behaviors": [
                            {"name": "url", "value": "https://x", "description": "", "active": True}
                        ],
                    }
                ],
            }
        ],
        "updates": {"preferences": {"ideology": "0.31"}},
    }
    fields: dict[str, Any] = {
        "provenance": Provenance(
            spec_path="design/day.yaml",
            spec_sha256="ab" * 32,
            generator=None,
            git_commit="a2024c9",
            pmspec_version="0.1.0",
            seed=42,
            base_lock_sha256="cd" * 32,
            base_spec_path="design/fleet.yaml",
        ),
        "experiment": {"name": "Study", "description": "", "runner_user_id": None, "contact": None},
        "schedule": {"anchor": "upload"},
        "crawlers": [crawler],
        "kind": "runs",
    }
    fields.update(overrides)
    return Lock(**fields)


# --- the fleet lock's bytes do not move ------------------------------------


def test_fleet_lock_bytes_are_unchanged_by_the_runs_kind():
    # Pinned before the runs kind existed: a fleet lock's canonical bytes are
    # what `verify` binds by sha256, so they may never drift.
    from pmspec.lock.io import sha256_text

    text = dump_lock(make_lock())
    assert sha256_text(text) == "00b785b37bc7876dccb033ae156a81813b0bf39e1ac319f52a7d2d5707dc39ef"
    assert "kind:" not in text
    assert "base_lock_sha256" not in text and "base_spec_path" not in text


def test_fleet_lock_is_the_default_kind_and_round_trips():
    lock = make_lock()
    assert lock.kind == "fleet"
    assert not lock.is_runs
    assert load_lock(dump_lock(lock)) == lock


def test_kind_absent_means_fleet():
    d = make_lock().to_dict()
    assert "kind" not in d
    assert Lock.from_dict(d).kind == "fleet"


# --- the runs lock ---------------------------------------------------------


def test_runs_lock_round_trips_byte_identical():
    lock = make_runs_lock()
    text = dump_lock(lock)
    assert dump_lock(load_lock(text)) == text
    assert load_lock(text) == lock


def test_runs_lock_key_order_puts_kind_right_after_the_format_marker():
    text = dump_lock(make_runs_lock())
    keys = [line.split(":")[0] for line in text.splitlines() if line and not line.startswith(" ")]
    assert keys == [
        "pmspec_lock",
        "kind",
        "provenance",
        "experiment",
        "schedule",
        "parts",
        "crawlers",
    ]
    assert text.startswith(f"pmspec_lock: {LOCK_FORMAT_VERSION}\nkind: runs\n")


def test_runs_lock_carries_the_base_binding_and_no_agents():
    d = make_runs_lock().to_dict()
    assert d["provenance"]["base_lock_sha256"] == "cd" * 32
    assert d["provenance"]["base_spec_path"] == "design/fleet.yaml"
    assert d["parts"]["agents"] == {}
    assert d["crawlers"][0] == {
        "name": "bot_01",
        "queues": [{"ref": d["crawlers"][0]["queues"][0]["ref"], "start_offset": "+2h"}],
        "updates": {"preferences": {"ideology": "0.31"}},
    }
    assert validate_lock_dict(d) == []


def test_runs_lock_without_updates_omits_the_key():
    lock = make_runs_lock()
    del lock.crawlers[0]["updates"]
    d = lock.to_dict()
    assert "updates" not in d["crawlers"][0]
    assert validate_lock_dict(d) == []
    assert load_lock(dump_lock(lock)) == lock


# --- what a runs lock may not carry ----------------------------------------


@pytest.mark.parametrize(
    "key,value",
    [
        ("configuration", {"name": "c"}),
        ("agent", {"ref": "a:" + "0" * 16, "name": "a"}),
        ("proxy", {"name": "p"}),
    ],
)
def test_runs_lock_refuses_a_fleet_only_crawler_key(key, value):
    d = make_runs_lock().to_dict()
    d["crawlers"][0][key] = value
    with pytest.raises(LockError) as exc:
        Lock.from_dict(d)
    assert exc.value.errors == [
        f"runs lock crawler 'bot_01' carries {key}; a runs lock adds queues only"
    ]


def test_runs_lock_refuses_updates_outside_preferences():
    d = make_runs_lock().to_dict()
    d["crawlers"][0]["updates"] = {"params": {"pi": "1"}}
    with pytest.raises(LockError) as exc:
        Lock.from_dict(d)
    assert exc.value.errors == [
        "runs lock crawler 'bot_01': updates may only contain preferences (got params)"
    ]


def test_a_fleet_lock_may_still_carry_configuration_and_agent():
    # The refusals above are scoped to the runs kind, not to crawlers at large.
    d = make_lock().to_dict()
    assert Lock.from_dict(d).crawlers[0]["configuration"]["name"] == "c"


# --- the schema ------------------------------------------------------------


def test_schema_rejects_a_runs_crawler_carrying_configuration():
    d = make_runs_lock().to_dict()
    d["crawlers"][0]["configuration"] = {"name": "c"}
    errors = validate_lock_dict(d)
    assert any("configuration" in e for e in errors)


def test_schema_rejects_a_runs_crawler_without_queues():
    d = make_runs_lock().to_dict()
    del d["crawlers"][0]["queues"]
    assert any("queues" in e for e in validate_lock_dict(d))


def test_schema_rejects_a_non_string_update_value():
    d = make_runs_lock().to_dict()
    d["crawlers"][0]["updates"]["preferences"]["ideology"] = 0.31
    assert any("ideology" in e or "string" in e for e in validate_lock_dict(d))


def test_schema_rejects_updates_outside_preferences():
    d = make_runs_lock().to_dict()
    d["crawlers"][0]["updates"] = {"params": {"pi": "1"}}
    assert validate_lock_dict(d) != []


def test_schema_rejects_an_unknown_kind():
    d = make_runs_lock().to_dict()
    d["kind"] = "swarm"
    assert any("kind" in e for e in validate_lock_dict(d))


def test_schema_still_requires_configuration_on_a_fleet_crawler():
    d = make_lock().to_dict()
    del d["crawlers"][0]["configuration"]
    assert any("configuration" in e for e in validate_lock_dict(d))


def test_schema_rejects_a_bad_base_lock_sha256():
    d = make_runs_lock().to_dict()
    d["provenance"]["base_lock_sha256"] = "not-a-sha"
    assert any("base_lock_sha256" in e for e in validate_lock_dict(d))


# --- many crawlers share one queue part, as in a fleet lock ----------------


def test_runs_crawlers_sharing_a_session_share_one_queue_part():
    base = make_runs_lock()
    crawlers = []
    for i in range(3):
        c = copy.deepcopy(base.crawlers[0])
        c["name"] = f"bot_{i:02d}"
        crawlers.append(c)
    lock = Lock(
        provenance=base.provenance,
        experiment=base.experiment,
        schedule=base.schedule,
        crawlers=crawlers,
        kind="runs",
    )
    d = lock.to_dict()
    assert len(d["parts"]["queues"]) == 1
    assert load_lock(dump_lock(lock)) == lock
