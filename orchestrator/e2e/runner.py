"""
E2E Test Runner.

Executes end-to-end tests of the PrimeMover system.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

import httpx

from orchestrator.clients.api import APIClient
from orchestrator.config import get_settings

from .reporter import E2EReporter


@dataclass
class ValidationResult:
    """Result of setup validation."""

    ok: bool
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class E2EResult:
    """Result of an e2e test run."""

    success: bool
    duration: float = 0.0
    error: str | None = None
    experiment_id: int | None = None
    crawler_id: int | None = None
    queue_id: int | None = None
    jobs: list[dict[str, Any]] = field(default_factory=list)


# Path to the test experiment JSON templates
TEST_EXPERIMENT_JSON = Path(__file__).parent / "test_experiment.json"
TEST_EXPERIMENT_YOUTUBE_JSON = Path(__file__).parent / "test_experiment_youtube.json"


class QueuePickupTimeoutError(TimeoutError):
    """The runner never claimed the queue within the pickup budget."""


class QueueExecutionTimeoutError(TimeoutError):
    """The runner claimed the queue, but the jobs did not finish in time."""


# Queue-level status codes, as emitted by the runner's JoblistState enum.
# NOTE: "skpped" is a real typo in the runner's enum (SKIPPED_INACTIVE and
# SKIPPED_REVIEWED), so both spellings have to be matched.
QUEUE_SUCCESS_STATUSES = ("completed", "done", "finished", "success")
QUEUE_FAILURE_STATUSES = ("failed", "error", "failure")
QUEUE_SKIPPED_STATUSES = ("skipped", "skpped")


class TestTypeConfig(TypedDict, total=False):
    """Configuration for one e2e test type.

    ``total=False`` because ``identification_override`` is only set for the
    "chrome" test type; every other key is present for all test types.
    """

    template: Path
    name: str
    timeout: int
    pickup_timeout: int
    description: str
    identification_override: str


# Test type configurations.
#
# The wait for a queue to finish is split into two independently budgeted
# phases, because they have completely different characteristics:
#
#   pickup_timeout - how long the runner may take to *claim* the queue. The
#       runner polls for new work on a schedule (`runner.UpdateInterval`
#       minutes, on a 60s tick), so this latency is variable and does not
#       depend on the test workload at all. Observed range: 2s to 112s with
#       UpdateInterval=1; a deployment with a larger interval is slower still.
#   timeout - how long the jobs themselves may take once the runner has picked
#       the queue up. This is what actually depends on the test type.
#
# Sharing one budget between the two used to fail healthy runs: a queue that
# waited 112s for pickup and then executed in 88s (a success) was reported as
# "Jobs did not complete within 180s timeout".
TEST_TYPES: dict[str, TestTypeConfig] = {
    "basic": {
        "template": TEST_EXPERIMENT_JSON,
        "name": "Basic E2E Test",
        "timeout": 180,
        "pickup_timeout": 180,
        "description": "Simple visit and wait jobs",
    },
    "chrome": {
        "template": TEST_EXPERIMENT_JSON,
        "name": "Chrome E2E Test",
        "timeout": 180,
        "pickup_timeout": 180,
        "description": "Simple visit and wait jobs using plain Chrome (no Multilogin X)",
        "identification_override": "chrome",
    },
    "youtube": {
        "template": TEST_EXPERIMENT_YOUTUBE_JSON,
        "name": "YouTube E2E Test",
        "timeout": 600,  # 10 minutes for YouTube workflow with Lambda calls
        "pickup_timeout": 180,
        "description": "YouTube recommendation workflow with Lambda selection",
    },
}


def _queue_status(queue: dict[str, Any]) -> str:
    """Return the queue's status code, or "" when it has none yet.

    The API returns ``status_code: null`` for a queue no runner has touched, and
    ``dict.get(key, default)`` yields that stored ``None`` rather than the
    default -- which is how a never-picked-up queue used to be reported as
    ``Last status: None``.
    """
    return str(queue.get("status_code") or queue.get("status") or "")


def _queue_picked_up(queue: dict[str, Any]) -> bool:
    """Whether the runner has claimed this queue yet.

    The runner sets ``status_code``/``started_at``/``processing`` the moment it
    claims a queue (JobListRunner -> RUNNING_INPROGRESS), before it even starts
    the browser. These are used purely as presence signals: ``started_at`` is
    written by the runner and has been observed with a wrong UTC/CET offset, so
    no arithmetic is done on it. ``processing`` flips back to false on
    completion, hence the three-way OR.
    """
    return bool(queue.get("started_at") or queue.get("processing") or _queue_status(queue))


class E2ETestRunner:
    """
    Runs end-to-end tests of the PrimeMover system.

    The test:
    1. Validates all components are running
    2. Uploads a minimal test experiment
    3. Waits for the runner to execute jobs (runner creates Multilogin profile automatically)
    4. Reports results
    """

    def __init__(
        self,
        timeout: int | None = None,
        verbose: bool = False,
        test_type: str = "basic",
        pickup_timeout: int | None = None,
    ):
        """
        Initialize e2e test runner.

        Args:
            timeout: Maximum wait time in seconds for the jobs to finish, measured from the
                moment the runner picks the queue up (default: based on test type)
            verbose: Show detailed output
            test_type: Type of test to run ("basic", "chrome" or "youtube")
            pickup_timeout: Maximum wait time in seconds for the runner to pick the queue up
                at all (default: based on test type). Pickup latency depends on the runner's
                `runner.UpdateInterval` poll schedule, not on the test workload.
        """
        if test_type not in TEST_TYPES:
            raise ValueError(
                f"Unknown test type: {test_type}. Available: {list(TEST_TYPES.keys())}"
            )

        self.test_type = test_type
        self.test_config = TEST_TYPES[test_type]
        self.timeout = timeout if timeout is not None else self.test_config["timeout"]
        self.pickup_timeout = (
            pickup_timeout if pickup_timeout is not None else self.test_config["pickup_timeout"]
        )
        self.verbose = verbose
        self.settings = get_settings()
        self.reporter = E2EReporter(verbose=verbose)

    def run(self) -> E2EResult:
        """
        Run the complete e2e test.

        Returns:
            E2EResult with success status and details
        """
        start_time = time.time()
        self.reporter.print_header()

        # Step 1: Validate setup
        self.reporter.print_step(1, 4, "Validating setup...")
        validation = self._validate_setup()
        self.reporter.print_validation(validation)

        if not validation.ok:
            self.reporter.print_error("Setup validation failed")
            self.reporter.print_troubleshooting()
            return E2EResult(
                success=False,
                duration=time.time() - start_time,
                error="Setup validation failed",
            )

        # Step 2: Upload test experiment
        self.reporter.print_step(2, 4, "Uploading test experiment...")
        try:
            experiment_id, crawler_id, queue_id, job_count, exp_name, sched_time = (
                self._upload_experiment()
            )
            self.reporter.print_upload(
                experiment_id=experiment_id,
                experiment_name=exp_name,
                crawler_count=1,
                job_count=job_count,
                start_time=sched_time,
            )
        except Exception as e:
            self.reporter.print_error(f"Failed to upload experiment: {e}")
            return E2EResult(
                success=False,
                duration=time.time() - start_time,
                error=f"Upload failed: {e}",
            )

        # Step 3: Wait for execution
        self.reporter.print_step(3, 4, "Waiting for execution...")
        try:
            jobs = self._wait_for_completion(queue_id)
        except TimeoutError as e:
            self.reporter.print_error(str(e))
            # The troubleshooting block is about a runner that never got going
            # (MLX not logged in, runner down, components unhealthy). It is
            # actively misleading for a queue that *was* executing normally and
            # merely outran its budget, so only print it for a pickup failure.
            if isinstance(e, QueuePickupTimeoutError):
                self.reporter.print_troubleshooting()
            return E2EResult(
                success=False,
                duration=time.time() - start_time,
                error=str(e),
                experiment_id=experiment_id,
                crawler_id=crawler_id,
                queue_id=queue_id,
            )
        except Exception as e:
            self.reporter.print_error(f"Error waiting for completion: {e}")
            return E2EResult(
                success=False,
                duration=time.time() - start_time,
                error=str(e),
                experiment_id=experiment_id,
                crawler_id=crawler_id,
                queue_id=queue_id,
            )

        # Step 4: Report results
        self.reporter.print_step(4, 4, "Results")

        # Check if all jobs completed successfully. Best-effort job types
        # (try*) may fail without failing the run — the runner itself continues
        # past them (e.g. the YouTube consent dialog is only sometimes shown, so
        # its tryclickjob times out on profiles that never see it). Jobs the
        # runner skipped in a best-effort job's wake report status "skipped".
        def _job_ok(job: dict) -> bool:
            status = job.get("status_code", job.get("status"))
            if status in ("completed", "done", "success", "skipped"):
                return True
            return str(job.get("type", "")).startswith("try")

        all_completed = bool(jobs) and all(_job_ok(j) for j in jobs)
        failed_jobs = [j for j in jobs if not _job_ok(j)]

        duration = time.time() - start_time
        result = E2EResult(
            success=all_completed and not failed_jobs,
            duration=duration,
            error=f"{len(failed_jobs)} job(s) failed" if failed_jobs else None,
            experiment_id=experiment_id,
            crawler_id=crawler_id,
            queue_id=queue_id,
            jobs=jobs,
        )

        self.reporter.print_results(result)
        return result

    def _validate_setup(self) -> ValidationResult:
        """Validate that all components are running."""
        checks: dict[str, dict[str, Any]] = {}

        # Check API (via authentication - if auth works, API is reachable)
        try:
            client = APIClient()
            auth_ok = client.is_authenticated()
            client.close()
            checks["API"] = {
                "ok": auth_ok,
                "message": (
                    f"localhost:{self.settings.pm_api_http_port}" if auth_ok else "Auth failed"
                ),
            }
        except httpx.ConnectError:
            checks["API"] = {"ok": False, "message": "Connection refused"}
        except Exception as e:
            checks["API"] = {"ok": False, "message": str(e)}

        # Check Loki (monitoring)
        try:
            response = httpx.get(f"{self.settings.loki_url}/ready", timeout=5.0)
            checks["Monitoring (Loki)"] = {
                "ok": response.status_code == 200,
                "message": (
                    "Healthy" if response.status_code == 200 else f"Status {response.status_code}"
                ),
            }
        except Exception as e:
            checks["Monitoring (Loki)"] = {"ok": False, "message": str(e)}

        # Check Grafana
        try:
            response = httpx.get(f"{self.settings.grafana_url}/api/health", timeout=5.0)
            checks["Monitoring (Grafana)"] = {
                "ok": response.status_code == 200,
                "message": (
                    "Healthy" if response.status_code == 200 else f"Status {response.status_code}"
                ),
            }
        except Exception as e:
            checks["Monitoring (Grafana)"] = {"ok": False, "message": str(e)}

        # All checks must pass
        all_ok = all(c["ok"] for c in checks.values())
        return ValidationResult(ok=all_ok, checks=checks)

    def _upload_experiment(self) -> tuple[int, int, int, int, str, str]:
        """
        Upload test experiment to API.

        Uses curl to match the exact upload format that works with the API.

        Returns:
            Tuple of (experiment_id, crawler_id, queue_id, job_count, experiment_name, scheduled_time)
        """
        # Load the test experiment JSON template based on test type
        template_path = self.test_config["template"]
        with open(template_path) as f:
            crawlers_data = json.load(f)

        # Generate experiment name with timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        test_name = self.test_config["name"]
        experiment_name = f"{test_name} - {timestamp}"

        # Schedule start time. The API (Laravel APP_TIMEZONE=Europe/Zurich) strips
        # the offset and interprets the naive datetime as Europe/Zurich local time,
        # so we send the current Zurich wall-clock time with its current offset.
        # Using zoneinfo makes this DST-safe (CET +01:00 winter / CEST +02:00
        # summer); the previous hard-coded +1h/+01:00 broke under summer DST — the
        # queue landed an hour in the past and the runner marked the jobs "skipped".
        # NOTE: the host VM runs in UTC, so we must use the API's zone explicitly,
        # not the host's local time.
        api_now = datetime.now(ZoneInfo("Europe/Zurich"))
        offset = api_now.strftime("%z")  # e.g. +0200
        offset = offset[:3] + ":" + offset[3:]  # -> +02:00
        start_time = api_now.strftime("%Y-%m-%dT%H:%M:%S.00") + offset

        # Update the crawler data with dynamic values
        crawlers_data[0]["name"] = f"E2E Test Bot - {timestamp}"
        crawlers_data[0]["queues"][0]["start_at"] = start_time

        # Override agent identification if configured (e.g., "chrome" instead of "mlx")
        id_override = self.test_config.get("identification_override")
        if id_override:
            crawlers_data[0]["agent"]["identification"] = id_override

        # Get auth token using curl (matching the working script exactly)
        api_base = self.settings.pm_api_base_url.rstrip("/")
        email = self.settings.pm_api_email
        password = self.settings.pm_api_password

        # Authenticate
        auth_result = subprocess.run(
            [
                "curl",
                "-s",
                "-X",
                "POST",
                f"{api_base}/login",
                "-H",
                "Content-Type: application/json",
                "-H",
                "Accept: application/json",
                "-d",
                json.dumps({"email": email, "password": password}),
            ],
            capture_output=True,
            text=True,
        )
        auth_response = json.loads(auth_result.stdout)
        token = auth_response.get("access_token")
        if not token:
            raise ValueError(f"Authentication failed: {auth_response}")

        # Create experiment
        create_result = subprocess.run(
            [
                "curl",
                "-s",
                "-X",
                "POST",
                f"{api_base}/experiments",
                "-H",
                f"Authorization: Bearer {token}",
                "-H",
                "Content-Type: application/json",
                "-H",
                "Accept: application/json",
                "-d",
                json.dumps(
                    {
                        "name": experiment_name,
                        "description": "Automated end-to-end verification test",
                        "contact": "Orchestrator E2E Test",
                        "active": 1,
                    }
                ),
            ],
            capture_output=True,
            text=True,
        )
        create_response = json.loads(create_result.stdout)
        experiment_id: int | None
        experiment_id = create_response.get("data", {}).get("id") or create_response.get("id")
        if not experiment_id:
            raise ValueError(f"Failed to create experiment: {create_response}")

        # Write crawlers JSON to temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(crawlers_data, f)
            temp_json_path = f.name

        if self.verbose:
            print(f"  [DEBUG] Temp JSON file: {temp_json_path}")
            print(
                f"  [DEBUG] Crawlers data queues: {json.dumps(crawlers_data[0].get('queues', [])[:1], indent=2)[:300]}"
            )

        try:
            # Upload crawlers using curl -F (exactly like the working script)
            upload_result = subprocess.run(
                [
                    "curl",
                    "-s",
                    "-X",
                    "POST",
                    f"{api_base}/experiments/{experiment_id}/upload",
                    "-H",
                    f"Authorization: Bearer {token}",
                    "-H",
                    "Accept: application/json",
                    "-F",
                    f"json=@{temp_json_path};type=application/json",
                ],
                capture_output=True,
                text=True,
            )

            if self.verbose:
                print(f"  [DEBUG] Upload stdout: {upload_result.stdout[:500]}")
                if upload_result.stderr:
                    print(f"  [DEBUG] Upload stderr: {upload_result.stderr}")

            upload_response = json.loads(upload_result.stdout) if upload_result.stdout else {}

            if "error" in upload_response:
                raise ValueError(f"Upload failed: {upload_response}")
            if "errors" in upload_response:
                raise ValueError(f"Upload validation failed: {upload_response['errors']}")
            if "message" in upload_response and upload_response.get("message") != "OK":
                # Laravel returns {"message": "..."} on validation errors
                if any(k in str(upload_response).lower() for k in ("invalid", "fail", "required")):
                    raise ValueError(f"Upload failed: {upload_response}")

        finally:
            Path(temp_json_path).unlink(missing_ok=True)

        # Fetch experiment to get crawler IDs
        client = APIClient()
        try:
            exp_details = client.get_experiment(experiment_id)

            crawlers = exp_details.get("crawlers", [])
            if not crawlers:
                raise ValueError(f"No crawlers created. Experiment details: {exp_details}")

            crawler_id: int | None = crawlers[0].get("id")

            # Get queue ID by querying /queues-unprocessed and filtering by crawler_id
            # (The /crawlers/{id} endpoint doesn't include queues in its response)
            unprocessed_queues = client.list_queues_by_status("unprocessed")
            crawler_queues = [q for q in unprocessed_queues if q.get("crawler_id") == crawler_id]

            if not crawler_queues:
                raise ValueError(f"No unprocessed queues found for crawler {crawler_id}")

            queue_id: int | None = crawler_queues[0].get("id")

            # `id` is the primary key of the API's own crawler/queue records, so it is
            # always present on a record the API just handed us. Assert it instead of
            # casting, so the int return type below is honest.
            assert crawler_id is not None, f"crawler record without id: {crawlers[0]}"
            assert queue_id is not None, f"queue record without id: {crawler_queues[0]}"

            # Get job count from API
            queue_details = client.get_queue(queue_id)
            jobs = queue_details.get("jobs", [])
            job_count = len(jobs)

            return experiment_id, crawler_id, queue_id, job_count, experiment_name, start_time

        finally:
            client.close()

    def _wait_for_completion(self, queue_id: int) -> list[dict[str, Any]]:
        """
        Wait for queue execution to complete.

        The wait is split into two independently budgeted phases (see TEST_TYPES):
        first ``pickup_timeout`` seconds for the runner to claim the queue, then
        ``timeout`` seconds for the claimed queue to finish executing. Each poll
        reads the queue *before* the deadline is checked, so the decision to give
        up is always made on a fresh observation.

        Args:
            queue_id: Queue ID to monitor

        Returns:
            List of job details with status

        Raises:
            QueuePickupTimeoutError: If the runner never claims the queue
            QueueExecutionTimeoutError: If a claimed queue does not finish in time
            RuntimeError: If the queue reaches a terminal failed/skipped state
        """
        client = APIClient()
        wait_started = time.time()
        poll_interval = 5  # seconds
        picked_up_at: float | None = None
        picked_up_after = 0.0
        last_waiting_bucket = -1

        try:
            while True:
                queue = client.get_queue(queue_id)
                queue_status = _queue_status(queue)
                status_message = str(queue.get("status_message") or "")
                jobs: list[dict[str, Any]] = queue.get("jobs", [])

                now = time.time()
                if picked_up_at is None and _queue_picked_up(queue):
                    picked_up_at = now
                    picked_up_after = now - wait_started

                completed_jobs = sum(
                    1
                    for j in jobs
                    if j.get("status_code", j.get("status")) in ("completed", "done", "success")
                )

                # --- Terminal states, checked before any deadline so that a
                # queue which finishes on the very last poll still counts.
                if queue_status in QUEUE_SUCCESS_STATUSES:
                    self.reporter.clear_line()
                    # Print individual job results
                    for job in jobs:
                        job_type = job.get("type", job.get("job_type", "unknown"))
                        status = job.get("status_code", job.get("status", "unknown"))
                        duration = job.get("duration", 0) or 0

                        if status in ("completed", "done", "success"):
                            self.reporter.print_job_completed(job_type, duration)
                        else:
                            error = job.get("error", job.get("message", "Unknown error"))
                            self.reporter.print_job_failed(job_type, error)

                    return jobs

                if queue_status in QUEUE_FAILURE_STATUSES:
                    self.reporter.clear_line()
                    error_msg = queue.get("error", queue.get("message", "Queue failed"))
                    raise RuntimeError(f"Queue failed: {error_msg}")

                if queue_status in QUEUE_SKIPPED_STATUSES:
                    # Terminal too: every SKIPPED_* state sets processed=true and
                    # a finish time. Waiting out the budget here would only turn a
                    # precise diagnosis into a generic timeout.
                    self.reporter.clear_line()
                    raise RuntimeError(
                        f"Queue {queue_id} was skipped by the runner "
                        f"({status_message or 'no reason given'}). This usually means the queue "
                        f"was scheduled in the past, or is inactive/already reviewed."
                    )

                # --- Not terminal: enforce the budget for the phase we are in.
                if picked_up_at is None:
                    elapsed = now - wait_started
                    bucket = int(elapsed // 30)
                    if self.verbose or bucket != last_waiting_bucket:
                        last_waiting_bucket = bucket
                        self.reporter.print_waiting(
                            f"Waiting for runner to pick up queue... "
                            f"({elapsed:.0f}s / {self.pickup_timeout}s)"
                        )
                    if elapsed > self.pickup_timeout:
                        raise QueuePickupTimeoutError(
                            f"Queue {queue_id} was never picked up by the runner within "
                            f"{self.pickup_timeout}s (queue status: "
                            f"{queue_status or 'not set'}). No jobs ran. The runner polls for "
                            f"new work every runner.UpdateInterval minutes - check that it is "
                            f"running and that the interval is short enough: "
                            f"orchestrator logs query -c runner -t 10m"
                        )
                else:
                    running_for = now - picked_up_at
                    if self.verbose:
                        self.reporter.clear_line()
                        self.reporter.print_progress(completed_jobs, len(jobs), queue_status)
                    if running_for > self.timeout:
                        self.reporter.clear_line()
                        raise QueueExecutionTimeoutError(
                            f"Queue {queue_id} was picked up after {picked_up_after:.0f}s but "
                            f"had not finished {self.timeout}s later (status: "
                            f"{queue_status or 'unknown'}"
                            f"{f' / {status_message}' if status_message else ''}, "
                            f"{completed_jobs}/{len(jobs)} jobs completed). The run may still be "
                            f"progressing - check with: orchestrator trace queue {queue_id}. "
                            f"Raise the execution budget with --timeout if this workload is "
                            f"simply slower than the default."
                        )

                time.sleep(poll_interval)

        finally:
            client.close()
