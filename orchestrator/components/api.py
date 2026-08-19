"""
API Component for managing primemover_api Docker Compose stack.

Manages the Laravel API with Apache, PHP-FPM, MySQL, Redis, and Memcached.
"""

import subprocess
import time
from pathlib import Path

import httpx

from orchestrator.components.base import BaseComponent, ComponentHealth, ComponentStatus
from orchestrator.config import get_settings

# Subprocess timeout constants (seconds)
COMPOSE_TIMEOUT = 300  # 5 min for docker compose operations (build can be slow)
HEALTH_CHECK_TIMEOUT = 10  # 10 sec for health checks
DOCKER_INSPECT_TIMEOUT = 30  # 30 sec for docker inspect

# Cache for compose command detection
_compose_cmd: list[str] | None = None


def _get_compose_cmd() -> list[str]:
    """
    Detect whether to use 'docker compose' (v2) or 'docker-compose' (v1).

    Returns:
        List of command parts (e.g., ["docker", "compose"] or ["docker-compose"])
    """
    global _compose_cmd
    if _compose_cmd is not None:
        return _compose_cmd

    # Try docker compose (v2) first - this is the modern approach
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
            timeout=HEALTH_CHECK_TIMEOUT,
        )
        if result.returncode == 0:
            _compose_cmd = ["docker", "compose"]
            return _compose_cmd
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fall back to docker-compose (v1)
    try:
        result = subprocess.run(
            ["docker-compose", "--version"],
            capture_output=True,
            text=True,
            timeout=HEALTH_CHECK_TIMEOUT,
        )
        if result.returncode == 0:
            _compose_cmd = ["docker-compose"]
            return _compose_cmd
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Default to v2 if neither works (will fail with informative error)
    _compose_cmd = ["docker", "compose"]
    return _compose_cmd


