"""verify over a runs lock: the queues it appended, and the preferences it updated.

A runs lock owns neither the bots nor their configuration -- the fleet lock it
was compiled against does. So a runs verify compares exactly what the runs
upload wrote: per crawler, the queues at the wall-clock starts the lock asked
for, and the preference values its ``updates:`` set. Every other live queue on
those crawlers belongs to another upload and is not a difference.
"""

from __future__ import annotations

import csv
import io
import zipfile
from typing import Any

from pmspec.lock.io import dump_lock, sha256_text
from pmspec.lock.verify import Difference, verify_lock

from .test_lock_runs_upload import BASE_SHA, make_runs_lock

EXPERIMENT_ID = 181
ANCHOR = "2026-10-01T10:00:00+02:00"
#: make_runs_lock(): one queue "2026-10-01" per bot, at +2h and +3h.
LOCK_START = {"bot_01": "2026-10-01 12:00:00", "bot_02": "2026-10-01 13:00:00"}
CRAWLER_ID = {"bot_01": 10, "bot_02": 11}
QUEUE_ID = {"bot_01": 500, "bot_02": 501}


def _queue_row(
    bot: str, *, name: str = "2026-10-01", start_at: str | None = None
) -> dict[str, Any]:
    return {
        "id": QUEUE_ID[bot],
        "crawler_id": CRAWLER_ID[bot],
        "name": name,
        "start_at": start_at or LOCK_START[bot],
    }


def _appended() -> list[dict[str, Any]]:
    return [_queue_row("bot_01"), _queue_row("bot_02")]


class RunsVerifySession:
    """The live experiment a runs lock was appended to.

    Carries provenance for two uploads (the fleet that created it, then this
    runs lock) so the anchor has to be picked by label, and serves crawlers
    with a proxy, a location and a configuration -- none of which a runs verify
    may look at. ``configurations/{id}`` raises if it is ever fetched.
    """

    base_url = "http://fake"

    def __init__(
        self,
        lock_text: str,
        *,
        queues: list[dict[str, Any]] | None = None,
        ideology: str = "0.10",
        preferences: list[dict[str, str]] | None = None,
        sha: str | None = None,
        extra_crawler: bool = False,
    ) -> None:
        self.sha = sha or sha256_text(lock_text)
        self.queues = _appended() if queues is None else list(queues)
        self.ideology = ideology
        self.preferences = preferences
        self.extra_crawler = extra_crawler
        self.calls: list[str] = []

    def _crawler(self, name: str) -> dict[str, Any]:
        prefs = [{"name": "lean", "value": "left"}]
        if self.ideology is not None:
            prefs.append({"name": "ideology", "value": self.ideology})
        return {
            "id": CRAWLER_ID.get(name, 99),
            "name": name,
            "agent": {"location": "US-NC"},
            "proxy": {"type": "http", "hostname": "pr.oxylabs.io", "port": 7777},
            "configuration": {"id": 100 + CRAWLER_ID.get(name, 99), "preferences": prefs},
        }

    def get(self, path, params=None):
        self.calls.append(path)
        if path == f"experiments/{EXPERIMENT_ID}":
            prefs = self.preferences
            if prefs is None:
                prefs = [
                    {
                        "name": "pmspec.lock_sha256",
                        "description": "upload 1",
                        "value": BASE_SHA,
                    },
                    {
                        "name": "pmspec.anchor",
                        "description": "upload 1",
                        "value": "2026-09-01T10:00:00+02:00",
                    },
                    {"name": "pmspec.lock_sha256", "description": "upload 2", "value": self.sha},
                    {"name": "pmspec.anchor", "description": "upload 2", "value": ANCHOR},
                    {"name": "pmspec.kind", "description": "upload 2", "value": "runs"},
                ]
            crawlers = [self._crawler("bot_01"), self._crawler("bot_02")]
            if self.extra_crawler:
                crawlers.append(self._crawler("bot_99"))
            return {
                "id": EXPERIMENT_ID,
                "name": "Study",
                "user_id": 5,
                "preferences": prefs,
                "crawlers": crawlers,
            }
        if path.startswith(f"experiments/{EXPERIMENT_ID}/queues-"):
            if path.endswith("queues-processed"):
                return []
            return self.queues if (params or {}).get("page", 1) == 1 else []
        raise AssertionError(path)

    def get_bytes(self, path, params=None):
        self.calls.append(path)
        assert path == f"experiments/{EXPERIMENT_ID}/download"
        return _export_zip()


