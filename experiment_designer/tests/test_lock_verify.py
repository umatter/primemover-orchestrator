"""verify: is this live experiment the one this lock describes?"""

import csv
import io
import zipfile
from datetime import datetime
from typing import Any

from experiment_designer.lock.io import dump_lock, sha256_text
from experiment_designer.lock.verify import Difference, compare_shapes, lock_shape, verify_lock
from experiment_designer.spec.time_utils import API_TIMEZONE

from .test_lock_io import make_lock

NOW = datetime(2026, 10, 1, 10, 0, tzinfo=API_TIMEZONE)


class FakeSession:
    """Serves a live experiment that matches make_lock() uploaded at NOW."""

    base_url = "http://fake"

    def __init__(
        self,
        lock_text: str,
        *,
        mutate: dict[str, Any] | None = None,
        preferences: list[dict[str, str]] | None = None,
        extra_crawlers: bool = False,
    ) -> None:
        self.sha = sha256_text(lock_text)
        self.mutate = mutate or {}
        self.preferences = preferences
        self.extra_crawlers = extra_crawlers
        self.calls: list[str] = []
        self.pages: list[tuple[str, dict[str, Any] | None]] = []

    def _crawler(self, cid: int, name: str, config_id: int) -> dict[str, Any]:
        return {
            "id": cid,
            "name": name,
            "agent": {"location": self.mutate.get("location", "US-NC")},
            "proxy": {
                "type": "http",
                "hostname": "pr.oxylabs.io",
                "port": 7777,
                "username": "cust",
                "password": "testpass",
            },
            "configuration": {
                "id": config_id,
                "preferences": [{"name": "reliability", "value": "high"}],
            },
        }

    def _unprocessed(self) -> list[dict[str, Any]]:
        rows = [
            {
                "id": 500,
                "name": "s1",
                "crawler_id": 30,
                "start_at": self.mutate.get("start_at", "2026-10-01 10:05:00"),
            }
        ]
        if self.extra_crawlers:
            rows.append(
                {"id": 501, "name": "s1", "crawler_id": 31, "start_at": "2026-10-01 10:05:00"}
            )
        return rows

    def get(self, path, params=None):
        self.calls.append(path)
        if path == "experiments/7":
            prefs = self.preferences
            if prefs is None:
                prefs = [
                    {"name": "pmspec.lock_sha256", "value": self.mutate.get("sha", self.sha)},
                    {"name": "pmspec.anchor", "value": "2026-10-01T10:00:00+02:00"},
                ]
            crawlers = [self._crawler(30, "bot_01", 90)]
            if self.extra_crawlers:
                crawlers.append(self._crawler(31, "bot_99", 91))
            return {
                "id": 7,
                "name": "Study",
                "user_id": 5,
                "preferences": prefs,
                "crawlers": crawlers,
            }
        if path in ("configurations/90", "configurations/91"):
            return {
                "id": int(path.split("/")[1]),
                "params": [
                    {
                        "pi": self.mutate.get("pi", "1"),
                        "psi": "0",
                        "kappa": "0",
                        "tau": "0",
                        "alpha": None,
                        "beta": None,
                        "search_terms": None,
                        # The API casts search_terms to an array but leaves this one
                        # as the JSON string it is stored as.
                        "media_outlet_urls": self.mutate.get("media_outlet_urls"),
                    }
                ],
            }
        if path.startswith("experiments/7/queues-"):
            # As the real sessions do: `data` unwrapped, a plain list, one page.
            self.pages.append((path, params))
            if "processed" in path and "unprocessed" not in path:
                return []
            return self._unprocessed() if (params or {}).get("page", 1) == 1 else []
        raise AssertionError(path)

    def get_bytes(self, path, params=None):
        self.calls.append(path)
        assert path == "experiments/7/download"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            jobs = io.StringIO()
            w = csv.writer(jobs)
            w.writerow(
                [
                    "ID",
                    "Name",
                    "Description",
                    "Type",
                    "Status Code",
                    "Status Message",
                    "Order",
                    "Active",
                    "Updated At",
                    "Created At",
                    "Started At",
                    "Finished At",
                    "User ID",
                    "Queue ID",
                ]
            )
            w.writerow([900, "visit", "d", "visitjob", "", "", 1, 1, "", "", "", "", 5, 500])
            z.writestr("7_primemover_jobs.csv", jobs.getvalue())
            beh = io.StringIO()
            w = csv.writer(beh)
            w.writerow(
                [
                    "ID",
                    "Name",
                    "Description",
                    "Value",
                    "Active",
                    "Updated At",
                    "Created At",
                    "User ID",
                    "Job ID",
                ]
            )
            w.writerow([1000, "url", "", self.mutate.get("url", "https://x"), 1, "", "", 5, 900])
            z.writestr("7_primemover_behaviors.csv", beh.getvalue())
            z.writestr("7_primemover_crawlers.csv", "ID\n30\n")
            z.writestr("7_primemover_queues.csv", "ID\n500\n")
        return buf.getvalue()


