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


@pytest.mark.parametrize(
    "key,block",
    [
        ("configuration", "    configuration:\n      name: c\n"),
        ("agent", "    agent:\n      ref: a:0000000000000000\n      name: a\n"),
        ("proxy", "    proxy:\n      name: p\n"),
    ],
)
def test_loading_a_hand_edited_runs_lock_says_what_is_wrong(key, block):
    # Through load_lock, not Lock.from_dict: the runs crawler item is
    # `additionalProperties: false`, so without the check running ahead of the
    # schema the author would be told "Additional properties are not allowed".
    text = dump_lock(make_runs_lock()).replace("  - name: bot_01\n", "  - name: bot_01\n" + block)
    with pytest.raises(LockError) as exc:
        load_lock(text)
    assert exc.value.errors == [
        f"runs lock crawler 'bot_01' carries {key}; a runs lock adds queues only"
    ]


def test_loading_a_runs_lock_with_updates_outside_preferences_says_what_is_wrong():
    text = dump_lock(make_runs_lock()).replace(
        "      preferences:\n        ideology:", "      params:\n        pi:"
    )
    with pytest.raises(LockError) as exc:
        load_lock(text)
    assert exc.value.errors == [
        "runs lock crawler 'bot_01': updates may only contain preferences (got params)"
    ]


def test_the_check_ahead_of_the_schema_leaves_shape_errors_to_the_schema():
    # It runs before every shape check, so it must not itself trip over a
    # malformed document -- the schema is still the one that names the problem.
    full = dump_lock(make_runs_lock())
    text = full[: full.index("crawlers:")] + "crawlers:\n  - just a string\n"
    with pytest.raises(LockError) as exc:
        load_lock(text)
    assert any("crawlers" in e or "object" in e for e in exc.value.errors)


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


# --- compiling a runs spec against a fleet lock -----------------------------

FLEET_SPEC = """
version: "1.0"
experiment:
  name: Fleet Study
  description: the randomization record
design:
  factors:
    arm:
      levels: [control, treated]
      maps_to:
        params.pi: {control: 0, treated: 1}
crawlers:
  - from_design: true
    name: "bot_{arm}"
    configuration:
      preferences:
        - {name: arm, value: "{arm}"}
    agent:
      location: US-IL-CHICAGO
    session:
      start: "+5m"
      workflow:
        - visit: https://example.com
"""

RUNS_SPEC = """
version: "1.0"
runs_for: fleet.lock.yaml
crawlers:
  - name: bot_control
    session:
      start: "2026-10-01T10:00:00"
      workflow:
        - visit: https://example.com/day1
  - name: bot_treated
    session:
      start: "2026-10-01T10:00:00"
      workflow:
        - visit: https://example.com/day1
    updates:
      preferences:
        ideology: "0.31"
"""

FROM_BASE_SPEC = """
version: "1.0"
runs_for: fleet.lock.yaml
templates:
  day:
    - visit: "https://example.com/${arm}"
crawlers:
  - from_base: true
    name: "{base.name}"
    description: "{base.name} in {base.location}"
    session:
      start: "2026-10-01T10:00:00"
      workflow:
        - template: day
          with: {arm: "{base.arm}"}
"""


@pytest.fixture
def study(tmp_path):
    """A compiled fleet lock on disk, for a runs spec to be compiled against."""
    from pmspec.lock.build import compile_spec

    (tmp_path / "fleet.yaml").write_text(FLEET_SPEC)
    result = compile_spec(
        FLEET_SPEC, base_dir=tmp_path, spec_path="fleet.yaml", seed=7, generator="test"
    )
    assert result.lock is not None, result.errors
    (tmp_path / "fleet.lock.yaml").write_text(dump_lock(result.lock))
    return tmp_path


def compile_runs(study_dir, text=RUNS_SPEC, *, seed=11, name="day.yaml", **kw):
    from pmspec.lock.build import compile_spec

    (study_dir / name).write_text(text)
    return compile_spec(text, base_dir=study_dir, spec_path=name, seed=seed, **kw)


def test_a_runs_spec_compiles_to_a_runs_lock(study):
    from pmspec.lock.io import sha256_text

    result = compile_runs(study)
    assert result.errors == []
    lock = result.lock
    assert lock is not None and lock.kind == "runs" and lock.is_runs
    assert lock.provenance.base_lock_sha256 == sha256_text((study / "fleet.lock.yaml").read_text())
    assert lock.provenance.base_spec_path == "fleet.yaml"
    assert lock.experiment["name"] == "Fleet Study"
    assert lock.experiment["description"] == "the randomization record"
    assert [c["name"] for c in lock.crawlers] == ["bot_control", "bot_treated"]
    for c in lock.crawlers:
        assert set(c) <= {"name", "queues", "updates"}
        assert len(c["queues"]) == 1 and c["queues"][0]["jobs"]


def test_a_runs_lock_round_trips_through_the_schema(study):
    lock = compile_runs(study).lock
    assert load_lock(dump_lock(lock)) == lock


def test_updates_reach_the_lock_as_strings(study):
    lock = compile_runs(study).lock
    assert "updates" not in lock.crawlers[0]
    assert lock.crawlers[1]["updates"] == {"preferences": {"ideology": "0.31"}}


def test_the_anchor_comes_from_the_runs_spec_not_the_base(study):
    lock = compile_runs(study).lock
    assert lock.schedule["anchor"] == "2026-10-01T10:00:00"


