"""
PrimeMover Orchestrator CLI

Command-line interface for monitoring, debugging, and managing experiments.
"""

import click
from rich.console import Console
from rich.table import Table

from orchestrator import __version__
from orchestrator.config import get_settings

console = Console()


@click.group()
@click.version_option(version=__version__, prog_name="orchestrator")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """PrimeMover Orchestrator - Monitor, debug, and manage experiments."""
    ctx.ensure_object(dict)
    ctx.obj["settings"] = get_settings()


# =============================================================================
# STATUS COMMAND
# =============================================================================


def _get_component_manager(settings):
    """Create ComponentManager from settings."""
    from orchestrator.components import ComponentManager

    return ComponentManager(
        api_path=settings.get_api_path(),
        runner_path=settings.get_runner_path(),
        orchestrator_path=settings.get_orchestrator_path(),
        api_http_port=settings.pm_api_http_port,
        api_mysql_port=settings.pm_api_mysql_port,
        api_redis_port=settings.pm_api_redis_port,
        grafana_port=settings.grafana_port,
        runner_java_opts=settings.runner_java_opts,
        runner_spring_profile=settings.runner_spring_profile,
    )


@cli.command()
@click.option("-v", "--verbose", is_flag=True, help="Show detailed status")
@click.option("--legacy", is_flag=True, help="Use legacy URL-based status checks")
@click.pass_context
def status(ctx: click.Context, verbose: bool, legacy: bool) -> None:
    """Show system health status."""
    settings = ctx.obj["settings"]

    if legacy:
        # Legacy URL-based status checks
        table = Table(title="PrimeMover System Status (Legacy)")
        table.add_column("Component", style="cyan")
        table.add_column("Status", style="green")
        table.add_column("Details", style="dim")

        # Check Grafana
        grafana_status = _check_url(settings.grafana_url, "/api/health")
        table.add_row("Grafana", grafana_status[0], grafana_status[1])

        # Check Loki
        loki_status = _check_url(settings.loki_url, "/ready")
        table.add_row("Loki", loki_status[0], loki_status[1])

        # Check Prometheus
        prometheus_status = _check_url(settings.prometheus_url, "/-/healthy")
        table.add_row("Prometheus", prometheus_status[0], prometheus_status[1])

        # Check Tempo
        tempo_status = _check_url(settings.tempo_url, "/ready")
        table.add_row("Tempo", tempo_status[0], tempo_status[1])

        # Check PrimeMover API
        api_status = _check_url(settings.pm_api_base_url.rstrip("/api/v1"), "/api/health")
        table.add_row("PrimeMover API", api_status[0], api_status[1])

        console.print(table)
    else:
        # New component-based status
        manager = _get_component_manager(settings)
        manager.print_status(verbose=verbose)

    if verbose:
        console.print("\n[bold]Configuration:[/bold]")
        console.print(f"  Environment: {settings.env}")
        console.print(f"  API Path: {settings.pm_api_path}")
        console.print(f"  Runner Path: {settings.pm_runner_path}")
        console.print(f"  API URL: {settings.pm_api_base_url}")
        console.print(f"  Lambda Log Group: {settings.lambda_log_group}")


def _check_url(base_url: str, path: str) -> tuple[str, str]:
    """Check if a URL is healthy."""
    import httpx

    try:
        url = f"{base_url.rstrip('/')}{path}"
        response = httpx.get(url, timeout=5.0)
        if response.status_code < 400:
            return "[green]OK[/green]", url
        return "[yellow]WARN[/yellow]", f"{response.status_code}"
    except httpx.ConnectError:
        return "[red]DOWN[/red]", "Connection refused"
    except httpx.TimeoutException:
        return "[red]DOWN[/red]", "Timeout"
    except Exception as e:
        return "[red]ERROR[/red]", str(e)[:30]


# =============================================================================
# LIFECYCLE COMMANDS (up, down, restart)
# =============================================================================


@cli.command()
@click.option("--api/--no-api", default=True, help="Start API component")
@click.option("--runner/--no-runner", default=True, help="Start Runner component")
@click.option("--monitoring/--no-monitoring", default=True, help="Start Monitoring component")
@click.pass_context
def up(ctx: click.Context, api: bool, runner: bool, monitoring: bool) -> None:
    """
    Start PrimeMover components.

    By default starts all components in correct order:
    1. Monitoring (Grafana, Loki, Prometheus, Tempo)
    2. API (Apache, PHP, MySQL, Redis)
    3. Runner (Java process)

    Examples:
        orchestrator up                   # Start all components
        orchestrator up --no-runner       # Start API and monitoring only
        orchestrator up --api --no-monitoring  # Start just API
    """
    settings = ctx.obj["settings"]

    if not any([api, runner, monitoring]):
        console.print("[yellow]No components selected. Use --api, --runner, or --monitoring.[/yellow]")
        return

    console.print("[bold]Starting PrimeMover components...[/bold]\n")

    manager = _get_component_manager(settings)
    system_status = manager.start(api=api, runner=runner, monitoring=monitoring)

    console.print()
    if system_status.overall.value == "running":
        console.print("[green bold]All requested components started successfully![/green bold]")
    elif system_status.overall.value == "error":
        console.print("[red bold]Some components failed to start. Check the output above.[/red bold]")
    else:
        console.print(f"[yellow]Components status: {system_status.overall.value}[/yellow]")


@cli.command()
@click.option("--api/--no-api", default=True, help="Stop API component")
@click.option("--runner/--no-runner", default=True, help="Stop Runner component")
@click.option("--monitoring/--no-monitoring", default=True, help="Stop Monitoring component")
@click.option("--volumes", is_flag=True, help="Also remove Docker volumes (data loss!)")
@click.pass_context
def down(ctx: click.Context, api: bool, runner: bool, monitoring: bool, volumes: bool) -> None:
    """
    Stop PrimeMover components.

    By default stops all components in reverse order:
    1. Runner (Java process)
    2. API (Apache, PHP, MySQL, Redis)
    3. Monitoring (Grafana, Loki, Prometheus, Tempo)

    Examples:
        orchestrator down                 # Stop all components
        orchestrator down --no-monitoring # Stop API and Runner only
        orchestrator down --volumes       # Stop all and remove data volumes
    """
    settings = ctx.obj["settings"]

    if not any([api, runner, monitoring]):
        console.print("[yellow]No components selected. Use --api, --runner, or --monitoring.[/yellow]")
        return

    if volumes:
        console.print("[red bold]WARNING: This will remove Docker volumes![/red bold]")
        console.print("[dim]This means database data, metrics, and logs will be lost.[/dim]")
        if not click.confirm("Are you sure?"):
            console.print("Cancelled.")
            return

    console.print("[bold]Stopping PrimeMover components...[/bold]\n")

    manager = _get_component_manager(settings)
    system_status = manager.stop(api=api, runner=runner, monitoring=monitoring, remove_volumes=volumes)

    console.print()
    if system_status.overall.value == "stopped":
        console.print("[green bold]All requested components stopped successfully![/green bold]")
    elif system_status.overall.value == "error":
        console.print("[red bold]Some components failed to stop. Check the output above.[/red bold]")


@cli.command()
@click.option("--api/--no-api", default=True, help="Restart API component")
@click.option("--runner/--no-runner", default=True, help="Restart Runner component")
@click.option("--monitoring/--no-monitoring", default=True, help="Restart Monitoring component")
@click.pass_context
def restart(ctx: click.Context, api: bool, runner: bool, monitoring: bool) -> None:
    """
    Restart PrimeMover components.

    Stops then starts the specified components.

    Examples:
        orchestrator restart              # Restart all components
        orchestrator restart --runner     # Restart just the runner
    """
    settings = ctx.obj["settings"]

    if not any([api, runner, monitoring]):
        console.print("[yellow]No components selected. Use --api, --runner, or --monitoring.[/yellow]")
        return

    console.print("[bold]Restarting PrimeMover components...[/bold]\n")

    manager = _get_component_manager(settings)
    system_status = manager.restart(api=api, runner=runner, monitoring=monitoring)

    console.print()
    if system_status.overall.value == "running":
        console.print("[green bold]All requested components restarted successfully![/green bold]")
    elif system_status.overall.value == "error":
        console.print("[red bold]Some components failed to restart. Check the output above.[/red bold]")