class ApiComponent(BaseComponent):
    """
    Manages primemover_api via docker-compose.

    The API stack includes:
    - Apache (web server, ports 8080/8443)
    - PHP-FPM (application runtime)
    - MySQL (database, port 3307)
    - Redis (cache/queues, port 8889)
    - Memcached (additional caching)
    """

    # Container name prefix from docker-compose
    CONTAINER_PREFIX = "primemover"

    # Services in the API stack
    SERVICES = ["apache", "php", "mysql", "redis", "memcached"]

    # Health check timeout (seconds)
    HEALTH_CHECK_TIMEOUT = 60

    def __init__(
        self,
        path: Path,
        http_port: int = 8090,
        mysql_port: int = 3307,
        redis_port: int = 8889,
    ):
        """
        Initialize API component.

        Args:
            path: Path to primemover_api directory
            http_port: HTTP port for Apache
            mysql_port: MySQL port
            redis_port: Redis port
        """
        super().__init__(name="API", path=path)
        self.http_port = http_port
        self.mysql_port = mysql_port
        self.redis_port = redis_port
        self._compose_file = path / "docker-compose.yml"

    def _run_compose(
        self,
        args: list[str],
        capture_output: bool = True,
        timeout: int = COMPOSE_TIMEOUT,
    ) -> subprocess.CompletedProcess:
        """
        Run docker compose command in API directory.

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
        Start the API stack.

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
                message="API is already running",
                url=f"http://localhost:{self.http_port}",
            )

        # Clean up any old containers that might hold ports
        # This handles cases where container names changed (e.g., primemover_api-* -> primemover-*)
        self._cleanup_old_containers()

        # Start docker compose
        result = self._run_compose(["up", "-d", "--build"])

        if result.returncode != 0:
            return ComponentHealth(
                status=ComponentStatus.ERROR,
                message="Failed to start API",
                details={"stderr": result.stderr},
            )

        # Wait for services to be healthy
        if self._wait_for_health():
            return ComponentHealth(
                status=ComponentStatus.RUNNING,
                message="API started successfully",
                url=f"http://localhost:{self.http_port}",
                details={"containers": self._get_container_status()},
            )
        else:
            return ComponentHealth(
                status=ComponentStatus.ERROR,
                message="API started but health checks failed",
                details={"containers": self._get_container_status()},
            )

    def stop(self, remove_volumes: bool = False) -> ComponentHealth:
        """
        Stop the API stack.

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
                message="Failed to stop API",
                details={"stderr": result.stderr},
            )

        return ComponentHealth(
            status=ComponentStatus.STOPPED,
            message="API stopped successfully",
        )

    def status(self) -> ComponentHealth:
        """
        Get current API status with container details.

        Returns:
            ComponentHealth with current status and container info
        """
        containers = self._get_container_status()

        if not containers:
            return ComponentHealth(
                status=ComponentStatus.STOPPED,
                message="API is not running",
            )

        running = sum(1 for c in containers.values() if c.get("running", False))
        total = len(self.SERVICES)

        if running == 0:
            status = ComponentStatus.STOPPED
            message = "API is not running"
        elif running < total:
            status = ComponentStatus.ERROR
            message = f"API partially running ({running}/{total} containers)"
        else:
            # Check HTTP health
            if self._check_http_health():
                status = ComponentStatus.RUNNING
                message = "API is running and healthy"
            else:
                status = ComponentStatus.ERROR
                message = "API containers running but HTTP health check failed"

        # Build detailed info
        details = {"containers": containers}

        # Add health check results if running
        if running > 0:
            details["health"] = {
                "mysql": self._check_mysql_health(),
                "redis": self._check_redis_health(),
                "http": self._check_http_health(),
            }
            details["ports"] = {
                "http": self.http_port,
                "mysql": self.mysql_port,
                "redis": self.redis_port,
            }

        return ComponentHealth(
            status=status,
            message=message,
            url=f"http://localhost:{self.http_port}" if running > 0 else None,
            details=details,
        )

    def is_running(self) -> bool:
        """
        Quick check if API is running.

        Returns:
            True if all core containers are running
        """
        result = self._run_compose(["ps", "--services", "--filter", "status=running"])
        if result.returncode != 0:
            return False

        running_services = result.stdout.strip().split("\n") if result.stdout.strip() else []

        # Core services that must be running
        core_services = {"apache", "php", "mysql"}
        return core_services.issubset(set(running_services))

    def validate(self) -> tuple[bool, list[str]]:
        """
        Validate API component prerequisites.

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        # Check path exists
        if not self.path or not self.path.exists():
            errors.append(f"API path does not exist: {self.path}")
            return False, errors

        # Check docker-compose.yml exists
        if not self._compose_file.exists():
            errors.append(f"docker-compose.yml not found at: {self._compose_file}")

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
                compose_cmd + ["version"],
                capture_output=True,
                text=True,
                timeout=HEALTH_CHECK_TIMEOUT,
            )
            if compose_check.returncode != 0:
                errors.append("Docker Compose is not installed or not accessible")
        except subprocess.TimeoutExpired:
            errors.append("Docker Compose check timed out")

        return len(errors) == 0, errors

    def run_artisan(self, command: str) -> tuple[bool, str]:
        """
        Run a Laravel Artisan command in the PHP container.

        Args:
            command: Artisan command to run (e.g., "migrate", "db:seed")

        Returns:
            Tuple of (success, output)
        """
        result = self._run_compose(["exec", "-T", "php", "php", "artisan"] + command.split())
        return result.returncode == 0, result.stdout + result.stderr

    def _cleanup_old_containers(self) -> None:
        """
        Clean up old API containers that might be holding ports.

        This handles cases where container naming changed (e.g., from primemover_api-*
        to primemover-* after setting COMPOSE_PROJECT_NAME), or where old containers
        from a previous run are still present.
        """
        # Patterns of old container names that might conflict
        old_patterns = [
            "primemover_api-",  # Old naming before COMPOSE_PROJECT_NAME fix
            "primemover-api-",  # Alternative pattern
        ]

        try:
            # Get all containers
            result = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                timeout=HEALTH_CHECK_TIMEOUT,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    container_name = line.strip()
                    if not container_name:
                        continue
                    # Check if this is an old API container
                    for pattern in old_patterns:
                        if container_name.startswith(pattern):
                            # Stop and remove old container
                            subprocess.run(
                                ["docker", "rm", "-f", container_name],
                                capture_output=True,
                                timeout=HEALTH_CHECK_TIMEOUT,
                            )
                            break
        except subprocess.TimeoutExpired:
            pass  # Ignore timeout, best effort cleanup

    def get_network_name(self) -> str | None:
        """
        Get the Docker network name for the API stack.

        Returns:
            Network name or None if not found
        """
        result = self._run_compose(["config", "--format", "json"])
        if result.returncode == 0:
            # The network is typically named primemover-network
            return "primemover-network"
        return None

    def _get_container_status(self) -> dict:
        """Get status of all API containers."""
        result = self._run_compose(["ps", "--format", "json"])

        if result.returncode != 0 or not result.stdout.strip():
            return {}

        containers = {}
        # Parse docker-compose ps output
        for service in self.SERVICES:
            # Try primary container name, then alternatives (e.g., php -> php8.1)
            container_names = [f"{self.CONTAINER_PREFIX}-{service}"]
            if service == "php":
                # PHP container may be versioned (php8.1, php8.2, etc.)
                container_names.extend(
                    [
                        f"{self.CONTAINER_PREFIX}-php8.1",
                        f"{self.CONTAINER_PREFIX}-php8.2",
                        f"{self.CONTAINER_PREFIX}-php8.3",
                    ]
                )

            inspect_result = None
            container_name = container_names[0]

            for name in container_names:
                try:
                    inspect_result = subprocess.run(
                        [
                            "docker",
                            "inspect",
                            "--format",
                            '{"running": {{.State.Running}}, "status": "{{.State.Status}}"}',
                            name,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=DOCKER_INSPECT_TIMEOUT,
                    )
                    if inspect_result.returncode == 0:
                        container_name = name
                        break
                except subprocess.TimeoutExpired:
                    continue

            if inspect_result is None:
                containers[service] = {"running": False, "status": "timeout"}
                continue
            if inspect_result.returncode == 0:
                import json

                try:
                    info = json.loads(inspect_result.stdout.strip())
                    containers[service] = {
                        "name": container_name,
                        "running": info.get("running", False),
                        "status": info.get("status", "unknown"),
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

    def _wait_for_health(self) -> bool:
        """
        Wait for API to become healthy.

        Returns:
            True if healthy within timeout
        """
        start_time = time.time()

        while time.time() - start_time < self.HEALTH_CHECK_TIMEOUT:
            # First check MySQL is ready
            if self._check_mysql_health():
                # Then check HTTP endpoint
                if self._check_http_health():
                    return True
            time.sleep(2)

        return False

    def _check_mysql_health(self) -> bool:
        """Check if MySQL is accepting connections."""
        settings = get_settings()
        mysql_password = settings.pm_mysql_password

        cmd = [
            "docker",
            "exec",
            f"{self.CONTAINER_PREFIX}-mysql",
            "mysqladmin",
            "ping",
            "-h",
            "localhost",
            "-u",
            "root",
        ]

        # Only add password flag if password is configured
        if mysql_password:
            cmd.append(f"-p{mysql_password}")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=HEALTH_CHECK_TIMEOUT
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def _check_http_health(self) -> bool:
        """Check if HTTP endpoint is responding."""
        try:
            response = httpx.get(
                f"http://localhost:{self.http_port}/api/v1/health",
                timeout=5.0,
            )
            return response.status_code in (200, 404)  # 404 is OK if no health endpoint
        except httpx.RequestError:
            # Try simpler endpoint
            try:
                response = httpx.get(
                    f"http://localhost:{self.http_port}/",
                    timeout=5.0,
                )
                return response.status_code < 500
            except httpx.RequestError:
                return False

    def _check_redis_health(self) -> bool:
        """Check if Redis is accepting connections."""
        settings = get_settings()
        redis_password = settings.pm_redis_password

        cmd = [
            "docker",
            "exec",
            f"{self.CONTAINER_PREFIX}-redis",
            "redis-cli",
        ]

        # Only add password flag if password is configured
        if redis_password:
            cmd.extend(["-a", redis_password])

        cmd.append("ping")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=HEALTH_CHECK_TIMEOUT
            )
            return result.returncode == 0 and "PONG" in result.stdout
        except subprocess.TimeoutExpired:
            return False

    def get_health_details(self) -> dict:
        """
        Get detailed health status for all services.

        Returns:
            Dictionary with health status for each service
        """
        health = {
            "mysql": {
                "healthy": self._check_mysql_health(),
                "check": "mysqladmin ping",
            },
            "redis": {
                "healthy": self._check_redis_health(),
                "check": "redis-cli ping",
            },
            "http": {
                "healthy": self._check_http_health(),
                "check": f"HTTP GET :{self.http_port}",
            },
        }
        return health
