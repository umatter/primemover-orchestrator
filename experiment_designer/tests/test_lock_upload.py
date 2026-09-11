"""Upload reads locks only, resolves secrets and the anchor in memory, and writes
provenance onto the experiment. A fake ApiSession records every call."""

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from experiment_designer.lock.io import dump_lock, sha256_text
from experiment_designer.lock.secrets import MissingSecret, SecretStore
from experiment_designer.lock.upload import (
    TEST_RUNNER_USER_ID,
    UploadOptions,
    UploadRefused,
    effective_experiment,
    provenance_preferences,
    upload_lock,
)
from experiment_designer.spec.time_utils import API_TIMEZONE

from .test_lock_io import make_lock


class FakeSession:
    base_url = "http://fake"

    def __init__(self, fail_upload: bool = False) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.fail_upload = fail_upload
        self.next_id = 181

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path.startswith("experiments/"):
            return {
                "id": int(path.split("/")[1]),
                "name": "Study",
                "user_id": 5,
                "preferences": [{"name": "pmspec.lock_sha256", "value": "old"}],
            }
        return {}

    def post(self, path, json):
        self.calls.append(("POST", path, json))
        if path == "experiments":
            return {"id": self.next_id, "name": json["name"]}
        return {}

    def put(self, path, json):
        self.calls.append(("PUT", path, json))
        return {"id": int(path.split("/")[1])}

    def post_file(self, path, field, filename, content, content_type):
        self.calls.append(("POST_FILE", path, content))
        if self.fail_upload:
            from experiment_designer.lock.api import ApiError

            raise ApiError("upload failed", status=422, detail="bad json")
        return {}


@pytest.fixture
def store(tmp_path: Path) -> SecretStore:
    p = tmp_path / "s.toml"
    p.write_text('[oxylabs.a]\nusername = "u"\npassword = "testpass"\n')
    p.chmod(0o600)
    return SecretStore.load(p)


NOW = datetime(2026, 10, 1, 10, 0, tzinfo=API_TIMEZONE)


def _opts(**kw) -> UploadOptions:
    kw.setdefault("mode", "real")
    kw.setdefault("now", NOW)
    kw.setdefault("uploaded_by", "tester@box")
    return UploadOptions(**kw)


def test_creates_experiment_with_provenance_then_uploads_crawlers(store):
    lock = make_lock()
    text = dump_lock(lock)
    session = FakeSession()
    result = upload_lock(text, session=session, store=store, options=_opts())

    kinds = [c[0] for c in session.calls]
    assert kinds[:2] == ["POST", "POST_FILE"]
    create = session.calls[0][2]
    assert create["name"] == "Study" and create["active"] == 1
    prefs = {p["name"]: p["value"] for p in create["preferences"]}
    assert prefs["pmspec.lock_sha256"] == sha256_text(text)
    assert prefs["pmspec.seed"] == "42"
    assert prefs["pmspec.mode"] == "real"
    assert prefs["pmspec.anchor"] == "2026-10-01T10:00:00+02:00"
    assert prefs["pmspec.git_commit"] == "a2024c9"
    assert prefs["pmspec.uploaded_by"] == "tester@box"
    assert "testpass" not in str(create)
    assert result.experiment_id == 181 and result.lock_sha256 == sha256_text(text)
    assert result.record_line.startswith("2026-10-01T10:00:00+02:00 exp=181 mode=real lock=")


def test_uploaded_json_has_resolved_start_at_and_secrets(store):
    session = FakeSession()
    upload_lock(dump_lock(make_lock()), session=session, store=store, options=_opts())
    body = session.calls[1][2]
    assert '"start_at": "2026-10-01T10:05:00+02:00"' in body
    assert '"password": "testpass"' in body and "${secret:" not in body
    assert "start_offset" not in body