def test_compiling_a_runs_spec_is_byte_deterministic(study):
    a = compile_runs(study).lock
    b = compile_runs(study).lock
    assert dump_lock(a) == dump_lock(b)


def test_from_base_writes_one_crawler_per_base_crawler(study):
    result = compile_runs(study, FROM_BASE_SPEC)
    assert result.errors == []
    lock = result.lock
    assert [c["name"] for c in lock.crawlers] == ["bot_control", "bot_treated"]
    urls = [
        b["value"]
        for c in lock.crawlers
        for q in c["queues"]
        for j in q["jobs"]
        for b in j["behaviors"]
        if b["name"] == "url"
    ]
    assert urls == ["https://example.com/control", "https://example.com/treated"]


def test_a_name_the_base_does_not_have_is_an_error(study):
    text = RUNS_SPEC.replace("bot_control", "bot_ghost")
    result = compile_runs(study, text)
    assert result.lock is None
    assert "runs spec names crawler 'bot_ghost' which the base lock does not have" in result.errors


def test_runs_for_pointing_at_a_runs_lock_is_refused(study):
    lock = compile_runs(study).lock
    (study / "day.lock.yaml").write_text(dump_lock(lock))
    result = compile_runs(
        study, RUNS_SPEC.replace("fleet.lock.yaml", "day.lock.yaml"), name="d2.yaml"
    )
    assert result.lock is None
    assert any("fleet lock" in e for e in result.errors)


def test_runs_for_pointing_at_a_missing_file_is_refused(study):
    result = compile_runs(study, RUNS_SPEC.replace("fleet.lock.yaml", "nope.lock.yaml"))
    assert result.lock is None
    assert any("nope.lock.yaml" in e for e in result.errors)


# --- what a runs *spec* may not carry --------------------------------------


def test_design_in_a_runs_spec_is_an_error(study):
    text = RUNS_SPEC.replace(
        "crawlers:", "design:\n  factors:\n    a:\n      levels: [x, y]\ncrawlers:"
    )
    result = compile_runs(study, text)
    assert result.lock is None
    assert any(e.startswith("design:") for e in result.errors)


def test_experiment_in_a_runs_spec_is_an_error(study):
    text = RUNS_SPEC.replace("crawlers:", "experiment:\n  name: Other\ncrawlers:")
    result = compile_runs(study, text)
    assert result.lock is None
    assert any(e.startswith("experiment:") for e in result.errors)


@pytest.mark.parametrize(
    "block,key",
    [
        ("    agent:\n      location: DE-BE-BERLIN\n", "agent"),
        ("    proxy:\n      type: http\n      hostname: h\n      port: 1\n", "proxy"),
        ("    configuration:\n      params: {pi: 1}\n", "configuration"),
    ],
)
def test_a_fleet_only_block_on_a_runs_crawler_is_an_error(study, block, key):
    text = RUNS_SPEC.replace("  - name: bot_control\n", "  - name: bot_control\n" + block)
    result = compile_runs(study, text)
    assert result.lock is None
    assert (
        f"crawler[0].{key}: a runs spec adds sessions to existing bots; "
        "agent/proxy/configuration live in the fleet spec"
    ) in result.errors


def test_updates_outside_preferences_is_an_error(study):
    text = RUNS_SPEC.replace(
        '    updates:\n      preferences:\n        ideology: "0.31"\n',
        '    updates:\n      params:\n        pi: "1"\n',
    )
    result = compile_runs(study, text)
    assert result.lock is None
    assert "crawler[1].updates.params: only preferences can be updated" in result.errors


def test_defaults_in_a_runs_spec_may_only_carry_session_keys(study):
    text = RUNS_SPEC.replace(
        "crawlers:", "defaults:\n  timezone: Europe/Zurich\n  location: DE-BE-BERLIN\ncrawlers:"
    )
    result = compile_runs(study, text)
    assert result.lock is None
    assert any(e.startswith("defaults.location:") for e in result.errors)


def test_a_fleet_spec_still_treats_the_runs_keys_as_unknown(tmp_path):
    from pmspec.lock.build import compile_spec

    text = """
version: "1.0"
experiment:
  name: Fleet
crawlers:
  - name: bot
    updates: {preferences: {x: "1"}}
    session:
      start: "+5m"
      workflow:
        - visit: https://example.com
"""
    result = compile_spec(text, base_dir=tmp_path, spec_path="s.yaml", seed=1)
    assert result.lock is None
    assert any("crawler[0].updates" in e and "unknown key" in e for e in result.errors)


def test_an_unknown_base_variable_is_a_drop(study):
    # Not an error raised by the expansion: the converter's unresolved-variable
    # scan is what sees the token in its final place, with a full path, after
    # every other pass has had its chance to fill it.
    text = FROM_BASE_SPEC.replace("{base.arm}", "{base.ghost}")
    result = compile_runs(study, text)
    assert result.lock is None
    assert any("base.ghost" in e for e in result.errors)


def test_a_bad_format_spec_on_a_base_variable_is_an_error(study):
    text = FROM_BASE_SPEC.replace("{base.arm}", "{base.arm:.2f}")
    result = compile_runs(study, text)
    assert result.lock is None
    assert any("base.arm" in e and "format" in e for e in result.errors)
