"""
Hermetic tests for the pm_* write/control tools in ``orchestrator.mcp_server.server``.

These tools mutate real research data, so the properties under test are:

* **dry-run by default** — without ``confirm=True`` no mutating API call is made;
* **payload construction** — the exact body handed to the API;
* **validation / error paths** — unknown job types, unknown entities, mid-flight failures.

Every test patches ``server._api_request`` (the single seam through which the tools reach
the API) with an in-memory recorder, so no test can ever touch the network.  The local-write
guard is satisfied by patching ``server.get_settings`` with the ``mock_settings`` fixture
(``PM_API_BASE_URL=http://localhost:8090/api/v1``); ``_write()`` / ``_require_local_write_target()``
themselves are covered in ``test_mcp_write_guard.py`` and are not re-tested here.
"""

import json
from typing import Any

import pytest

from orchestrator.mcp_server import server as s


class RecordingAPI:
    """Stand-in for ``server._api_request`` that records calls and replays scripted responses."""

    def __init__(self, responses: list[Any] | None = None, default: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses: list[Any] = list(responses or [])
        self.default: Any = default if default is not None else {"data": {"id": 1}}

    def __call__(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append({"method": method, "endpoint": endpoint, "data": data, "params": params})
        if self._responses:
            nxt = self._responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return self.default

    @property
    def writes(self) -> list[dict[str, Any]]:
        """Every non-GET call — i.e. everything that would have mutated the database."""
        return [c for c in self.calls if c["method"] != "GET"]

    def signature(self) -> list[tuple[str, str]]:
        return [(c["method"], c["endpoint"]) for c in self.calls]


@pytest.fixture(autouse=True)
def local_api_target(monkeypatch: pytest.MonkeyPatch, mock_settings) -> None:
    """Point the write guard at the local dev API so the tools are reachable at all."""
    monkeypatch.setattr(s, "get_settings", lambda: mock_settings)
    assert s._require_local_write_target() is None


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> RecordingAPI:
    """Patch the one seam to the API. Present in *every* test: nothing may reach the network."""
    recorder = RecordingAPI()
    monkeypatch.setattr(s, "_api_request", recorder)
    return recorder


def scripted(monkeypatch: pytest.MonkeyPatch, *responses: Any) -> RecordingAPI:
    """Patch ``_api_request`` with a recorder replaying ``responses`` in order."""
    recorder = RecordingAPI(list(responses))
    monkeypatch.setattr(s, "_api_request", recorder)
    return recorder


# =============================================================================
# pm_create_experiment
# =============================================================================


class TestCreateExperiment:
    def test_dry_run_previews_and_makes_no_api_call(self, api: RecordingAPI) -> None:
        out = json.loads(s.pm_create_experiment(name="Study A", contact="ada@example.org"))

        assert api.calls == []
        assert out == {
            "dry_run": True,
            "action": "create experiment",
            "method": "POST",
            "endpoint": "experiments",
            "payload": {
                "name": "Study A",
                "contact": "ada@example.org",
                "description": "Study A",  # defaults to the name
                "testing": False,
            },
            "to_apply": "call again with confirm=true",
        }

    def test_explicit_description_and_testing_flag(self, api: RecordingAPI) -> None:
        out = json.loads(
            s.pm_create_experiment(
                name="Study A", contact="ada@example.org", description="desc", testing=True
            )
        )
        assert out["payload"]["description"] == "desc"
        assert out["payload"]["testing"] is True
        assert api.calls == []

    def test_confirm_posts_and_unwraps_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = scripted(monkeypatch, {"data": {"id": 77, "name": "Study A"}})

        out = json.loads(
            s.pm_create_experiment(name="Study A", contact="ada@example.org", confirm=True)
        )

        assert out == {
            "applied": True,
            "action": "create experiment",
            "result": {"id": 77, "name": "Study A"},
        }
        assert rec.calls == [
            {
                "method": "POST",
                "endpoint": "experiments",
                "data": {
                    "name": "Study A",
                    "contact": "ada@example.org",
                    "description": "Study A",
                    "testing": False,
                },
                "params": None,
            }
        ]

    def test_api_failure_is_reported_not_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scripted(monkeypatch, Exception("API request failed: 422 contact required"))

        out = json.loads(s.pm_create_experiment(name="Study A", contact="", confirm=True))

        assert out["applied"] is False
        assert out["action"] == "create experiment"
        assert out["error"] == "API request failed: 422 contact required"

    def test_runner_assignment_is_passed_through_when_given(self, api: RecordingAPI) -> None:
        out = json.loads(
            s.pm_create_experiment(name="Study A", contact="ada@example.org", runner_user_id=3)
        )
        assert out["payload"]["runner_user_id"] == 3
        assert api.calls == []

    def test_runner_assignment_is_absent_by_default(self, api: RecordingAPI) -> None:
        out = json.loads(s.pm_create_experiment(name="Study A", contact="ada@example.org"))
        assert "runner_user_id" not in out["payload"]  # NULL = default runner, API-side


# =============================================================================
# pm_add_bot
# =============================================================================


class TestAddBot:
    def test_dry_run_payload(self, api: RecordingAPI) -> None:
        out = json.loads(
            s.pm_add_bot(experiment_id=12, name="bot_1", agent_id=3, proxy_id=4, configuration_id=5)
        )

        assert api.calls == []
        assert out["dry_run"] is True
        assert out["action"] == "add bot"
        assert out["method"] == "POST"
        assert out["endpoint"] == "crawlers"
        assert out["payload"] == {
            "experiment_id": 12,
            "name": "bot_1",
            "description": "bot_1",
            "agent_id": 3,
            "proxy_id": 4,
            "configuration_id": 5,
        }

    def test_confirm_posts_exact_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = scripted(monkeypatch, {"data": {"id": 31}})

        out = json.loads(
            s.pm_add_bot(
                experiment_id=12,
                name="bot_1",
                agent_id=3,
                proxy_id=4,
                configuration_id=5,
                description="treatment arm",
                confirm=True,
            )
        )

        assert out == {"applied": True, "action": "add bot", "result": {"id": 31}}
        assert rec.calls[0]["method"] == "POST"
        assert rec.calls[0]["endpoint"] == "crawlers"
        assert rec.calls[0]["data"]["description"] == "treatment arm"
        assert len(rec.calls) == 1


# =============================================================================
# pm_create_run
# =============================================================================


class TestCreateRun:
    def test_dry_run_defaults(self, api: RecordingAPI) -> None:
        out = json.loads(
            s.pm_create_run(crawler_id=9, name="morning run", start_at="2026-08-01T09:00")
        )

        assert api.calls == []
        assert out["endpoint"] == "queues"
        assert out["method"] == "POST"
        assert out["payload"] == {
            "crawler_id": 9,
            "name": "morning run",
            "description": "morning run",
            "start_at": "2026-08-01T09:00",
            "timezone": "Europe/Berlin",
            "timezone_utc_offset": "+02:00",
            "active": 0,  # runs are created inactive so jobs can be composed first
        }

    def test_confirm_honours_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = scripted(monkeypatch, {"data": {"id": 40}})

        out = json.loads(
            s.pm_create_run(
                crawler_id=9,
                name="zurich run",
                start_at="2026-08-01T09:00",
                timezone="Europe/Zurich",
                timezone_utc_offset="+01:00",
                description="d",
                active=1,
                confirm=True,
            )
        )

        assert out["applied"] is True
        assert rec.calls[0]["data"] == {
            "crawler_id": 9,
            "name": "zurich run",
            "description": "d",
            "start_at": "2026-08-01T09:00",
            "timezone": "Europe/Zurich",
            "timezone_utc_offset": "+01:00",
            "active": 1,
        }


# =============================================================================
# pm_activate_run
# =============================================================================


class TestActivateRun:
    def test_dry_run_uses_put_on_the_queue(self, api: RecordingAPI) -> None:
        out = json.loads(s.pm_activate_run(queue_id=42))

        assert api.calls == []
        assert out["action"] == "set run 42 active=1"
        assert out["method"] == "PUT"
        assert out["endpoint"] == "queues/42"
        assert out["payload"] == {"active": 1}

    def test_deactivate_dry_run(self, api: RecordingAPI) -> None:
        out = json.loads(s.pm_activate_run(queue_id=42, active=0))

        assert api.calls == []
        assert out["action"] == "set run 42 active=0"
        assert out["payload"] == {"active": 0}

    def test_confirm_puts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = scripted(monkeypatch, {"data": {"id": 42, "active": 1}})

        out = json.loads(s.pm_activate_run(queue_id=42, confirm=True))

        assert out == {
            "applied": True,
            "action": "set run 42 active=1",
            "result": {"id": 42, "active": 1},
        }
        assert rec.calls == [
            {"method": "PUT", "endpoint": "queues/42", "data": {"active": 1}, "params": None}
        ]


# =============================================================================
# pm_add_job
# =============================================================================


class TestAddJob:
    def test_unknown_job_type_is_rejected_before_any_api_call(self, api: RecordingAPI) -> None:
        out = json.loads(s.pm_add_job(queue_id=5, type="bogusjob", name="x"))

        assert api.calls == []
        assert "Unknown job type 'bogusjob'" in out["error"]
        assert "visitjob" in out["error"]
        assert "dry_run" not in out

    def test_dry_run_with_explicit_slot_makes_no_api_call(self, api: RecordingAPI) -> None:
        out = json.loads(
            s.pm_add_job(queue_id=5, type="visitjob", name="Open YT", order=3, previous_job_id=99)
        )

        assert api.calls == []
        assert out["dry_run"] is True
        assert out["endpoint"] == "jobs"
        assert out["payload"] == {
            "queue_id": 5,
            "type": "visitjob",
            "name": "Open YT",
            "description": "",
            "order": 3,
            "active": 1,
            "previous_job_id": 99,
        }

    def test_auto_slot_appends_after_highest_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Deliberately out of order: _next_job_slot must sort before picking the tail.
        rec = scripted(
            monkeypatch,
            {"data": [{"id": 11, "order": 2}, {"id": 10, "order": 1}, {"id": 12, "order": 3}]},
        )

        out = json.loads(s.pm_add_job(queue_id=5, type="waitjob", name="Watch"))

        assert out["payload"]["order"] == 4
        assert out["payload"]["previous_job_id"] == 12
        # The auto-slot lookup is a read; no write may happen during a dry run.
        assert rec.writes == []
        assert rec.calls == [
            {
                "method": "GET",
                "endpoint": "jobs",
                "data": None,
                "params": {"filter[queue]": "5"},
            }
        ]

    def test_auto_slot_on_empty_queue_starts_the_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = scripted(monkeypatch, {"data": []})

        out = json.loads(s.pm_add_job(queue_id=5, type="visitjob", name="first"))

        assert out["payload"]["order"] == 1
        assert out["payload"]["previous_job_id"] is None
        assert rec.writes == []

    def test_auto_slot_swallows_lookup_failure_and_falls_back_to_order_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # NOTE: a failed lookup silently yields slot 1 rather than surfacing the error.
        scripted(monkeypatch, Exception("API request failed: 500"))

        out = json.loads(s.pm_add_job(queue_id=5, type="visitjob", name="x"))

        assert out["payload"]["order"] == 1
        assert out["payload"]["previous_job_id"] is None
        assert "error" not in out

    def test_explicit_order_still_auto_resolves_previous_job(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = scripted(monkeypatch, {"data": [{"id": 21, "order": 1}]})

        out = json.loads(s.pm_add_job(queue_id=5, type="waitjob", name="x", order=9))

        assert out["payload"]["order"] == 9  # caller's value wins
        assert out["payload"]["previous_job_id"] == 21  # the missing half is filled in
        assert rec.signature() == [("GET", "jobs")]

    def test_confirm_posts_job(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = scripted(monkeypatch, {"data": {"id": 501}})

        out = json.loads(
            s.pm_add_job(
                queue_id=5,
                type="waitjob",
                name="Watch",
                order=2,
                description="pause",
                active=0,
                previous_job_id=500,
                confirm=True,
            )
        )

        assert out == {"applied": True, "action": "add job", "result": {"id": 501}}
        assert rec.calls == [
            {
                "method": "POST",
                "endpoint": "jobs",
                "data": {
                    "queue_id": 5,
                    "type": "waitjob",
                    "name": "Watch",
                    "description": "pause",
                    "order": 2,
                    "active": 0,
                    "previous_job_id": 500,
                },
                "params": None,
            }
        ]

    def test_confirm_reports_api_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scripted(monkeypatch, Exception("API request failed: 422 queue_id invalid"))

        out = json.loads(
            s.pm_add_job(
                queue_id=0, type="visitjob", name="x", order=1, previous_job_id=1, confirm=True
            )
        )

        assert out["applied"] is False
        assert "422" in out["error"]


# =============================================================================
# pm_set_behaviour
# =============================================================================


class TestSetBehaviour:
    def test_create_dry_run(self, api: RecordingAPI) -> None:
        out = json.loads(s.pm_set_behaviour(job_id=501, name="url", value="https://youtube.com"))

        assert api.calls == []
        assert out["action"] == "set behaviour"
        assert out["method"] == "POST"
        assert out["endpoint"] == "behaviors"  # API spells it without the 'u'
        assert out["payload"] == {
            "job_id": 501,
            "name": "url",
            "value": "https://youtube.com",
            "description": "",
            "active": True,
        }

    def test_update_dry_run_targets_the_behaviour(self, api: RecordingAPI) -> None:
        out = json.loads(
            s.pm_set_behaviour(job_id=501, name="url", value="https://x.test", behaviour_id=7)
        )

        assert api.calls == []
        assert out["action"] == "update behaviour"
        assert out["method"] == "PUT"
        assert out["endpoint"] == "behaviors/7"
        assert out["payload"]["value"] == "https://x.test"

    def test_behaviour_id_zero_falls_back_to_create(self, api: RecordingAPI) -> None:
        # `if behaviour_id:` is falsy for 0 — documents current behaviour, not an endorsement.
        out = json.loads(s.pm_set_behaviour(job_id=501, name="url", value="v", behaviour_id=0))

        assert api.calls == []
        assert out["action"] == "set behaviour"
        assert out["endpoint"] == "behaviors"

    def test_confirm_create_posts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = scripted(monkeypatch, {"data": {"id": 900}})

        out = json.loads(
            s.pm_set_behaviour(
                job_id=501,
                name="seconds",
                value="30",
                description="dwell",
                active=False,
                confirm=True,
            )
        )

        assert out == {"applied": True, "action": "set behaviour", "result": {"id": 900}}
        assert rec.calls == [
            {
                "method": "POST",
                "endpoint": "behaviors",
                "data": {
                    "job_id": 501,
                    "name": "seconds",
                    "value": "30",
                    "description": "dwell",
                    "active": False,
                },
                "params": None,
            }
        ]

    def test_confirm_update_puts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = scripted(monkeypatch, {"data": {"id": 7}})

        out = json.loads(
            s.pm_set_behaviour(
                job_id=501, name="url", value="https://x.test", behaviour_id=7, confirm=True
            )
        )

        assert out["applied"] is True
        assert out["action"] == "update behaviour"
        assert rec.signature() == [("PUT", "behaviors/7")]
        assert rec.calls[0]["data"]["job_id"] == 501


# =============================================================================
# pm_delete
# =============================================================================


class TestDelete:
    def test_unknown_entity_is_rejected(self, api: RecordingAPI) -> None:
        out = json.loads(s.pm_delete(entity="crawler", id=5))

        assert api.calls == []
        assert "Unknown entity 'crawler'" in out["error"]
        for known in ("experiment", "bot", "run", "job", "behaviour"):
            assert known in out["error"]

    @pytest.mark.parametrize(
        ("entity", "endpoint"),
        [
            ("experiment", "experiments/5"),
            ("bot", "crawlers/5/destroy"),
            ("run", "queues/5"),
            ("job", "jobs/5"),
            ("behaviour", "behaviors/5"),
        ],
    )
    def test_dry_run_routes(self, api: RecordingAPI, entity: str, endpoint: str) -> None:
        out = json.loads(s.pm_delete(entity=entity, id=5))

        assert api.calls == []
        assert out["method"] == "DELETE"
        assert out["endpoint"] == endpoint
        assert out["action"] == f"delete {entity} {5}"
        assert out["payload"] is None

    def test_cascade_warning_text(self, api: RecordingAPI) -> None:
        out = json.loads(s.pm_delete(entity="experiment", id=5))
        assert out["warnings"] == [
            "Deleting this experiment also removes its bots, runs, jobs and behaviours."
        ]

        out = json.loads(s.pm_delete(entity="run", id=5))
        assert out["warnings"] == ["Deleting this run also removes its jobs and behaviours."]

    def test_leaf_delete_has_no_warning(self, api: RecordingAPI) -> None:
        out = json.loads(s.pm_delete(entity="behaviour", id=5))
        assert "warnings" not in out
        assert api.calls == []

    def test_confirm_sends_delete_with_no_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = scripted(monkeypatch, {"status": 204})

        out = json.loads(s.pm_delete(entity="run", id=3, confirm=True))

        assert out == {"applied": True, "action": "delete run 3", "result": {"status": 204}}
        assert rec.calls == [
            {"method": "DELETE", "endpoint": "queues/3", "data": None, "params": None}
        ]

    def test_confirm_reports_api_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scripted(monkeypatch, Exception("API request failed: 404 Not Found"))

        out = json.loads(s.pm_delete(entity="job", id=999, confirm=True))

        assert out["applied"] is False
        assert out["action"] == "delete job 999"
        assert "404" in out["error"]


# =============================================================================
# pm_compose_run (composite: run + jobs + behaviours)
# =============================================================================


JOBS = [
    {
        "type": "visitjob",
        "name": "Open YouTube",
        "behaviours": [{"name": "url", "value": "https://youtube.com"}],
    },
    {"type": "waitjob", "name": "Watch", "behaviours": [{"name": "seconds", "value": 30}]},
]


class TestComposeRun:
    def test_dry_run_plan_and_no_api_calls(self, api: RecordingAPI) -> None:
        out = json.loads(
            s.pm_compose_run(
                crawler_id=14,
                run={"name": "compose run", "start_at": "2026-08-01T09:00"},
                jobs=JOBS,
            )
        )

        assert api.calls == []
        assert out["dry_run"] is True
        assert out["action"] == "compose run"
        assert out["to_apply"] == "call again with confirm=true"
        assert out["plan"]["create_run"] == {
            "crawler_id": 14,
            "name": "compose run",
            "start_at": "2026-08-01T09:00",
        }
        assert out["plan"]["jobs"] == [
            {
                "type": "visitjob",
                "name": "Open YouTube",
                "behaviours": [{"name": "url", "value": "https://youtube.com"}],
            },
            {"type": "waitjob", "name": "Watch", "behaviours": [{"name": "seconds", "value": 30}]},
        ]

    def test_dry_run_defaults_behaviours_to_empty_list(self, api: RecordingAPI) -> None:
        out = json.loads(
            s.pm_compose_run(
                crawler_id=14,
                run={"name": "r", "start_at": "2026-08-01T09:00"},
                jobs=[{"type": "visitjob", "name": "only"}],
            )
        )
        assert out["plan"]["jobs"] == [{"type": "visitjob", "name": "only", "behaviours": []}]
        assert api.calls == []

    def test_unknown_job_type_blocks_even_with_confirm(self, api: RecordingAPI) -> None:
        out = json.loads(
            s.pm_compose_run(
                crawler_id=14,
                run={"name": "r", "start_at": "2026-08-01T09:00"},
                jobs=[{"type": "visitjob", "name": "ok"}, {"type": "nopejob", "name": "bad"}],
                confirm=True,
            )
        )

        assert api.calls == []  # validation precedes every write
        assert "Unknown job type(s): ['nopejob']" in out["error"]
        assert "applied" not in out

    def test_job_without_type_is_rejected(self, api: RecordingAPI) -> None:
        out = json.loads(
            s.pm_compose_run(
                crawler_id=14,
                run={"name": "r", "start_at": "2026-08-01T09:00"},
                jobs=[{"name": "missing type"}],
                confirm=True,
            )
        )

        assert api.calls == []
        assert "Unknown job type(s): [None]" in out["error"]

    def test_confirm_creates_run_then_chained_jobs_and_behaviours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = scripted(
            monkeypatch,
            {"data": {"id": 40}},  # POST queues
            {"data": {"id": 501}},  # POST jobs #1
            {"data": {"id": 900}},  # POST behaviors for job #1
            {"data": {"id": 502}},  # POST jobs #2
            {"data": {"id": 901}},  # POST behaviors for job #2
        )

        out = json.loads(
            s.pm_compose_run(
                crawler_id=14,
                run={"name": "compose run", "start_at": "2026-08-01T09:00"},
                jobs=JOBS,
                confirm=True,
            )
        )

        assert out == {
            "applied": True,
            "action": "compose run",
            "run_id": 40,
            "jobs": [
                {"id": 501, "type": "visitjob", "behaviour_ids": [900]},
                {"id": 502, "type": "waitjob", "behaviour_ids": [901]},
            ],
        }
        assert rec.signature() == [
            ("POST", "queues"),
            ("POST", "jobs"),
            ("POST", "behaviors"),
            ("POST", "jobs"),
            ("POST", "behaviors"),
        ]
        # Run defaults are filled in from the sparse `run` dict.
        assert rec.calls[0]["data"] == {
            "crawler_id": 14,
            "name": "compose run",
            "description": "compose run",
            "start_at": "2026-08-01T09:00",
            "timezone": "Europe/Berlin",
            "timezone_utc_offset": "+02:00",
            "active": 0,
        }
        # Jobs are ordered 1..n and chained: first has no predecessor, second points at the first.
        assert rec.calls[1]["data"] == {
            "queue_id": 40,
            "type": "visitjob",
            "name": "Open YouTube",
            "description": "",
            "order": 1,
            "active": 1,
            "previous_job_id": None,
        }
        assert rec.calls[3]["data"]["order"] == 2
        assert rec.calls[3]["data"]["previous_job_id"] == 501
        assert rec.calls[3]["data"]["queue_id"] == 40
        # Behaviour values are coerced to strings (30 -> "30").
        assert rec.calls[2]["data"] == {
            "job_id": 501,
            "name": "url",
            "value": "https://youtube.com",
            "description": "",
            "active": True,
        }
        assert rec.calls[4]["data"]["value"] == "30"
        assert rec.calls[4]["data"]["job_id"] == 502

    def test_confirm_honours_run_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = scripted(monkeypatch, {"data": {"id": 41}})

        out = json.loads(
            s.pm_compose_run(
                crawler_id=14,
                run={
                    "name": "r",
                    "description": "explicit",
                    "start_at": "2026-08-01T09:00",
                    "timezone": "Europe/Zurich",
                    "timezone_utc_offset": "+01:00",
                    "active": 1,
                },
                jobs=[],
                confirm=True,
            )
        )

        assert out == {"applied": True, "action": "compose run", "run_id": 41, "jobs": []}
        assert rec.calls[0]["data"]["description"] == "explicit"
        assert rec.calls[0]["data"]["timezone"] == "Europe/Zurich"
        assert rec.calls[0]["data"]["timezone_utc_offset"] == "+01:00"
        assert rec.calls[0]["data"]["active"] == 1
        assert len(rec.calls) == 1  # no jobs -> no further writes

    def test_job_name_defaults_to_its_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = scripted(monkeypatch, {"data": {"id": 40}}, {"data": {"id": 501}})

        s.pm_compose_run(
            crawler_id=14,
            run={"name": "r", "start_at": "2026-08-01T09:00"},
            jobs=[{"type": "waitjob"}],
            confirm=True,
        )

        assert rec.calls[1]["data"]["name"] == "waitjob"

    def test_missing_run_key_fails_before_any_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = scripted(monkeypatch)

        out = json.loads(
            s.pm_compose_run(
                crawler_id=14,
                run={"start_at": "2026-08-01T09:00"},  # no name
                jobs=[{"type": "visitjob", "name": "x"}],
                confirm=True,
            )
        )

        assert rec.calls == []
        assert out["applied"] is False
        assert out["action"] == "compose run"
        assert out["error"] == "'name'"

    def test_missing_start_at_fails_before_any_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = scripted(monkeypatch)

        out = json.loads(
            s.pm_compose_run(
                crawler_id=14,
                run={"name": "r"},
                jobs=[],
                confirm=True,
            )
        )

        assert rec.calls == []
        assert out["applied"] is False
        assert out["error"] == "'start_at'"

    def test_failure_midway_leaves_partial_state_and_reports_no_run_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = scripted(
            monkeypatch,
            {"data": {"id": 40}},  # POST queues       -> succeeds
            {"data": {"id": 501}},  # POST jobs #1      -> succeeds
            {"data": {"id": 900}},  # POST behaviors    -> succeeds
            Exception("API request failed: 422 previous_job_id invalid"),  # POST jobs #2 fails
        )

        out = json.loads(
            s.pm_compose_run(
                crawler_id=14,
                run={"name": "compose run", "start_at": "2026-08-01T09:00"},
                jobs=JOBS,
                confirm=True,
            )
        )

        assert out["applied"] is False
        assert out["action"] == "compose run"
        assert "422" in out["error"]
        # There is no rollback: the queue and the first job really were created ...
        assert rec.signature() == [
            ("POST", "queues"),
            ("POST", "jobs"),
            ("POST", "behaviors"),
            ("POST", "jobs"),
        ]
        # ... yet the caller is told neither the run id nor which jobs survived.
        assert "run_id" not in out
        assert "jobs" not in out

    def test_behaviour_failure_aborts_the_remaining_jobs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = scripted(
            monkeypatch,
            {"data": {"id": 40}},
            {"data": {"id": 501}},
            Exception("API request failed: 500 behaviour"),
        )

        out = json.loads(
            s.pm_compose_run(
                crawler_id=14,
                run={"name": "r", "start_at": "2026-08-01T09:00"},
                jobs=JOBS,
                confirm=True,
            )
        )

        assert out["applied"] is False
        assert "500" in out["error"]
        assert rec.signature() == [("POST", "queues"), ("POST", "jobs"), ("POST", "behaviors")]

    def test_remote_target_is_refused_without_touching_the_api(
        self, monkeypatch: pytest.MonkeyPatch, mock_settings, api: RecordingAPI
    ) -> None:
        # pm_compose_run carries its own copy of the guard (it does not go through _write).
        remote = mock_settings.model_copy(
            update={"pm_api_base_url": "https://api.primemover.example/api/v1"}
        )
        monkeypatch.setattr(s, "get_settings", lambda: remote)

        out = json.loads(
            s.pm_compose_run(
                crawler_id=14,
                run={"name": "r", "start_at": "2026-08-01T09:00"},
                jobs=JOBS,
                confirm=True,
            )
        )

        assert api.calls == []
        assert "MCP writes are restricted to the local dev API" in out["error"]
        assert "api.primemover.example" in out["error"]


# =============================================================================
# Cross-cutting: none of the eight tools writes without confirm=true
# =============================================================================


def test_no_write_tool_mutates_without_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The single most important guarantee of this module, asserted for all eight tools."""
    rec = RecordingAPI([], default={"data": []})
    monkeypatch.setattr(s, "_api_request", rec)

    invocations = [
        lambda: s.pm_create_experiment(name="n", contact="c"),
        lambda: s.pm_add_bot(experiment_id=1, name="n", agent_id=1, proxy_id=1, configuration_id=1),
        lambda: s.pm_create_run(crawler_id=1, name="n", start_at="2026-08-01T09:00"),
        lambda: s.pm_activate_run(queue_id=1),
        lambda: s.pm_add_job(queue_id=1, type="visitjob", name="n"),
        lambda: s.pm_set_behaviour(job_id=1, name="url", value="v"),
        lambda: s.pm_delete(entity="run", id=1),
        lambda: s.pm_compose_run(
            crawler_id=1,
            run={"name": "n", "start_at": "2026-08-01T09:00"},
            jobs=[{"type": "visitjob", "name": "n"}],
        ),
    ]

    for call in invocations:
        out = json.loads(call())
        assert out.get("dry_run") is True, out
        assert out.get("to_apply") == "call again with confirm=true"

    assert rec.writes == []
