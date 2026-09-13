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
    # A job still running has not had a chance to report anything: never a
    # failure, strict or not (the design doc's rule; unlike unreported/
    # degraded/missing, --strict does not escalate it).
    assert result.ok(strict=False)
    assert result.ok(strict=True)


def test_missing():
    result = audit_experiment(FakeSession({}, missing_job_t=True), EXPERIMENT_ID, LOCK_TEXT)
    finding = _bot_t_finding(result)
    assert finding.outcome == "missing"
    assert finding.job_id is None
    assert result.ok(strict=False)
    assert not result.ok(strict=True)


class MissingCrawlerSession(FakeSession):
    """Serves an experiment where bot_t was never created (wrong experiment
    id, or a partial upload) -- ``experiments/{id}`` lists only bot_c."""

    def get(self, path, params=None):
        if path == f"experiments/{EXPERIMENT_ID}":
            self.calls.append(path)
            return {"id": EXPERIMENT_ID, "crawlers": [{"id": 31, "name": "bot_c"}]}
        return super().get(path, params)


def test_missing_crawler_is_reported_not_skipped():
    job2 = {"status_code": "success"}  # never read: bot_t has no live crawler at all
    result = audit_experiment(MissingCrawlerSession(job2), EXPERIMENT_ID, LOCK_TEXT)
    finding = _bot_t_finding(result)
    assert finding.outcome == "missing"
    assert finding.detail == "no live crawler"
    assert finding.job_id is None
    assert finding.arm == TREATED_ARM
    assert result.ok(strict=False)
    assert not result.ok(strict=True)
    # bot_c is unaffected and still audited normally.
    assert any(f.crawler == "bot_c" for f in result.findings)


def test_report_derives_requested_applied_from_filters_only():
    # Every backfilled row, and the decision function's own payload, carry
    # only `filters` -- never `requested`/`applied` directly.
    job2 = {
        "status_code": "success",
        "decision_report": {"filters": {"mainstream_media": "ok", "similar": "ok"}},
        "decision_treatment_lost": False,
    }
    result = audit_experiment(FakeSession(job2), EXPERIMENT_ID, LOCK_TEXT)
    finding = _bot_t_finding(result)
    assert finding.outcome == "ok"


def test_report_derives_degraded_from_filters_only():
    job2 = {
        "status_code": "success",
        "decision_report": {"filters": {"mainstream_media": "ok", "similar": "failed: xpath"}},
        "decision_treatment_lost": False,
    }
    result = audit_experiment(FakeSession(job2), EXPERIMENT_ID, LOCK_TEXT)
    finding = _bot_t_finding(result)
    assert finding.outcome == "degraded"


def test_report_with_neither_requested_nor_filters_is_unreported():
    job2 = {
        "status_code": "success",
        "decision_report": {},
        "decision_treatment_lost": False,
    }
    result = audit_experiment(FakeSession(job2), EXPERIMENT_ID, LOCK_TEXT)
    finding = _bot_t_finding(result)
    assert finding.outcome == "unreported"


def test_ok_finding_carries_degraded_in_detail_when_verdict_says_so():
    # requested <= applied and not treatment_lost -- but the decision
    # function's own `degraded` flag is still set (e.g. selection failed for
    # an unrelated reason). Outcome stays "ok"; the flag must not be silent.
    job2 = {
        "status_code": "success",
        "decision_degraded": True,
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
    assert "degraded" in finding.detail


class OldQueueSession(FakeSession):
    """bot_t's queue finished long before the audit window; bot_c's did not."""

    def get(self, path, params=None):
        if path == "jobs" and (params or {}).get("filter[queue]") == "500":
            raise AssertionError(
                "bot_t's queue is outside the window; its jobs must not be fetched"
            )
        result = super().get(path, params)
        if (
            path == f"experiments/{EXPERIMENT_ID}/queues-unprocessed"
            and (params or {}).get("page", 1) == 1
        ):
            for row in result:
                if row["crawler_id"] == 30:
                    row["finished_at"] = "2020-01-01T00:00:00+00:00"
        return result


def test_queue_outside_the_window_is_skipped_before_fetching_its_jobs():
    job2 = {
        "status_code": "success",
        "decision_report": {
            "requested": ["mainstream_media", "similar"],
            "applied": ["mainstream_media", "similar"],
            "filters": {"mainstream_media": "ok", "similar": "ok"},
        },
        "decision_treatment_lost": False,
    }
    result = audit_experiment(OldQueueSession(job2), EXPERIMENT_ID, LOCK_TEXT, hours=24)
    assert not any(f.crawler == "bot_t" for f in result.findings)
    assert any(f.crawler == "bot_c" and f.outcome == "ok" for f in result.findings)


def _crawler_with_jobs(name: str, jobs: list[dict]) -> dict:
    return {
        "name": name,
        "description": "d",
        "configuration": {
            "name": "c",
            "description": "d",
            "active": 1,
            "params": {"pi": 0, "psi": 0, "kappa": 0, "tau": 0},
            "preferences": [],
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
                "jobs": jobs,
            }
        ],
    }


