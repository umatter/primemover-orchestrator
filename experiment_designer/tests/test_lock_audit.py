"""audit: did each finished lambda job apply the treatment its lock says it should?"""

import random

from experiment_designer.lock.audit import audit_experiment, expected_filters
from experiment_designer.lock.io import dump_lock
from experiment_designer.lock.model import Lock, Provenance

EXPERIMENT_ID = 9


def _proxy() -> dict:
    return {
        "name": "p",
        "description": "d",
        "type": "http",
        "hostname": "pr.oxylabs.io",
        "port": 7777,
        "username": "${secret:oxylabs.a.username}",
        "password": "${secret:oxylabs.a.password}",
        "active": True,
    }


def _agent() -> dict:
    return {"name": "a", "identification": "mlx", "location": "US-NC", "active": True}


def _behavior(name: str, value) -> dict:
    return {"name": name, "value": str(value), "description": "", "active": True}


def _job(name: str, job_type: str, behaviors: list[dict]) -> dict:
    return {
        "name": name,
        "type": job_type,
        "description": "d",
        "active": True,
        "behaviors": behaviors,
    }


def _crawler(
    name: str, *, preferences: list[dict], params: dict, lambda_behaviors: list[dict]
) -> dict:
    return {
        "name": name,
        "description": "d",
        "configuration": {
            "name": "c",
            "description": "d",
            "active": 1,
            "params": params,
            "preferences": preferences,
        },
        "proxy": _proxy(),
        "agent": _agent(),
        "queues": [
            {
                "name": "s1",
                "description": "d",
                "start_offset": "+5m",
                "timezone": "Europe/Zurich",
                "timezone_utc_offset": "+02:00",
                "active": True,
                "jobs": [
                    _job("visit", "visitjob", [_behavior("url", "https://x")]),
                    _job("select", "lambdaselect", lambda_behaviors),
                ],
            }
        ],
    }


def make_lock_text() -> str:
    bot_t = _crawler(
        "bot_t",
        preferences=[{"name": "reliability", "value": "green"}, {"name": "lean", "value": "left"}],
        params={"pi": 0, "psi": 0, "kappa": 0, "tau": 0},
        lambda_behaviors=[
            _behavior("strategy", "youtube_v1"),
            _behavior("pi", 1),
            _behavior("kappa", 1),
        ],
    )
    bot_c = _crawler(
        "bot_c",
        preferences=[],
        params={"pi": 0, "psi": 0, "kappa": 0, "tau": 0},
        lambda_behaviors=[
            _behavior("strategy", "youtube_v1"),
            _behavior("pi", 0),
            _behavior("psi", 0),
            _behavior("kappa", 0),
            _behavior("tau", 0),
        ],
    )
    lock = Lock(
        provenance=Provenance(
            spec_path="design/spec.yaml",
            spec_sha256="ab" * 32,
            generator=None,
            git_commit=None,
            pmspec_version="0.1.0",
            seed=1,
        ),
        experiment={"name": "Study", "description": "", "runner_user_id": None, "contact": None},
        schedule={"anchor": "upload"},
        crawlers=[bot_t, bot_c],
    )
    return dump_lock(lock)


LOCK_TEXT = make_lock_text()

CONTROL_ARM = "pi=0,psi=0,kappa=0,tau=0"
TREATED_ARM = "green/left"


class FakeSession:
    """Serves one experiment with two crawlers, one queue each, two jobs each.

    ``job2_t`` is the live record for bot_t's lambda job (order 2); bot_c's
    lambda job is always a clean, fully-applied control so every scenario has
    a healthy baseline alongside the case under test. ``missing_job_t`` drops
    bot_t's lambda job entirely, to exercise the ``missing`` outcome.
    """

    base_url = "http://fake"

    def __init__(self, job2_t: dict, *, missing_job_t: bool = False) -> None:
        self.job2_t = job2_t
        self.missing_job_t = missing_job_t
        self.calls: list[str] = []

    def get(self, path, params=None):
        self.calls.append(path)
        if path == f"experiments/{EXPERIMENT_ID}":
            return {
                "id": EXPERIMENT_ID,
                "crawlers": [
                    {"id": 30, "name": "bot_t"},
                    {"id": 31, "name": "bot_c"},
                ],
            }
        if path == f"experiments/{EXPERIMENT_ID}/queues-unprocessed":
            if (params or {}).get("page", 1) > 1:
                return []
            return [
                {"id": 500, "name": "s1", "crawler_id": 30, "start_at": "2026-09-13 09:00:00"},
                {"id": 501, "name": "s1", "crawler_id": 31, "start_at": "2026-09-13 09:00:00"},
            ]
        if path == f"experiments/{EXPERIMENT_ID}/queues-processed":
            return []
        if path == "jobs":
            queue_id = int((params or {})["filter[queue]"])
            if queue_id == 500:
                jobs = [{"id": 900, "order": 1, "status_code": "success"}]
                if not self.missing_job_t:
                    jobs.append({"id": 901, "order": 2, **self.job2_t})
                return jobs
            if queue_id == 501:
                return [
                    {"id": 910, "order": 1, "status_code": "success"},
                    {
                        "id": 911,
                        "order": 2,
                        "status_code": "success",
                        "decision_report": {"requested": [], "applied": [], "filters": {}},
                        "decision_treatment_lost": False,
                    },
                ]
            raise AssertionError(queue_id)
        raise AssertionError(path)


def _bot_t_finding(result):
    matches = [f for f in result.findings if f.crawler == "bot_t" and f.ordinal == 1]
    assert len(matches) == 1
    return matches[0]


