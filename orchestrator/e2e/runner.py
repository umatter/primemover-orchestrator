"""
E2E Test Runner.

Executes end-to-end tests of the PrimeMover system.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

import httpx

from orchestrator.config import get_settings
from orchestrator.clients.api import APIClient
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


import json
import subprocess
import tempfile
from pathlib import Path

# Path to the test experiment JSON templates
TEST_EXPERIMENT_JSON = Path(__file__).parent / "test_experiment.json"
TEST_EXPERIMENT_YOUTUBE_JSON = Path(__file__).parent / "test_experiment_youtube.json"

# Test type configurations
TEST_TYPES = {
    "basic": {
        "template": TEST_EXPERIMENT_JSON,
        "name": "Basic E2E Test",
        "timeout": 180,
        "description": "Simple visit and wait jobs",
    },
    "chrome": {
        "template": TEST_EXPERIMENT_JSON,
        "name": "Chrome E2E Test",
        "timeout": 180,
        "description": "Simple visit and wait jobs using plain Chrome (no Multilogin X)",
        "identification_override": "chrome",
    },
    "youtube": {
        "template": TEST_EXPERIMENT_YOUTUBE_JSON,
        "name": "YouTube E2E Test",
        "timeout": 600,  # 10 minutes for YouTube workflow with Lambda calls
        "description": "YouTube recommendation workflow with Lambda selection",
    },
}


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
    ):
        """
        Initialize e2e test runner.

        Args:
            timeout: Maximum wait time in seconds for job completion (default: based on test type)
            verbose: Show detailed output
            test_type: Type of test to run ("basic" or "youtube")
        """
        if test_type not in TEST_TYPES:
            raise ValueError(f"Unknown test type: {test_type}. Available: {list(TEST_TYPES.keys())}")

        self.test_type = test_type
        self.test_config = TEST_TYPES[test_type]
        self.timeout = timeout if timeout is not None else self.test_config["timeout"]
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
            experiment_id, crawler_id, queue_id, job_count, exp_name, sched_time = self._upload_experiment()
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

        # Check if all jobs completed successfully
        all_completed = all(
            job.get("status_code", job.get("status")) in ("completed", "done", "success")
            for job in jobs
        )
        failed_jobs = [j for j in jobs if j.get("status_code", j.get("status")) in ("failed", "error", "failure")]

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
                "message": f"localhost:{self.settings.pm_api_http_port}" if auth_ok else "Auth failed",
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
                "message": "Healthy" if response.status_code == 200 else f"Status {response.status_code}",
            }
        except Exception as e:
            checks["Monitoring (Loki)"] = {"ok": False, "message": str(e)}

        # Check Grafana
        try:
            response = httpx.get(f"{self.settings.grafana_url}/api/health", timeout=5.0)
            checks["Monitoring (Grafana)"] = {
                "ok": response.status_code == 200,
                "message": "Healthy" if response.status_code == 200 else f"Status {response.status_code}",
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
        offset = api_now.strftime("%z")              # e.g. +0200
        offset = offset[:3] + ":" + offset[3:]       # -> +02:00
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
                "curl", "-s", "-X", "POST", f"{api_base}/login",
                "-H", "Content-Type: application/json",
                "-H", "Accept: application/json",
                "-d", json.dumps({"email": email, "password": password}),
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
                "curl", "-s", "-X", "POST", f"{api_base}/experiments",
                "-H", f"Authorization: Bearer {token}",
                "-H", "Content-Type: application/json",
                "-H", "Accept: application/json",
                "-d", json.dumps({
                    "name": experiment_name,
                    "description": "Automated end-to-end verification test",
                    "contact": "Orchestrator E2E Test",
                    "active": 1,
                }),
            ],
            capture_output=True,
            text=True,
        )
        create_response = json.loads(create_result.stdout)
        experiment_id = create_response.get("data", {}).get("id") or create_response.get("id")
        if not experiment_id:
            raise ValueError(f"Failed to create experiment: {create_response}")

        # Write crawlers JSON to temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(crawlers_data, f)
            temp_json_path = f.name

        if self.verbose:
            print(f"  [DEBUG] Temp JSON file: {temp_json_path}")
            print(f"  [DEBUG] Crawlers data queues: {json.dumps(crawlers_data[0].get('queues', [])[:1], indent=2)[:300]}")

        try:
            # Upload crawlers using curl -F (exactly like the working script)
            upload_result = subprocess.run(
                [
                    "curl", "-s", "-X", "POST", f"{api_base}/experiments/{experiment_id}/upload",
                    "-H", f"Authorization: Bearer {token}",
                    "-H", "Accept: application/json",
                    "-F", f"json=@{temp_json_path};type=application/json",
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

            crawler_id = crawlers[0].get("id")

            # Get queue ID by querying /queues-unprocessed and filtering by crawler_id
            # (The /crawlers/{id} endpoint doesn't include queues in its response)
            unprocessed_queues = client.list_queues_by_status("unprocessed")
            crawler_queues = [q for q in unprocessed_queues if q.get("crawler_id") == crawler_id]

            if not crawler_queues:
                raise ValueError(f"No unprocessed queues found for crawler {crawler_id}")

            queue_id = crawler_queues[0].get("id")

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

        Args:
            queue_id: Queue ID to monitor

        Returns:
            List of job details with status

        Raises:
            TimeoutError: If jobs don't complete within timeout
        """
        client = APIClient()
        start_time = time.time()
        poll_interval = 5  # seconds
        last_status = ""

        try:
            while True:
                elapsed = time.time() - start_time

                if elapsed > self.timeout:
                    raise TimeoutError(
                        f"Jobs did not complete within {self.timeout}s timeout. "
                        f"Last status: {last_status}. Check runner logs."
                    )

                # Get queue status
                queue = client.get_queue(queue_id)
                queue_status = queue.get("status_code", queue.get("status", "unknown"))
                jobs = queue.get("jobs", [])

                # Update status display
                completed_jobs = sum(1 for j in jobs if j.get("status_code", j.get("status")) in ("completed", "done", "success"))
                last_status = queue_status

                if self.verbose:
                    self.reporter.clear_line()
                    self.reporter.print_progress(completed_jobs, len(jobs), queue_status)

                # Check if queue is finished
                if queue_status in ("completed", "done", "finished", "success"):
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

                # Check if queue failed
                if queue_status in ("failed", "error", "failure"):
                    self.reporter.clear_line()
                    error_msg = queue.get("error", queue.get("message", "Queue failed"))
                    raise RuntimeError(f"Queue failed: {error_msg}")

                # Still waiting
                if elapsed < 30:
                    self.reporter.print_waiting(f"Waiting for runner to pick up job... ({elapsed:.0f}s)")

                time.sleep(poll_interval)

        finally:
            client.close()
