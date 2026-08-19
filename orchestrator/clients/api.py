"""
PrimeMover API Client.

Provides authenticated access to the PrimeMover API for experiment management.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from orchestrator.config import get_settings


class APIClient:
    """Client for the PrimeMover API."""

    def __init__(
        self,
        base_url: str | None = None,
        email: str | None = None,
        password: str | None = None,
        timeout: float = 30.0,
    ):
        """
        Initialize API client.

        Args:
            base_url: API base URL (defaults to PM_API_BASE_URL)
            email: Login email (defaults to PM_API_EMAIL)
            password: Login password (defaults to PM_API_PASSWORD)
            timeout: Request timeout in seconds
        """
        settings = get_settings()
        self.base_url = (base_url or settings.pm_api_base_url).rstrip("/")
        self._email = email or settings.pm_api_email
        self._password = password or settings.pm_api_password
        self._timeout = timeout
        self._token: str | None = None
        self._client = httpx.Client(timeout=timeout)

    def _authenticate(self) -> None:
        """Authenticate and get access token."""
        if not self._email or not self._password:
            raise ValueError("API credentials not configured (PM_API_EMAIL, PM_API_PASSWORD)")

        # Try /login first (standard), then /auth/login (some deployments)
        endpoints = [f"{self.base_url}/login", f"{self.base_url}/auth/login"]

        for endpoint in endpoints:
            try:
                response = self._client.post(
                    endpoint,
                    json={"email": self._email, "password": self._password},
                    headers={"Accept": "application/json"},
                )
                if response.status_code == 200:
                    data = response.json()
                    self._token = data.get("access_token") or data.get("token")
                    if self._token:
                        return
            except httpx.HTTPError:
                continue

        raise RuntimeError("Authentication failed: could not obtain access token")

    def _ensure_authenticated(self) -> None:
        """Ensure we have a valid token."""
        if not self._token:
            self._authenticate()

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json_data: dict | None = None,
        files: dict | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated API request."""
        self._ensure_authenticated()

        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

        if json_data and not files:
            headers["Content-Type"] = "application/json"

        response = self._client.request(
            method,
            url,
            params=params,
            json=json_data if not files else None,
            files=files,
            headers=headers,
        )
        response.raise_for_status()

        # Handle empty responses
        if not response.content:
            return {}

        data: dict[str, Any] = response.json()
        return data

    def is_authenticated(self) -> bool:
        """Check if authentication is working."""
        try:
            self._ensure_authenticated()
            return self._token is not None
        except Exception:
            return False

    def close(self) -> None:
        """Close the client."""
        self._client.close()

    def __enter__(self) -> APIClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # =========================================================================
    # Experiments
    # =========================================================================

    def list_experiments(self, page: int = 1, per_page: int = 15) -> list[dict]:
        """List experiments (paginated)."""
        result = self._request("GET", "experiments", params={"page": page, "per_page": per_page})
        experiments: list[dict] = result.get("data", []) if isinstance(result, dict) else result
        return experiments

    def get_experiment(self, experiment_id: int) -> dict[str, Any]:
        """Get experiment details."""
        result = self._request("GET", f"experiments/{experiment_id}")
        experiment: dict[str, Any] = (
            result.get("data", result) if isinstance(result, dict) else result
        )
        return experiment

    def create_experiment(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create a new experiment."""
        result = self._request("POST", "experiments", json_data=data)
        experiment: dict[str, Any] = (
            result.get("data", result) if isinstance(result, dict) else result
        )
        return experiment

    def delete_experiment(self, experiment_id: int) -> dict[str, Any]:
        """Delete an experiment."""
        return self._request("DELETE", f"experiments/{experiment_id}")

    def upload_crawlers(
        self,
        experiment_id: int,
        crawlers_json: str | list[dict],
    ) -> dict[str, Any]:
        """
        Upload crawlers to an experiment.

        Args:
            experiment_id: Target experiment ID
            crawlers_json: JSON string or list of crawler data
        """
        if isinstance(crawlers_json, list):
            crawlers_json = json.dumps(crawlers_json, indent=2)

        files = {"json": ("crawlers.json", crawlers_json, "application/json")}
        return self._request("POST", f"experiments/{experiment_id}/upload", files=files)

    # =========================================================================
    # Crawlers
    # =========================================================================

    def list_crawlers(self, experiment_id: int) -> list[dict]:
        """List crawlers for an experiment."""
        result = self._request("GET", f"experiments/{experiment_id}/crawlers")
        crawlers: list[dict] = result.get("data", []) if isinstance(result, dict) else result
        return crawlers

    def get_crawler(self, crawler_id: int) -> dict[str, Any]:
        """Get crawler details including queues."""
        result = self._request("GET", f"crawlers/{crawler_id}")
        crawler: dict[str, Any] = result.get("data", result) if isinstance(result, dict) else result
        return crawler

    # =========================================================================
    # Queues
    # =========================================================================

    def get_queue(self, queue_id: int) -> dict[str, Any]:
        """Get queue details including jobs."""
        result = self._request("GET", f"queues/{queue_id}")
        queue: dict[str, Any] = result.get("data", result) if isinstance(result, dict) else result
        return queue

    def list_queues_by_status(self, status: str = "") -> list[dict]:
        """List queues by status (pending, running, completed, failed)."""
        endpoint = f"queues-{status}" if status else "queues"
        result = self._request("GET", endpoint)
        queues: list[dict] = result.get("data", []) if isinstance(result, dict) else result
        return queues

    # =========================================================================
    # Jobs
    # =========================================================================

    def get_job(self, job_id: int) -> dict[str, Any]:
        """Get job details."""
        result = self._request("GET", f"jobs/{job_id}")
        job: dict[str, Any] = result.get("data", result) if isinstance(result, dict) else result
        return job