def test_bound_and_identical():
    text = dump_lock(make_lock())
    r = verify_lock(text, session=FakeSession(text), experiment_id=7)
    assert r.bound and r.differences == [] and r.ok
    assert r.render().startswith("BOUND")


def test_unbound_reports_both_hashes():
    text = dump_lock(make_lock())
    r = verify_lock(text, session=FakeSession(text, mutate={"sha": "0" * 64}), experiment_id=7)
    assert not r.bound and r.live_sha256s == ["0" * 64] and r.local_sha256 == sha256_text(text)
    assert "UNBOUND" in r.render()


def test_param_difference_is_reported_at_its_path():
    text = dump_lock(make_lock())
    r = verify_lock(text, session=FakeSession(text, mutate={"pi": "0"}), experiment_id=7)
    assert Difference("crawlers.bot_01.params.pi", 1.0, 0.0) in r.differences


def test_start_at_difference_uses_wall_clock_digits():
    text = dump_lock(make_lock())
    r = verify_lock(
        text, session=FakeSession(text, mutate={"start_at": "2026-10-01 10:06:00"}), experiment_id=7
    )
    assert any(d.path == "crawlers.bot_01.queues[0].start_at" for d in r.differences)


def test_location_difference():
    text = dump_lock(make_lock())
    r = verify_lock(text, session=FakeSession(text, mutate={"location": "US-TX"}), experiment_id=7)
    assert Difference("crawlers.bot_01.location", "US-NC", "US-TX") in r.differences


def test_shallow_verify_does_not_download_and_ignores_jobs():
    text = dump_lock(make_lock())
    s = FakeSession(text, mutate={"url": "https://other"})
    r = verify_lock(text, session=s, experiment_id=7)
    assert r.ok and not any("download" in c for c in s.calls)


def test_deep_verify_compares_job_behaviours():
    text = dump_lock(make_lock())
    s = FakeSession(text, mutate={"url": "https://other"})
    r = verify_lock(text, session=s, experiment_id=7, deep=True)
    assert r.deep and any(
        d.path == "crawlers.bot_01.queues[0].jobs[0].behaviors.url" for d in r.differences
    )
    ok = verify_lock(text, session=FakeSession(text), experiment_id=7, deep=True)
    assert ok.ok


def test_render_never_contains_credentials():
    text = dump_lock(make_lock())
    r = verify_lock(text, session=FakeSession(text, mutate={"pi": "0"}), experiment_id=7)
    assert "testpass" not in r.render() and "cust" not in r.render()


def test_compare_shapes_reports_missing_and_extra_crawlers():
    diffs = compare_shapes({"crawlers": {"a": {}, "b": {}}}, {"crawlers": {"a": {}, "c": {}}})
    paths = {d.path for d in diffs}
    assert paths == {"crawlers.b", "crawlers.c"}


def test_lock_shape_masks_nothing_but_never_includes_credentials():
    shape = lock_shape(make_lock(), NOW, deep=False)
    proxy = shape["crawlers"]["bot_01"]["proxy"]
    assert set(proxy) == {"type", "hostname", "port"}


def _lock_with_urls():
    lock = make_lock()
    lock.crawlers[0]["configuration"]["params"]["media_outlet_urls"] = ["nyt.com", "fox.com"]
    return lock


def test_json_string_media_outlet_urls_compares_equal_to_the_lock_list():
    text = dump_lock(_lock_with_urls())
    s = FakeSession(text, mutate={"media_outlet_urls": '["nyt.com", "fox.com"]'})
    r = verify_lock(text, session=s, experiment_id=7)
    assert r.ok, r.render()


def test_json_string_media_outlet_urls_difference_is_still_seen():
    text = dump_lock(_lock_with_urls())
    s = FakeSession(text, mutate={"media_outlet_urls": '["nyt.com"]'})
    r = verify_lock(text, session=s, experiment_id=7)
    assert any(d.path.startswith("crawlers.bot_01.params.media_outlet_urls") for d in r.differences)


class PagingSession(FakeSession):
    """500 unprocessed queues on page 1 and 3 on page 2, all for bot_01."""

    def get(self, path, params=None):
        if path.startswith("experiments/7/queues-unprocessed"):
            self.calls.append(path)
            self.pages.append((path, params))
            page = (params or {}).get("page", 1)
            assert (params or {}).get("per_page") == 500
            n = {1: 500, 2: 3}.get(page, 0)
            base = (page - 1) * 500
            return [
                {"id": 500 + base + i, "name": f"s{base + i}", "crawler_id": 30, "start_at": "x"}
                for i in range(n)
            ]
        return super().get(path, params)


