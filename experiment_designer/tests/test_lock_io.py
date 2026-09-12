"""The lock is the reproducibility artifact, so two properties matter more than
anything else here: dumping is byte-deterministic, and a document that is not
a lock is refused with a message naming what is wrong."""

import hashlib
from typing import Any

import pytest

from experiment_designer.lock.io import (
    LockError,
    dump_lock,
    is_lock_document,
    load_lock,
    sha256_text,
    validate_lock_dict,
)
from experiment_designer.lock.model import LOCK_FORMAT_VERSION, Lock, Provenance


def make_lock(**overrides) -> Lock:
    crawler = {
        "name": "bot_01",
        "description": "d",
        "configuration": {
            "name": "c",
            "description": "d",
            "active": 1,
            "params": {"pi": 1, "psi": 0, "kappa": 0, "tau": 0},
            "preferences": [{"name": "reliability", "value": "high"}],
        },
        "proxy": {
            "name": "p",
            "description": "d",
            "type": "http",
            "hostname": "pr.oxylabs.io",
            "port": 7777,
            "username": "${secret:oxylabs.a.username}",
            "password": "${secret:oxylabs.a.password}",
            "active": True,
        },
        "agent": {"name": "a", "identification": "mlx", "location": "US-NC", "active": True},
        "queues": [
            {
                "name": "s1",
                "description": "d",
                "start_offset": "+5m",
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
    }
    fields: dict[str, Any] = {
        "provenance": Provenance(
            spec_path="design/spec.yaml",
            spec_sha256="ab" * 32,
            generator="build_spec.R",
            git_commit="a2024c9",
            pmspec_version="0.1.0",
            seed=42,
        ),
        "experiment": {"name": "Study", "description": "", "runner_user_id": None, "contact": None},
        "schedule": {"anchor": "upload"},
        "crawlers": [crawler],
    }
    fields.update(overrides)
    return Lock(**fields)


def test_dump_is_deterministic_and_starts_with_the_format_marker():
    a, b = dump_lock(make_lock()), dump_lock(make_lock())
    assert a == b
    assert a.startswith(f"pmspec_lock: {LOCK_FORMAT_VERSION}\n")
    assert "\r" not in a


def test_top_level_key_order_is_fixed():
    text = dump_lock(make_lock())
    keys = [line.split(":")[0] for line in text.splitlines() if line and not line.startswith(" ")]
    assert keys == ["pmspec_lock", "provenance", "experiment", "schedule", "crawlers"]


def test_round_trip():
    lock = make_lock()
    assert load_lock(dump_lock(lock)) == lock


def test_unicode_survives_unescaped():
    lock = make_lock(experiment={"name": "Zürich Ärzte", "description": "", "runner_user_id": None})
    assert "Zürich" in dump_lock(lock)


def test_is_lock_document():
    assert is_lock_document(dump_lock(make_lock()))
    assert is_lock_document("# comment\npmspec_lock: 1\n")
    assert not is_lock_document("version: '1.0'\nexperiment:\n  name: x\n")


def test_schema_rejects_missing_required_keys():
    d = make_lock().to_dict()
    del d["provenance"]["seed"]
    errors = validate_lock_dict(d)
    assert any("seed" in e for e in errors)


def test_schema_rejects_start_at_in_a_lock():
    d = make_lock().to_dict()
    d["crawlers"][0]["queues"][0]["start_at"] = "2026-01-01T00:00:00"
    assert any("start_at" in e for e in validate_lock_dict(d))


def test_load_raises_lock_error_with_all_messages():
    text = dump_lock(make_lock()).replace("pmspec_lock: 1", "pmspec_lock: 99")
    with pytest.raises(LockError) as exc:
        load_lock(text)
    assert exc.value.errors and "pmspec_lock" in exc.value.errors[0]


def test_sha256_text_matches_hashlib_on_utf8_bytes():
    text = dump_lock(make_lock())
    assert sha256_text(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