@cli.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """
    Diagnose common setup issues.

    Checks:
    - Docker and docker-compose availability
    - Java installation and version
    - Component paths exist
    - Required ports are available
    - Configuration files exist
    """
    import shutil
    import subprocess
    from pathlib import Path

    settings = ctx.obj["settings"]

    console.print("[bold]Checking system requirements...[/bold]\n")

    all_ok = True

    # Check Docker
    docker_path = shutil.which("docker")
    if docker_path:
        result = subprocess.run(["docker", "--version"], capture_output=True, text=True)
        version = result.stdout.strip().split()[-1] if result.returncode == 0 else "unknown"
        console.print(f"  [green]✓[/green] Docker version {version}")
    else:
        console.print("  [red]✗[/red] Docker not found")
        all_ok = False

    # Check docker-compose
    compose_path = shutil.which("docker-compose")
    if compose_path:
        result = subprocess.run(["docker-compose", "--version"], capture_output=True, text=True)
        version = result.stdout.strip().split()[-1] if result.returncode == 0 else "unknown"
        console.print(f"  [green]✓[/green] Docker Compose version {version}")
    else:
        console.print("  [red]✗[/red] docker-compose not found")
        all_ok = False

    # Check Java
    java_path = shutil.which("java")
    if java_path:
        result = subprocess.run(["java", "-version"], capture_output=True, text=True)
        # Java outputs version to stderr
        output = result.stderr or result.stdout
        if "version" in output:
            import re
            match = re.search(r'version "(\d+)', output)
            version = match.group(1) if match else "unknown"
            if version != "unknown" and int(version) >= 17:
                console.print(f"  [green]✓[/green] Java version {version}")
            else:
                console.print(f"  [yellow]⚠[/yellow] Java version {version} (17+ recommended)")
        else:
            console.print(f"  [green]✓[/green] Java found")
    else:
        console.print("  [yellow]⚠[/yellow] Java not found (needed for Runner)")

    console.print("\n[bold]Checking component paths...[/bold]\n")

    # Check API path
    api_path = settings.get_api_path()
    if api_path.exists():
        compose_file = api_path / "docker-compose.yml"
        if compose_file.exists():
            console.print(f"  [green]✓[/green] API path exists: {api_path}")
        else:
            console.print(f"  [yellow]⚠[/yellow] API path exists but missing docker-compose.yml")
            all_ok = False
    else:
        console.print(f"  [red]✗[/red] API path not found: {api_path}")
        all_ok = False

    # Check Runner path
    runner_path = settings.get_runner_path()
    if runner_path.exists():
        jar_pattern = runner_path / "primemover-runner" / "target" / "primemover-runner-*.jar"
        jars = list((runner_path / "primemover-runner" / "target").glob("primemover-runner-*.jar")) if (runner_path / "primemover-runner" / "target").exists() else []
        if jars:
            console.print(f"  [green]✓[/green] Runner path exists: {runner_path}")
        else:
            console.print(f"  [yellow]⚠[/yellow] Runner path exists but JAR not found (run mvn package)")
    else:
        console.print(f"  [red]✗[/red] Runner path not found: {runner_path}")
        all_ok = False

    # Check Orchestrator path
    orch_path = settings.get_orchestrator_path()
    compose_file = orch_path / "docker-compose.yml"
    if compose_file.exists():
        console.print(f"  [green]✓[/green] Orchestrator path: {orch_path}")
    else:
        console.print(f"  [red]✗[/red] Orchestrator docker-compose.yml not found")
        all_ok = False

    console.print("\n[bold]Checking ports...[/bold]\n")

    import socket

    def check_port(port: int, name: str):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('localhost', port))
        sock.close()
        if result == 0:
            # Port is in use
            return False
        return True

    ports_to_check = [
        (settings.pm_api_http_port, "API HTTP"),
        (settings.pm_api_mysql_port, "MySQL"),
        (settings.grafana_port, "Grafana"),
        (settings.prometheus_port, "Prometheus"),
    ]

    for port, name in ports_to_check:
        if check_port(port, name):
            console.print(f"  [green]✓[/green] Port {port} available ({name})")
        else:
            console.print(f"  [yellow]⚠[/yellow] Port {port} in use ({name}) - component may be running")

    console.print()
    if all_ok:
        console.print("[green bold]All checks passed![/green bold] Run 'orchestrator up' to start.")
    else:
        console.print("[yellow]Some checks failed. Please resolve issues above.[/yellow]")


# =============================================================================
# CONFIG COMMAND GROUP
# =============================================================================


@cli.group()
def config() -> None:
    """Manage orchestrator configuration."""
    pass


@config.command("init")
@click.option("--force", is_flag=True, help="Overwrite existing .env file")
@click.pass_context
def config_init(ctx: click.Context, force: bool) -> None:
    """
    Initialize configuration from template.

    Creates a .env file from .env.example with interactive prompts
    for customizing paths and ports.
    """
    from pathlib import Path

    settings = ctx.obj["settings"]
    orchestrator_path = settings.get_orchestrator_path()

    env_file = orchestrator_path / ".env"
    env_example = orchestrator_path / ".env.example"

    if env_file.exists() and not force:
        console.print(f"[yellow].env file already exists at {env_file}[/yellow]")
        console.print("Use --force to overwrite")
        return

    if not env_example.exists():
        console.print(f"[red].env.example not found at {env_example}[/red]")
        return

    console.print("[bold]Initializing configuration...[/bold]\n")

    # Read the example file
    content = env_example.read_text()

    # Interactive customization
    console.print("Press Enter to accept defaults, or enter custom values:\n")

    # API path
    default_api = "../primemover_api"
    api_path = click.prompt("  API path", default=default_api, show_default=True)
    content = content.replace(f"PM_API_PATH={default_api}", f"PM_API_PATH={api_path}")

    # Runner path
    default_runner = "../primemover_runner"
    runner_path = click.prompt("  Runner path", default=default_runner, show_default=True)
    content = content.replace(f"PM_RUNNER_PATH={default_runner}", f"PM_RUNNER_PATH={runner_path}")

    # API HTTP port
    default_port = "8090"
    api_port = click.prompt("  API HTTP port", default=default_port, show_default=True)
    content = content.replace(f"PM_API_HTTP_PORT={default_port}", f"PM_API_HTTP_PORT={api_port}")

    # Grafana port
    default_grafana = "3000"
    grafana_port = click.prompt("  Grafana port", default=default_grafana, show_default=True)
    content = content.replace(f"GRAFANA_PORT={default_grafana}", f"GRAFANA_PORT={grafana_port}")

    # Write the file
    env_file.write_text(content)

    console.print(f"\n[green]Created {env_file}[/green]")
    console.print("\nRun 'orchestrator config validate' to verify your configuration.")
    console.print("Run 'orchestrator doctor' to check system requirements.")