def test_ok():
    job2 = {
        "status_code": "success",
        "decision_report": {
            "requested": ["mainstream_media", "similar"],
            "applied": ["mainstream_media", "similar"],
            "filters": {"mainstream_media": "ok", "similar": "ok"},
        },
        "decision_treatment_lost": False,
    }
    result = audit_experiment(FakeSession(job2), EXPERIMENT_ID, LOCK_TEXT)
    finding = _bot_t_finding(result)
    assert finding.outcome == "ok"
    assert finding.arm == TREATED_ARM
    assert finding.job_id == 901
    assert result.counts()[(TREATED_ARM, "ok")] == 1
    assert result.counts()[(CONTROL_ARM, "ok")] == 1
    assert result.ok(strict=False)
    assert result.ok(strict=True)


def test_unreported():
    job2 = {"status_code": "success"}
    result = audit_experiment(FakeSession(job2), EXPERIMENT_ID, LOCK_TEXT)
    finding = _bot_t_finding(result)
    assert finding.outcome == "unreported"
    assert result.ok(strict=False)
    assert not result.ok(strict=True)


def test_mismatch():
    job2 = {
        "status_code": "success",
        "decision_report": {
            "requested": ["mainstream_media"],
            "applied": ["mainstream_media"],
            "filters": {"mainstream_media": "ok"},
        },
        "decision_treatment_lost": False,
    }
    result = audit_experiment(FakeSession(job2), EXPERIMENT_ID, LOCK_TEXT)
    finding = _bot_t_finding(result)
    assert finding.outcome == "mismatch"
    assert "similar" in finding.detail
    assert not result.ok(strict=False)
    assert not result.ok(strict=True)


def test_lost():
    job2 = {
        "status_code": "success",
        "decision_report": {
            "requested": ["mainstream_media", "similar"],
            "applied": [],
            "filters": {},
        },
        "decision_treatment_lost": True,
    }
    result = audit_experiment(FakeSession(job2), EXPERIMENT_ID, LOCK_TEXT)
    finding = _bot_t_finding(result)
    assert finding.outcome == "lost"
    assert not result.ok(strict=False)
    assert not result.ok(strict=True)


def test_degraded():
    job2 = {
        "status_code": "success",
        "decision_report": {
            "requested": ["mainstream_media", "similar"],
            "applied": ["mainstream_media"],
            "filters": {"similar": "failed: xpath"},
        },
        "decision_treatment_lost": False,
    }
    result = audit_experiment(FakeSession(job2), EXPERIMENT_ID, LOCK_TEXT)
    finding = _bot_t_finding(result)
    assert finding.outcome == "degraded"
    assert result.ok(strict=False)
    assert not result.ok(strict=True)


def test_unfinished():
    job2 = {"status_code": "running"}
    result = audit_experiment(FakeSession(job2), EXPERIMENT_ID, LOCK_TEXT)
    finding = _bot_t_finding(result)
    assert finding.outcome == "unfinished"
    assert result.ok(strict=False)
    assert not result.ok(strict=True)


def test_missing():
    result = audit_experiment(FakeSession({}, missing_job_t=True), EXPERIMENT_ID, LOCK_TEXT)
    finding = _bot_t_finding(result)
    assert finding.outcome == "missing"
    assert finding.job_id is None
    assert result.ok(strict=False)
    assert not result.ok(strict=True)


def test_sample_audits_one_crawler():
    job2 = {
        "status_code": "success",
        "decision_report": {
            "requested": ["mainstream_media", "similar"],
            "applied": ["mainstream_media", "similar"],
            "filters": {"mainstream_media": "ok", "similar": "ok"},
        },
        "decision_treatment_lost": False,
    }
    result = audit_experiment(FakeSession(job2), EXPERIMENT_ID, LOCK_TEXT, sample=1)
    assert result.sampled == 1
    assert result.total == 2
    crawlers = {f.crawler for f in result.findings}
    assert len(crawlers) == 1
    expected_pick = random.Random(EXPERIMENT_ID).sample(["bot_c", "bot_t"], 1)[0]
    assert crawlers == {expected_pick}


def test_render_contains_counts_and_first_offending_job_id():
    job2 = {
        "status_code": "success",
        "decision_report": {
            "requested": ["mainstream_media"],
            "applied": ["mainstream_media"],
            "filters": {"mainstream_media": "ok"},
        },
        "decision_treatment_lost": False,
    }
    result = audit_experiment(FakeSession(job2), EXPERIMENT_ID, LOCK_TEXT)
    text = result.render()
    assert "mismatch" in text
    assert "901" in text
    assert TREATED_ARM in text
    assert CONTROL_ARM in text


# --- expected_filters ------------------------------------------------------


def test_expected_filters_youtube_v1_table():
    treatment = {"pi": 1, "psi": 1, "kappa": 1, "tau": 1, "beta": 1}
    assert expected_filters("youtube_v1", treatment) == {
        "mainstream_media",
        "children_entertainment",
        "similar",
        "political",
        "fringe",
    }
    off = {"pi": 0, "psi": 0, "kappa": 0, "tau": 0, "beta": 0}
    assert expected_filters("youtube_v1", off) == set()


def test_expected_filters_youtube_v1_beta_negative_requests_fringe():
    assert expected_filters("youtube_v1", {"beta": -1}) == {"fringe"}


def test_expected_filters_custom_strategy_default():
    treatment = {"foo": "1", "bar": "0", "baz": ""}
    assert expected_filters("custom_x", treatment) == {"foo"}


def test_expected_filters_custom_strategy_with_x_requested_schema():
    schema = {"x-requested": {"foo": "filterA", "other": "filterB"}}
    treatment = {"foo": "1", "bar": "1"}
    # "bar" is truthy but absent from the mapping, so it is ignored.
    assert expected_filters("custom_x", treatment, schema) == {"filterA"}