def make_pagination_lock_text(n: int) -> str:
    jobs = [
        _job(f"select{i}", "lambdaselect", [_behavior("strategy", "youtube_v1")]) for i in range(n)
    ]
    crawler = _crawler_with_jobs("bot_p", jobs)
    lock = Lock(
        provenance=Provenance(
            spec_path="design/spec.yaml",
            spec_sha256="cd" * 32,
            generator=None,
            git_commit=None,
            pmspec_version="0.1.0",
            seed=2,
        ),
        experiment={
            "name": "Pagination",
            "description": "",
            "runner_user_id": None,
            "contact": None,
        },
        schedule={"anchor": "upload"},
        crawlers=[crawler],
    )
    return dump_lock(lock)


class PaginatingSession:
    """A queue with 20 jobs, served exactly like the real API defaults: a
    request with no explicit ``per_page`` gets back only 15, newest-id-first."""

    base_url = "http://fake"

    def __init__(self, n_jobs: int) -> None:
        self.n_jobs = n_jobs
        self.job_page_requests: list[dict] = []

    def get(self, path, params=None):
        params = params or {}
        if path == f"experiments/{EXPERIMENT_ID}":
            return {"id": EXPERIMENT_ID, "crawlers": [{"id": 40, "name": "bot_p"}]}
        if path == f"experiments/{EXPERIMENT_ID}/queues-unprocessed":
            if params.get("page", 1) > 1:
                return []
            return [{"id": 600, "name": "s1", "crawler_id": 40, "start_at": "2026-09-13 09:00:00"}]
        if path == f"experiments/{EXPERIMENT_ID}/queues-processed":
            return []
        if path == "jobs":
            assert params.get("filter[queue]") == "600"
            self.job_page_requests.append(dict(params))
            per_page = params.get("per_page", 15)
            page = params.get("page", 1)
            all_jobs = [
                {
                    "id": i + 1,
                    "order": i + 1,
                    "status_code": "success",
                    "decision_report": {"requested": [], "applied": [], "filters": {}},
                    "decision_treatment_lost": False,
                }
                for i in range(self.n_jobs)
            ]
            start = (page - 1) * per_page
            return {"data": all_jobs[start : start + per_page]}
        raise AssertionError(path)


def test_queue_with_more_than_fifteen_jobs_is_fully_paginated():
    session = PaginatingSession(20)
    lock_text = make_pagination_lock_text(20)
    result = audit_experiment(session, EXPERIMENT_ID, lock_text)
    ordinals = sorted(f.ordinal for f in result.findings if f.crawler == "bot_p")
    assert ordinals == list(range(20))
    assert all(f.outcome == "ok" for f in result.findings if f.crawler == "bot_p")
    # The fix requests a page size well above the real API's own default of
    # 15 -- without it, this queue would have been truncated to 15 jobs.
    assert any(req.get("per_page", 0) > 15 for req in session.job_page_requests)


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


def test_expected_filters_custom_strategy_treats_false_and_zero_float_as_not_requested():
    # A treatment value can arrive already typed (bool/float) rather than as
    # the string a lock behaviour normally carries; str(False) == "False" and
    # str(0.0) == "0.0" would otherwise slip past the "empty-ish" string check.
    treatment = {"foo": False, "bar": 0.0, "baz": "1"}
    assert expected_filters("custom_x", treatment) == {"baz"}