def test_missing_secret_aborts_before_any_request(tmp_path: Path):
    p = tmp_path / "empty.toml"
    p.write_text("")
    p.chmod(0o600)
    session = FakeSession()
    with pytest.raises(MissingSecret, match="oxylabs.a.username"):
        upload_lock(
            dump_lock(make_lock()), session=session, store=SecretStore.load(p), options=_opts()
        )
    assert session.calls == []


def test_dirty_commit_refused_in_real_mode(store):
    lock = make_lock()
    lock.provenance.git_commit = "a2024c9-dirty"
    with pytest.raises(UploadRefused, match="dirty"):
        upload_lock(dump_lock(lock), session=FakeSession(), store=store, options=_opts())


def test_dirty_commit_allowed_in_test_mode_and_stamped(store):
    lock = make_lock()
    lock.provenance.git_commit = "a2024c9-dirty"
    session = FakeSession()
    upload_lock(dump_lock(lock), session=session, store=store, options=_opts(mode="test"))
    prefs = {p["name"]: p["value"] for p in session.calls[0][2]["preferences"]}
    assert prefs["pmspec.git_commit"] == "a2024c9-dirty" and prefs["pmspec.mode"] == "test"


def test_no_commit_refused_in_real_mode_unless_allow_dirty(store):
    lock = make_lock()
    lock.provenance.git_commit = None
    with pytest.raises(UploadRefused, match="commit"):
        upload_lock(dump_lock(lock), session=FakeSession(), store=store, options=_opts())
    upload_lock(
        dump_lock(lock), session=FakeSession(), store=store, options=_opts(allow_dirty=True)
    )


def test_test_mode_forces_vm_runner_and_prefixes_name():
    name, runner = effective_experiment(make_lock(), "test", date(2026, 9, 11))
    assert name == "test-Study-20260911" and runner == TEST_RUNNER_USER_ID
    lock = make_lock(experiment={"name": "Study", "description": "", "runner_user_id": 1})
    assert effective_experiment(lock, "real", date(2026, 9, 11)) == ("Study", 1)


def test_fixed_anchor_in_the_past_is_refused(store):
    lock = make_lock(schedule={"anchor": "2020-01-01T00:00:00"})
    with pytest.raises(UploadRefused, match="past"):
        upload_lock(dump_lock(lock), session=FakeSession(), store=store, options=_opts())
    upload_lock(dump_lock(lock), session=FakeSession(), store=store, options=_opts(allow_past=True))


def test_append_mode_puts_numbered_provenance_and_skips_create(store):
    session = FakeSession()
    upload_lock(
        dump_lock(make_lock()), session=session, store=store, options=_opts(experiment_id=42)
    )
    kinds = [c[0] for c in session.calls]
    assert kinds == ["GET", "PUT", "POST_FILE"]
    put = session.calls[1][2]
    assert put["name"] == "Study" and put["user_id"] == 5  # resent unchanged
    assert all(p["description"] == "upload 2" for p in put["preferences"])


def test_a_spec_is_rejected(store):
    with pytest.raises(UploadRefused, match="compile"):
        upload_lock(
            "version: '1.0'\nexperiment: {name: x}\n",
            session=FakeSession(),
            store=store,
            options=_opts(),
        )


def test_provenance_values_fit_the_api_column():
    lock = make_lock()
    prefs = provenance_preferences(
        lock,
        lock_sha256="f" * 64,
        anchor_iso="2026-10-01T10:00:00+02:00",
        mode="real",
        uploaded_at="2026-09-11T10:00:00+02:00",
        uploaded_by="u@h",
    )
    assert {p["name"] for p in prefs} == {
        "pmspec.lock_sha256",
        "pmspec.spec_sha256",
        "pmspec.seed",
        "pmspec.anchor",
        "pmspec.git_commit",
        "pmspec.generator",
        "pmspec.version",
        "pmspec.mode",
        "pmspec.lax",
        "pmspec.uploaded_at",
        "pmspec.uploaded_by",
    }
    assert all(len(p["value"]) <= 255 and isinstance(p["value"], str) for p in prefs)
