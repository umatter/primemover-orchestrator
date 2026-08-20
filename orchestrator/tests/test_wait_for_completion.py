"""
Tests for E2ETestRunner._wait_for_completion phase budgets.

The wait for an e2e queue covers two very different things: how long the runner
takes to *claim* the queue (driven by its `runner.UpdateInterval` poll schedule)
and how long the jobs take once claimed. These tests pin down that the two have
separate budgets and that a timeout says which phase ran out.

NOTE: this file must not be named ``test_e2e*`` -- conftest skips those unless
``--e2e`` is passed, and these tests are pure unit tests with a fake clock.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from orchestrator.e2e.runner import (
    TEST_TYPES,
    E2ETestRunner,
    QueueExecutionTimeoutError,
    QueuePickupTimeoutError,
    ValidationResult,
)

START = 1000.0
QUEUE_ID = 633


class FakeClock:
    """Virtual clock: only ``sleep()`` advances time, so tests run instantly."""

    def __init__(self) -> None:
        self.now = START

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    @property
    def elapsed(self) -> float:
        return self.now - START


def unclaimed_queue() -> dict[str, Any]:
    """A queue the runner has not touched yet (API returns nulls, not 'pending')."""
    return {
        "id": QUEUE_ID,
        "status_code": None,
        "status_message": None,
        "started_at": None,
        "finished_at": None,
        "processing": False,
        "jobs": [],
    }


def running_queue() -> dict[str, Any]:
    return {
        "id": QUEUE_ID,
        "status_code": "running",
        "status_message": "in progress",
        "started_at": "2026-08-18T22:18:41",
        "processing": True,
        "jobs": [{"id": 1, "type": "visit", "status_code": "running"}],
    }


def finished_queue() -> dict[str, Any]:
    return {
        "id": QUEUE_ID,
        "status_code": "success",
        "status_message": "done",
        "started_at": "2026-08-18T22:18:41",
        "finished_at": "2026-08-18T22:19:50",
        "processing": False,
        "jobs": [{"id": 1, "type": "visit", "status_code": "success", "duration": 88.0}],
    }


def run_wait(
    runner: E2ETestRunner,
    queue_for: Callable[[float], dict[str, Any]],
    polls: list[float] | None = None,
) -> tuple[Any, list[float]]:
    """Drive ``_wait_for_completion`` against a scripted queue timeline.

    ``queue_for(elapsed_seconds)`` returns the queue snapshot the API would
    serve at that point. Returns (result, poll times) or re-raises. Pass
    ``polls`` to inspect the timeline of a call that is expected to raise.
    """
    clock = FakeClock()
    poll_times: list[float] = [] if polls is None else polls

    class _FakeAPIClient:
        def get_queue(self, queue_id: int) -> dict[str, Any]:
            poll_times.append(clock.elapsed)
            return queue_for(clock.elapsed)

        def close(self) -> None:
            pass

    with (
        patch("orchestrator.e2e.runner.time", clock),
        patch("orchestrator.e2e.runner.APIClient", _FakeAPIClient),
    ):
        return runner._wait_for_completion(QUEUE_ID), poll_times


class TestPhaseBudgets:
    """The pickup wait must not eat into the execution budget."""

    def test_slow_pickup_plus_normal_execution_succeeds(self):
        """Reproduces the reported incident: 112s pickup + 88s execution = 181s
        total, which the single 180s budget failed even though the queue was a
        success. With separate budgets this passes."""
        runner = E2ETestRunner(test_type="chrome")
        assert runner.timeout == 180  # execution budget unchanged

        def queue_for(elapsed: float) -> dict[str, Any]:
            if elapsed < 112:
                return unclaimed_queue()
            if elapsed < 200:
                return running_queue()
            return finished_queue()

        jobs, polls = run_wait(runner, queue_for)

        assert [j["status_code"] for j in jobs] == ["success"]
        assert polls[-1] >= 200  # it really did wait past the old 180s budget

    def test_execution_budget_starts_at_pickup_not_at_upload(self):
        """A queue picked up at 100s gets its full execution budget from 100s."""
        runner = E2ETestRunner(test_type="chrome", timeout=60)

        def queue_for(elapsed: float) -> dict[str, Any]:
            if elapsed < 100:
                return unclaimed_queue()
            if elapsed < 155:
                return running_queue()
            return finished_queue()

        jobs, _ = run_wait(runner, queue_for)
        assert jobs  # 155s total wall time under a 60s execution budget

    def test_terminal_success_on_the_deadline_poll_wins(self):
        """Poll-then-check ordering: the queue is read before the deadline is
        judged, so a success observed at the deadline is not thrown away."""
        runner = E2ETestRunner(test_type="chrome", timeout=180)

        def queue_for(elapsed: float) -> dict[str, Any]:
            return running_queue() if elapsed < 185 else finished_queue()

        jobs, _ = run_wait(runner, queue_for)
        assert jobs


class TestFailureMessages:
    """A timeout must say which phase ran out."""

    def test_never_picked_up_names_the_pickup_phase(self):
        runner = E2ETestRunner(test_type="chrome", pickup_timeout=60)

        with pytest.raises(QueuePickupTimeoutError) as exc:
            run_wait(runner, lambda elapsed: unclaimed_queue())

        message = str(exc.value)
        assert "never picked up" in message
        assert "60s" in message
        assert "No jobs ran" in message
        # The API serves status_code: null before pickup; it must not leak out
        # as the literal "None" the way dict.get(k, default) used to let it.
        assert "None" not in message
        assert "not set" in message

    def test_pickup_timeout_is_not_charged_the_execution_budget(self):
        """A dead runner fails at pickup_timeout and does not also sit through
        the (much larger, for youtube) execution budget."""
        runner = E2ETestRunner(test_type="youtube", pickup_timeout=60)
        assert runner.timeout == 600
        polls: list[float] = []

        with pytest.raises(QueuePickupTimeoutError):
            run_wait(runner, lambda elapsed: unclaimed_queue(), polls)

        assert polls[-1] <= 60 + 5  # gave up one poll after the pickup budget

    def test_picked_up_but_slow_says_it_may_still_be_progressing(self):
        runner = E2ETestRunner(test_type="chrome", timeout=60)

        def queue_for(elapsed: float) -> dict[str, Any]:
            return unclaimed_queue() if elapsed < 20 else running_queue()

        with pytest.raises(QueueExecutionTimeoutError) as exc:
            run_wait(runner, queue_for)

        message = str(exc.value)
        assert "picked up after 20s" in message
        assert "60s" in message
        assert "may still be progressing" in message
        assert f"orchestrator trace queue {QUEUE_ID}" in message
        assert "--timeout" in message
        # Must NOT accuse the runner of being broken; it was executing fine.
        assert "never picked up" not in message

    def test_both_timeouts_are_timeout_errors(self):
        assert issubclass(QueuePickupTimeoutError, TimeoutError)
        assert issubclass(QueueExecutionTimeoutError, TimeoutError)


class TestTerminalStates:
    def test_skipped_queue_fails_immediately(self):
        """SKIPPED_* is terminal (processed=true), so waiting out the budget
        would only turn a precise diagnosis into a generic timeout."""
        runner = E2ETestRunner(test_type="chrome")

        def queue_for(elapsed: float) -> dict[str, Any]:
            return {
                "id": QUEUE_ID,
                "status_code": "skipped",
                "status_message": "timed out",
                "started_at": "2026-08-18T22:18:41",
                "processing": False,
                "jobs": [],
            }

        with pytest.raises(RuntimeError, match="was skipped by the runner") as exc:
            run_wait(runner, queue_for)
        assert "timed out" in str(exc.value)
        assert not isinstance(exc.value, TimeoutError)

    def test_skipped_typo_spelling_is_also_terminal(self):
        """The runner's enum really does emit "skpped" for INACTIVE/REVIEWED."""
        runner = E2ETestRunner(test_type="chrome")

        def queue_for(elapsed: float) -> dict[str, Any]:
            return {
                "id": QUEUE_ID,
                "status_code": "skpped",
                "status_message": "job inactive",
                "processing": False,
                "jobs": [],
            }

        with pytest.raises(RuntimeError, match="was skipped by the runner"):
            run_wait(runner, queue_for)

    def test_failed_queue_still_raises_runtime_error(self):
        runner = E2ETestRunner(test_type="chrome")

        def queue_for(elapsed: float) -> dict[str, Any]:
            return {
                "id": QUEUE_ID,
                "status_code": "failure",
                "message": "failure running jobs",
                "processing": False,
                "jobs": [],
            }

        with pytest.raises(RuntimeError, match="Queue failed"):
            run_wait(runner, queue_for)


