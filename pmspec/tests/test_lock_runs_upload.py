"""Uploading a runs lock: queues appended per crawler, idempotently, with
opt-in preference updates.

A runs lock writes into an experiment that is already running, so the order of
operations is the feature. Every refusal is a read; the first write happens only
after all of them have passed, and a re-run of the same lock adds nothing twice.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from typing import Any

import pytest

from pmspec.lock.io import dump_lock, sha256_text
from pmspec.lock.model import Lock, LockError, Provenance
from pmspec.lock.upload import (
    UploadOptions,
    UploadRefused,
    plan_runs_upload,
    upload_lock,
)
from pmspec.spec.time_utils import API_TIMEZONE

NOW = datetime(2026, 10, 1, 10, 0, tzinfo=API_TIMEZONE)
BASE_SHA = "cd" * 32


def _queue(name: str, offset: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": "d",
        "start_offset": offset,
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


def make_runs_lock(*, updates: dict[str, Any] | None = None, **overrides) -> Lock:
    crawlers: list[dict[str, Any]] = [
        {"name": "bot_01", "queues": [_queue("2026-10-01", "+2h")]},
        {"name": "bot_02", "queues": [_queue("2026-10-01", "+3h")]},
    ]
    if updates:
        for crawler in crawlers:
            if crawler["name"] in updates:
                crawler["updates"] = {"preferences": dict(updates[crawler["name"]])}
    fields: dict[str, Any] = {
        "provenance": Provenance(
            spec_path="design/day.yaml",
            spec_sha256="ab" * 32,
            generator=None,
            git_commit="a2024c9",
            pmspec_version="0.1.0",
            seed=42,
            base_lock_sha256=BASE_SHA,
            base_spec_path="design/fleet.yaml",
        ),
        "experiment": {"name": "Study", "description": "", "runner_user_id": None, "contact": None},
        "schedule": {"anchor": "upload"},
        "crawlers": crawlers,
        "kind": "runs",
    }
    fields.update(overrides)
    return Lock(**fields)


class RunsSession:
    """A live experiment with crawlers, configurations and queues.

    Records every call. ``preference_update_supported`` is the *API version*
    switch the support probe reads: an API without the preferences sync answers
    the same 200 but its ``data`` carries no ``preferences`` key.

    It also *reflects its writes*, as the real API does: preferences PUT on the
    experiment come back on the next read, and queues posted to a crawler come
    back in the queue listings. That is what lets the post-upload verify in
    ``upload`` see what the upload just wrote.
    """

    base_url = "http://fake"

    def __init__(
        self,
        *,
        live_sha: str = BASE_SHA,
        live_mode: str | None = "real",
        crawler_names: tuple[str, ...] = ("bot_01", "bot_02"),
        preference_update_supported: bool = False,
        existing_queues: list[dict[str, Any]] | None = None,
        fail_on_crawler: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.extra_prefs: list[dict[str, Any]] = []
        self.live_sha = live_sha
        self.live_mode = live_mode
        self.preference_update_supported = preference_update_supported
        self.existing_queues = list(existing_queues or [])
        self.fail_on_crawler = fail_on_crawler
        self.crawler_uploads: dict[int, list[dict[str, Any]]] = {}
        self.configuration_puts: list[tuple[int, dict[str, Any]]] = []
        self.experiment_puts: list[dict[str, Any]] = []
        self.crawlers = [
            {
                "name": name,
                "id": 10 + i,
                "configuration": {
                    "id": 100 + i,
                    "name": f"cfg-{name}",
                    "description": "d",
                    "active": 1,
                    "preferences": [{"name": "ideology", "value": "0.10"}],
                },
            }
            for i, name in enumerate(crawler_names)
        ]

    # --- reads ---------------------------------------------------------

    def _experiment(self, experiment_id: int) -> dict[str, Any]:
        prefs = [{"name": "pmspec.lock_sha256", "description": "upload 1", "value": self.live_sha}]
        if self.live_mode is not None:
            prefs.append(
                {"name": "pmspec.mode", "description": "upload 1", "value": self.live_mode}
            )
        return {
            "id": experiment_id,
            "name": "Study",
            "user_id": 5,
            "description": "the study",
            "contact": "ada@example.org",
            "preferences": prefs + self.extra_prefs,
            "crawlers": copy.deepcopy(self.crawlers),
        }

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path.endswith("/queues-unprocessed"):
            return self.existing_queues
        if path.endswith("/queues-processed"):
            return []
        if path.startswith("experiments/"):
            return self._experiment(int(path.split("/")[1]))
        return {}

    # --- writes --------------------------------------------------------

    def post(self, path, json):  # pragma: no cover - a runs upload never creates
        self.calls.append(("POST", path, json))
        return {}

    def put(self, path, json):
        self.calls.append(("PUT", path, json))
        if path.startswith("configurations/"):
            config_id = int(path.split("/")[1])
            self.configuration_puts.append((config_id, json))
            data: dict[str, Any] = {"id": config_id, "name": json.get("name")}
            if self.preference_update_supported:
                data["preferences"] = [
                    {"name": p["name"], "value": p["value"]} for p in json.get("preferences", [])
                ]
            return data
        self.experiment_puts.append(json)
        self.extra_prefs.extend(json.get("preferences") or [])
        return {"id": int(path.split("/")[1])}

    def post_file(self, path, field, filename, content, content_type):
        self.calls.append(("POST_FILE", path, content))
        crawler_id = int(path.split("/")[1])
        name = next(c["name"] for c in self.crawlers if c["id"] == crawler_id)
        if self.fail_on_crawler == name:
            from pmspec.lock.api import ApiError

            raise ApiError("upload failed", status=422, detail="bad queue")
        queues = json.loads(content)
        self.crawler_uploads[crawler_id] = queues
        for q in queues:
            self.existing_queues.append(
                {
                    "id": 700 + len(self.existing_queues),
                    "crawler_id": crawler_id,
                    "name": q["name"],
                    "start_at": str(q["start_at"]).replace("T", " ")[:19],
                }
            )
        return {}


def _opts(**kw) -> UploadOptions:
    kw.setdefault("mode", "real")
    kw.setdefault("now", NOW)
    kw.setdefault("uploaded_by", "tester@box")
    kw.setdefault("experiment_id", 181)
    return UploadOptions(**kw)


def _live_queue(crawler_id: int, name: str, start_at: str) -> dict[str, Any]:
    return {"id": 1, "crawler_id": crawler_id, "name": name, "start_at": start_at}


def _prefs(session: RunsSession) -> dict[str, str]:
    return {p["name"]: p["value"] for p in session.experiment_puts[-1]["preferences"]}


# --- the happy path --------------------------------------------------------


def test_posts_one_queue_upload_per_crawler_with_resolved_start_at():
    text = dump_lock(make_runs_lock())
    session = RunsSession()
    result = upload_lock(text, session=session, store=None, options=_opts())

    assert sorted(session.crawler_uploads) == [10, 11]
    first = session.crawler_uploads[10]
    assert isinstance(first, list) and len(first) == 1
    assert first[0]["start_at"] == "2026-10-01T12:00:00+02:00"
    assert first[0]["name"] == "2026-10-01" and first[0]["jobs"]
    assert "start_offset" not in first[0]
    assert session.crawler_uploads[11][0]["start_at"] == "2026-10-01T13:00:00+02:00"
    paths = [c[1] for c in session.calls if c[0] == "POST_FILE"]
    assert paths == ["crawlers/10/upload", "crawlers/11/upload"]

    assert result.kind == "runs"
    assert result.queue_count == 2 and result.skipped_existing == 0
    assert result.experiment_id == 181 and result.experiment_name == "Study"
    assert result.crawler_count == 2 and result.runner_user_id is None


def test_provenance_is_upload_2_with_kind_and_base():
    text = dump_lock(make_runs_lock())
    session = RunsSession()
    upload_lock(text, session=session, store=None, options=_opts())

    body = session.experiment_puts[-1]
    assert body["name"] == "Study" and body["user_id"] == 5
    labels = {p["description"] for p in body["preferences"]}
    assert labels == {"upload 2"}
    prefs = _prefs(session)
    assert prefs["pmspec.lock_sha256"] == sha256_text(text)
    assert prefs["pmspec.kind"] == "runs"
    assert prefs["pmspec.base_lock_sha256"] == BASE_SHA
    assert prefs["pmspec.mode"] == "real"
    assert "pmspec.updates" not in prefs


def test_provenance_is_written_after_every_crawler():
    session = RunsSession()
    upload_lock(dump_lock(make_runs_lock()), session=session, store=None, options=_opts())
    kinds = [c[0] for c in session.calls]
    assert kinds.index("PUT") > max(i for i, k in enumerate(kinds) if k == "POST_FILE")


def test_record_line_carries_kind_base_queues_and_skipped():
    session = RunsSession()
    result = upload_lock(dump_lock(make_runs_lock()), session=session, store=None, options=_opts())
    assert "exp=181 mode=real lock=" in result.record_line
    assert f"kind=runs base={BASE_SHA[:12]} queues=2 skipped=0" in result.record_line


# --- idempotency -----------------------------------------------------------


def test_rerun_skips_every_queue_and_still_records_an_upload():
    session = RunsSession(
        existing_queues=[
            _live_queue(10, "2026-10-01", "2026-10-01 12:00:00"),
            _live_queue(11, "2026-10-01", "2026-10-01 13:00:00"),
        ]
    )
    result = upload_lock(dump_lock(make_runs_lock()), session=session, store=None, options=_opts())
    assert session.crawler_uploads == {}
    assert not [c for c in session.calls if c[0] == "POST_FILE"]
    assert result.queue_count == 0 and result.skipped_existing == 2
    assert _prefs(session)["pmspec.kind"] == "runs"
    assert "queues=0 skipped=2" in result.record_line


def test_a_half_finished_upload_posts_only_the_missing_queues():
    session = RunsSession(existing_queues=[_live_queue(10, "2026-10-01", "2026-10-01 12:00:00")])
    result = upload_lock(dump_lock(make_runs_lock()), session=session, store=None, options=_opts())
    assert sorted(session.crawler_uploads) == [11]
    assert result.queue_count == 1 and result.skipped_existing == 1


def test_a_live_queue_with_another_name_at_the_same_time_is_not_a_match():
    session = RunsSession(existing_queues=[_live_queue(10, "other", "2026-10-01 12:00:00")])
    result = upload_lock(dump_lock(make_runs_lock()), session=session, store=None, options=_opts())
    assert sorted(session.crawler_uploads) == [10, 11]
    assert result.skipped_existing == 0


def test_another_crawlers_queue_does_not_mask_this_ones():
    # Same (name, wall clock), wrong crawler: grouping by crawler_id is what
    # keeps a shared session name from suppressing a real append.
    session = RunsSession(existing_queues=[_live_queue(11, "2026-10-01", "2026-10-01 12:00:00")])
    result = upload_lock(dump_lock(make_runs_lock()), session=session, store=None, options=_opts())
    assert sorted(session.crawler_uploads) == [10, 11]
    assert result.skipped_existing == 0


# --- the refusals, all before any write ------------------------------------


def _no_writes(session: RunsSession) -> bool:
    return not [c for c in session.calls if c[0] in ("POST", "PUT", "POST_FILE")]


def test_refuses_without_an_experiment_id():
    session = RunsSession()
    with pytest.raises(UploadRefused, match=r"a runs lock appends to an experiment: pass -e <id>"):
        upload_lock(
            dump_lock(make_runs_lock()),
            session=session,
            store=None,
            options=_opts(experiment_id=None),
        )
    assert _no_writes(session)


def test_refuses_a_base_mismatch_naming_both_shas():
    session = RunsSession(live_sha="ef" * 32)
    with pytest.raises(UploadRefused) as e:
        upload_lock(dump_lock(make_runs_lock()), session=session, store=None, options=_opts())
    assert str(e.value) == (
        f"experiment 181 was created from lock {('ef' * 32)[:12]}, "
        f"this runs lock is based on {BASE_SHA[:12]}"
    )
    assert _no_writes(session)


def test_refuses_a_mode_mismatch_naming_the_mode_to_use():
    session = RunsSession(live_mode="test")
    with pytest.raises(UploadRefused) as e:
        upload_lock(dump_lock(make_runs_lock()), session=session, store=None, options=_opts())
    assert str(e.value) == (
        "experiment 181 is a test experiment; upload this runs lock with --mode test"
    )
    assert _no_writes(session)


def test_refuses_a_crawler_name_that_is_not_live():
    session = RunsSession(crawler_names=("bot_01",))
    with pytest.raises(UploadRefused, match="bot_02"):
        upload_lock(dump_lock(make_runs_lock()), session=session, store=None, options=_opts())
    assert _no_writes(session)


def test_refuses_a_duplicated_live_crawler_name():
    session = RunsSession(crawler_names=("bot_01", "bot_01", "bot_02"))
    with pytest.raises(UploadRefused, match="bot_01"):
        upload_lock(dump_lock(make_runs_lock()), session=session, store=None, options=_opts())
    assert _no_writes(session)


def test_refuses_a_past_anchor_without_allow_past():
    lock = make_runs_lock(schedule={"anchor": "2026-09-01T10:00:00"})
    session = RunsSession()
    with pytest.raises(UploadRefused, match="in the past"):
        upload_lock(dump_lock(lock), session=session, store=None, options=_opts())
    assert _no_writes(session)


def test_refuses_a_runs_lock_with_no_base_sha():
    lock = make_runs_lock()
    lock.provenance.base_lock_sha256 = None
    session = RunsSession()
    with pytest.raises(UploadRefused, match="base_lock_sha256"):
        upload_lock(dump_lock(lock), session=session, store=None, options=_opts())
    assert _no_writes(session)


def test_a_secret_placeholder_in_a_runs_lock_is_a_lock_error():
    lock = make_runs_lock()
    lock.crawlers[0]["queues"][0]["jobs"][0]["behaviors"][0]["value"] = "${secret:a.b}"
    session = RunsSession()
    with pytest.raises(LockError, match="secret"):
        upload_lock(dump_lock(lock), session=session, store=None, options=_opts())
    assert _no_writes(session)


# --- updates ---------------------------------------------------------------


def test_updates_are_refused_before_any_write_when_the_api_cannot_apply_them():
    lock = make_runs_lock(updates={"bot_01": {"ideology": "0.31"}})
    session = RunsSession(preference_update_supported=False)
    with pytest.raises(UploadRefused) as e:
        upload_lock(dump_lock(lock), session=session, store=None, options=_opts())
    assert str(e.value) == (
        "this API cannot update preferences (PUT configurations/{id} strips them); "
        "deploy primemover_api with preferences sync, or drop `updates:`"
    )
    # The probe is the one PUT, and it changes nothing.
    assert not [c for c in session.calls if c[0] == "POST_FILE"]
    assert len(session.configuration_puts) == 1
    assert session.configuration_puts[0][1]["preferences"] == []


def test_the_probe_runs_once_and_only_when_updates_are_present():
    session = RunsSession(preference_update_supported=True)
    upload_lock(dump_lock(make_runs_lock()), session=session, store=None, options=_opts())
    assert session.configuration_puts == []

    lock = make_runs_lock(updates={"bot_01": {"ideology": "0.31"}, "bot_02": {"ideology": "0.7"}})
    session = RunsSession(preference_update_supported=True)
    upload_lock(dump_lock(lock), session=session, store=None, options=_opts())
    probes = [p for p in session.configuration_puts if p[1]["preferences"] == []]
    assert len(probes) == 1 and probes[0][0] == 100


def test_updates_are_applied_and_recorded_with_the_old_value():
    lock = make_runs_lock(updates={"bot_01": {"ideology": "0.31"}})
    session = RunsSession(preference_update_supported=True)
    result = upload_lock(dump_lock(lock), session=session, store=None, options=_opts())

    applied = [p for p in session.configuration_puts if p[1]["preferences"]]
    assert len(applied) == 1
    config_id, body = applied[0]
    assert config_id == 100
    assert body["name"] == "cfg-bot_01" and body["active"] == 1
    assert body["preferences"] == [{"name": "ideology", "value": "0.31"}]
    assert result.updates_applied == 1

    recorded = json.loads(_prefs(session)["pmspec.updates"])
    assert recorded == {"bot_01": {"preferences.ideology": ["0.10", "0.31"]}}


def test_a_long_updates_blob_is_replaced_by_a_pointer_to_the_lock():
    names = tuple(f"bot_{i:02d}" for i in range(12))
    updates = {n: {"ideology": f"0.{i:02d}"} for i, n in enumerate(names)}
    lock = make_runs_lock(
        crawlers=[
            {
                "name": n,
                "queues": [_queue("2026-10-01", "+2h")],
                "updates": {"preferences": updates[n]},
            }
            for n in names
        ]
    )
    session = RunsSession(crawler_names=names, preference_update_supported=True)
    upload_lock(dump_lock(lock), session=session, store=None, options=_opts())
    assert _prefs(session)["pmspec.updates"] == "<12 crawlers updated; see lock>"


# --- failure part-way ------------------------------------------------------


def test_a_failure_mid_way_names_the_crawler_and_writes_no_provenance():
    from pmspec.lock.api import ApiError

    names = ("bot_01", "bot_02", "bot_03")
    lock = make_runs_lock(
        crawlers=[{"name": n, "queues": [_queue("2026-10-01", "+2h")]} for n in names]
    )
    session = RunsSession(crawler_names=names, fail_on_crawler="bot_02")
    with pytest.raises(ApiError, match="crawler bot_02"):
        upload_lock(dump_lock(lock), session=session, store=None, options=_opts())
    assert sorted(session.crawler_uploads) == [10]
    assert session.experiment_puts == []


# --- dry run ---------------------------------------------------------------


def test_plan_reports_the_counts_and_writes_nothing():
    lock = make_runs_lock(updates={"bot_01": {"ideology": "0.31"}})
    session = RunsSession(
        preference_update_supported=True,
        existing_queues=[_live_queue(10, "2026-10-01", "2026-10-01 12:00:00")],
    )
    plan = plan_runs_upload(dump_lock(lock), session=session, options=_opts())
    assert plan == {
        "kind": "runs",
        "experiment_id": 181,
        "crawlers": 2,
        "queues": 1,
        "queues_in_lock": 2,
        "would_skip": 1,
        "updates": 1,
        "base_ok": True,
        "preference_update_supported": True,
        "anchor": "2026-10-01T10:00:00+02:00",
    }
    assert not [c for c in session.calls if c[0] in ("POST", "POST_FILE")]
    assert session.experiment_puts == []


def test_plan_refuses_the_same_things_the_upload_does():
    session = RunsSession(live_sha="ef" * 32)
    with pytest.raises(UploadRefused, match="was created from lock"):
        plan_runs_upload(dump_lock(make_runs_lock()), session=session, options=_opts())


def test_plan_reports_an_api_that_cannot_update_preferences():
    lock = make_runs_lock(updates={"bot_01": {"ideology": "0.31"}})
    session = RunsSession(preference_update_supported=False)
    with pytest.raises(UploadRefused, match="cannot update preferences"):
        plan_runs_upload(dump_lock(lock), session=session, options=_opts())


def test_plan_refuses_a_fleet_lock():
    from .test_lock_io import make_lock

    with pytest.raises(UploadRefused, match="not a runs lock"):
        plan_runs_upload(dump_lock(make_lock()), session=RunsSession(), options=_opts())


# --- the support probe itself ----------------------------------------------


def test_api_session_probes_once_and_caches_the_answer(monkeypatch):
    from pmspec.lock.api import ApiSession

    calls: list[tuple[str, Any]] = []

    def fake_put(self, path, json):
        calls.append((path, json))
        return {"id": 100, "name": json["name"], "preferences": []}

    monkeypatch.setattr(ApiSession, "put", fake_put)
    session = ApiSession("http://x", "e", "p")
    config = {"id": 100, "name": "c", "description": "d", "active": 1}
    assert session.supports_preference_update(config) is True
    assert session.supports_preference_update(config) is True
    assert len(calls) == 1
    assert calls[0][0] == "configurations/100" and calls[0][1]["preferences"] == []


def test_an_api_that_strips_preferences_answers_no(monkeypatch):
    from pmspec.lock.api import ApiSession

    # The old repository returns fresh() with no preferences relation loaded.
    monkeypatch.setattr(ApiSession, "put", lambda self, path, json: {"id": 100, "name": "c"})
    session = ApiSession("http://x", "e", "p")
    assert session.supports_preference_update({"id": 100, "name": "c"}) is False


def test_a_session_without_the_method_is_probed_through_put():
    from pmspec.lock.upload import supports_preference_update

    session = RunsSession(preference_update_supported=True)
    assert supports_preference_update(session, session.crawlers[0]["configuration"]) is True
    assert len(session.configuration_puts) == 1
