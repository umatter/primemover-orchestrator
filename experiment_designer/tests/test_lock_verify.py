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

    def __init__(self, lock_text: str, *, mutate: dict[str, Any] | None = None) -> None:
        self.sha = sha256_text(lock_text)
        self.mutate = mutate or {}
        self.calls: list[str] = []

    def get(self, path, params=None):
        self.calls.append(path)
        if path == "experiments/7":
            return {
                "id": 7,
                "name": "Study",
                "user_id": 5,
                "preferences": [
                    {"name": "pmspec.lock_sha256", "value": self.mutate.get("sha", self.sha)},
                    {"name": "pmspec.anchor", "value": "2026-10-01T10:00:00+02:00"},
                ],
                "crawlers": [
                    {
                        "id": 30,
                        "name": "bot_01",
                        "agent": {"location": self.mutate.get("location", "US-NC")},
                        "proxy": {
                            "type": "http",
                            "hostname": "pr.oxylabs.io",
                            "port": 7777,
                            "username": "cust",
                            "password": "testpass",
                        },
                        "configuration": {
                            "id": 90,
                            "preferences": [{"name": "reliability", "value": "high"}],
                        },
                    }
                ],
            }
        if path == "configurations/90":
            return {
                "id": 90,
                "params": [
                    {
                        "pi": self.mutate.get("pi", "1"),
                        "psi": "0",
                        "kappa": "0",
                        "tau": "0",
                        "alpha": None,
                        "beta": None,
                        "search_terms": None,
                        "media_outlet_urls": None,
                    }
                ],
            }
        if path.startswith("experiments/7/queues-"):
            rows = (
                []
                if "processed" in path and "unprocessed" not in path
                else [
                    {
                        "id": 500,
                        "name": "s1",
                        "crawler_id": 30,
                        "start_at": self.mutate.get("start_at", "2026-10-01 10:05:00"),
                    }
                ]
            )
            return {"data": rows, "meta": {"last_page": 1}}
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