class TestConfiguration:
    def test_every_test_type_has_both_budgets(self):
        for name, config in TEST_TYPES.items():
            assert config["timeout"] > 0, name
            assert config["pickup_timeout"] > 0, name

    def test_defaults_and_overrides(self):
        assert E2ETestRunner(test_type="youtube").timeout == 600
        assert E2ETestRunner(test_type="youtube").pickup_timeout == 180
        # The pre-existing --timeout / timeout= kwarg keeps working.
        assert E2ETestRunner(timeout=300).timeout == 300
        assert E2ETestRunner(pickup_timeout=900).pickup_timeout == 900

    def test_cli_exposes_pickup_timeout(self):
        from orchestrator.cli import cli

        result = CliRunner().invoke(cli, ["e2e", "--help"])
        assert result.exit_code == 0
        assert "--pickup-timeout" in result.output
        assert "--timeout" in result.output
        assert "pick up" in result.output


class TestTroubleshootingHint:
    """The MLX/doctor troubleshooting block only fits a never-started run."""

    @staticmethod
    def _run_with_wait_error(error: Exception) -> MagicMock:
        runner = E2ETestRunner(test_type="chrome")
        runner.reporter = MagicMock()
        with (
            patch.object(runner, "_validate_setup", return_value=ValidationResult(ok=True)),
            patch.object(
                runner, "_upload_experiment", return_value=(1, 2, 3, 4, "exp", "2026-01-01")
            ),
            patch.object(runner, "_wait_for_completion", side_effect=error),
        ):
            result = runner.run()
        assert result.success is False
        assert str(error) in (result.error or "")
        return runner.reporter

    def test_pickup_timeout_prints_troubleshooting(self):
        reporter = self._run_with_wait_error(QueuePickupTimeoutError("never picked up"))
        reporter.print_troubleshooting.assert_called_once()

    def test_execution_timeout_does_not_print_troubleshooting(self):
        reporter = self._run_with_wait_error(QueueExecutionTimeoutError("still running"))
        reporter.print_troubleshooting.assert_not_called()