def test_paginate_walks_every_page_of_an_unwrapped_list(monkeypatch):
    from experiment_designer.lock import verify as verify_mod

    text = dump_lock(make_lock())
    s = PagingSession(text)
    rows = verify_mod._paginate(s, "experiments/7/queues-unprocessed")
    assert len(rows) == 503
    assert [p[1]["page"] for p in s.pages] == [1, 2]


def test_paginate_stops_on_an_empty_first_page():
    from experiment_designer.lock import verify as verify_mod

    text = dump_lock(make_lock())
    s = FakeSession(text)
    assert verify_mod._paginate(s, "experiments/7/queues-processed") == []
    assert len(s.pages) == 1


def test_live_crawlers_absent_from_the_lock_are_a_note_not_a_difference():
    text = dump_lock(make_lock())
    r = verify_lock(text, session=FakeSession(text, extra_crawlers=True), experiment_id=7)
    assert r.ok and r.differences == []
    assert r.extra_live_crawlers == ["bot_99"]
    assert "bot_99" in r.render() and "not in this lock" in r.render()


def test_lock_crawler_missing_live_is_still_a_difference():
    lock = make_lock()
    lock.crawlers.append(dict(lock.crawlers[0], name="bot_02"))
    text = dump_lock(lock)
    r = verify_lock(text, session=FakeSession(text), experiment_id=7)
    assert Difference("crawlers.bot_02", "<present>", None) in r.differences


def _prefs(sha: str, uploads: list[tuple[str, str]]) -> list[dict[str, str]]:
    """One (sha, anchor) pair per upload, labelled the way upload_lock labels them."""
    out = []
    for i, (lock_sha, anchor) in enumerate(uploads, start=1):
        out.append({"name": "pmspec.lock_sha256", "description": f"upload {i}", "value": lock_sha})
        out.append({"name": "pmspec.anchor", "description": f"upload {i}", "value": anchor})
    return out


def test_anchor_is_the_one_from_the_same_upload_as_the_matching_hash():
    text = dump_lock(make_lock())
    sha = sha256_text(text)
    # This lock was upload 1 (anchor 10:00); a later upload 2 anchored at 12:00.
    prefs = _prefs(
        sha, [(sha, "2026-10-01T10:00:00+02:00"), ("0" * 64, "2026-10-01T12:00:00+02:00")]
    )
    r = verify_lock(text, session=FakeSession(text, preferences=prefs), experiment_id=7)
    assert r.bound and r.anchor == "2026-10-01T10:00:00+02:00" and r.ok


def test_anchor_falls_back_to_the_last_one_when_unbound():
    text = dump_lock(make_lock())
    prefs = _prefs(
        "x", [("0" * 64, "2026-10-01T09:00:00+02:00"), ("1" * 64, "2026-10-01T10:00:00+02:00")]
    )
    r = verify_lock(text, session=FakeSession(text, preferences=prefs), experiment_id=7)
    assert not r.bound and r.anchor == "2026-10-01T10:00:00+02:00" and r.differences == []


def test_no_live_anchor_skips_start_times():
    text = dump_lock(make_lock())
    prefs = [{"name": "pmspec.lock_sha256", "description": "upload 1", "value": sha256_text(text)}]
    s = FakeSession(text, mutate={"start_at": "2031-01-01 00:00:00"}, preferences=prefs)
    r = verify_lock(text, session=s, experiment_id=7)
    assert r.anchor is None and r.ok
    assert "start times not compared" in r.render()


def test_secret_valued_differences_are_redacted_at_the_source():
    """A lock keeps ``${secret:...}`` in a preference; live holds the resolved
    credential. Neither .differences nor .render() may carry the literal."""
    lock = make_lock()
    lock.crawlers[0]["configuration"]["preferences"].append(
        {"name": "token", "value": "${secret:a.b}"}
    )
    text = dump_lock(lock)

    class LeakySession(FakeSession):
        def _crawler(self, cid, name, config_id):
            c = super()._crawler(cid, name, config_id)
            c["configuration"]["preferences"].append({"name": "token", "value": "hunter2"})
            return c

    r = verify_lock(text, session=LeakySession(text), experiment_id=7)
    assert not r.ok
    assert "hunter2" not in r.render()
    assert all(d.actual != "hunter2" for d in r.differences)
    assert Difference("crawlers.bot_01.preferences.token", "${secret:a.b}", "<redacted>") in (
        r.differences
    )


def test_redact_differences_leaves_plain_differences_alone():
    from experiment_designer.lock.verify import redact_differences

    plain = Difference("crawlers.bot_01.params.pi", 1.0, 0.0)
    secret = Difference("crawlers.bot_01.preferences.token", "${secret:a.b}", "hunter2")
    out = redact_differences([plain, secret])
    assert out[0] == plain
    assert out[1] == Difference(secret.path, secret.expected, "<redacted>")
