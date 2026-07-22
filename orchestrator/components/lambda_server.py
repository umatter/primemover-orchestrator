"""
Lambda Server Component for running the local Lambda service.

Manages the lifecycle of the local Lambda HTTP server that provides
an alternative to AWS Lambda for the bot-decision-yt function.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import requests

from orchestrator.components.base import BaseComponent, ComponentHealth, ComponentStatus


class LambdaServerComponent(BaseComponent):
    """
    Manages the local Lambda server as a subprocess.

    The Lambda server provides an HTTP endpoint that mirrors AWS Lambda
    invocation, allowing the runner to call the decision function locally.
    """

    DEFAULT_PORT = 5001
    DEFAULT_HOST = "127.0.0.1"
    STARTUP_TIMEOUT = 10  # seconds to wait for server to start
    HEALTH_CHECK_INTERVAL = 0.5  # seconds between health checks during startup

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        host: str = DEFAULT_HOST,
        orchestrator_path: Optional[Path] = None,
    ):
        """
        Initialize Lambda Server component.

        Args:
            port: Port for the Lambda server to listen on
            host: Host address to bind to
            orchestrator_path: Path to orchestrator directory (for venv)
        """
        super().__init__(name="Lambda Server", path=orchestrator_path)
        self.port = port
        self.host = host
        self.orchestrator_path = orchestrator_path or Path(__file__).parent.parent.parent
        self._process: Optional[subprocess.Popen] = None
        self._pid_file = self.orchestrator_path / ".lambda_server.pid"
        self._log_file = self.orchestrator_path / "log" / "lambda-server.log"

    @property
    def url(self) -> str:
        """Get the server URL."""
        return f"http://{self.host}:{self.port}"

    @property
    def invoke_url(self) -> str:
        """Get the invoke endpoint URL."""
        return f"{self.url}/invoke"

    def start(self) -> ComponentHealth:
        """
        Start the Lambda server.

        Returns:
            ComponentHealth with status after start attempt
        """
        # Check if already running
        if self.is_running():
            return ComponentHealth(
                status=ComponentStatus.RUNNING,
                message="Lambda server already running",
                url=self.url,
                details={"pid": self._get_pid(), "port": self.port},
            )

        # Ensure log directory exists
        self._log_file.parent.mkdir(parents=True, exist_ok=True)

        # Build command to run the server
        python_path = self.orchestrator_path / "venv" / "bin" / "python"
        if not python_path.exists():
            python_path = Path(sys.executable)

        cmd = [
            str(python_path),
            "-m", "orchestrator.lambda_service.server",
            "--port", str(self.port),
            "--host", self.host,
        ]

        try:
            # Build environment: inherit current env + load .env file
            env = self._build_env()

            # Start server as subprocess
            # Use decision module directory as cwd so that relative file reads
            # (e.g. mainstream_media_channels.csv) resolve correctly, matching
            # the AWS Lambda task root behavior.
            decision_cwd = self.orchestrator_path / "orchestrator" / "lambda_service" / "decision"
            if not decision_cwd.exists():
                decision_cwd = self.orchestrator_path

            with open(self._log_file, "a") as log_file:
                self._process = subprocess.Popen(
                    cmd,
                    cwd=str(decision_cwd),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=True,  # Detach from parent process
                )

            # Save PID
            self._save_pid(self._process.pid)

            # Wait for server to be ready
            if self._wait_for_healthy():
                return ComponentHealth(
                    status=ComponentStatus.RUNNING,
                    message="Lambda server started",
                    url=self.url,
                    details={"pid": self._process.pid, "port": self.port},
                )
            else:
                # Server didn't become healthy, try to stop it
                self.stop()
                return ComponentHealth(
                    status=ComponentStatus.ERROR,
                    message="Lambda server failed to start (health check failed)",
                    details={"log_file": str(self._log_file)},
                )

        except Exception as e:
            return ComponentHealth(
                status=ComponentStatus.ERROR,
                message=f"Failed to start Lambda server: {e}",
            )

    def stop(self, remove_volumes: bool = False) -> ComponentHealth:
        """
        Stop the Lambda server.

        Args:
            remove_volumes: Ignored for this component

        Returns:
            ComponentHealth with status after stop attempt
        """
        pid = self._get_pid()

        if pid is None:
            return ComponentHealth(
                status=ComponentStatus.STOPPED,
                message="Lambda server not running",
            )

        try:
            # Try graceful shutdown first
            os.kill(pid, signal.SIGTERM)

            # Wait for process to exit
            for _ in range(10):
                try:
                    os.kill(pid, 0)  # Check if process exists
                    time.sleep(0.5)
                except OSError:
                    break
            else:
                # Process didn't exit, force kill
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

            self._remove_pid()

            return ComponentHealth(
                status=ComponentStatus.STOPPED,
                message="Lambda server stopped",
            )

        except OSError as e:
            # Process might already be dead
            self._remove_pid()
            return ComponentHealth(
                status=ComponentStatus.STOPPED,
                message=f"Lambda server stopped (was not running: {e})",
            )

    def status(self) -> ComponentHealth:
        """
        Get current Lambda server status.

        Returns:
            ComponentHealth with current status and details
        """
        pid = self._get_pid()

        if pid is None:
            # No PID file does not mean stopped: on the VM the server runs under the
            # primemover-lambda systemd unit, which never writes one. Trust the health
            # endpoint before reporting STOPPED, otherwise `orchestrator status` always
            # reports the Lambda server down in the documented production deployment.
            if self._check_health():
                return ComponentHealth(
                    status=ComponentStatus.RUNNING,
                    message="Lambda server healthy (externally managed)",
                    url=self.url,
                    details={"port": self.port, "invoke_url": self.invoke_url},
                )
            return ComponentHealth(
                status=ComponentStatus.STOPPED,
                message="Lambda server not running",
            )

        # Check if process is running
        try:
            os.kill(pid, 0)
        except OSError:
            self._remove_pid()
            if self._check_health():
                return ComponentHealth(
                    status=ComponentStatus.RUNNING,
                    message="Lambda server healthy (externally managed)",
                    url=self.url,
                    details={"port": self.port, "invoke_url": self.invoke_url},
                )
            return ComponentHealth(
                status=ComponentStatus.STOPPED,
                message="Lambda server not running (stale PID)",
            )

        # Check health endpoint
        if self._check_health():
            return ComponentHealth(
                status=ComponentStatus.RUNNING,
                message="Lambda server healthy",
                url=self.url,
                details={
                    "pid": pid,
                    "port": self.port,
                    "invoke_url": self.invoke_url,
                },
            )
        else:
            return ComponentHealth(
                status=ComponentStatus.ERROR,
                message="Lambda server running but health check failed",
                details={"pid": pid, "port": self.port},
            )

    def is_running(self) -> bool:
        """
        Quick check if Lambda server is running.

        Returns:
            True if server is running and healthy
        """
        pid = self._get_pid()
        if pid is None:
            # May be running under systemd without a PID file - see status().
            return self._check_health()

        try:
            os.kill(pid, 0)
            return self._check_health()
        except OSError:
            return self._check_health()

    def validate(self) -> tuple[bool, list[str]]:
        """
        Validate Lambda server prerequisites.

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        # Check if decision symlink exists
        decision_path = self.orchestrator_path / "orchestrator" / "lambda_service" / "decision"
        if not decision_path.exists():
            errors.append(
                f"Decision symlink not found: {decision_path}. "
                "Create it with: ln -s /path/to/bot-decision-yt/bot-decision-EN decision"
            )

        # Check if lambda_function.py exists in decision
        lambda_func = decision_path / "lambda_function.py"
        if decision_path.exists() and not lambda_func.exists():
            errors.append(f"lambda_function.py not found in {decision_path}")

        # Check if port is available (only if not already running)
        if not self.is_running():
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind((self.host, self.port))
                sock.close()
            except OSError:
                errors.append(f"Port {self.port} is already in use")

        return len(errors) == 0, errors

    def _build_env(self) -> dict[str, str]:
        """Build subprocess environment by merging os.environ with .env file."""
        env = os.environ.copy()

        env_file = self.orchestrator_path / ".env"
        if env_file.exists():
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key:
                        env[key] = value

        return env

    def _check_health(self) -> bool:
        """Check if the health endpoint responds."""
        try:
            response = requests.get(f"{self.url}/health", timeout=2)
            return response.status_code == 200
        except requests.RequestException:
            return False

    def _wait_for_healthy(self) -> bool:
        """Wait for server to become healthy."""
        start_time = time.time()
        while time.time() - start_time < self.STARTUP_TIMEOUT:
            if self._check_health():
                return True
            time.sleep(self.HEALTH_CHECK_INTERVAL)
        return False

    def _get_pid(self) -> Optional[int]:
        """Get PID from file."""
        if self._pid_file.exists():
            try:
                return int(self._pid_file.read_text().strip())
            except (ValueError, IOError):
                return None
        return None

    def _save_pid(self, pid: int) -> None:
        """Save PID to file."""
        self._pid_file.write_text(str(pid))

    def _remove_pid(self) -> None:
        """Remove PID file."""
        if self._pid_file.exists():
            self._pid_file.unlink()