@config.command("validate")
@click.pass_context
def config_validate(ctx: click.Context) -> None:
    """
    Validate current configuration.

    Checks:
    - Component paths exist
    - Required files present
    - Ports are available or in use by expected services
    """
    import socket
    from pathlib import Path

    settings = ctx.obj["settings"]

    console.print("[bold]Validating configuration...[/bold]\n")

    all_valid = True
    warnings = []

    # Check API path
    api_path = settings.get_api_path()
    if api_path.exists():
        compose_file = api_path / "docker-compose.yml"
        if compose_file.exists():
            console.print(f"  [green]✓[/green] API path: {api_path}")
        else:
            console.print(f"  [red]✗[/red] API path exists but missing docker-compose.yml")
            all_valid = False
    else:
        console.print(f"  [red]✗[/red] API path not found: {api_path}")
        all_valid = False

    # Check Runner path
    runner_path = settings.get_runner_path()
    if runner_path.exists():
        jar_dir = runner_path / "primemover-runner" / "target"
        if jar_dir.exists():
            jars = list(jar_dir.glob("primemover-runner-*.jar"))
            jars = [j for j in jars if "-sources" not in j.name]
            if jars:
                console.print(f"  [green]✓[/green] Runner path: {runner_path}")
                console.print(f"      JAR: {jars[0].name}")
            else:
                console.print(f"  [yellow]⚠[/yellow] Runner path exists but JAR not built")
                warnings.append("Run 'mvn package' in the runner directory")
        else:
            console.print(f"  [yellow]⚠[/yellow] Runner path exists but target/ not found")
            warnings.append("Run 'mvn package' in the runner directory")
    else:
        console.print(f"  [red]✗[/red] Runner path not found: {runner_path}")
        all_valid = False

    # Check Orchestrator path
    orch_path = settings.get_orchestrator_path()
    compose_file = orch_path / "docker-compose.yml"
    if compose_file.exists():
        console.print(f"  [green]✓[/green] Orchestrator path: {orch_path}")
    else:
        console.print(f"  [red]✗[/red] Orchestrator docker-compose.yml not found")
        all_valid = False

    console.print("\n[bold]Checking ports...[/bold]\n")

    def check_port(port: int) -> tuple[bool, str]:
        """Check if port is available or in use."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('localhost', port))
        sock.close()
        if result == 0:
            return False, "in use"
        return True, "available"

    ports = [
        (settings.pm_api_http_port, "API HTTP"),
        (settings.pm_api_mysql_port, "MySQL"),
        (settings.pm_api_redis_port, "Redis"),
        (settings.grafana_port, "Grafana"),
        (settings.loki_port, "Loki"),
        (settings.prometheus_port, "Prometheus"),
        (settings.tempo_port, "Tempo"),
    ]

    for port, name in ports:
        available, status = check_port(port)
        if available:
            console.print(f"  [green]✓[/green] Port {port} ({name}): available")
        else:
            console.print(f"  [yellow]⚠[/yellow] Port {port} ({name}): in use")

    console.print()
    if all_valid:
        console.print("[green bold]Configuration is valid![/green bold]")
    else:
        console.print("[red bold]Configuration has errors. Please fix issues above.[/red bold]")

    if warnings:
        console.print("\n[yellow]Warnings:[/yellow]")
        for w in warnings:
            console.print(f"  - {w}")


@config.command("show")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def config_show(ctx: click.Context, as_json: bool) -> None:
    """
    Display effective configuration.

    Shows all configuration values from environment and .env file.
    """
    import json

    settings = ctx.obj["settings"]

    config_data = {
        "environment": settings.env,
        "paths": {
            "api": str(settings.get_api_path()),
            "runner": str(settings.get_runner_path()),
            "orchestrator": str(settings.get_orchestrator_path()),
            "gui": settings.pm_gui_path,
        },
        "ports": {
            "api_http": settings.pm_api_http_port,
            "api_mysql": settings.pm_api_mysql_port,
            "api_redis": settings.pm_api_redis_port,
            "grafana": settings.grafana_port,
            "loki": settings.loki_port,
            "prometheus": settings.prometheus_port,
            "tempo": settings.tempo_port,
        },
        "runner": {
            "java_opts": settings.runner_java_opts,
            "spring_profile": settings.runner_spring_profile,
        },
        "api": {
            "base_url": settings.pm_api_base_url,
            "email": settings.pm_api_email,
        },
        "aws": {
            "region": settings.aws_region,
            "lambda_function": settings.lambda_function_name,
            "lambda_log_group": settings.lambda_log_group,
        },
    }

    if as_json:
        console.print(json.dumps(config_data, indent=2))
    else:
        console.print("[bold]Effective Configuration[/bold]\n")

        console.print("[bold cyan]Environment:[/bold cyan]")
        console.print(f"  {settings.env}")

        console.print("\n[bold cyan]Paths:[/bold cyan]")
        console.print(f"  API:          {settings.get_api_path()}")
        console.print(f"  Runner:       {settings.get_runner_path()}")
        console.print(f"  Orchestrator: {settings.get_orchestrator_path()}")
        console.print(f"  GUI:          {settings.pm_gui_path}")

        console.print("\n[bold cyan]Ports:[/bold cyan]")
        console.print(f"  API HTTP:     {settings.pm_api_http_port}")
        console.print(f"  MySQL:        {settings.pm_api_mysql_port}")
        console.print(f"  Redis:        {settings.pm_api_redis_port}")
        console.print(f"  Grafana:      {settings.grafana_port}")
        console.print(f"  Loki:         {settings.loki_port}")
        console.print(f"  Prometheus:   {settings.prometheus_port}")
        console.print(f"  Tempo:        {settings.tempo_port}")

        console.print("\n[bold cyan]Runner:[/bold cyan]")
        console.print(f"  Java Opts:    {settings.runner_java_opts}")
        console.print(f"  Profile:      {settings.runner_spring_profile}")

        console.print("\n[bold cyan]API Connection:[/bold cyan]")
        console.print(f"  Base URL:     {settings.pm_api_base_url}")
        console.print(f"  Email:        {settings.pm_api_email}")

        console.print("\n[bold cyan]AWS:[/bold cyan]")
        console.print(f"  Region:       {settings.aws_region}")
        console.print(f"  Lambda:       {settings.lambda_function_name}")


# =============================================================================
# DATABASE COMMAND GROUP
# =============================================================================


@cli.group()
def db() -> None:
    """Manage API database (migrations, seeding)."""
    pass


@db.command("migrate")
@click.option("--fresh", is_flag=True, help="Drop all tables and re-run migrations")
@click.option("--seed", is_flag=True, help="Seed database after migration")
@click.option("--force", is_flag=True, help="Force in production environment")
@click.pass_context
def db_migrate(ctx: click.Context, fresh: bool, seed: bool, force: bool) -> None:
    """
    Run database migrations.

    Examples:
        orchestrator db migrate           # Run pending migrations
        orchestrator db migrate --fresh   # Fresh migration (drops all tables)
        orchestrator db migrate --seed    # Migrate and seed
    """
    settings = ctx.obj["settings"]
    manager = _get_component_manager(settings)

    # Check API is running
    if not manager.api.is_running():
        console.print("[red]API is not running. Start it first with 'orchestrator up --api'[/red]")
        return

    if fresh:
        console.print("[yellow]Running fresh migration (dropping all tables)...[/yellow]")
        if not force:
            if not click.confirm("This will DELETE all data. Are you sure?"):
                console.print("Cancelled.")
                return

        cmd = "migrate:fresh"
        if seed:
            cmd += " --seed"
        if force:
            cmd += " --force"
    else:
        cmd = "migrate"
        if force:
            cmd += " --force"

    console.print(f"[dim]Running: php artisan {cmd}[/dim]\n")

    success, output = manager.api.run_artisan(cmd)

    if success:
        console.print("[green]Migration completed successfully![/green]")
        if seed and not fresh:
            # Run seed separately
            console.print("\n[dim]Running seeder...[/dim]")
            seed_success, seed_output = manager.api.run_artisan("db:seed")
            if seed_success:
                console.print("[green]Seeding completed![/green]")
            else:
                console.print(f"[red]Seeding failed:[/red]\n{seed_output}")
    else:
        console.print(f"[red]Migration failed:[/red]\n{output}")


@db.command("seed")
@click.option("--class", "seeder_class", default="", help="Specific seeder class to run")
@click.option("--force", is_flag=True, help="Force in production environment")
@click.pass_context
def db_seed(ctx: click.Context, seeder_class: str, force: bool) -> None:
    """
    Seed the database with test data.

    Examples:
        orchestrator db seed                      # Run default seeder
        orchestrator db seed --class=UserSeeder  # Run specific seeder
    """
    settings = ctx.obj["settings"]
    manager = _get_component_manager(settings)

    if not manager.api.is_running():
        console.print("[red]API is not running. Start it first with 'orchestrator up --api'[/red]")
        return

    cmd = "db:seed"
    if seeder_class:
        cmd += f" --class={seeder_class}"
    if force:
        cmd += " --force"

    console.print(f"[dim]Running: php artisan {cmd}[/dim]\n")

    success, output = manager.api.run_artisan(cmd)

    if success:
        console.print("[green]Seeding completed successfully![/green]")
    else:
        console.print(f"[red]Seeding failed:[/red]\n{output}")


@db.command("reset")
@click.option("--force", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def db_reset(ctx: click.Context, force: bool) -> None:
    """
    Reset database: fresh migration + seed.

    This drops all tables, runs migrations, and seeds the database.
    Equivalent to 'orchestrator db migrate --fresh --seed'.
    """
    settings = ctx.obj["settings"]
    manager = _get_component_manager(settings)

    if not manager.api.is_running():
        console.print("[red]API is not running. Start it first with 'orchestrator up --api'[/red]")
        return

    console.print("[yellow bold]DATABASE RESET[/yellow bold]")
    console.print("[yellow]This will DROP all tables and recreate the database![/yellow]")

    if not force:
        if not click.confirm("Are you sure you want to reset the database?"):
            console.print("Cancelled.")
            return

    console.print("\n[dim]Running: php artisan migrate:fresh --seed --force[/dim]\n")

    success, output = manager.api.run_artisan("migrate:fresh --seed --force")

    if success:
        console.print("[green bold]Database reset completed![/green bold]")
        console.print("[dim]Default users: admin@primemover.example / password[/dim]")
    else:
        console.print(f"[red]Reset failed:[/red]\n{output}")


@db.command("status")
@click.pass_context
def db_status(ctx: click.Context) -> None:
    """
    Show database migration status.

    Lists all migrations and their status (pending/ran).
    """
    settings = ctx.obj["settings"]
    manager = _get_component_manager(settings)

    if not manager.api.is_running():
        console.print("[red]API is not running. Start it first with 'orchestrator up --api'[/red]")
        return

    console.print("[bold]Migration Status[/bold]\n")

    success, output = manager.api.run_artisan("migrate:status")

    if success:
        # Parse and format the output
        lines = output.strip().split('\n')
        for line in lines:
            if 'Ran' in line or '✓' in line or 'Yes' in line:
                console.print(f"  [green]✓[/green] {line.strip()}")
            elif 'Pending' in line or '✗' in line or 'No' in line:
                console.print(f"  [yellow]○[/yellow] {line.strip()}")
            elif line.strip():
                console.print(f"  {line.strip()}")
    else:
        console.print(f"[red]Failed to get status:[/red]\n{output}")


@db.command("clear-test")
@click.option("--force", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def db_clear_test(ctx: click.Context, force: bool) -> None:
    """
    Clear all test data (experiments, crawlers, jobs, etc).

    Preserves users and system data. Useful for resetting the test environment
    without dropping all tables.
    """
    settings = ctx.obj["settings"]
    manager = _get_component_manager(settings)

    if not manager.api.is_running():
        console.print("[red]API is not running. Start it first with 'orchestrator up --api'[/red]")
        return

    console.print("[yellow]Clear Test Data[/yellow]")
    console.print("[dim]This removes experiments, crawlers, agents, proxies, jobs, etc.[/dim]")
    console.print("[dim]Users and system data will be preserved.[/dim]")

    if not force:
        if not click.confirm("Proceed?"):
            console.print("Cancelled.")
            return

    console.print("\n[dim]Running: php artisan data:clear-test --force[/dim]\n")

    success, output = manager.api.run_artisan("data:clear-test --force")

    if success:
        console.print("[green]Test data cleared![/green]")
    else:
        console.print(f"[red]Failed:[/red]\n{output}")


# =============================================================================
# LOGS COMMAND GROUP
# =============================================================================


@cli.group()
def logs() -> None:
    """Query and analyze logs from all components."""
    pass


@logs.command("query")
@click.option(
    "-c",
    "--component",
    type=click.Choice(["all", "api", "runner", "lambda"]),
    default="all",
    help="Component to query",
)
@click.option("-t", "--time-range", default="1h", help="Time range (15m, 1h, 24h, 7d)")
@click.option(
    "-l",
    "--level",
    type=click.Choice(["all", "debug", "info", "warning", "error"]),
    default="all",
    help="Log level filter",
)
@click.option("-s", "--search", default="", help="Search pattern")
@click.option("-e", "--experiment-id", type=int, help="Filter by experiment ID")
@click.option("--crawler-id", type=int, help="Filter by crawler ID")
@click.option("--job-id", type=int, help="Filter by job ID")
@click.option("-n", "--limit", default=100, help="Max results")
@click.option("-f", "--format", "output_format", type=click.Choice(["table", "json", "plain"]), default="plain")
@click.pass_context
def logs_query(
    ctx: click.Context,
    component: str,
    time_range: str,
    level: str,
    search: str,
    experiment_id: int | None,
    crawler_id: int | None,
    job_id: int | None,
    limit: int,
    output_format: str,
) -> None:
    """Query logs from all PrimeMover components."""
    from orchestrator.logs.aggregator import LogAggregator

    aggregator = LogAggregator()

    console.print(f"[dim]Querying logs: component={component}, time={time_range}, level={level}[/dim]")
    if search:
        console.print(f"[dim]Search: {search}[/dim]")
    if experiment_id:
        console.print(f"[dim]Experiment: {experiment_id}[/dim]")
    console.print()

    try:
        result = aggregator.query(
            component=component if component != "all" else None,
            level=level if level != "all" else None,
            search=search or None,
            experiment_id=experiment_id,
            crawler_id=crawler_id,
            job_id=job_id,
            time_range=time_range,
            limit=limit,
        )

        if not result.entries:
            console.print("[yellow]No logs found matching criteria[/yellow]")
            console.print(f"[dim]Sources checked: {', '.join(result.sources.keys())}[/dim]")
            return

        # Show source summary
        source_info = ", ".join(f"{k}: {v}" for k, v in result.sources.items())
        console.print(f"[dim]Found {result.total_count} entries from {source_info}[/dim]\n")

        if output_format == "json":
            import json
            entries = [
                {
                    "timestamp": e.timestamp.isoformat(),
                    "component": e.component,
                    "level": e.level,
                    "message": e.message,
                }
                for e in result.entries
            ]
            console.print(json.dumps(entries, indent=2))

        elif output_format == "table":
            table = Table()
            table.add_column("Time", style="dim")
            table.add_column("Component", style="cyan")
            table.add_column("Level")
            table.add_column("Message")

            for entry in result.entries:
                level_style = {
                    "error": "red",
                    "warning": "yellow",
                    "warn": "yellow",
                    "info": "green",
                    "debug": "dim",
                }.get(entry.level.lower(), "")

                table.add_row(
                    entry.timestamp.strftime("%H:%M:%S"),
                    entry.component,
                    f"[{level_style}]{entry.level}[/{level_style}]" if level_style else entry.level,
                    entry.message[:100],
                )
            console.print(table)

        else:  # plain
            for entry in result.entries:
                level_style = {
                    "error": "red",
                    "warning": "yellow",
                    "warn": "yellow",
                    "info": "green",
                    "debug": "dim",
                }.get(entry.level.lower(), "")

                time_str = entry.timestamp.strftime("%H:%M:%S")
                comp = f"[cyan]{entry.component:6}[/cyan]"
                lvl = f"[{level_style}]{entry.level:5}[/{level_style}]" if level_style else entry.level

                console.print(f"[dim]{time_str}[/dim] {comp} {lvl} {entry.message[:150]}")

    except Exception as e:
        console.print(f"[red]Error querying logs: {e}[/red]")
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/dim]")
    finally:
        aggregator.close()


@logs.command("errors")
@click.option("-t", "--time-range", default="24h", help="Time range")
@click.option(
    "-c",
    "--component",
    type=click.Choice(["all", "api", "runner", "lambda"]),
    default="all",
)
@click.pass_context
def logs_errors(ctx: click.Context, time_range: str, component: str) -> None:
    """Get error summary and patterns."""
    from collections import Counter
    from orchestrator.logs.aggregator import LogAggregator

    console.print(f"[dim]Fetching errors from last {time_range}...[/dim]\n")

    aggregator = LogAggregator()

    try:
        result = aggregator.query(
            component=component if component != "all" else None,
            level="error",
            time_range=time_range,
            limit=500,
        )

        if not result.entries:
            console.print("[green]No errors found![/green]")
            return

        # Group by component
        by_component: Counter = Counter()
        by_message: Counter = Counter()

        for entry in result.entries:
            by_component[entry.component] += 1
            # Truncate message for grouping
            msg_key = entry.message[:80].strip()
            by_message[msg_key] += 1

        # Summary table
        console.print(f"[bold]Total Errors: {len(result.entries)}[/bold]\n")

        console.print("[bold]By Component:[/bold]")
        for comp, count in by_component.most_common():
            console.print(f"  [cyan]{comp}[/cyan]: {count}")

        console.print(f"\n[bold]Top Error Messages:[/bold]")
        for msg, count in by_message.most_common(10):
            console.print(f"  [red]{count:3}x[/red] {msg[:70]}...")

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
    finally:
        aggregator.close()


# =============================================================================
# WATCH COMMAND
# =============================================================================


@cli.command()
@click.option(
    "-c",
    "--component",
    type=click.Choice(["all", "api", "runner", "lambda"]),
    default="all",
)
@click.option("-l", "--level", default="info")
@click.option("-e", "--experiment-id", type=int)
@click.option("--crawler-id", type=int)
@click.option("-f", "--filter", "search_filter", default="", help="Filter pattern")
@click.pass_context
def watch(
    ctx: click.Context,
    component: str,
    level: str,
    experiment_id: int | None,
    crawler_id: int | None,
    search_filter: str,
) -> None:
    """
    Stream logs in real-time (like tail -f).

    Press Ctrl+C to stop.
    """
    from orchestrator.logs.aggregator import LogAggregator

    console.print("[bold]Watching logs...[/bold] (Press Ctrl+C to stop)\n")
    console.print(f"[dim]Component: {component}, Level: {level}[/dim]")

    if experiment_id:
        console.print(f"[dim]Experiment: {experiment_id}[/dim]")
    if search_filter:
        console.print(f"[dim]Filter: {search_filter}[/dim]")

    console.print()

    aggregator = LogAggregator()
    level_order = {"debug": 0, "info": 1, "warning": 2, "warn": 2, "error": 3}
    min_level = level_order.get(level.lower(), 1)

    try:
        for entry in aggregator.tail(
            component=component if component != "all" else None,
            level=level if level != "all" else None,
            search=search_filter or None,
        ):
            # Apply level filter
            entry_level = level_order.get(entry.level.lower(), 1)
            if entry_level < min_level:
                continue

            # Apply experiment/crawler filter
            if experiment_id and entry.experiment_id != experiment_id:
                continue
            if crawler_id and entry.crawler_id != crawler_id:
                continue

            # Format and print
            level_style = {
                "error": "red",
                "warning": "yellow",
                "warn": "yellow",
                "info": "green",
                "debug": "dim",
            }.get(entry.level.lower(), "")

            time_str = entry.timestamp.strftime("%H:%M:%S")
            comp = f"[cyan]{entry.component:6}[/cyan]"
            lvl = f"[{level_style}]{entry.level:5}[/{level_style}]" if level_style else entry.level

            console.print(f"[dim]{time_str}[/dim] {comp} {lvl} {entry.message[:150]}")

    except KeyboardInterrupt:
        console.print("\n[dim]Stopped watching.[/dim]")
    finally:
        aggregator.close()


# =============================================================================
# TRACE COMMAND GROUP
# =============================================================================


@cli.group()
def trace() -> None:
    """Trace experiment and job execution flow."""
    pass


@trace.command("experiment")
@click.argument("experiment_id", type=int)
@click.option("--jobs/--no-jobs", default=False, help="Include job-level details")
@click.option("--crawlers/--no-crawlers", default=True, help="Include crawler-level details")
@click.option("-t", "--time-range", default="7d", help="Time range for log queries")
@click.option("-f", "--format", "output_format", type=click.Choice(["tree", "json", "summary"]), default="tree")
@click.pass_context
def trace_experiment(
    ctx: click.Context,
    experiment_id: int,
    jobs: bool,
    crawlers: bool,
    time_range: str,
    output_format: str,
) -> None:
    """Trace full execution flow for an experiment."""
    from orchestrator.tracing import TraceBuilder

    console.print(f"[bold]Tracing experiment {experiment_id}...[/bold]\n")

    builder = TraceBuilder()

    try:
        trace = builder.trace_experiment(
            experiment_id,
            include_crawlers=crawlers,
            include_jobs=jobs,
            time_range=time_range,
        )

        if output_format == "json":
            console.print(trace.to_json())
        elif output_format == "summary":
            _print_trace_summary(trace)
        else:  # tree
            _print_trace_tree(trace)

    except Exception as e:
        console.print(f"[red]Error tracing experiment: {e}[/red]")
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/dim]")
    finally:
        builder.close()


@trace.command("crawler")
@click.argument("crawler_id", type=int)
@click.option("--jobs/--no-jobs", default=True, help="Include job-level details")
@click.option("-t", "--time-range", default="7d", help="Time range for log queries")
@click.option("-f", "--format", "output_format", type=click.Choice(["tree", "json", "summary"]), default="tree")
@click.pass_context
def trace_crawler(
    ctx: click.Context,
    crawler_id: int,
    jobs: bool,
    time_range: str,
    output_format: str,
) -> None:
    """Trace execution for a specific crawler."""
    from orchestrator.tracing import TraceBuilder

    console.print(f"[bold]Tracing crawler {crawler_id}...[/bold]\n")

    builder = TraceBuilder()

    try:
        trace = builder.trace_crawler(
            crawler_id,
            include_jobs=jobs,
            time_range=time_range,
        )

        if output_format == "json":
            console.print(trace.to_json())
        elif output_format == "summary":
            _print_trace_summary(trace)
        else:  # tree
            _print_trace_tree(trace)

    except Exception as e:
        console.print(f"[red]Error tracing crawler: {e}[/red]")
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/dim]")
    finally:
        builder.close()


@trace.command("job")
@click.argument("job_id", type=int)
@click.option("--include-logs/--no-logs", "include_logs", default=True, help="Show log entries")
@click.option("-t", "--time-range", default="7d", help="Time range for log queries")
@click.option("-f", "--format", "output_format", type=click.Choice(["tree", "json", "summary"]), default="tree")
@click.pass_context
def trace_job(
    ctx: click.Context,
    job_id: int,
    include_logs: bool,
    time_range: str,
    output_format: str,
) -> None:
    """Get detailed execution trace for a job."""
    from orchestrator.tracing import TraceBuilder

    console.print(f"[bold]Tracing job {job_id}...[/bold]\n")

    builder = TraceBuilder()

    try:
        trace = builder.trace_job(job_id, time_range=time_range)

        if output_format == "json":
            console.print(trace.to_json())
        elif output_format == "summary":
            _print_trace_summary(trace)
        else:  # tree
            _print_trace_tree(trace, show_logs=include_logs)

    except Exception as e:
        console.print(f"[red]Error tracing job: {e}[/red]")
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/dim]")
    finally:
        builder.close()


def _print_trace_summary(trace) -> None:
    """Print a summary of a trace."""
    summary = trace.to_dict()["summary"]

    console.print(f"[bold]{trace.entity_type.title()}:[/bold] {trace.entity_id}")

    if trace.entity_data.get("name"):
        console.print(f"[bold]Name:[/bold] {trace.entity_data['name']}")

    console.print()
    console.print(f"Total Spans:  {summary['total_spans']}")
    console.print(f"Total Logs:   {summary['total_logs']}")
    console.print(f"Errors:       {summary['error_count']}")

    if summary.get("first_event"):
        console.print(f"First Event:  {summary['first_event']}")
    if summary.get("last_event"):
        console.print(f"Last Event:   {summary['last_event']}")
    if summary.get("duration_ms"):
        duration = summary["duration_ms"] / 1000
        if duration > 3600:
            console.print(f"Duration:     {duration / 3600:.1f} hours")
        elif duration > 60:
            console.print(f"Duration:     {duration / 60:.1f} minutes")
        else:
            console.print(f"Duration:     {duration:.1f} seconds")

    if trace.root_span and trace.root_span.errors:
        console.print()
        console.print("[bold red]Errors:[/bold red]")
        for err in trace.root_span.errors[:5]:
            console.print(f"  [red]- {err[:100]}[/red]")


def _print_trace_tree(trace, indent: int = 0, show_logs: bool = False) -> None:
    """Print a trace as a tree structure."""
    from rich.tree import Tree
    from rich.panel import Panel

    def _status_style(status: str) -> str:
        return {
            "success": "green",
            "failed": "red",
            "running": "yellow",
            "pending": "dim",
            "unknown": "dim",
        }.get(status, "dim")

    def _add_span_to_tree(tree, span) -> None:
        status_style = _status_style(span.status.value if hasattr(span.status, 'value') else span.status)
        status_icon = {
            "success": "[green]✓[/green]",
            "failed": "[red]✗[/red]",
            "running": "[yellow]⋯[/yellow]",
            "pending": "[dim]○[/dim]",
            "unknown": "[dim]?[/dim]",
        }.get(span.status.value if hasattr(span.status, 'value') else span.status, "?")

        # Build span label
        component = f"[cyan]{span.component}[/cyan]"
        duration = ""
        if span.duration_ms:
            if span.duration_ms > 60000:
                duration = f" ({span.duration_ms / 60000:.1f}m)"
            elif span.duration_ms > 1000:
                duration = f" ({span.duration_ms / 1000:.1f}s)"
            else:
                duration = f" ({span.duration_ms:.0f}ms)"

        label = f"{status_icon} {span.name} {component}{duration}"

        # Add metadata
        if span.job_id:
            label += f" [dim]job:{span.job_id}[/dim]"
        elif span.crawler_id:
            label += f" [dim]cr:{span.crawler_id}[/dim]"

        branch = tree.add(label)

        # Show errors
        if span.errors:
            for err in span.errors[:3]:
                branch.add(f"[red]ERROR: {err[:80]}...[/red]")

        # Show logs if requested
        if show_logs and span.logs:
            logs_branch = branch.add(f"[dim]Logs ({len(span.logs)})[/dim]")
            for log in span.logs[:10]:
                level_style = {
                    "error": "red",
                    "warning": "yellow",
                    "info": "green",
                    "debug": "dim",
                }.get(log.level.lower(), "")
                time_str = log.timestamp.strftime("%H:%M:%S") if log.timestamp else "?"
                msg = log.message[:80]
                if level_style:
                    logs_branch.add(f"[dim]{time_str}[/dim] [{level_style}]{log.level}[/{level_style}] {msg}")
                else:
                    logs_branch.add(f"[dim]{time_str}[/dim] {log.level} {msg}")

        # Add children
        for child in span.children:
            _add_span_to_tree(branch, child)

    # Build the tree
    root_status = _status_style(
        trace.root_span.status.value if trace.root_span and hasattr(trace.root_span.status, 'value')
        else "unknown"
    )
    tree_title = f"[bold]{trace.entity_type.title()} {trace.entity_id}[/bold]"

    if trace.entity_data.get("name"):
        tree_title += f" - {trace.entity_data['name']}"

    tree = Tree(tree_title)

    if trace.root_span:
        _add_span_to_tree(tree, trace.root_span)

    console.print(tree)

    # Print summary footer
    summary = trace.to_dict()["summary"]
    console.print()
    console.print(
        f"[dim]Spans: {summary['total_spans']} | "
        f"Logs: {summary['total_logs']} | "
        f"Errors: {summary['error_count']}[/dim]"
    )


# =============================================================================
# LAMBDA COMMAND GROUP
# =============================================================================


@cli.group("lambda")
def lambda_() -> None:
    """Analyze Lambda (bot-decision-yt) behavior."""
    pass


@lambda_.command("invocations")
@click.option("-t", "--time-range", default="1h")
@click.option("-e", "--experiment-id", type=int)
@click.option("--crawler-id", type=int)
@click.option("-n", "--limit", default=50)
@click.pass_context
def lambda_invocations(
    ctx: click.Context,
    time_range: str,
    experiment_id: int | None,
    crawler_id: int | None,
    limit: int,
) -> None:
    """List recent Lambda invocations."""
    from orchestrator.logs.aggregator import LogAggregator

    console.print(f"[dim]Fetching Lambda invocations from last {time_range}...[/dim]\n")

    aggregator = LogAggregator()

    try:
        result = aggregator.query(
            component="lambda",
            experiment_id=experiment_id,
            crawler_id=crawler_id,
            time_range=time_range,
            limit=limit,
        )

        if not result.entries:
            console.print("[yellow]No Lambda invocations found[/yellow]")
            return

        # Filter to just decision entries (not warnings)
        decisions = [e for e in result.entries if "decision" in e.message.lower()]

        console.print(f"[bold]Found {len(decisions)} Lambda decisions[/bold]\n")

        table = Table()
        table.add_column("Time", style="dim")
        table.add_column("Job ID")
        table.add_column("Selected Video")
        table.add_column("XPath", style="dim")

        for entry in decisions:
            table.add_row(
                entry.timestamp.strftime("%Y-%m-%d %H:%M"),
                str(entry.job_id) if entry.job_id else "?",
                entry.message[17:67] if len(entry.message) > 17 else entry.message,  # Skip "Lambda decision: "
                entry.labels.get("xpath", "")[:50],
            )

        console.print(table)

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
    finally:
        aggregator.close()


@lambda_.command("decisions")
@click.argument("experiment_id", type=int)
@click.option(
    "-g",
    "--group-by",
    type=click.Choice(["crawler", "hour", "decision"]),
    default="crawler",
)
@click.pass_context
def lambda_decisions(ctx: click.Context, experiment_id: int, group_by: str) -> None:
    """Analyze Lambda decision patterns for an experiment."""
    from collections import Counter
    from orchestrator.logs.aggregator import LogAggregator

    console.print(f"[bold]Analyzing decisions for experiment {experiment_id}...[/bold]\n")

    aggregator = LogAggregator()

    try:
        result = aggregator.query(
            component="lambda",
            experiment_id=experiment_id,
            time_range="7d",
            limit=1000,
        )

        decisions = [e for e in result.entries if "decision" in e.message.lower()]

        if not decisions:
            console.print("[yellow]No Lambda decisions found for this experiment[/yellow]")
            return

        console.print(f"[bold]Total Decisions: {len(decisions)}[/bold]\n")

        if group_by == "crawler":
            by_crawler: Counter = Counter()
            for d in decisions:
                by_crawler[d.crawler_id or "unknown"] += 1

            console.print("[bold]By Crawler:[/bold]")
            for crawler, count in by_crawler.most_common():
                console.print(f"  Crawler {crawler}: {count} decisions")

        elif group_by == "hour":
            by_hour: Counter = Counter()
            for d in decisions:
                hour = d.timestamp.strftime("%Y-%m-%d %H:00")
                by_hour[hour] += 1

            console.print("[bold]By Hour:[/bold]")
            for hour, count in sorted(by_hour.items()):
                console.print(f"  {hour}: {count} decisions")

        else:  # decision
            console.print("[bold]Recent Decisions:[/bold]")
            for d in decisions[:20]:
                console.print(f"  Job {d.job_id}: {d.message[17:80]}...")

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
    finally:
        aggregator.close()


@lambda_.command("show")
@click.argument("job_id", type=int)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def lambda_show(ctx: click.Context, job_id: int, as_json: bool) -> None:
    """Show full Lambda output for a specific job."""
    import json
    from orchestrator.logs.aggregator import LogAggregator

    console.print(f"[dim]Fetching Lambda output for job {job_id}...[/dim]\n")

    aggregator = LogAggregator()

    try:
        output = aggregator.get_lambda_output(job_id)

        if not output:
            console.print(f"[yellow]No Lambda output found for job {job_id}[/yellow]")
            console.print(f"[dim]Looked in: {aggregator.har_path / str(job_id)}[/dim]")
            return

        if as_json:
            console.print(json.dumps(output, indent=2))
        else:
            # Pretty print key fields
            console.print(f"[bold]Lambda Output for Job {job_id}[/bold]\n")

            # Selected video
            selected = output.get("selected", {})
            console.print("[bold]Selected Video:[/bold]")
            console.print(f"  Title: {selected.get('title', 'N/A')}")
            console.print(f"  ID: {selected.get('id', 'N/A')}")
            console.print(f"  Channel: {selected.get('channel', 'N/A')}")

            # XPath
            console.print(f"\n[bold]XPath:[/bold]")
            console.print(f"  {output.get('xpath', 'N/A')}")

            # Timestamps
            if "timestamps" in output:
                console.print(f"\n[bold]Execution Timeline:[/bold]")
                ts = output["timestamps"]
                if isinstance(ts, dict) and "progress" in ts:
                    for idx, step in ts["progress"].items():
                        console.print(f"  - {step}")

            # Warnings
            warnings = output.get("warnings", [])
            if warnings:
                console.print(f"\n[bold]Warnings ({len(warnings)}):[/bold]")
                for w in warnings[:10]:
                    console.print(f"  [yellow]- {str(w)[:100]}[/yellow]")

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
    finally:
        aggregator.close()


@lambda_.command("logs")
@click.option("-t", "--time-range", default="1h", help="Time range (15m, 1h, 24h, 7d)")
@click.option("-r", "--request-id", help="Filter by Lambda request ID")
@click.option("-j", "--job-id", type=int, help="Filter by job ID")
@click.option("-s", "--search", default="", help="Search pattern")
@click.option("-n", "--limit", default=100, help="Max results")
@click.option("-f", "--format", "output_format", type=click.Choice(["plain", "json", "table"]), default="plain")
@click.pass_context
def lambda_logs(
    ctx: click.Context,
    time_range: str,
    request_id: str | None,
    job_id: int | None,
    search: str,
    limit: int,
    output_format: str,
) -> None:
    """Query Lambda logs from CloudWatch."""
    import json
    try:
        from orchestrator.clients.cloudwatch import CloudWatchClient
    except ImportError as e:
        console.print(f"[red]CloudWatch client not available: {e}[/red]")
        console.print("[dim]Ensure boto3 is installed: pip install boto3[/dim]")
        return

    console.print(f"[dim]Querying CloudWatch logs for Lambda (last {time_range})...[/dim]\n")

    try:
        client = CloudWatchClient()

        if not client.is_available():
            console.print("[yellow]CloudWatch is not available[/yellow]")
            console.print("[dim]Ensure AWS credentials are configured[/dim]")
            return

        result = client.query_logs(
            time_range=time_range,
            request_id=request_id,
            job_id=job_id,
            filter_pattern=f'"{search}"' if search else None,
            limit=limit,
        )

        if not result.entries:
            console.print("[yellow]No Lambda logs found[/yellow]")
            return

        console.print(f"[dim]Found {len(result.entries)} log entries[/dim]\n")

        if output_format == "json":
            entries = [
                {
                    "timestamp": e.timestamp.isoformat(),
                    "level": e.level,
                    "message": e.message,
                    "request_id": e.request_id,
                    "log_stream": e.log_stream,
                }
                for e in result.entries
            ]
            console.print(json.dumps(entries, indent=2))

        elif output_format == "table":
            table = Table()
            table.add_column("Time", style="dim")
            table.add_column("Level")
            table.add_column("Request ID", style="cyan")
            table.add_column("Message")

            for entry in result.entries:
                level_style = {
                    "error": "red",
                    "warning": "yellow",
                    "info": "green",
                    "debug": "dim",
                }.get(entry.level.lower(), "")

                table.add_row(
                    entry.timestamp.strftime("%H:%M:%S"),
                    f"[{level_style}]{entry.level}[/{level_style}]" if level_style else entry.level,
                    (entry.request_id or "")[:8],
                    entry.message[:80],
                )
            console.print(table)

        else:  # plain
            for entry in result.entries:
                level_style = {
                    "error": "red",
                    "warning": "yellow",
                    "info": "green",
                    "debug": "dim",
                }.get(entry.level.lower(), "")

                time_str = entry.timestamp.strftime("%H:%M:%S")
                req_id = f"[cyan]{entry.request_id[:8]}[/cyan]" if entry.request_id else "[dim]--------[/dim]"
                lvl = f"[{level_style}]{entry.level:5}[/{level_style}]" if level_style else entry.level

                console.print(f"[dim]{time_str}[/dim] {req_id} {lvl} {entry.message[:120]}")

        client.close()

    except Exception as e:
        console.print(f"[red]Error querying CloudWatch: {e}[/red]")
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/dim]")


@lambda_.command("metrics")
@click.option("-t", "--time-range", default="1h", help="Time range (15m, 1h, 24h, 7d)")
@click.option("-f", "--format", "output_format", type=click.Choice(["summary", "json"]), default="summary")
@click.pass_context
def lambda_metrics(ctx: click.Context, time_range: str, output_format: str) -> None:
    """Show Lambda execution metrics."""
    import json
    try:
        from orchestrator.clients.cloudwatch import CloudWatchClient
    except ImportError as e:
        console.print(f"[red]CloudWatch client not available: {e}[/red]")
        return

    console.print(f"[dim]Fetching Lambda metrics (last {time_range})...[/dim]\n")

    try:
        client = CloudWatchClient()

        if not client.is_available():
            console.print("[yellow]CloudWatch is not available[/yellow]")
            return

        metrics = client.get_metrics_summary(time_range=time_range)

        if output_format == "json":
            console.print(json.dumps(metrics, indent=2))
        else:
            console.print("[bold]Lambda Execution Metrics[/bold]\n")

            console.print(f"[bold]Invocations:[/bold]")
            console.print(f"  Total:       {metrics.get('invocation_count', 0)}")
            console.print(f"  Errors:      {metrics.get('error_count', 0)}")
            console.print(f"  Cold Starts: {metrics.get('cold_start_count', 0)}")

            console.print()
            console.print(f"[bold]Duration (ms):[/bold]")
            duration = metrics.get('duration', {})
            console.print(f"  Average:  {duration.get('avg', 0):.1f}")
            console.print(f"  Min:      {duration.get('min', 0):.1f}")
            console.print(f"  Max:      {duration.get('max', 0):.1f}")
            console.print(f"  P99:      {duration.get('p99', 0):.1f}")

            console.print()
            console.print(f"[bold]Memory (MB):[/bold]")
            memory = metrics.get('memory', {})
            console.print(f"  Configured: {memory.get('configured', 0)}")
            console.print(f"  Avg Used:   {memory.get('avg_used', 0):.1f}")
            console.print(f"  Max Used:   {memory.get('max_used', 0)}")

            if metrics.get('recent_errors'):
                console.print()
                console.print(f"[bold red]Recent Errors ({len(metrics['recent_errors'])}):[/bold red]")
                for err in metrics['recent_errors'][:5]:
                    console.print(f"  [red]- {err.get('message', '')[:80]}[/red]")
                    console.print(f"    [dim]{err.get('timestamp', '')}[/dim]")

        client.close()

    except Exception as e:
        console.print(f"[red]Error fetching metrics: {e}[/red]")
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/dim]")


@lambda_.command("errors")
@click.option("-t", "--time-range", default="24h", help="Time range")
@click.option("-n", "--limit", default=20, help="Max errors to show")
@click.option("-f", "--format", "output_format", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def lambda_errors(ctx: click.Context, time_range: str, limit: int, output_format: str) -> None:
    """Show recent Lambda errors from CloudWatch."""
    import json
    try:
        from orchestrator.clients.cloudwatch import CloudWatchClient
    except ImportError as e:
        console.print(f"[red]CloudWatch client not available: {e}[/red]")
        return

    console.print(f"[dim]Fetching Lambda errors (last {time_range})...[/dim]\n")

    try:
        client = CloudWatchClient()

        if not client.is_available():
            console.print("[yellow]CloudWatch is not available[/yellow]")
            return

        errors = client.get_recent_errors(time_range=time_range, limit=limit)

        if not errors:
            console.print("[green]No Lambda errors found![/green]")
            return

        console.print(f"[bold red]Found {len(errors)} error invocations[/bold red]\n")

        if output_format == "json":
            error_data = []
            for inv in errors:
                error_data.append({
                    "request_id": inv.request_id,
                    "start_time": inv.start_time.isoformat() if inv.start_time else None,
                    "duration_ms": inv.duration_ms,
                    "error": inv.error,
                    "log_count": len(inv.logs),
                })
            console.print(json.dumps(error_data, indent=2))
        else:
            table = Table(title="Lambda Errors")
            table.add_column("Time", style="dim")
            table.add_column("Request ID", style="cyan")
            table.add_column("Duration")
            table.add_column("Error", style="red")

            for inv in errors:
                time_str = inv.start_time.strftime("%Y-%m-%d %H:%M") if inv.start_time else "?"
                duration = f"{inv.duration_ms:.0f}ms" if inv.duration_ms else "?"

                table.add_row(
                    time_str,
                    inv.request_id[:12] if inv.request_id else "?",
                    duration,
                    (inv.error or "Unknown error")[:60],
                )

            console.print(table)

        client.close()

    except Exception as e:
        console.print(f"[red]Error fetching Lambda errors: {e}[/red]")
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/dim]")


@lambda_.command("invocation")
@click.argument("request_id")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def lambda_invocation(ctx: click.Context, request_id: str, as_json: bool) -> None:
    """Get detailed info about a specific Lambda invocation."""
    import json
    try:
        from orchestrator.clients.cloudwatch import CloudWatchClient
    except ImportError as e:
        console.print(f"[red]CloudWatch client not available: {e}[/red]")
        return

    console.print(f"[dim]Fetching invocation {request_id}...[/dim]\n")

    try:
        client = CloudWatchClient()

        if not client.is_available():
            console.print("[yellow]CloudWatch is not available[/yellow]")
            return

        invocation = client.get_invocation(request_id=request_id)

        if not invocation:
            console.print(f"[yellow]Invocation not found: {request_id}[/yellow]")
            console.print("[dim]Note: CloudWatch logs may take a few seconds to appear[/dim]")
            return

        if as_json:
            data = {
                "request_id": invocation.request_id,
                "start_time": invocation.start_time.isoformat() if invocation.start_time else None,
                "end_time": invocation.end_time.isoformat() if invocation.end_time else None,
                "duration_ms": invocation.duration_ms,
                "billed_duration_ms": invocation.billed_duration_ms,
                "memory_size_mb": invocation.memory_size_mb,
                "max_memory_used_mb": invocation.max_memory_used_mb,
                "init_duration_ms": invocation.init_duration_ms,
                "is_cold_start": invocation.is_cold_start,
                "is_error": invocation.is_error,
                "error": invocation.error,
                "logs": [
                    {
                        "timestamp": log.timestamp.isoformat(),
                        "level": log.level,
                        "message": log.message,
                    }
                    for log in invocation.logs
                ],
            }
            console.print(json.dumps(data, indent=2))
        else:
            console.print(f"[bold]Lambda Invocation: {invocation.request_id}[/bold]\n")

            # Status
            if invocation.is_error:
                console.print("[red bold]STATUS: ERROR[/red bold]")
                if invocation.error:
                    console.print(f"  [red]{invocation.error}[/red]")
            else:
                console.print("[green bold]STATUS: SUCCESS[/green bold]")

            # Timing
            console.print()
            console.print("[bold]Timing:[/bold]")
            if invocation.start_time:
                console.print(f"  Start: {invocation.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
            if invocation.end_time:
                console.print(f"  End:   {invocation.end_time.strftime('%Y-%m-%d %H:%M:%S')}")
            if invocation.duration_ms:
                console.print(f"  Duration: {invocation.duration_ms:.1f} ms")
            if invocation.billed_duration_ms:
                console.print(f"  Billed:   {invocation.billed_duration_ms:.1f} ms")
            if invocation.is_cold_start and invocation.init_duration_ms:
                console.print(f"  [cyan]Cold Start Init: {invocation.init_duration_ms:.1f} ms[/cyan]")

            # Memory
            console.print()
            console.print("[bold]Memory:[/bold]")
            if invocation.memory_size_mb:
                console.print(f"  Configured: {invocation.memory_size_mb} MB")
            if invocation.max_memory_used_mb:
                console.print(f"  Max Used:   {invocation.max_memory_used_mb} MB")
                if invocation.memory_size_mb:
                    pct = (invocation.max_memory_used_mb / invocation.memory_size_mb) * 100
                    style = "red" if pct > 80 else ("yellow" if pct > 60 else "green")
                    console.print(f"  Utilization: [{style}]{pct:.1f}%[/{style}]")

            # Logs
            if invocation.logs:
                console.print()
                console.print(f"[bold]Logs ({len(invocation.logs)} entries):[/bold]")
                for log in invocation.logs[:30]:
                    level_style = {
                        "error": "red",
                        "warning": "yellow",
                        "info": "green",
                        "debug": "dim",
                    }.get(log.level.lower(), "")

                    time_str = log.timestamp.strftime("%H:%M:%S.%f")[:-3]
                    lvl = f"[{level_style}]{log.level:5}[/{level_style}]" if level_style else log.level
                    console.print(f"  [dim]{time_str}[/dim] {lvl} {log.message[:100]}")

                if len(invocation.logs) > 30:
                    console.print(f"  [dim]... and {len(invocation.logs) - 30} more[/dim]")

        client.close()

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/dim]")


# =============================================================================
# EXPERIMENT COMMAND GROUP
# =============================================================================


@cli.group()
def experiment() -> None:
    """Manage experiments with YAML specifications."""
    pass


@experiment.command("validate")
@click.argument("file", type=click.Path(exists=True))
@click.option("--strict", is_flag=True, help="Treat warnings as errors")
@click.option("--seed", "-s", type=int, help="Random seed for reproducible randomization")
@click.option("-f", "--format", "output_format", type=click.Choice(["text", "json"]), default="text")
@click.pass_context
def experiment_validate(
    ctx: click.Context, file: str, strict: bool, seed: int | None, output_format: str
) -> None:
    """Validate an experiment YAML specification."""
    import json
    from pathlib import Path

    try:
        from experiment_designer.spec import SpecParser, SpecConverter, SpecValidator
        from experiment_designer.spec.design_expander import DesignExpander
        from experiment_designer.spec.file_loader import create_file_tag_loader, resolve_file_references
        import yaml as yaml_lib
    except ImportError as e:
        console.print(f"[red]Error importing experiment_designer: {e}[/red]")
        console.print("[dim]Install with: pip install -e .[/dim]")
        return

    file_path = Path(file)
    base_dir = file_path.parent

    with open(file, 'r') as f:
        content = f.read()

    # Create custom YAML loader with !file tag support
    FileTagLoader, file_loader = create_file_tag_loader(base_dir=base_dir, seed=seed)

    # Parse YAML first
    try:
        raw_spec = yaml_lib.load(content, Loader=FileTagLoader)
        raw_spec = resolve_file_references(raw_spec, file_loader)
    except yaml_lib.YAMLError as e:
        if output_format == 'json':
            console.print(json.dumps({"valid": False, "errors": [f"YAML parse error: {e}"], "warnings": []}))
        else:
            console.print("[red bold]Validation FAILED[/red bold]")
            console.print(f"  [red]YAML parse error: {e}[/red]")
        return

    # Run validator
    validator = SpecValidator()
    validation_result = validator.validate(raw_spec)

    # Also parse and convert to catch additional errors
    parser = SpecParser(base_dir=base_dir, seed=seed)
    parsed = parser.parse(content)

    converter = SpecConverter()
    result = converter.convert(parsed)

    # Combine all errors and warnings
    all_errors = list(set(
        validation_result.errors + parsed.errors + result.errors
    ))
    all_warnings = list(set(
        validation_result.warnings + parsed.warnings + result.warnings
    ))

    if strict:
        all_errors.extend(all_warnings)
        all_warnings = []

    if output_format == 'json':
        output = {
            "valid": len(all_errors) == 0,
            "errors": all_errors,
            "warnings": all_warnings,
            "summary": {
                "experiment_name": parsed.experiment.get("name", ""),
                "crawler_count": result.crawler_count,
                "total_jobs": result.total_jobs,
                "has_design": parsed.design is not None,
            },
        }
        console.print(json.dumps(output, indent=2))
    else:
        if all_errors:
            console.print("[red bold]Validation FAILED[/red bold]\n")
            for error in all_errors:
                console.print(f"  [red]ERROR: {error}[/red]")
        else:
            console.print("[green bold]Validation PASSED[/green bold]")

        if all_warnings:
            console.print()
            for warning in all_warnings:
                console.print(f"  [yellow]WARNING: {warning}[/yellow]")

        console.print()
        console.print(f"Experiment: {parsed.experiment.get('name', 'N/A')}")
        console.print(f"Crawlers:   {result.crawler_count}")
        console.print(f"Total Jobs: {result.total_jobs}")

        if parsed.design:
            expander = DesignExpander()
            preview = expander.preview(parsed.design)
            console.print(f"Design:     {preview.get('formula', 'N/A')}")


@experiment.command("upload")
@click.argument("file", type=click.Path(exists=True))
@click.option("-e", "--experiment-id", type=int, help="Add to existing experiment")
@click.option("--dry-run", is_flag=True, help="Validate and convert without uploading")
@click.option("--seed", "-s", type=int, help="Random seed for reproducible randomization")
@click.pass_context
def experiment_upload(
    ctx: click.Context, file: str, experiment_id: int | None, dry_run: bool, seed: int | None
) -> None:
    """Upload experiment from YAML spec to PrimeMover API."""
    import json
    import os
    from pathlib import Path
    from datetime import datetime

    try:
        from experiment_designer.spec import SpecParser, SpecConverter
        import requests
    except ImportError as e:
        console.print(f"[red]Error importing required modules: {e}[/red]")
        return

    settings = ctx.obj["settings"]

    file_path = Path(file)
    base_dir = file_path.parent

    with open(file, 'r') as f:
        content = f.read()

    # Parse and validate
    parser = SpecParser(base_dir=base_dir, seed=seed)
    parsed = parser.parse(content)

    if parsed.errors:
        console.print("[red bold]Validation FAILED[/red bold]")
        for error in parsed.errors:
            console.print(f"  [red]{error}[/red]")
        return

    # Convert to JSON
    converter = SpecConverter()
    result = converter.convert(parsed)

    if result.errors:
        console.print("[red bold]Conversion FAILED[/red bold]")
        for error in result.errors:
            console.print(f"  [red]{error}[/red]")
        return

    if result.warnings:
        for warning in result.warnings:
            console.print(f"  [yellow]WARNING: {warning}[/yellow]")

    console.print()
    console.print(f"Experiment: {result.experiment_name}")
    console.print(f"Crawlers:   {result.crawler_count}")
    console.print(f"Jobs:       {result.total_jobs}")
    console.print()

    if dry_run:
        console.print("[cyan]Dry run - not uploading[/cyan]")
        console.print("[dim]Use 'orchestrator experiment generate' to save JSON file[/dim]")
        return

    # Get credentials
    api_url = settings.pm_api_base_url
    email = settings.pm_api_email
    password = settings.pm_api_password

    if not all([api_url, email, password]):
        console.print("[red]API credentials not configured[/red]")
        console.print()
        console.print("Set environment variables:")
        console.print("  PM_API_BASE_URL=http://localhost:8090/api/v1")
        console.print("  PM_API_EMAIL=admin@primemover.example")
        console.print("  PM_API_PASSWORD=password")
        return

    try:
        # Authenticate
        console.print("Authenticating...")
        auth_response = requests.post(
            f"{api_url}/login",
            json={"email": email, "password": password},
            headers={"Accept": "application/json"}
        )
        auth_response.raise_for_status()
        token = auth_response.json().get("access_token")

        if not token:
            console.print("[red]Authentication failed: no token received[/red]")
            return

        # Create experiment if needed
        if not experiment_id:
            console.print("Creating new experiment...")
            exp_name = result.experiment_name or f"Experiment {datetime.now().strftime('%Y%m%d_%H%M%S')}"

            create_response = requests.post(
                f"{api_url}/experiments",
                json={
                    "name": exp_name,
                    "description": result.experiment_description or "Created via orchestrator CLI",
                    "contact": "PrimeMover Orchestrator",
                    "active": 1
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json"
                }
            )
            create_response.raise_for_status()
            create_result = create_response.json()

            experiment_id = (
                create_result.get("data", {}).get("id") or
                create_result.get("id")
            )

            if not experiment_id:
                console.print("[red]Failed to create experiment: no ID returned[/red]")
                return

            console.print(f"Created experiment: {exp_name} (ID: {experiment_id})")
        else:
            console.print(f"Adding to experiment {experiment_id}...")

        # Upload crawlers
        console.print("Uploading crawlers...")

        json_content = json.dumps(result.json_data, indent=2)

        upload_response = requests.post(
            f"{api_url}/experiments/{experiment_id}/upload",
            files={"json": ("crawlers.json", json_content, "application/json")},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json"
            }
        )
        upload_response.raise_for_status()

        console.print()
        console.print("[green bold]Upload SUCCESS[/green bold]")
        console.print(f"Experiment ID: {experiment_id}")
        console.print(f"Crawlers created: {result.crawler_count}")

    except requests.exceptions.HTTPError as e:
        console.print(f"[red]Upload failed: {e}[/red]")
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_detail = e.response.json()
                console.print(f"API Error: {json.dumps(error_detail, indent=2)}")
            except Exception:
                console.print(f"Response: {e.response.text[:1000]}")
    except requests.exceptions.RequestException as e:
        console.print(f"[red]Request failed: {e}[/red]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


@experiment.command("generate")
@click.argument("file", type=click.Path(exists=True))
@click.option("-o", "--output", type=click.Path(), help="Output file (default: stdout)")
@click.option("--pretty/--compact", default=True, help="Pretty-print JSON")
@click.option("--seed", "-s", type=int, help="Random seed for reproducible randomization")
@click.pass_context
def experiment_generate(
    ctx: click.Context, file: str, output: str | None, pretty: bool, seed: int | None
) -> None:
    """Generate API JSON from YAML spec."""
    from pathlib import Path

    try:
        from experiment_designer.spec import SpecParser, SpecConverter
    except ImportError as e:
        console.print(f"[red]Error importing experiment_designer: {e}[/red]")
        return

    file_path = Path(file)
    base_dir = file_path.parent

    with open(file, 'r') as f:
        content = f.read()

    parser = SpecParser(base_dir=base_dir, seed=seed)
    parsed = parser.parse(content)

    if parsed.errors:
        console.print("[red]Cannot generate: validation errors found[/red]")
        for error in parsed.errors:
            console.print(f"  [red]{error}[/red]")
        return

    converter = SpecConverter()
    result = converter.convert(parsed)

    if result.errors:
        console.print("[red]Conversion errors:[/red]")
        for error in result.errors:
            console.print(f"  [red]{error}[/red]")
        return

    json_output = result.to_json(pretty=pretty)

    if output:
        with open(output, 'w') as f:
            f.write(json_output)
        console.print(f"[green]Generated: {output}[/green]")
        console.print(f"  Crawlers: {result.crawler_count}")
        console.print(f"  Jobs:     {result.total_jobs}")
    else:
        console.print(json_output)


@experiment.command("preview")
@click.argument("file", type=click.Path(exists=True))
@click.option("--seed", "-s", type=int, help="Random seed for reproducible randomization")
@click.pass_context
def experiment_preview(ctx: click.Context, file: str, seed: int | None) -> None:
    """Preview design expansion without generating JSON."""
    from pathlib import Path

    try:
        from experiment_designer.spec import SpecParser
        from experiment_designer.spec.design_expander import DesignExpander
    except ImportError as e:
        console.print(f"[red]Error importing experiment_designer: {e}[/red]")
        return

    file_path = Path(file)
    base_dir = file_path.parent

    with open(file, 'r') as f:
        content = f.read()

    parser = SpecParser(base_dir=base_dir, seed=seed)
    parsed = parser.parse(content)

    if parsed.errors:
        console.print("[red]Parse errors:[/red]")
        for error in parsed.errors:
            console.print(f"  [red]{error}[/red]")
        return

    console.print(f"[bold]Experiment: {parsed.experiment.get('name', 'N/A')}[/bold]\n")

    if not parsed.design:
        console.print("No factorial design specified.")
        console.print(f"Crawlers defined: {len(parsed.crawlers)}")
        return

    expander = DesignExpander(seed=seed)
    preview_info = expander.preview(parsed.design)

    console.print("[bold underline]Factorial Design[/bold underline]\n")

    console.print(f"Design Type: {preview_info.get('design_type', 'factorial')}\n")

    console.print("[bold]Factors:[/bold]")
    for factor in preview_info.get('factors', []):
        console.print(f"  [cyan]{factor['name']}[/cyan]: {factor['levels']}")
        if factor.get('maps_to'):
            console.print(f"    [dim]-> maps to: {', '.join(factor['maps_to'])}[/dim]")

    console.print()
    console.print(f"Replications: {preview_info.get('replications', 1)}")
    console.print()
    console.print(f"[cyan bold]{preview_info.get('formula', '')}[/cyan bold]")


@experiment.command("list")
@click.option(
    "-s",
    "--status",
    type=click.Choice(["all", "active", "completed"]),
    default="all",
)
@click.option("-n", "--limit", default=20)
@click.pass_context
def experiment_list(ctx: click.Context, status: str, limit: int) -> None:
    """List experiments from API."""
    try:
        import requests
    except ImportError:
        console.print("[red]requests package required[/red]")
        return

    settings = ctx.obj["settings"]

    api_url = settings.pm_api_base_url
    email = settings.pm_api_email
    password = settings.pm_api_password

    if not all([api_url, email, password]):
        console.print("[red]API credentials not configured[/red]")
        return

    try:
        # Authenticate
        auth_response = requests.post(
            f"{api_url}/login",
            json={"email": email, "password": password},
            headers={"Accept": "application/json"}
        )
        auth_response.raise_for_status()
        token = auth_response.json().get("access_token")

        # Fetch experiments
        list_response = requests.get(
            f"{api_url}/experiments",
            params={"per_page": limit},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json"
            }
        )
        list_response.raise_for_status()
        data = list_response.json()

        experiments = data.get("data", [])
        if not experiments:
            console.print("[yellow]No experiments found[/yellow]")
            return

        table = Table(title="Experiments")
        table.add_column("ID", style="cyan")
        table.add_column("Name")
        table.add_column("Active", style="green")
        table.add_column("Crawlers")
        table.add_column("Updated")

        for exp in experiments:
            active = "Yes" if exp.get("active") else "No"
            crawler_count = str(exp.get("crawlers_count", "?"))
            updated = exp.get("updated_at", "")[:10] if exp.get("updated_at") else ""

            table.add_row(
                str(exp.get("id", "")),
                exp.get("name", "")[:40],
                active,
                crawler_count,
                updated
            )

        console.print(table)

    except requests.exceptions.RequestException as e:
        console.print(f"[red]Error fetching experiments: {e}[/red]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


# =============================================================================
# DASHBOARD COMMAND
# =============================================================================


@cli.command()
@click.option("--open/--no-open", default=True, help="Open Grafana in browser")
@click.pass_context
def dashboard(ctx: click.Context, open: bool) -> None:
    """Open Grafana dashboard in browser."""
    settings = ctx.obj["settings"]
    url = settings.grafana_url

    console.print(f"[bold]Grafana Dashboard:[/bold] {url}")

    if open:
        import webbrowser

        webbrowser.open(url)
        console.print("[green]Opened in browser[/green]")


if __name__ == "__main__":
    cli()
