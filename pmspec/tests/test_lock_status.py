"""status: what was uploaded (provenance), and how it is running (health), on one screen."""

from pmspec.lock.status import experiment_status


class FakeSession:
    base_url = "http://fake"

    def __init__(self, prefs, health, name="Study", runner=None):
        self.prefs, self.health, self.name, self.runner = prefs, health, name, runner
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == "experiments/181":
            return {
                "id": 181,
                "name": self.name,
                "user_id": 5,
                "runner_user_id": self.runner,
                "preferences": self.prefs,
                "crawlers": [],
            }
        if path == "experiments/181/health":
            return self.health
        raise AssertionError(path)


def _prefs(label, **values):
    return [{"name": f"pmspec.{k}", "description": label, "value": v} for k, v in values.items()]


HEALTH = {
    "experiment_id": 181,
    "window_hours": 24,
    "bots": 3,
    "runs": {"success": 5, "failure": 1},
    "jobs": {"success": 40, "warning": 2},
    "bad_runs": 1,
    "bad_jobs": 2,
    "treatment_lost_jobs": 0,
    "degraded_jobs": 2,
    "last_finished_at": "2026-09-12 06:00:00",
}


def test_groups_provenance_by_upload_label():
    prefs = _prefs(
        "upload 1",
        lock_sha256="a" * 64,
        mode="test",
        git_commit="abc1234",
        anchor="2026-09-11T10:00:00+02:00",
    ) + _prefs(
        "upload 2",
        lock_sha256="b" * 64,
        mode="test",
        git_commit="abc1234-dirty",
        anchor="2026-09-12T10:00:00+02:00",
    )
    s = experiment_status(FakeSession(prefs, HEALTH, runner=3), 181)
    assert [u.label for u in s.uploads] == ["upload 1", "upload 2"]
    assert s.uploads[1].values["git_commit"] == "abc1234-dirty"
    assert s.runner_user_id == 3 and s.bound_to_any_lock and s.verify is None
    text = s.render()
    assert "upload 2" in text and "aaaaaaaaaaaa" in text and "runner=3" in text
    assert "treatment_lost_jobs=0" in text and "bad_runs=1" in text


def test_hours_is_passed_to_health():
    fake = FakeSession(_prefs("upload 1", lock_sha256="a" * 64), HEALTH)
    experiment_status(fake, 181, hours=6)
    assert ("experiments/181/health", {"hours": 6}) in fake.calls


def test_no_provenance_is_said_not_assumed():
    s = experiment_status(FakeSession([], HEALTH), 181)
    assert s.uploads == [] and not s.bound_to_any_lock
    assert "not uploaded by pmspec" in s.render()


def test_absent_decision_counts_render_as_unknown():
    health = {k: v for k, v in HEALTH.items() if k not in ("treatment_lost_jobs", "degraded_jobs")}
    s = experiment_status(FakeSession(_prefs("upload 1", lock_sha256="a" * 64), health), 181)
    assert "treatment_lost_jobs=unknown" in s.render()
    assert s.ok  # unknown is not a failure


def test_no_provenance_is_not_ok():
    """Spec §5: exit 1 when the experiment carries no pmspec provenance at all --
    a healthy experiment nobody can tie to a lock is not a clean answer."""
    s = experiment_status(FakeSession([], HEALTH), 181)
    assert not s.bound_to_any_lock and not s.ok


def test_with_a_lock_the_verify_verdict_decides(monkeypatch):
    from pmspec.lock import status as status_mod

    class V:
        ok = True
        bound = True
        differences = []
        extra_live_crawlers = []

        def render(self):
            return "BOUND x"

    monkeypatch.setattr(status_mod, "verify_lock", lambda *a, **k: V())
    s = experiment_status(FakeSession([], HEALTH), 181, lock_text="pmspec_lock: 1\n")
    assert s.verify is not None and s.ok


def test_treatment_lost_makes_status_not_ok():
    s = experiment_status(FakeSession([], {**HEALTH, "treatment_lost_jobs": 1}), 181)
    assert not s.ok and "treatment_lost_jobs=1" in s.render()


def test_with_a_lock_runs_verify(monkeypatch):
    from pmspec.lock import status as status_mod

    class V:
        ok = False
        bound = False
        differences = []
        extra_live_crawlers = []

        def render(self):
            return "UNBOUND x"

    monkeypatch.setattr(status_mod, "verify_lock", lambda *a, **k: V())
    s = experiment_status(FakeSession([], HEALTH), 181, lock_text="pmspec_lock: 1\n")
    assert s.verify is not None and not s.ok and "UNBOUND x" in s.render()


# --- a runs upload ---------------------------------------------------------


FLEET = {
    "lock_sha256": "a" * 64,
    "mode": "real",
    "anchor": "2026-10-01T10:00:00+02:00",
    "uploaded_at": "2026-10-01T10:00:00+02:00",
}


def test_a_runs_upload_shows_its_kind_and_base():
    prefs = _prefs("upload 1", **FLEET) + _prefs(
        "upload 2",
        lock_sha256="b" * 64,
        mode="real",
        kind="runs",
        base_lock_sha256="a" * 64,
        updates='{"bot_01": {"preferences.ideology": ["0.10", "0.90"]}}',
        uploaded_at="2026-10-02T09:00:00+02:00",
    )
    text = experiment_status(FakeSession(prefs, HEALTH), 181).render()
    assert "kind=runs base=aaaaaaaaaaaa" in text
    assert "updates=1" in text
    assert "uploads: 1 fleet + 1 runs, last 2026-10-02T09:00:00+02:00" in text


def test_a_fleet_only_experiment_counts_no_runs():
    text = experiment_status(FakeSession(_prefs("upload 1", **FLEET), HEALTH), 181).render()
    assert "uploads: 1 fleet, last 2026-10-01T10:00:00+02:00" in text
    assert "kind=runs" not in text


def test_the_clipped_updates_blob_renders_as_yes():
    """Past 255 characters the upload records a pointer at the lock, not the
    diff; the count is gone, so status says there were updates and no more."""
    prefs = _prefs("upload 1", **FLEET) + _prefs(
        "upload 2",
        lock_sha256="b" * 64,
        kind="runs",
        base_lock_sha256="a" * 64,
        updates="<40 crawlers updated; see lock>",
    )
    assert "updates=yes" in experiment_status(FakeSession(prefs, HEALTH), 181).render()