def _export_zip(url: str = "https://x") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        jobs = io.StringIO()
        w = csv.writer(jobs)
        w.writerow(["ID", "Order", "Type", "Queue ID"])
        for i, qid in enumerate(QUEUE_ID.values()):
            w.writerow([900 + i, 1, "visitjob", qid])
        z.writestr("181_primemover_jobs.csv", jobs.getvalue())
        beh = io.StringIO()
        w = csv.writer(beh)
        w.writerow(["ID", "Name", "Value", "Job ID"])
        for i in range(len(QUEUE_ID)):
            w.writerow([1000 + i, "url", url, 900 + i])
        z.writestr("181_primemover_behaviors.csv", beh.getvalue())
    return buf.getvalue()


def _verify(session: RunsVerifySession, lock_text: str, **kw):
    return verify_lock(lock_text, session=session, experiment_id=EXPERIMENT_ID, **kw)


def test_bound_and_identical_when_the_appended_queues_are_live():
    text = dump_lock(make_runs_lock())
    r = _verify(RunsVerifySession(text), text)
    assert r.bound and r.differences == [], r.differences
    assert r.ok and r.anchor == ANCHOR
    assert r.render().startswith("BOUND")


def test_a_runs_verify_reads_no_configuration_and_compares_no_fleet_shape():
    """params, proxy and location belong to the base fleet lock, so a runs
    verify neither compares them nor pays for the request that fetches them."""
    text = dump_lock(make_runs_lock())
    session = RunsVerifySession(text)
    r = _verify(session, text)
    assert r.ok
    assert not any(c.startswith("configurations/") for c in session.calls), session.calls


def test_queues_from_another_upload_are_not_differences():
    text = dump_lock(make_runs_lock())
    other = [
        _queue_row("bot_01", name="2026-10-02", start_at="2026-10-02 12:00:00"),
        _queue_row("bot_02", name="2026-10-02", start_at="2026-10-02 13:00:00"),
    ]
    r = _verify(RunsVerifySession(text, queues=_appended() + other, extra_crawler=True), text)
    assert r.differences == [], r.differences
    assert r.extra_live_crawlers == []


def test_a_queue_the_lock_appended_that_is_not_live_is_a_difference():
    text = dump_lock(make_runs_lock())
    r = _verify(RunsVerifySession(text, queues=[_queue_row("bot_01")]), text)
    assert Difference("crawlers.bot_02.queues.length", 1, 0) in r.differences
    assert not r.ok


def test_a_queue_at_a_different_start_is_not_the_lock_s_queue():
    text = dump_lock(make_runs_lock())
    session = RunsVerifySession(
        text, queues=[_queue_row("bot_01"), _queue_row("bot_02", start_at="2026-10-01 14:00:00")]
    )
    r = _verify(session, text)
    assert Difference("crawlers.bot_02.queues.length", 1, 0) in r.differences


def test_updates_are_compared_against_the_live_preference():
    lock = make_runs_lock(updates={"bot_01": {"ideology": "0.90"}})
    text = dump_lock(lock)
    r = _verify(RunsVerifySession(text, ideology="0.10"), text)
    assert Difference("crawlers.bot_01.updates.ideology", "0.90", "0.10") in r.differences
    assert _verify(RunsVerifySession(text, ideology="0.90"), text).ok


def test_only_the_updated_preference_names_are_compared():
    """A runs lock's `updates:` names the preferences it set; every other
    preference on the bot is the fleet's business, not this lock's."""
    text = dump_lock(make_runs_lock(updates={"bot_01": {"ideology": "0.10"}}))
    r = _verify(RunsVerifySession(text), text)
    assert r.ok
    assert not any("lean" in d.path for d in r.differences)


def test_an_update_whose_preference_is_absent_is_a_difference():
    text = dump_lock(make_runs_lock(updates={"bot_01": {"ideology": "0.90"}}))
    r = _verify(RunsVerifySession(text, ideology=None), text)
    assert Difference("crawlers.bot_01.updates.ideology", "<present>", None) in r.differences


def test_unbound_when_the_experiment_carries_another_lock():
    text = dump_lock(make_runs_lock())
    r = _verify(RunsVerifySession(text, sha="0" * 64), text)
    assert not r.bound and "UNBOUND" in r.render()


def test_deep_compares_the_jobs_of_the_appended_queues():
    text = dump_lock(make_runs_lock())
    assert _verify(RunsVerifySession(text), text, deep=True).ok


def test_deep_reports_a_behaviour_difference_in_an_appended_queue(monkeypatch):
    text = dump_lock(make_runs_lock())
    session = RunsVerifySession(text)
    monkeypatch.setattr(session, "get_bytes", lambda *a, **k: _export_zip(url="https://other"))
    r = _verify(session, text, deep=True)
    assert any(d.path.endswith("behaviors.url") for d in r.differences), r.differences
