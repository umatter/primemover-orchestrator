from datetime import datetime, timedelta

import pytest

from experiment_designer.lock.api import ApiError
from experiment_designer.lock.cleanup import (
    CleanupError,
    delete_experiments,
    find_test_experiments,
    parse_age,
)
from experiment_designer.spec.time_utils import API_TIMEZONE

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=API_TIMEZONE)


class FakeSession:
    base_url = "http://fake"

    def __init__(self, experiments, fail_delete=None):
        self.experiments = experiments  # id -> {"name", "prefs"}
        self.deleted, self.calls, self.fail_delete = [], [], fail_delete

    def get(self, path, params=None):
        self.calls.append(("GET", path))
        if path == "experiments/experiments-list":
            return [{"id": i, "name": e["name"]} for i, e in self.experiments.items()]
        i = int(path.split("/")[1])
        return {
            "id": i,
            "name": self.experiments[i]["name"],
            "preferences": self.experiments[i]["prefs"],
        }

    def delete(self, path):
        self.calls.append(("DELETE", path))
        i = int(path.split("/")[1])
        if i == self.fail_delete:
            raise ApiError("DELETE failed", status=500)
        self.deleted.append(i)
        return {}


def _p(mode, uploaded_at=None):
    prefs = [{"name": "pmspec.mode", "description": "upload 1", "value": mode}]
    if uploaded_at:
        prefs.append(
            {"name": "pmspec.uploaded_at", "description": "upload 1", "value": uploaded_at}
        )
    return prefs


EXPS = {
    1: {"name": "test-Study-20260901", "prefs": _p("test", "2026-09-01T10:00:00+02:00")},
    2: {"name": "test-Study-20260911", "prefs": _p("test", "2026-09-11T10:00:00+02:00")},
    3: {"name": "test-looks-like-one", "prefs": _p("real")},  # name lies; preference says real
    4: {"name": "Real study", "prefs": _p("real")},
    5: {"name": "test-no-provenance", "prefs": []},  # never uploaded by pmspec
}


def test_only_pmspec_test_experiments_are_candidates():
    fake = FakeSession(EXPS)
    ids = [c.experiment_id for c in find_test_experiments(fake, now=NOW)]
    assert ids == [1, 2]
    assert ("GET", "experiments/4") not in fake.calls  # non test- names are never fetched


def test_older_than_filters_on_uploaded_at():
    ids = [
        c.experiment_id
        for c in find_test_experiments(FakeSession(EXPS), older_than=timedelta(days=7), now=NOW)
    ]
    assert ids == [1]


def test_pure_test_candidate_reports_mode_test():
    candidates = find_test_experiments(FakeSession(EXPS), now=NOW)
    assert candidates and all(c.mode == "test" for c in candidates)


def test_mixed_mode_experiment_is_not_a_candidate():
    """A test experiment later appended to with --mode real is not test-only anymore.

    "cannot tell -> keep" applies to authorisation, not just to age: an experiment whose
    pmspec.mode history is {test, real} must never be deleted just because one of its
    uploads happened to be a test one.
    """
    prefs = [
        {"name": "pmspec.mode", "description": "upload 1", "value": "test"},
        {"name": "pmspec.mode", "description": "upload 2", "value": "real"},
    ]
    fake = FakeSession({1: {"name": "test-Study-mixed", "prefs": prefs}})
    assert find_test_experiments(fake, now=NOW) == []


def test_naive_uploaded_at_is_unparseable_not_an_exception():
    """A naive timestamp (no offset) must not raise -- and must not count as "old enough"."""
    prefs = [
        {"name": "pmspec.mode", "description": "upload 1", "value": "test"},
        {"name": "pmspec.uploaded_at", "description": "upload 1", "value": "2026-09-11T10:00:00"},
    ]
    fake = FakeSession({1: {"name": "test-naive", "prefs": prefs}})

    # Kept when no age filter is given: the timestamp is unparseable, not "old".
    candidates = find_test_experiments(fake, now=NOW)
    assert len(candidates) == 1
    assert candidates[0].uploaded_at is None

    # Excluded once an age filter is given: "cannot tell" must not be treated as "old enough".
    ids = [
        c.experiment_id for c in find_test_experiments(fake, older_than=timedelta(days=7), now=NOW)
    ]
    assert ids == []


def test_delete_deletes_only_the_given_ids():
    fake = FakeSession(EXPS)
    assert delete_experiments(fake, [1, 2]) == [1, 2]
    assert fake.calls[-2:] == [
        ("DELETE", "experiments/1/destroy"),
        ("DELETE", "experiments/2/destroy"),
    ]


def test_delete_stops_at_first_failure_and_reports_progress():
    fake = FakeSession(EXPS, fail_delete=2)
    with pytest.raises(CleanupError) as exc:
        delete_experiments(fake, [1, 2])
    assert exc.value.deleted == [1] and exc.value.failed_id == 2


@pytest.mark.parametrize(
    "text,delta",
    [("7d", timedelta(days=7)), ("36h", timedelta(hours=36)), ("90m", timedelta(minutes=90))],
)
def test_parse_age(text, delta):
    assert parse_age(text) == delta


def test_parse_age_rejects_garbage():
    with pytest.raises(ValueError):
        parse_age("soon")
