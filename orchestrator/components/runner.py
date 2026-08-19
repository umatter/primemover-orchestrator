"""
Runner Component for managing primemover_runner as a native Java process.

Manages the Spring Boot Java runner application with PID-based process tracking.
"""

import fcntl
import os
import signal
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from orchestrator.components.base import BaseComponent, ComponentHealth, ComponentStatus

# Subprocess timeout constants (seconds)
COMMAND_TIMEOUT = 30  # 30 sec for general commands


@contextmanager
def _pid_file_lock(pid_file: Path, timeout: float = 5.0) -> Generator[None, None, None]:
    """
    Context manager for PID file locking to prevent race conditions.

    Uses fcntl.flock for atomic locking operations.

    Args:
        pid_file: Path to the PID file
        timeout: Maximum time to wait for lock (seconds)

    Raises:
        TimeoutError: If lock cannot be acquired within timeout
    """
    lock_file = pid_file.with_suffix(".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
    try:
        start_time = time.time()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if time.time() - start_time > timeout:
                    raise TimeoutError(
                        f"Could not acquire lock on {lock_file} within {timeout}s"
                    ) from e
                time.sleep(0.1)

        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


class RunnerComponent(BaseComponent):
    """
    Manages primemover_runner as a native Java process.

    The runner is a Spring Boot application that executes
    automation jobs. It runs as a native process (not containerized)
    with PID file tracking for lifecycle management.
    """

    # RunnerComponent always receives a path (unlike the optional base), so
    # narrow it here rather than guarding every use.
    path: Path

    # PID file location
    PID_FILE = Path("/tmp/primemover-runner.pid")

    # Log file location (relative to runner path)
    LOG_DIR = "primemover-runner/log"

    # Default Java options
    DEFAULT_JAVA_OPTS = "-Xmx2g -Xms512m"

    # Startup timeout (seconds)
    STARTUP_TIMEOUT = 30

    # Shutdown timeout (seconds)
    SHUTDOWN_TIMEOUT = 15

    def __init__(
        self,
        path: Path,
        java_opts: str | None = None,
        spring_profile: str = "local",
        log_path: Path | None = None,
    ):
        """
        Initialize Runner component.

        Args:
            path: Path to primemover_runner directory
            java_opts: JVM options (default: -Xmx2g -Xms512m)
            spring_profile: Spring profile to use (default: local)
            log_path: Custom log directory path (default: path/primemover-runner/log)
        """
        super().__init__(name="Runner", path=path)
        self.java_opts = java_opts or self.DEFAULT_JAVA_OPTS
        self.spring_profile = spring_profile
        self._jar_path: Path | None = None
        self._config_path: Path | None = None
        self._log_path: Path | None = log_path

    @property
    def jar_path(self) -> Path | None:
        """Get path to the runner JAR file."""
        if self._jar_path is None:
            self._jar_path = self._find_jar()
        return self._jar_path

    @property
    def config_path(self) -> Path:
        """Get path to the config directory."""
        if self._config_path is None:
            self._config_path = self.path / "primemover-runner" / "config"
        return self._config_path

    @property
    def log_path(self) -> Path:
        """Get path to the log directory."""
        if self._log_path is not None:
            return self._log_path
        return self.path / self.LOG_DIR

    def _find_jar(self) -> Path | None:
        """Find the runner JAR file."""
        target_dir = self.path / "primemover-runner" / "target"
        if not target_dir.exists():
            return None

        # Find JAR files matching the pattern
        jars = list(target_dir.glob("primemover-runner-*.jar"))
        # Exclude test/sources JARs
        jars = [j for j in jars if "-sources" not in j.name and "-tests" not in j.name]

        if jars:
            # Return the most recently modified
            return max(jars, key=lambda p: p.stat().st_mtime)
        return None

    def start(self) -> ComponentHealth:
        """
        Start the runner process.

        Uses file locking to prevent race conditions with concurrent start attempts.

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

        try:
            with _pid_file_lock(self.PID_FILE):
                # Check if already running (within lock to prevent race)
                pid = self._get_pid()
                if pid and self._is_process_running(pid):
                    return ComponentHealth(
                        status=ComponentStatus.RUNNING,
                        message=f"Runner is already running (PID: {pid})",
                        details={"pid": pid},
                    )

                # Clean up any orphaned browsers from a previous run
                self._cleanup_orphaned_browsers()

                # Build the command
                jar = self.jar_path
                cmd = [
                    "java",
                    *self.java_opts.split(),
                    "-jar",
                    str(jar),
                    f"--spring.profiles.active={self.spring_profile}",
                    f"--spring.config.location={self.config_path}/",
                ]

                # Ensure log directory exists
                self.log_path.mkdir(parents=True, exist_ok=True)

                # TaskOperator.startupChrome builds ChromeOptions without --headless, so
                # plain-Chrome jobs need an X display. On the headless VM that is the VNC
                # display; without DISPLAY, Chrome exits immediately and Selenium reports
                # "session not created: Chrome instance exited". MLX-driven jobs are
                # unaffected (the launcher spawns its own browser), which is why this only
                # surfaces on the "chrome" e2e path.
                env = os.environ.copy()
                env.setdefault("DISPLAY", os.environ.get("PM_RUNNER_DISPLAY", ":1"))

                # Start the process
                log_file = self.log_path / "runner-stdout.log"
                try:
                    with open(log_file, "a") as stdout_log:
                        process = subprocess.Popen(
                            cmd,
                            stdout=stdout_log,
                            stderr=subprocess.STDOUT,
                            cwd=self.path / "primemover-runner",
                            env=env,
                            start_new_session=True,  # Detach from terminal
                        )

                    # Save PID atomically
                    self._write_pid(process.pid)

                    # Wait for startup
                    if self._wait_for_startup(process.pid):
                        return ComponentHealth(
                            status=ComponentStatus.RUNNING,
                            message=f"Runner started (PID: {process.pid})",
                            details={
                                "pid": process.pid,
                                "log_file": str(log_file),
                                "profile": self.spring_profile,
                            },
                        )
                    else:
                        # Process may have failed to start properly
                        if not self._is_process_running(process.pid):
                            return ComponentHealth(
                                status=ComponentStatus.ERROR,
                                message="Runner process exited unexpectedly",
                                details={"log_file": str(log_file)},
                            )
                        else:
                            return ComponentHealth(
                                status=ComponentStatus.STARTING,
                                message="Runner started but not yet responsive",
                                details={"pid": process.pid, "log_file": str(log_file)},
                            )

                except Exception as e:
                    return ComponentHealth(
                        status=ComponentStatus.ERROR,
                        message=f"Failed to start runner: {e}",
                    )

        except TimeoutError as e:
            return ComponentHealth(
                status=ComponentStatus.ERROR,
                message=f"Could not acquire PID file lock: {e}",
            )

    def stop(self, remove_volumes: bool = False) -> ComponentHealth:
        """
        Stop the runner process.

        Uses file locking to prevent race conditions with concurrent stop attempts.

        Args:
            remove_volumes: Ignored for runner (no volumes)

        Returns:
            ComponentHealth with status after stop attempt
        """
        try:
            with _pid_file_lock(self.PID_FILE):
                pid = self._get_pid()

                if not pid:
                    # Clean up stale PID file if exists
                    self._remove_pid_file()
                    return ComponentHealth(
                        status=ComponentStatus.STOPPED,
                        message="Runner is not running",
                    )

                if not self._is_process_running(pid):
                    # Clean up stale PID file
                    self._remove_pid_file()
                    return ComponentHealth(
                        status=ComponentStatus.STOPPED,
                        message="Runner is not running (cleaned up stale PID file)",
                    )

                # Try graceful shutdown first (SIGTERM)
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    self._remove_pid_file()
                    return ComponentHealth(
                        status=ComponentStatus.STOPPED,
                        message="Runner stopped (process not found)",
                    )

                # Wait for graceful shutdown
                for _ in range(self.SHUTDOWN_TIMEOUT):
                    if not self._is_process_running(pid):
                        self._remove_pid_file()
                        orphaned = self._cleanup_orphaned_browsers()
                        msg = "Runner stopped gracefully"
                        if orphaned:
                            msg += f", cleaned up {orphaned} orphaned browser process(es)"
                        return ComponentHealth(
                            status=ComponentStatus.STOPPED,
                            message=msg,
                        )
                    time.sleep(1)

                # Force kill if still running
                try:
                    os.kill(pid, signal.SIGKILL)
                    time.sleep(1)
                except ProcessLookupError:
                    pass

                self._remove_pid_file()

                if self._is_process_running(pid):
                    return ComponentHealth(
                        status=ComponentStatus.ERROR,
                        message=f"Failed to stop runner (PID: {pid})",
                        details={"pid": pid},
                    )

                # Clean up orphaned browser processes left by the runner
                orphaned = self._cleanup_orphaned_browsers()

                msg = "Runner stopped (forced)"
                if orphaned:
                    msg += f", cleaned up {orphaned} orphaned browser process(es)"

                return ComponentHealth(
                    status=ComponentStatus.STOPPED,
                    message=msg,
                )

        except TimeoutError as e:
            return ComponentHealth(
                status=ComponentStatus.ERROR,
                message=f"Could not acquire PID file lock: {e}",
            )

    def status(self) -> ComponentHealth:
        """
        Get current runner status.

        Returns:
            ComponentHealth with current status and details
        """
        pid = self._get_pid()

        if not pid:
            return ComponentHealth(
                status=ComponentStatus.STOPPED,
                message="Runner is not running (no PID file)",
            )

        if not self._is_process_running(pid):
            return ComponentHealth(
                status=ComponentStatus.STOPPED,
                message="Runner is not running (stale PID file)",
                details={"stale_pid": pid},
            )

        # Get process info
        details = self._get_process_details(pid)

        return ComponentHealth(
            status=ComponentStatus.RUNNING,
            message=f"Runner is running (PID: {pid})",
            details=details,
        )

    def is_running(self) -> bool:
        """
        Quick check if runner is running.

        Returns:
            True if runner process is running
        """
        pid = self._get_pid()
        return pid is not None and self._is_process_running(pid)

    def validate(self) -> tuple[bool, list[str]]:
        """
        Validate runner prerequisites.

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        # Check path exists
        if not self.path or not self.path.exists():
            errors.append(f"Runner path does not exist: {self.path}")
            return False, errors

        # Check JAR exists
        jar = self.jar_path
        if not jar or not jar.exists():
            errors.append(
                f"Runner JAR not found. Expected at: {self.path}/primemover-runner/target/primemover-runner-*.jar. "
                "Run 'mvn package' in the runner directory to build."
            )

        # Check config directory exists
        if not self.config_path.exists():
            errors.append(f"Config directory not found: {self.config_path}")

        # Check application.properties exists
        props_file = self.config_path / "application.properties"
        if not props_file.exists():
            errors.append(f"application.properties not found: {props_file}")

        # Check Java is available
        try:
            java_check = subprocess.run(
                ["java", "-version"], capture_output=True, text=True, timeout=COMMAND_TIMEOUT
            )
            if java_check.returncode != 0:
                errors.append("Java is not installed or not in PATH")
            else:
                # Check Java version (need 17+)
                version_output = java_check.stderr  # Java outputs version to stderr
                if "version" in version_output:
                    # Try to extract major version
                    import re

                    match = re.search(r'version "(\d+)', version_output)
                    if match:
                        major_version = int(match.group(1))
                        if major_version < 17:
                            errors.append(f"Java 17+ required, found Java {major_version}")
        except subprocess.TimeoutExpired:
            errors.append("Java version check timed out")

        return len(errors) == 0, errors

    def tail_logs(self, lines: int = 50) -> str:
        """
        Get the last N lines of the runner log.

        Args:
            lines: Number of lines to return

        Returns:
            Log content
        """
        log_file = self.log_path / "runner-stdout.log"
        if not log_file.exists():
            return "No log file found"

        try:
            result = subprocess.run(
                ["tail", "-n", str(lines), str(log_file)],
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT,
            )
            return result.stdout
        except subprocess.TimeoutExpired:
            return "Error: Log tail command timed out"

    def _get_pid(self) -> int | None:
        """Get PID from file if exists."""
        if not self.PID_FILE.exists():
            return None
        try:
            return int(self.PID_FILE.read_text().strip())
        except (ValueError, OSError):
            return None

    def _write_pid(self, pid: int) -> None:
        """Write PID to file atomically using write-and-rename pattern."""
        self.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.PID_FILE.with_suffix(".tmp")
        try:
            temp_file.write_text(str(pid))
            temp_file.rename(self.PID_FILE)
        except Exception:
            # Fall back to direct write if atomic rename fails
            if temp_file.exists():
                temp_file.unlink()
            self.PID_FILE.write_text(str(pid))

    def _remove_pid_file(self) -> None:
        """Remove PID file safely."""
        try:
            if self.PID_FILE.exists():
                self.PID_FILE.unlink()
        except OSError:
            pass  # Ignore errors during cleanup

    def _is_process_running(self, pid: int) -> bool:
        """Check if a process with the given PID is running."""
        try:
            os.kill(pid, 0)  # Signal 0 doesn't kill, just checks
            return True
        except (OSError, ProcessLookupError):
            return False

    def _wait_for_startup(self, pid: int) -> bool:
        """Wait for runner to complete startup."""
        start_time = time.time()

        while time.time() - start_time < self.STARTUP_TIMEOUT:
            if not self._is_process_running(pid):
                return False

            # Check for "Started" message in logs
            log_file = self.log_path / "runner-stdout.log"
            if log_file.exists():
                try:
                    content = log_file.read_text()
                    if (
                        "Started PrimemoverRunnerApplication" in content
                        or "Tomcat started" in content
                    ):
                        return True
                except OSError:
                    pass

            time.sleep(1)

        # Return True if still running (might just be slow to start)
        return self._is_process_running(pid)

    def _get_process_details(self, pid: int) -> dict:
        """Get detailed information about the running process."""
        details: dict[str, Any] = {"pid": pid}

        # Get process info from /proc
        try:
            proc_stat = Path(f"/proc/{pid}/stat").read_text().split()
            # Field 22 is starttime
            details["uptime_seconds"] = int((time.time() * 100 - int(proc_stat[21])) / 100)
        except (OSError, IndexError, ValueError):
            pass

        # Get memory usage
        try:
            proc_status = Path(f"/proc/{pid}/status").read_text()
            for line in proc_status.split("\n"):
                if line.startswith("VmRSS:"):
                    details["memory_mb"] = int(line.split()[1]) // 1024
                    break
        except (OSError, ValueError):
            pass

        details["log_file"] = str(self.log_path / "runner-stdout.log")
        details["profile"] = self.spring_profile

        return details

    def _cleanup_orphaned_browsers(self) -> int:
        """
        Kill orphaned chrome/chromedriver processes left behind by the runner.

        When the runner is killed (especially via SIGKILL), MLX browser processes
        survive as orphans reparented to PID 1. These consume significant memory
        and must be cleaned up.

        Returns:
            Number of processes killed
        """
        import logging

        logger = logging.getLogger(__name__)
        killed = 0

        try:
            # Find chrome/chromedriver processes spawned from MLX deps
            result = subprocess.run(
                ["pgrep", "-f", "mlx/deps/mimic.*/chrome"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]

            if not pids:
                return 0

            # Only kill browsers once the runner itself is gone — at that point every
            # remaining browser process is an orphan. While the runner is alive its
            # browsers are left alone, regardless of who their parent is.
            runner_pid = self._get_pid()
            for pid in pids:
                try:
                    # If the runner is not running, all browser processes are orphans
                    if runner_pid and self._is_process_running(runner_pid):
                        continue  # Runner is still alive, don't touch its browsers

                    os.kill(pid, signal.SIGTERM)
                    killed += 1
                except (OSError, IndexError, ValueError, ProcessLookupError):
                    continue

            if killed:
                # Give them a moment to shut down gracefully
                time.sleep(2)

                # Force kill any that survived
                for pid in pids:
                    try:
                        if self._is_process_running(pid):
                            os.kill(pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass

                logger.info("Cleaned up %d orphaned browser process(es)", killed)

        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            pass

        return killed
