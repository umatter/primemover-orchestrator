"""
Monitoring Component for managing the orchestrator's own Docker Compose stack.

Manages the observability stack: Grafana, Loki, Promtail, Prometheus, and Tempo.
"""

import subprocess
import time
from pathlib import Path
from typing import Optional

import httpx

from orchestrator.components.base import BaseComponent, ComponentHealth, ComponentStatus
from orchestrator.components.api import _get_compose_cmd

# Subprocess timeout constants (seconds)
COMPOSE_TIMEOUT = 300  # 5 min for docker compose operations
HEALTH_CHECK_TIMEOUT = 10  # 10 sec for health checks
DOCKER_INSPECT_TIMEOUT = 30  # 30 sec for docker inspect


class MonitoringComponent(BaseComponent):
    """
    Manages the orchestrator's monitoring stack via docker-compose.

    The monitoring stack includes:
    - Grafana (visualization, port 3000)
    - Loki (log aggregation, port 3100)
    - Promtail (log shipper)
    - Prometheus (metrics, port 9090)
    - Tempo (distributed tracing, port 3200)
    """

    # Container name prefix
    CONTAINER_PREFIX = "pm-orchestrator"

    # Services in the monitoring stack
    SERVICES = ["grafana", "loki", "promtail", "prometheus", "tempo"]

    # Health check timeout (seconds)
    HEALTH_CHECK_TIMEOUT = 60

    def __init__(
        self,
        path: Path,
        grafana_port: int = 3000,
        loki_port: int = 3100,
        prometheus_port: int = 9090,
        tempo_port: int = 3200,
    ):
        """
        Initialize Monitoring component.

        Args:
            path: Path to primemover-orchestrator directory
            grafana_port: Grafana HTTP port
            loki_port: Loki API port
            prometheus_port: Prometheus API port
            tempo_port: Tempo API port
        """
        super().__init__(name="Monitoring", path=path)
        self.grafana_port = grafana_port
        self.loki_port = loki_port
        self.prometheus_port = prometheus_port
        self.tempo_port = tempo_port
        self._compose_file = path / "docker-compose.yml"

    def _run_compose(
        self,
        args: list[str],
        capture_output: bool = True,
        timeout: int = COMPOSE_TIMEOUT,
    ) -> subprocess.CompletedProcess:
        """
        Run docker compose command in orchestrator directory.

        Uses 'docker compose' (v2) if available, falls back to 'docker-compose' (v1).

        Args:
            args: Arguments to pass to docker compose
            capture_output: Whether to capture stdout/stderr
            timeout: Command timeout in seconds (default: 5 minutes)

        Returns:
            CompletedProcess result

        Raises:
            subprocess.TimeoutExpired: If command exceeds timeout
        """
        cmd = _get_compose_cmd() + ["-f", str(self._compose_file)] + args
        return subprocess.run(
            cmd,
            capture_output=capture_output,
            text=True,
            cwd=self.path,
            timeout=timeout,
        )

    def start(self) -> ComponentHealth:
        """
        Start the monitoring stack.

        Returns:
            ComponentHealth with status after start attempt
        """
        # Validate first
        valid, errors = self.validate()
        if not valid:
            return ComponentHealth(
                status=ComponentStatus.ERROR,
                message="Validation failed",
                details={"errors": errors},
            )

        # Check if already running
        if self.is_running():
            return ComponentHealth(
                status=ComponentStatus.RUNNING,
                message="Monitoring stack is already running",
                url=f"http://localhost:{self.grafana_port}",
            )

        # Start docker-compose
        result = self._run_compose(["up", "-d"])

        if result.returncode != 0:
            return ComponentHealth(
                status=ComponentStatus.ERROR,
                message="Failed to start monitoring stack",
                details={"stderr": result.stderr},
            )

        # Wait for services to be healthy
        if self._wait_for_health():
            # Try to connect promtail to API network (optional - best effort)
            api_network_connected = self.connect_to_api_network()

            return ComponentHealth(
                status=ComponentStatus.RUNNING,
                message="Monitoring stack started successfully",
                url=f"http://localhost:{self.grafana_port}",
                details={
                    "containers": self._get_container_status(),
                    "urls": self._get_service_urls(),
                    "api_network_connected": api_network_connected,
                },
            )
        else:
            return ComponentHealth(
                status=ComponentStatus.ERROR,
                message="Monitoring started but health checks failed",
                details={"containers": self._get_container_status()},
            )

    def stop(self, remove_volumes: bool = False) -> ComponentHealth:
        """
        Stop the monitoring stack.

        Args:
            remove_volumes: If True, also remove data volumes

        Returns:
            ComponentHealth with status after stop attempt
        """
        args = ["down"]
        if remove_volumes:
            args.append("--volumes")

        result = self._run_compose(args)

        if result.returncode != 0:
            return ComponentHealth(
                status=ComponentStatus.ERROR,
                message="Failed to stop monitoring stack",
                details={"stderr": result.stderr},
            )

        return ComponentHealth(
            status=ComponentStatus.STOPPED,
            message="Monitoring stack stopped successfully",
        )

    def status(self) -> ComponentHealth:
        """
        Get current monitoring stack status with container details.

        Returns:
            ComponentHealth with current status and container info
        """
        containers = self._get_container_status()

        if not containers:
            return ComponentHealth(
                status=ComponentStatus.STOPPED,
                message="Monitoring stack is not running",
            )

        running = sum(1 for c in containers.values() if c.get("running", False))
        total = len(self.SERVICES)

        if running == 0:
            status = ComponentStatus.STOPPED
            message = "Monitoring stack is not running"
            url = None
        elif running < total:
            status = ComponentStatus.ERROR
            message = f"Monitoring partially running ({running}/{total} containers)"
            url = f"http://localhost:{self.grafana_port}"
        else:
            # Check Grafana health specifically
            if self._check_grafana_health():
                status = ComponentStatus.RUNNING
                message = "Monitoring stack is running and healthy"
            else:
                status = ComponentStatus.ERROR
                message = "Containers running but Grafana health check failed"
            url = f"http://localhost:{self.grafana_port}"

        return ComponentHealth(
            status=status,
            message=message,
            url=url,
            details={
                "containers": containers,
                "urls": self._get_service_urls() if running > 0 else {},
            },
        )

    def is_running(self) -> bool:
        """
        Quick check if monitoring stack is running.

        Returns:
            True if core containers are running
        """
        result = self._run_compose(["ps", "--services", "--filter", "status=running"])
        if result.returncode != 0:
            return False

        running_services = result.stdout.strip().split("\n") if result.stdout.strip() else []

        # Core services that must be running
        core_services = {"grafana", "loki", "prometheus"}
        return core_services.issubset(set(running_services))

    def validate(self) -> tuple[bool, list[str]]:
        """
        Validate monitoring component prerequisites.

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        # Check path exists
        if not self.path or not self.path.exists():
            errors.append(f"Orchestrator path does not exist: {self.path}")
            return False, errors

        # Check docker-compose.yml exists
        if not self._compose_file.exists():
            errors.append(f"docker-compose.yml not found at: {self._compose_file}")

        # Check infrastructure directory exists
        infra_dir = self.path / "infrastructure"
        if not infra_dir.exists():
            errors.append(f"Infrastructure directory not found: {infra_dir}")
        else:
            # Check for required config files
            required_configs = [
                "grafana/provisioning",
                "loki/loki-config.yaml",
                "prometheus/prometheus.yml",
                "promtail/promtail-config.yaml",
                "tempo/tempo-config.yaml",
            ]
            for config in required_configs:
                if not (infra_dir / config).exists():
                    errors.append(f"Missing config: {infra_dir / config}")

        # Check Docker is available
        try:
            docker_check = subprocess.run(
                ["docker", "info"], capture_output=True, text=True, timeout=HEALTH_CHECK_TIMEOUT
            )
            if docker_check.returncode != 0:
                errors.append("Docker is not running or not accessible")
        except subprocess.TimeoutExpired:
            errors.append("Docker check timed out - Docker may be unresponsive")

        # Check docker compose is available (v2 or v1)
        try:
            compose_cmd = _get_compose_cmd()
            compose_check = subprocess.run(
                compose_cmd + ["version"], capture_output=True, text=True, timeout=HEALTH_CHECK_TIMEOUT
            )
            if compose_check.returncode != 0:
                errors.append("Docker Compose is not installed or not accessible")
        except subprocess.TimeoutExpired:
            errors.append("Docker Compose check timed out")

        return len(errors) == 0, errors

    def get_network_name(self) -> str:
        """
        Get the Docker network name for the monitoring stack.

        Returns:
            Network name
        """
        return "pm-orchestrator-network"

    def connect_to_api_network(self, api_network: str = "primemover-network") -> bool:
        """
        Connect promtail to the API network for log access.

        Args:
            api_network: Name of the API Docker network

        Returns:
            True if successful
        """
        try:
            result = subprocess.run(
                [
                    "docker",
                    "network",
                    "connect",
                    api_network,
                    f"{self.CONTAINER_PREFIX}-promtail",
                ],
                capture_output=True,
                text=True,
                timeout=DOCKER_INSPECT_TIMEOUT,
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def _get_container_status(self) -> dict:
        """Get status of all monitoring containers."""
        import json

        containers = {}

        for service in self.SERVICES:
            container_name = f"{self.CONTAINER_PREFIX}-{service}"
            try:
                inspect_result = subprocess.run(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        '{"running": {{.State.Running}}, "status": "{{.State.Status}}", "health": "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"}',
                        container_name,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=DOCKER_INSPECT_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                containers[service] = {
                    "name": container_name,
                    "running": False,
                    "status": "timeout",
                }
                continue

            if inspect_result.returncode == 0:
                try:
                    info = json.loads(inspect_result.stdout.strip())
                    containers[service] = {
                        "name": container_name,
                        "running": info.get("running", False),
                        "status": info.get("status", "unknown"),
                        "health": info.get("health", "none"),
                    }
                except json.JSONDecodeError:
                    containers[service] = {
                        "name": container_name,
                        "running": False,
                        "status": "unknown",
                    }
            else:
                containers[service] = {
                    "name": container_name,
                    "running": False,
                    "status": "not found",
                }

        return containers

    def _get_service_urls(self) -> dict:
        """Get URLs for all monitoring services."""
        return {
            "grafana": f"http://localhost:{self.grafana_port}",
            "loki": f"http://localhost:{self.loki_port}",
            "prometheus": f"http://localhost:{self.prometheus_port}",
            "tempo": f"http://localhost:{self.tempo_port}",
        }

    def _wait_for_health(self) -> bool:
        """
        Wait for monitoring stack to become healthy.

        Returns:
            True if healthy within timeout
        """
        start_time = time.time()

        while time.time() - start_time < self.HEALTH_CHECK_TIMEOUT:
            if self._check_grafana_health() and self._check_loki_health():
                return True
            time.sleep(2)

        return False

    def _check_grafana_health(self) -> bool:
        """Check if Grafana is healthy."""
        try:
            response = httpx.get(
                f"http://localhost:{self.grafana_port}/api/health",
                timeout=5.0,
            )
            return response.status_code == 200
        except httpx.RequestError:
            return False

    def _check_loki_health(self) -> bool:
        """Check if Loki is ready."""
        try:
            response = httpx.get(
                f"http://localhost:{self.loki_port}/ready",
                timeout=5.0,
            )
            return response.status_code == 200
        except httpx.RequestError:
            return False

    def _check_prometheus_health(self) -> bool:
        """Check if Prometheus is healthy."""
        try:
            response = httpx.get(
                f"http://localhost:{self.prometheus_port}/-/healthy",
                timeout=5.0,
            )
            return response.status_code == 200
        except httpx.RequestError:
            return False
