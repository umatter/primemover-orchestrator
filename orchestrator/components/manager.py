"""
Component Manager for orchestrating all PrimeMover components.

Provides unified lifecycle management across API, Runner, and Monitoring components.
"""

from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from orchestrator.components.api import ApiComponent
from orchestrator.components.base import BaseComponent, ComponentHealth, ComponentStatus
from orchestrator.components.lambda_server import LambdaServerComponent
from orchestrator.components.monitoring import MonitoringComponent
from orchestrator.components.runner import RunnerComponent


@dataclass
class SystemStatus:
    """Aggregated status of all PrimeMover components."""

    api: ComponentHealth
    runner: ComponentHealth
    monitoring: ComponentHealth
    lambda_server: ComponentHealth | None = None
    overall: ComponentStatus = field(init=False)

    def __post_init__(self):
        """Calculate overall status based on component statuses."""
        statuses = [self.api.status, self.runner.status, self.monitoring.status]
        # Lambda server is optional, only include if present
        if self.lambda_server is not None:
            statuses.append(self.lambda_server.status)

        if all(s == ComponentStatus.RUNNING for s in statuses):
            self.overall = ComponentStatus.RUNNING
        elif all(s == ComponentStatus.STOPPED for s in statuses):
            self.overall = ComponentStatus.STOPPED
        elif any(s == ComponentStatus.ERROR for s in statuses):
            self.overall = ComponentStatus.ERROR
        elif any(s == ComponentStatus.STARTING for s in statuses):
            self.overall = ComponentStatus.STARTING
        elif any(s == ComponentStatus.STOPPING for s in statuses):
            self.overall = ComponentStatus.STOPPING
        else:
            self.overall = ComponentStatus.UNKNOWN

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        result = {
            "overall": self.overall.value,
            "components": {
                "api": self.api.to_dict(),
                "runner": self.runner.to_dict(),
                "monitoring": self.monitoring.to_dict(),
            },
        }
        if self.lambda_server is not None:
            result["components"]["lambda_server"] = self.lambda_server.to_dict()
        return result


class ComponentManager:
    """
    Orchestrates lifecycle management for all PrimeMover components.

    Handles:
    - Starting components in correct order (monitoring -> API -> runner)
    - Stopping components in reverse order
    - Aggregated status display
    - Health checks and validation
    """

    # Startup order: monitoring first, then lambda_server, then API (database), then runner
    # Lambda server starts before runner since runner needs it for local Lambda calls
    STARTUP_ORDER = ["monitoring", "lambda_server", "api", "runner"]

    def __init__(
        self,
        api_path: Path | None = None,
        runner_path: Path | None = None,
        orchestrator_path: Path | None = None,
        api_http_port: int = 8090,
        api_mysql_port: int = 3307,
        api_redis_port: int = 8889,
        grafana_port: int = 3000,
        runner_java_opts: str | None = None,
        runner_spring_profile: str = "local",
        runner_log_path: Path | None = None,
        lambda_server_port: int = 5001,
        lambda_server_enabled: bool = True,
    ):
        """
        Initialize ComponentManager with component paths and configuration.

        Args:
            api_path: Path to primemover_api directory
            runner_path: Path to primemover_runner directory
            orchestrator_path: Path to primemover-orchestrator directory
            api_http_port: API HTTP port
            api_mysql_port: API MySQL port
            api_redis_port: API Redis port
            grafana_port: Grafana port
            runner_java_opts: JVM options for runner
            runner_spring_profile: Spring profile for runner
            runner_log_path: Custom log directory for runner
            lambda_server_port: Port for local Lambda server
            lambda_server_enabled: Whether to enable local Lambda server
        """
        self.console = Console()

        # Initialize components
        self._api: ApiComponent | None = None
        self._runner: RunnerComponent | None = None
        self._monitoring: MonitoringComponent | None = None
        self._lambda_server: LambdaServerComponent | None = None

        if api_path:
            self._api = ApiComponent(
                path=Path(api_path),
                http_port=api_http_port,
                mysql_port=api_mysql_port,
                redis_port=api_redis_port,
            )

        if runner_path:
            self._runner = RunnerComponent(
                path=Path(runner_path),
                java_opts=runner_java_opts,
                spring_profile=runner_spring_profile,
                log_path=runner_log_path,
            )

        if orchestrator_path:
            self._monitoring = MonitoringComponent(
                path=Path(orchestrator_path),
                grafana_port=grafana_port,
            )
            # Lambda server uses orchestrator path for venv and code
            if lambda_server_enabled:
                self._lambda_server = LambdaServerComponent(
                    port=lambda_server_port,
                    orchestrator_path=Path(orchestrator_path),
                )

    @property
    def api(self) -> ApiComponent | None:
        """Get API component."""
        return self._api

    @property
    def runner(self) -> RunnerComponent | None:
        """Get Runner component."""
        return self._runner

    @property
    def monitoring(self) -> MonitoringComponent | None:
        """Get Monitoring component."""
        return self._monitoring

    @property
    def lambda_server(self) -> LambdaServerComponent | None:
        """Get Lambda Server component."""
        return self._lambda_server

    def get_component(self, name: str) -> BaseComponent | None:
        """
        Get a component by name.

        Args:
            name: Component name (api, runner, monitoring, lambda_server)

        Returns:
            Component instance or None
        """
        components = {
            "api": self._api,
            "runner": self._runner,
            "monitoring": self._monitoring,
            "lambda_server": self._lambda_server,
        }
        return components.get(name.lower())

    def start(
        self,
        api: bool = True,
        runner: bool = True,
        monitoring: bool = True,
        lambda_server: bool = True,
    ) -> SystemStatus:
        """
        Start components in correct order.

        Args:
            api: Start API component
            runner: Start Runner component
            monitoring: Start Monitoring component
            lambda_server: Start Lambda Server component

        Returns:
            SystemStatus with results
        """
        # Pre-create directories that Docker would otherwise create as root
        # This prevents permission issues when mounting volumes
        self._ensure_mount_directories()

        results = {}

        # Start in order: monitoring -> lambda_server -> API -> runner
        components_to_start = []
        if monitoring and self._monitoring:
            components_to_start.append(("monitoring", self._monitoring))
        if lambda_server and self._lambda_server:
            components_to_start.append(("lambda_server", self._lambda_server))
        if api and self._api:
            components_to_start.append(("api", self._api))
        if runner and self._runner:
            components_to_start.append(("runner", self._runner))

        for name, component in components_to_start:
            self.console.print(f"Starting {component.name}...", style="bold blue")
            result = component.start()
            results[name] = result

            if result.status == ComponentStatus.RUNNING:
                self.console.print(f"  ✓ {component.name} started", style="green")
                if result.url:
                    self.console.print(f"    URL: {result.url}", style="dim")
            elif result.status == ComponentStatus.ERROR:
                self.console.print(f"  ✗ {component.name} failed: {result.message}", style="red")
            else:
                self.console.print(f"  ⚠ {component.name}: {result.message}", style="yellow")

        # Connect networks if both monitoring and API started
        if monitoring and api and self._monitoring and self._api:
            self._connect_networks()

        return self._build_system_status(results)

    def stop(
        self,
        api: bool = True,
        runner: bool = True,
        monitoring: bool = True,
        lambda_server: bool = True,
        remove_volumes: bool = False,
    ) -> SystemStatus:
        """
        Stop components in reverse order.

        Args:
            api: Stop API component
            runner: Stop Runner component
            monitoring: Stop Monitoring component
            lambda_server: Stop Lambda Server component
            remove_volumes: Remove Docker volumes

        Returns:
            SystemStatus with results
        """
        results = {}

        # Stop in reverse order: runner -> API -> lambda_server -> monitoring
        components_to_stop = []
        if runner and self._runner:
            components_to_stop.append(("runner", self._runner))
        if api and self._api:
            components_to_stop.append(("api", self._api))
        if lambda_server and self._lambda_server:
            components_to_stop.append(("lambda_server", self._lambda_server))
        if monitoring and self._monitoring:
            components_to_stop.append(("monitoring", self._monitoring))

        for name, component in components_to_stop:
            self.console.print(f"Stopping {component.name}...", style="bold blue")
            result = component.stop(remove_volumes=remove_volumes)
            results[name] = result

            if result.status == ComponentStatus.STOPPED:
                self.console.print(f"  ✓ {component.name} stopped", style="green")
            elif result.status == ComponentStatus.ERROR:
                self.console.print(f"  ✗ {component.name} failed: {result.message}", style="red")
            else:
                self.console.print(f"  ⚠ {component.name}: {result.message}", style="yellow")

        return self._build_system_status(results)

    def restart(
        self,
        api: bool = True,
        runner: bool = True,
        monitoring: bool = True,
        lambda_server: bool = True,
    ) -> SystemStatus:
        """
        Restart components.

        Args:
            api: Restart API component
            runner: Restart Runner component
            monitoring: Restart Monitoring component
            lambda_server: Restart Lambda Server component

        Returns:
            SystemStatus with results
        """
        self.stop(api=api, runner=runner, monitoring=monitoring, lambda_server=lambda_server)
        return self.start(
            api=api, runner=runner, monitoring=monitoring, lambda_server=lambda_server
        )

    def status(self) -> SystemStatus:
        """
        Get status of all components.

        Returns:
            SystemStatus with current state
        """
        results = {}

        for name, component in [
            ("api", self._api),
            ("runner", self._runner),
            ("monitoring", self._monitoring),
        ]:
            if component:
                results[name] = component.status()
            else:
                results[name] = ComponentHealth(
                    status=ComponentStatus.UNKNOWN,
                    message=f"{name.title()} component not configured",
                )

        # Lambda server is optional
        if self._lambda_server:
            results["lambda_server"] = self._lambda_server.status()

        return self._build_system_status(results)

    def validate(self) -> tuple[bool, dict[str, list[str]]]:
        """
        Validate all component prerequisites.

        Returns:
            Tuple of (all_valid, errors_by_component)
        """
        all_errors = {}
        all_valid = True

        for name, component in [
            ("api", self._api),
            ("runner", self._runner),
            ("monitoring", self._monitoring),
        ]:
            if component:
                valid, errors = component.validate()
                if not valid:
                    all_valid = False
                    all_errors[name] = errors
            else:
                all_errors[name] = [f"{name.title()} component not configured"]
                all_valid = False

        # Lambda server is optional, only validate if configured
        if self._lambda_server:
            valid, errors = self._lambda_server.validate()
            if not valid:
                all_valid = False
                all_errors["lambda_server"] = errors

        return all_valid, all_errors

    def print_status(self, verbose: bool = False):
        """
        Print formatted status table to console.

        Args:
            verbose: Show detailed sub-component status
        """

        system_status = self.status()

        status_styles = {
            ComponentStatus.RUNNING: "green",
            ComponentStatus.STOPPED: "dim",
            ComponentStatus.STARTING: "yellow",
            ComponentStatus.STOPPING: "yellow",
            ComponentStatus.ERROR: "red",
            ComponentStatus.UNKNOWN: "dim",
        }

        def status_icon(status: ComponentStatus) -> str:
            icons = {
                ComponentStatus.RUNNING: "[green]✓[/green]",
                ComponentStatus.STOPPED: "[dim]○[/dim]",
                ComponentStatus.STARTING: "[yellow]⋯[/yellow]",
                ComponentStatus.STOPPING: "[yellow]⋯[/yellow]",
                ComponentStatus.ERROR: "[red]✗[/red]",
                ComponentStatus.UNKNOWN: "[dim]?[/dim]",
            }
            return icons.get(status, "?")

        if verbose:
            # Detailed tree view with sub-components
            self._print_verbose_status(system_status, status_styles, status_icon)
        else:
            # Compact table view
            self._print_compact_status(system_status, status_styles, status_icon)

        # Print overall status
        overall_style = status_styles.get(system_status.overall, "white")
        self.console.print(
            f"\nOverall: [{overall_style}]{system_status.overall.value.upper()}[/{overall_style}]"
        )

        # Print quick links if anything is running
        self._print_quick_links(system_status)

    def _print_compact_status(
        self, system_status: SystemStatus, status_styles: dict, status_icon
    ) -> None:
        """Print compact status table."""
        table = Table(title="PrimeMover System Status", show_header=True)
        table.add_column("Component", style="bold")
        table.add_column("Status")
        table.add_column("Health")
        table.add_column("Port")
        table.add_column("Details")

        # API row
        api = system_status.api
        api_style = status_styles.get(api.status, "white")
        api_details = []
        if api.details.get("containers"):
            running = sum(1 for c in api.details["containers"].values() if c.get("running", False))
            total = len(api.details["containers"])
            api_details.append(f"{running}/{total} containers")
        table.add_row(
            "API",
            f"[{api_style}]{api.status.value.upper()}[/{api_style}]",
            status_icon(api.status),
            str(self._api.http_port) if self._api and api.is_healthy else "-",
            ", ".join(api_details) if api_details else api.message,
        )

        # Runner row
        runner = system_status.runner
        runner_style = status_styles.get(runner.status, "white")
        runner_details = []
        if runner.details.get("pid"):
            runner_details.append(f"PID: {runner.details['pid']}")
        if runner.details.get("memory_mb"):
            runner_details.append(f"{runner.details['memory_mb']}MB")
        table.add_row(
            "Runner",
            f"[{runner_style}]{runner.status.value.upper()}[/{runner_style}]",
            status_icon(runner.status),
            "-",
            ", ".join(runner_details) if runner_details else runner.message,
        )

        # Monitoring row
        monitoring = system_status.monitoring
        mon_style = status_styles.get(monitoring.status, "white")
        mon_details = []
        if monitoring.details.get("containers"):
            running = sum(
                1 for c in monitoring.details["containers"].values() if c.get("running", False)
            )
            total = len(monitoring.details["containers"])
            mon_details.append(f"{running}/{total} containers")
        table.add_row(
            "Monitoring",
            f"[{mon_style}]{monitoring.status.value.upper()}[/{mon_style}]",
            status_icon(monitoring.status),
            (
                str(self._monitoring.grafana_port)
                if self._monitoring and monitoring.is_healthy
                else "-"
            ),
            ", ".join(mon_details) if mon_details else monitoring.message,
        )

        # Lambda Server row (optional)
        if system_status.lambda_server is not None:
            lambda_srv = system_status.lambda_server
            lambda_style = status_styles.get(lambda_srv.status, "white")
            lambda_details = []
            if lambda_srv.details.get("pid"):
                lambda_details.append(f"PID: {lambda_srv.details['pid']}")
            table.add_row(
                "Lambda Server",
                f"[{lambda_style}]{lambda_srv.status.value.upper()}[/{lambda_style}]",
                status_icon(lambda_srv.status),
                str(lambda_srv.details.get("port", "-")) if lambda_srv.is_healthy else "-",
                ", ".join(lambda_details) if lambda_details else lambda_srv.message,
            )

        self.console.print(table)

    def _print_verbose_status(
        self, system_status: SystemStatus, status_styles: dict, status_icon
    ) -> None:
        """Print detailed status with sub-components."""
        from rich.tree import Tree

        tree = Tree("[bold]PrimeMover System Status[/bold]")

        # API branch
        api = system_status.api
        api_style = status_styles.get(api.status, "white")
        api_port = self._api.http_port if self._api else "?"
        api_branch = tree.add(
            f"{status_icon(api.status)} [bold]API[/bold] "
            f"[{api_style}]{api.status.value.upper()}[/{api_style}] "
            f"[dim]port {api_port}[/dim]"
        )

        if api.details.get("containers"):
            health_checks = api.details.get("health", {})

            for service, info in api.details["containers"].items():
                svc_running = info.get("running", False)
                svc_status = info.get("status", "unknown")
                svc_icon = "[green]✓[/green]" if svc_running else "[red]✗[/red]"
                svc_style = "green" if svc_running else "red"

                # Get port for known services
                port_info = ""
                if service == "apache" and self._api:
                    port_info = f" [dim]:{self._api.http_port}[/dim]"
                elif service == "mysql" and self._api:
                    port_info = f" [dim]:{self._api.mysql_port}[/dim]"
                elif service == "redis" and self._api:
                    port_info = f" [dim]:{self._api.redis_port}[/dim]"

                # Add health check result for specific services
                health_info = ""
                if service == "mysql" and "mysql" in health_checks:
                    health_ok = health_checks["mysql"]
                    health_info = (
                        " [green](healthy)[/green]" if health_ok else " [red](unhealthy)[/red]"
                    )
                elif service == "redis" and "redis" in health_checks:
                    health_ok = health_checks["redis"]
                    health_info = (
                        " [green](healthy)[/green]" if health_ok else " [red](unhealthy)[/red]"
                    )

                api_branch.add(
                    f"{svc_icon} {service.title()}{port_info} "
                    f"[{svc_style}]{svc_status}[/{svc_style}]"
                    f"{health_info} "
                    f"[dim]{info.get('name', '')}[/dim]"
                )

        # Runner branch
        runner = system_status.runner
        runner_style = status_styles.get(runner.status, "white")
        runner_info = ""
        if runner.details.get("pid"):
            runner_info = f" [dim]PID {runner.details['pid']}[/dim]"
        if runner.details.get("memory_mb"):
            runner_info += f" [dim]{runner.details['memory_mb']}MB[/dim]"

        runner_branch = tree.add(
            f"{status_icon(runner.status)} [bold]Runner[/bold] "
            f"[{runner_style}]{runner.status.value.upper()}[/{runner_style}]"
            f"{runner_info}"
        )

        if runner.is_healthy and runner.details.get("profile"):
            runner_branch.add(f"[dim]Spring Profile: {runner.details['profile']}[/dim]")
        if runner.details.get("log_file"):
            runner_branch.add(f"[dim]Log: {runner.details['log_file']}[/dim]")

        # Monitoring branch
        monitoring = system_status.monitoring
        mon_style = status_styles.get(monitoring.status, "white")
        mon_branch = tree.add(
            f"{status_icon(monitoring.status)} [bold]Monitoring[/bold] "
            f"[{mon_style}]{monitoring.status.value.upper()}[/{mon_style}]"
        )

        if monitoring.details.get("containers"):
            for service, info in monitoring.details["containers"].items():
                svc_running = info.get("running", False)
                svc_status = info.get("status", "unknown")
                svc_health = info.get("health", "none")
                svc_icon = "[green]✓[/green]" if svc_running else "[red]✗[/red]"
                svc_style = "green" if svc_running else "red"

                # Get port for known services
                port_info = ""
                if service == "grafana" and self._monitoring:
                    port_info = f" [dim]:{self._monitoring.grafana_port}[/dim]"
                elif service == "loki" and self._monitoring:
                    port_info = f" [dim]:{self._monitoring.loki_port}[/dim]"
                elif service == "prometheus" and self._monitoring:
                    port_info = f" [dim]:{self._monitoring.prometheus_port}[/dim]"
                elif service == "tempo" and self._monitoring:
                    port_info = f" [dim]:{self._monitoring.tempo_port}[/dim]"

                health_info = ""
                if svc_health and svc_health != "none":
                    health_info = f" [dim]({svc_health})[/dim]"

                mon_branch.add(
                    f"{svc_icon} {service.title()}{port_info} "
                    f"[{svc_style}]{svc_status}[/{svc_style}]"
                    f"{health_info}"
                )

        # Lambda Server branch (optional)
        if system_status.lambda_server is not None:
            lambda_srv = system_status.lambda_server
            lambda_style = status_styles.get(lambda_srv.status, "white")
            lambda_info = ""
            if lambda_srv.details.get("pid"):
                lambda_info = f" [dim]PID {lambda_srv.details['pid']}[/dim]"
            if lambda_srv.details.get("port"):
                lambda_info += f" [dim]port {lambda_srv.details['port']}[/dim]"

            lambda_branch = tree.add(
                f"{status_icon(lambda_srv.status)} [bold]Lambda Server[/bold] "
                f"[{lambda_style}]{lambda_srv.status.value.upper()}[/{lambda_style}]"
                f"{lambda_info}"
            )

            if lambda_srv.is_healthy and lambda_srv.details.get("invoke_url"):
                lambda_branch.add(f"[dim]Invoke URL: {lambda_srv.details['invoke_url']}[/dim]")

        self.console.print(tree)

    def _print_quick_links(self, system_status: SystemStatus) -> None:
        """Print quick links section."""

        # Check if any component is running
        if system_status.api.is_healthy or system_status.monitoring.is_healthy:
            self.console.print("\n[bold]Quick Links:[/bold]")

        if system_status.api.is_healthy and self._api:
            base_url = f"http://localhost:{self._api.http_port}"
            self.console.print(f"  API:       {base_url}")
            self.console.print(f"  GUI:       {base_url} [dim](same as API)[/dim]")
            self.console.print(f"  API Docs:  {base_url}/api/documentation")

        if system_status.monitoring.is_healthy and self._monitoring:
            urls = system_status.monitoring.details.get("urls", {})
            if urls.get("grafana"):
                self.console.print(f"  Grafana:   {urls['grafana']} [dim](admin/admin)[/dim]")
            if urls.get("prometheus"):
                self.console.print(f"  Prometheus: {urls['prometheus']}")
            if urls.get("loki"):
                self.console.print(f"  Loki:      {urls['loki']}")

        if system_status.lambda_server and system_status.lambda_server.is_healthy:
            invoke_url = system_status.lambda_server.details.get("invoke_url", "")
            if invoke_url:
                self.console.print(f"  Lambda:    {invoke_url} [dim](local)[/dim]")

    def _ensure_mount_directories(self) -> None:
        """
        Pre-create directories that Docker would mount as volumes.

        When Docker mounts a host directory that doesn't exist, it creates
        it as root:root, which causes permission issues. This method creates
        the directories with proper ownership before Docker starts.
        """
        directories_to_create = []

        # Runner log directory (mounted by promtail in monitoring stack)
        if self._runner:
            runner_log_dir = self._runner.log_path
            if runner_log_dir:
                directories_to_create.append(runner_log_dir)

        for dir_path in directories_to_create:
            if not dir_path.exists():
                try:
                    dir_path.mkdir(parents=True, exist_ok=True)
                except PermissionError:
                    # Directory might already exist with wrong permissions
                    # Let Docker handle it (will fail with clear error)
                    pass

    def _build_system_status(self, results: dict) -> SystemStatus:
        """Build SystemStatus from component results."""
        return SystemStatus(
            api=results.get(
                "api",
                ComponentHealth(
                    status=ComponentStatus.UNKNOWN,
                    message="API component not configured",
                ),
            ),
            runner=results.get(
                "runner",
                ComponentHealth(
                    status=ComponentStatus.UNKNOWN,
                    message="Runner component not configured",
                ),
            ),
            monitoring=results.get(
                "monitoring",
                ComponentHealth(
                    status=ComponentStatus.UNKNOWN,
                    message="Monitoring component not configured",
                ),
            ),
            lambda_server=results.get("lambda_server"),  # Optional, may be None
        )

    def _connect_networks(self):
        """Connect monitoring to API network for log access."""
        if self._monitoring and self._api:
            api_network = self._api.get_network_name()
            if api_network:
                if self._monitoring.connect_to_api_network(api_network):
                    self.console.print(
                        f"  Connected promtail to {api_network}",
                        style="dim green",
                    )
