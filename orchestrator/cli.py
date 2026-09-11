"""
PrimeMover Orchestrator CLI

Command-line interface for monitoring, debugging, and managing experiments.
"""

from pathlib import Path
from typing import TypedDict

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
# SETUP COMMAND
# =============================================================================

# Default GitHub repository URLs
REPO_URLS = {
    "api": "git@github.com:umatter/primemover_api.git",
    "runner": "git@github.com:umatter/primemover_runner.git",
    "gui": "git@github.com:umatter/primemover_gui.git",
}


class RepoInfo(TypedDict):
    """Checkout location and clone URL for one sibling repository."""

    path: Path
    url: str


@cli.command()
@click.option("--skip-clone", is_flag=True, help="Skip cloning repositories")
@click.option("--skip-build", is_flag=True, help="Skip building runner JAR")
@click.option("--skip-gui", is_flag=True, help="Skip building GUI")
@click.option("--skip-start", is_flag=True, help="Don't start components after setup")
@click.option("--api-repo", default=REPO_URLS["api"], help="API repository URL")
@click.option("--runner-repo", default=REPO_URLS["runner"], help="Runner repository URL")
@click.option("--gui-repo", default=REPO_URLS["gui"], help="GUI repository URL")
@click.pass_context
def setup(
    ctx: click.Context,
    skip_clone: bool,
    skip_build: bool,
    skip_gui: bool,
    skip_start: bool,
    api_repo: str,
    runner_repo: str,
    gui_repo: str,
) -> None:
    """
    Set up complete PrimeMover development environment.

    This command is the entry point for new developers. It will:

    1. Clone required repositories (api, runner, gui) as siblings
    2. Create .env configuration file
    3. Build the runner JAR (requires Maven)
    4. Start all components

    \b
    Example:
        # Fresh setup (clone orchestrator first, then run setup)
        git clone git@github.com:umatter/primemover-orchestrator.git
        cd primemover-orchestrator
        pip install -e .
        orchestrator setup

    \b
        # Setup with existing repos (skip cloning)
        orchestrator setup --skip-clone

    \b
        # Setup without starting (manual start later)
        orchestrator setup --skip-start
    """
    import shutil
    import subprocess
    from pathlib import Path

    console.print("[bold cyan]PrimeMover Development Environment Setup[/bold cyan]\n")

    # Get the orchestrator directory (where we are)
    orchestrator_path = Path(__file__).parent.parent.resolve()
    parent_path = orchestrator_path.parent

    console.print(f"[dim]Orchestrator: {orchestrator_path}[/dim]")
    console.print(f"[dim]Parent dir:   {parent_path}[/dim]\n")

    # Step 0: Check prerequisites
    console.print("[bold]Step 0: Checking prerequisites[/bold]\n")

    prereqs_ok = True

    # Check git
    if shutil.which("git"):
        console.print("  [green]✓[/green] git installed")
    else:
        console.print("  [red]✗[/red] git not found")
        prereqs_ok = False

    # Check Docker
    if shutil.which("docker"):
        docker_result = subprocess.run(["docker", "info"], capture_output=True)
        if docker_result.returncode == 0:
            console.print("  [green]✓[/green] Docker installed and running")
        else:
            console.print("  [yellow]⚠[/yellow] Docker installed but not running")
    else:
        console.print("  [red]✗[/red] Docker not found")
        prereqs_ok = False

    # Check Docker Compose (v2 or v1)
    compose_v2 = (
        subprocess.run(["docker", "compose", "version"], capture_output=True, text=True).returncode
        == 0
    )
    compose_v1 = shutil.which("docker-compose") is not None

    if compose_v2:
        console.print("  [green]✓[/green] Docker Compose v2 (plugin) installed")
    elif compose_v1:
        console.print("  [green]✓[/green] docker-compose v1 (standalone) installed")
    else:
        console.print("  [red]✗[/red] Docker Compose not found (install docker-compose-plugin)")
        prereqs_ok = False

    # Check Java
    java_path = shutil.which("java")
    if java_path:
        java_result = subprocess.run(["java", "-version"], capture_output=True, text=True)
        version_output = java_result.stderr or java_result.stdout
        import re

        match = re.search(r'version "(\d+)', version_output)
        if match:
            major_version = int(match.group(1))
            if major_version >= 17:
                console.print(f"  [green]✓[/green] Java {major_version} installed")
            else:
                console.print(f"  [red]✗[/red] Java {major_version} found, but 17+ required")
                prereqs_ok = False
        else:
            console.print("  [green]✓[/green] Java installed")
    else:
        console.print("  [red]✗[/red] Java not found (required for runner)")
        prereqs_ok = False

    # Check Maven
    if shutil.which("mvn"):
        console.print("  [green]✓[/green] Maven installed")
    else:
        console.print("  [yellow]⚠[/yellow] Maven not found (needed to build runner)")

    # Check Chrome (required for browser automation)
    chrome_paths = [
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    chrome_found = any(chrome_paths)
    if not chrome_found:
        # Check common installation paths
        common_paths = [
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/google-chrome-stable"),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
            Path("/opt/google/chrome/chrome"),
        ]
        chrome_found = any(p.exists() for p in common_paths)

    if chrome_found:
        # Get Chrome version
        chrome_result = subprocess.run(
            (
                ["google-chrome", "--version"]
                if shutil.which("google-chrome")
                else ["chromium", "--version"]
            ),
            capture_output=True,
            text=True,
        )
        if chrome_result.returncode == 0:
            version = chrome_result.stdout.strip()
            console.print(f"  [green]✓[/green] Chrome: {version}")
        else:
            console.print("  [green]✓[/green] Chrome installed")
    else:
        console.print("  [yellow]⚠[/yellow] Chrome not found (required for browser automation)")
        console.print("    [dim]Install: sudo apt install google-chrome-stable[/dim]")

    # Check Multilogin X (optional)
    if shutil.which("mlx"):
        console.print("  [green]✓[/green] Multilogin X (mlx) installed")
    else:
        console.print(
            "  [dim]○[/dim] Multilogin X not found (optional, for production browser profiles)"
        )

    # Check Node.js (for GUI - optional if Docker available)
    node_path = shutil.which("node")
    has_docker = shutil.which("docker")
    if node_path:
        node_result = subprocess.run(["node", "--version"], capture_output=True, text=True)
        if node_result.returncode == 0:
            console.print(f"  [green]✓[/green] Node.js {node_result.stdout.strip()}")
        else:
            console.print("  [green]✓[/green] Node.js installed")
    else:
        if has_docker:
            console.print("  [dim]○[/dim] Node.js not found (optional, GUI builds with Docker)")
        else:
            console.print(
                "  [yellow]⚠[/yellow] Node.js not found (needed to build GUI without Docker)"
            )

    # Check npm
    if shutil.which("npm"):
        console.print("  [green]✓[/green] npm installed")
    else:
        if has_docker:
            console.print("  [dim]○[/dim] npm not found (optional, GUI builds with Docker)")
        else:
            console.print("  [yellow]⚠[/yellow] npm not found (needed to build GUI without Docker)")

    if not prereqs_ok:
        console.print("\n[red]Prerequisites not met. Please install missing tools.[/red]")
        if not click.confirm("Continue anyway?"):
            return

    console.print()

    # Define expected paths
    repos: dict[str, RepoInfo] = {
        "api": {"path": parent_path / "primemover_api", "url": api_repo},
        "runner": {"path": parent_path / "primemover_runner", "url": runner_repo},
        "gui": {"path": parent_path / "primemover_gui", "url": gui_repo},
    }

    # Step 1: Clone repositories
    if not skip_clone:
        console.print("[bold]Step 1: Cloning repositories[/bold]\n")

        # Check git is available
        if not shutil.which("git"):
            console.print("[red]Error: git is not installed[/red]")
            return

        for name, info in repos.items():
            path = info["path"]
            url = info["url"]

            if path.exists():
                console.print(f"  [green]✓[/green] {name}: already exists at {path}")
            else:
                console.print(f"  [cyan]⋯[/cyan] {name}: cloning from {url}...")
                result = subprocess.run(
                    ["git", "clone", url, str(path)],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    console.print(f"  [green]✓[/green] {name}: cloned successfully")
                else:
                    console.print(f"  [red]✗[/red] {name}: clone failed")
                    console.print(f"    [dim]{result.stderr.strip()}[/dim]")
                    if not click.confirm("Continue anyway?"):
                        return
    else:
        console.print("[bold]Step 1: Cloning repositories[/bold] [dim](skipped)[/dim]\n")

    # Step 2: Create .env file
    console.print("\n[bold]Step 2: Creating configuration[/bold]\n")

    env_file = orchestrator_path / ".env"
    env_example = orchestrator_path / ".env.example"

    if env_file.exists():
        console.print("  [green]✓[/green] .env already exists")
    elif env_example.exists():
        import secrets

        # Read template and set correct paths
        content = env_example.read_text()

        # Update paths to be relative from orchestrator
        content = content.replace("PM_API_PATH=../primemover_api", "PM_API_PATH=../primemover_api")
        content = content.replace(
            "PM_RUNNER_PATH=../primemover_runner", "PM_RUNNER_PATH=../primemover_runner"
        )
        content = content.replace("PM_GUI_PATH=../primemover_gui", "PM_GUI_PATH=../primemover_gui")

        # Generate a random Grafana admin password (required - no default)
        grafana_password = secrets.token_urlsafe(16)
        content = content.replace(
            "GRAFANA_ADMIN_PASSWORD=", f"GRAFANA_ADMIN_PASSWORD={grafana_password}"
        )

        # Set API credentials to match Laravel seed defaults
        content = content.replace("PM_API_EMAIL=", "PM_API_EMAIL=admin@primemover.example")
        content = content.replace("PM_API_PASSWORD=", "PM_API_PASSWORD=password")

        # Set database credentials to match Docker defaults
        content = content.replace("PM_MYSQL_PASSWORD=", "PM_MYSQL_PASSWORD=secret")
        content = content.replace("PM_REDIS_PASSWORD=", "PM_REDIS_PASSWORD=secret")

        env_file.write_text(content)
        console.print("  [green]✓[/green] Created .env from template")
        console.print(f"    [dim]Grafana password: {grafana_password}[/dim]")
        console.print("    [dim]API credentials: admin@primemover.example / password[/dim]")
    else:
        console.print("  [red]✗[/red] .env.example not found")

    # Step 3: Build runner JAR
    if not skip_build:
        console.print("\n[bold]Step 3: Building runner (multi-module Maven project)[/bold]\n")

        runner_path = repos["runner"]["path"]
        runner_module = runner_path / "primemover-runner"

        if not runner_path.exists():
            console.print("  [yellow]⚠[/yellow] Runner path not found, skipping build")
        elif not shutil.which("mvn"):
            console.print("  [yellow]⚠[/yellow] Maven (mvn) not found, skipping build")
            console.print(
                f"    [dim]Install Maven and run: cd {runner_path} && mvn package -DskipTests[/dim]"
            )
        else:
            # Check if JAR already exists
            target_dir = runner_module / "target"
            existing_jars = (
                list(target_dir.glob("primemover-runner-*.jar")) if target_dir.exists() else []
            )
            existing_jars = [j for j in existing_jars if "-sources" not in j.name]

            if existing_jars:
                console.print(f"  [green]✓[/green] JAR already exists: {existing_jars[0].name}")
                if not click.confirm("  Rebuild anyway?", default=False):
                    skip_build = True

            if not skip_build:
                console.print("  [dim]Building from parent directory to include all modules:[/dim]")
                console.print("  [dim]  - primemover-generated (Multilogin API client)[/dim]")
                console.print("  [dim]  - primemover-runner (main application)[/dim]")
                console.print(
                    "  [cyan]⋯[/cyan] Running mvn package (this may take a few minutes)..."
                )

                # Run from parent directory to build both modules
                result = subprocess.run(
                    ["mvn", "package", "-DskipTests"],
                    cwd=runner_path,  # Parent directory, not runner_module
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    # Find the built JAR
                    new_jars = list(target_dir.glob("primemover-runner-*.jar"))
                    new_jars = [j for j in new_jars if "-sources" not in j.name]
                    if new_jars:
                        console.print(f"  [green]✓[/green] Built: {new_jars[0].name}")
                    else:
                        console.print("  [green]✓[/green] Build completed")
                else:
                    console.print("  [red]✗[/red] Build failed")
                    if (
                        "primemover-generated" in result.stdout
                        or "primemover-generated" in result.stderr
                    ):
                        console.print(
                            "    [dim]Hint: Error may be in primemover-generated module[/dim]"
                        )
                    console.print(
                        f"    [dim]Run manually: cd {runner_path} && mvn package -DskipTests[/dim]"
                    )
    else:
        console.print("\n[bold]Step 3: Building runner[/bold] [dim](skipped)[/dim]\n")

    # Step 3b: Build GUI
    if not skip_gui:
        console.print("\n[bold]Step 3b: Building GUI (Angular)[/bold]\n")

        gui_path = repos["gui"]["path"]
        api_public_gui = repos["api"]["path"] / "public" / "gui"

        if not gui_path.exists():
            console.print("  [yellow]⚠[/yellow] GUI path not found, skipping build")
        else:
            # Check if already deployed
            if api_public_gui.exists() and (api_public_gui / "index.html").exists():
                console.print("  [green]✓[/green] GUI already deployed to API")
                if not click.confirm("  Rebuild anyway?", default=False):
                    skip_gui = True

            if not skip_gui:
                gui_dockerfile = gui_path / "Dockerfile"
                has_docker = shutil.which("docker")
                has_npm = shutil.which("npm")

                # Prefer Docker build (no Node.js dependency on host)
                if gui_dockerfile.exists() and has_docker:
                    console.print(
                        "  [cyan]⋯[/cyan] Building GUI with Docker (this may take a few minutes)..."
                    )

                    # Build Docker image
                    result = subprocess.run(
                        ["docker", "build", "-t", "primemover-gui:build", "."],
                        cwd=gui_path,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode != 0:
                        console.print("  [red]✗[/red] Docker build failed")
                        console.print(f"    [dim]{result.stderr[:300]}[/dim]")
                    else:
                        console.print("  [green]✓[/green] Docker image built")

                        # Extract built files from container
                        console.print("  [cyan]⋯[/cyan] Extracting built files...")

                        # Create temporary container and copy files out
                        container_name = "primemover-gui-extract"
                        subprocess.run(
                            ["docker", "rm", "-f", container_name],
                            capture_output=True,
                        )
                        subprocess.run(
                            ["docker", "create", "--name", container_name, "primemover-gui:build"],
                            capture_output=True,
                        )

                        # Remove old deployment if exists
                        if api_public_gui.exists():
                            shutil.rmtree(api_public_gui)
                        api_public_gui.mkdir(parents=True, exist_ok=True)

                        # Copy from container's nginx html directory
                        result = subprocess.run(
                            [
                                "docker",
                                "cp",
                                f"{container_name}:/usr/share/nginx/html/.",
                                str(api_public_gui),
                            ],
                            capture_output=True,
                            text=True,
                        )

                        # Cleanup container
                        subprocess.run(
                            ["docker", "rm", "-f", container_name],
                            capture_output=True,
                        )

                        if result.returncode == 0 and (api_public_gui / "index.html").exists():
                            console.print(f"  [green]✓[/green] GUI deployed to {api_public_gui}")
                            console.print("    [dim]Access at: http://localhost:8090/gui[/dim]")
                        else:
                            console.print("  [red]✗[/red] Failed to extract files from container")

                # Fallback to native npm build
                elif has_npm:
                    console.print("  [dim]No Dockerfile found, using npm build...[/dim]")
                    gui_dist = gui_path / "dist"
                    gui_modules = gui_path / "node_modules"

                    # Install dependencies if needed
                    if not gui_modules.exists():
                        console.print("  [cyan]⋯[/cyan] Installing npm dependencies...")
                        result = subprocess.run(
                            ["npm", "install"],
                            cwd=gui_path,
                            capture_output=True,
                            text=True,
                        )
                        if result.returncode == 0:
                            console.print("  [green]✓[/green] npm install completed")
                        else:
                            console.print("  [red]✗[/red] npm install failed")
                            console.print(f"    [dim]{result.stderr[:200]}[/dim]")

                    # Build
                    console.print("  [cyan]⋯[/cyan] Building GUI (this may take a minute)...")
                    result = subprocess.run(
                        ["npm", "run", "build"],
                        cwd=gui_path,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode == 0:
                        console.print("  [green]✓[/green] GUI built successfully")

                        # Deploy to API's public/gui folder
                        gui_dist_src = gui_path / "dist" / "primemover-frontend"

                        if gui_dist_src.exists():
                            console.print("  [cyan]⋯[/cyan] Deploying GUI to API public folder...")

                            # Remove old deployment if exists
                            if api_public_gui.exists():
                                shutil.rmtree(api_public_gui)

                            # Copy new build
                            shutil.copytree(gui_dist_src, api_public_gui)
                            console.print(f"  [green]✓[/green] GUI deployed to {api_public_gui}")
                            console.print("    [dim]Access at: http://localhost:8090/gui[/dim]")
                        else:
                            console.print(
                                f"  [yellow]⚠[/yellow] Build output not found at {gui_dist_src}"
                            )
                    else:
                        console.print("  [red]✗[/red] Build failed")
                        console.print(
                            f"    [dim]Run manually: cd {gui_path} && npm run build[/dim]"
                        )
                else:
                    # Provide more accurate message about why GUI can't be built
                    if not gui_dockerfile.exists() and not has_npm:
                        console.print(
                            "  [yellow]⚠[/yellow] GUI has no Dockerfile and npm not available, skipping build"
                        )
                        console.print(
                            "    [dim]Install Node.js/npm to build the GUI, or add a Dockerfile[/dim]"
                        )
                    elif not gui_dockerfile.exists():
                        console.print(
                            "  [yellow]⚠[/yellow] GUI has no Dockerfile, npm build would be needed"
                        )
                        console.print(
                            "    [dim]Add a Dockerfile to the GUI repo for Docker-based builds[/dim]"
                        )
                    else:
                        console.print("  [yellow]⚠[/yellow] Docker or npm required for GUI build")
                        console.print("    [dim]Install Docker or Node.js to build the GUI[/dim]")
    else:
        console.print("\n[bold]Step 3b: Building GUI[/bold] [dim](skipped)[/dim]\n")

    # Step 4: Verify setup
    console.print("\n[bold]Step 4: Verifying setup[/bold]\n")

    all_ok = True

    # Check API
    api_compose = repos["api"]["path"] / "docker-compose.yml"
    if api_compose.exists():
        console.print("  [green]✓[/green] API: docker-compose.yml found")
    else:
        console.print("  [red]✗[/red] API: docker-compose.yml not found")
        all_ok = False

    # Check API .env
    api_env = repos["api"]["path"] / ".env"
    if api_env.exists():
        console.print("  [green]✓[/green] API: .env configured")
    else:
        # Prefer .env.docker (Docker-ready) over .env.example
        api_env_docker = repos["api"]["path"] / ".env.docker"
        api_env_example = repos["api"]["path"] / ".env.example"

        if api_env_docker.exists():
            # Use .env.docker - already has all Docker-specific settings
            import shutil

            shutil.copy(api_env_docker, api_env)
            console.print("  [green]✓[/green] API: .env created from .env.docker")
        elif api_env_example.exists():
            console.print(
                "  [yellow]⚠[/yellow] API: .env missing (will be created from .env.example)"
            )
            # Copy .env.example to .env and configure for Docker
            content = api_env_example.read_text()

            # Configure for Docker networking (containers use service names, not localhost)
            content = content.replace("DB_HOST=127.0.0.1", "DB_HOST=mysql")
            content = content.replace("REDIS_HOST=127.0.0.1", "REDIS_HOST=redis")

            # Add Docker-specific variables if not present
            docker_vars = {
                "COMPOSE_PROJECT_NAME": "primemover",  # Consistent container naming
                "DOCUMENT_ROOT": ".",
                "PHP_INI": "./docker/php/php.ini",
                "HOST_MACHINE_UNSECURE_HOST_PORT": "8090",  # Runner uses 8080
                "THROTTLE_WHITELIST": "127.0.0.1,localhost",  # Required by ConfigService
            }

            for var, value in docker_vars.items():
                if var not in content:
                    content += f"\n# Docker configuration\n{var}={value}\n"
                elif f"{var}=" in content and f"{var}=\n" in content:
                    # Variable exists but is empty - set the value
                    content = content.replace(f"{var}=\n", f"{var}={value}\n")

            api_env.write_text(content)
            console.print(
                "  [green]✓[/green] API: .env created from template (configured for Docker)"
            )
        else:
            console.print("  [red]✗[/red] API: .env, .env.docker, and .env.example not found")

    # Check if vendor exists (composer install done)
    api_vendor = repos["api"]["path"] / "vendor"
    if api_vendor.exists():
        console.print("  [green]✓[/green] API: vendor/ exists (dependencies installed)")
    else:
        console.print("  [dim]○[/dim] API: vendor/ not found (composer install needed)")

    # Check primemover-generated (Multilogin API client)
    generated_target = repos["runner"]["path"] / "primemover-generated" / "target"
    if generated_target.exists():
        generated_jars = list(generated_target.glob("primemover-generated-*.jar"))
        if generated_jars:
            console.print(f"  [green]✓[/green] Generated: {generated_jars[0].name}")
        else:
            console.print("  [yellow]⚠[/yellow] Generated: JAR not built (Multilogin API client)")
    else:
        console.print("  [yellow]⚠[/yellow] Generated: not built yet (primemover-generated)")

    # Check Runner JAR
    runner_target = repos["runner"]["path"] / "primemover-runner" / "target"
    if runner_target.exists():
        jars = [
            j for j in runner_target.glob("primemover-runner-*.jar") if "-sources" not in j.name
        ]
        if jars:
            console.print(f"  [green]✓[/green] Runner: {jars[0].name}")
        else:
            console.print("  [yellow]⚠[/yellow] Runner: JAR not built yet")
    else:
        console.print("  [yellow]⚠[/yellow] Runner: target/ directory not found")

    # Check and configure runner config for local development
    runner_config_dir = repos["runner"]["path"] / "primemover-runner" / "config"
    runner_config = runner_config_dir / "application.properties"

    if runner_config.exists():
        content = runner_config.read_text()

        # Check if already configured for local API
        if "backend.endpoint.api=http://localhost:8090" in content:
            console.print("  [green]✓[/green] Runner config: configured for local API")
        else:
            # Update to point to local API
            import re

            # Update backend endpoints
            content = re.sub(
                r"backend\.endpoint\.api=.*",
                "backend.endpoint.api=http://localhost:8090/api/v1/",
                content,
            )
            content = re.sub(
                r"backend\.endpoint\.runner=.*",
                "backend.endpoint.runner=http://localhost:8090/api/v1/runner/",
                content,
            )

            # Update backend credentials to match local API
            content = re.sub(
                r"backend\.username=.*",
                "backend.username=admin@primemover.example",
                content,
            )
            content = re.sub(
                r"backend\.password=.*",
                "backend.password=password",
                content,
            )

            # Set faster polling interval for local development (1 minute instead of 10 minutes)
            content = re.sub(
                r"runner\.UpdateInterval=.*",
                "runner.UpdateInterval=1",
                content,
            )

            runner_config.write_text(content)
            console.print(
                "  [green]✓[/green] Runner config: updated for local API (localhost:8090, 1min polling)"
            )
    else:
        console.print("  [red]✗[/red] Runner config: application.properties not found")
        all_ok = False

    # Check GUI
    gui_package = repos["gui"]["path"] / "package.json"
    gui_dockerfile = repos["gui"]["path"] / "Dockerfile"
    api_public_gui = repos["api"]["path"] / "public" / "gui"

    if gui_package.exists():
        console.print("  [green]✓[/green] GUI: package.json found")

        # Check for Docker support
        if gui_dockerfile.exists():
            console.print("  [green]✓[/green] GUI: Dockerfile found (Docker build supported)")
        else:
            console.print("  [dim]○[/dim] GUI: No Dockerfile (requires npm to build)")

            # Only check node_modules if no Docker
            gui_modules = repos["gui"]["path"] / "node_modules"
            if gui_modules.exists():
                console.print("  [green]✓[/green] GUI: node_modules installed")
            else:
                console.print("  [dim]○[/dim] GUI: node_modules not found (npm install needed)")

            # Check if built locally
            gui_dist = repos["gui"]["path"] / "dist"
            if gui_dist.exists():
                console.print("  [green]✓[/green] GUI: dist/ built")
            else:
                console.print("  [dim]○[/dim] GUI: not built yet")

        # Check if deployed to API (most important check)
        if api_public_gui.exists() and (api_public_gui / "index.html").exists():
            console.print("  [green]✓[/green] GUI: deployed to API (public/gui)")
        else:
            console.print("  [dim]○[/dim] GUI: not deployed to API yet")
    else:
        console.print("  [yellow]⚠[/yellow] GUI: package.json not found")

    # Check .env
    if env_file.exists():
        console.print("  [green]✓[/green] Config: .env exists")
    else:
        console.print("  [red]✗[/red] Config: .env missing")
        all_ok = False

    # Lambda note (runs in AWS, not locally)
    console.print("\n  [dim]Note: Lambda (bot-decision-yt) runs in AWS, not locally.[/dim]")
    console.print("  [dim]The runner invokes it via HTTP during job execution.[/dim]")

    # Check AWS credentials (optional, for log viewing)
    import os

    if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_SECRET_ACCESS_KEY"):
        console.print("  [green]✓[/green] AWS credentials configured (for Lambda log viewing)")
    else:
        console.print("  [dim]○[/dim] AWS credentials not set (optional, for Lambda log viewing)")
        console.print(
            "    [dim]Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in .env for 'orchestrator lambda' commands[/dim]"
        )

    # Step 5: Start components
    if not skip_start and all_ok:
        console.print("\n[bold]Step 5: Starting components[/bold]\n")

        if click.confirm("Start all PrimeMover components now?", default=True):
            # Reload settings to pick up new .env
            from orchestrator.config.settings import Settings

            settings = Settings()

            manager = _get_component_manager(settings)
            console.print()
            manager.start(api=True, runner=True, monitoring=True)

            console.print("\n[bold]Step 6: Initialize API (Laravel)[/bold]\n")
            if click.confirm("Run full API initialization?", default=True):
                # Full initialization sequence for fresh setup
                init_steps = [
                    ("Installing PHP dependencies", "composer install --no-interaction"),
                    ("Generating app key", "key:generate --force"),
                    ("Generating JWT secret", "jwt:secret --force"),
                    ("Running migrations", "migrate --force"),
                    ("Seeding database", "db:seed --force"),
                    ("Installing Passport (OAuth)", "passport:install --force"),
                    ("Creating storage link", "storage:link"),
                ]

                for step_name, command in init_steps:
                    console.print(f"  [cyan]⋯[/cyan] {step_name}...")

                    # composer install needs special handling (not artisan)
                    if command.startswith("composer"):
                        # Use the API component's compose command detection
                        from orchestrator.components.api import _get_compose_cmd

                        compose_cmd = _get_compose_cmd()
                        api_compose_file = repos["api"]["path"] / "docker-compose.yml"
                        result = subprocess.run(
                            compose_cmd
                            + [
                                "-f",
                                str(api_compose_file),
                                "exec",
                                "-T",
                                "php",
                                "composer",
                                "install",
                                "--no-interaction",
                            ],
                            cwd=repos["api"]["path"],
                            capture_output=True,
                            text=True,
                        )
                        success = result.returncode == 0
                        output = result.stdout + result.stderr
                    else:
                        success, output = manager.api.run_artisan(command)

                    if success:
                        console.print(f"  [green]✓[/green] {step_name}")
                    else:
                        # Some commands may "fail" but are OK (e.g., storage link already exists)
                        if (
                            "already exists" in output.lower()
                            or "nothing to migrate" in output.lower()
                        ):
                            console.print(f"  [green]✓[/green] {step_name} (already done)")
                        else:
                            console.print(f"  [yellow]⚠[/yellow] {step_name}: {output[:100]}...")

                console.print("\n[green]API initialization complete![/green]")
                console.print("[dim]Default login: admin@primemover.example / password[/dim]")

                # Step 7: Verify API is working
                console.print("\n[bold]Step 7: Verifying API[/bold]\n")
                console.print("  [cyan]⋯[/cyan] Testing API authentication...")

                import time

                import httpx

                # Give API a moment to stabilize after initialization
                time.sleep(2)

                try:
                    response = httpx.post(
                        "http://localhost:8090/api/v1/login",
                        json={"email": "admin@primemover.example", "password": "password"},
                        timeout=10.0,
                    )
                    if response.status_code == 200 and "access_token" in response.text:
                        console.print("  [green]✓[/green] API authentication working")
                    else:
                        console.print(
                            f"  [yellow]⚠[/yellow] API responded but auth may have issues: {response.status_code}"
                        )
                except Exception as e:
                    console.print(f"  [yellow]⚠[/yellow] Could not verify API: {e}")
                    console.print(
                        '    [dim]The API may still be starting up. Try: curl -X POST http://localhost:8090/api/v1/login -H \'Content-Type: application/json\' -d \'{"email":"admin@primemover.example","password":"password"}\'[/dim]'
                    )

    elif skip_start:
        console.print("\n[bold]Step 5: Starting components[/bold] [dim](skipped)[/dim]\n")

    # Final summary
    console.print("\n" + "=" * 60)
    console.print("[bold green]Setup Complete![/bold green]\n")

    console.print("[bold]Quick Start:[/bold]")
    console.print("  orchestrator status      # Check component status")
    console.print("  orchestrator up          # Start all components")
    console.print("  orchestrator db reset    # Reset database")
    console.print()
    console.print("[bold]Access:[/bold]")
    console.print("  API:     http://localhost:8090")
    console.print("  GUI:     http://localhost:8090/gui")
    console.print("  Grafana: http://localhost:3000")
    console.print()
    console.print("[bold]Lambda (runs in AWS):[/bold]")
    console.print("  orchestrator lambda logs -t 1h        # View Lambda logs")
    console.print("  orchestrator lambda decisions <job>   # Analyze decisions")
    console.print("  [dim]Requires AWS credentials in .env for log access[/dim]")
    console.print()
    console.print("[dim]Credentials: admin@primemover.example / password[/dim]")


# =============================================================================
# STATUS COMMAND
# =============================================================================


def _get_component_manager(settings, lambda_server_enabled: bool = True):
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
        runner_log_path=Path(settings.pm_runner_log_path).resolve(),
        lambda_server_enabled=lambda_server_enabled,
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

        # Check PrimeMover API. There is no unauthenticated health route -- see
        # _check_url's note on why 401 is the healthy answer here.
        api_status = _check_url(
            settings.pm_api_base_url,
            "/crawlers",
            headers={"Accept": "application/json"},
            healthy_codes=(200, 401),
        )
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


def _check_url(
    base_url: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    healthy_codes: tuple[int, ...] | None = None,
) -> tuple[str, str]:
    """Check if a URL is healthy.

    ``healthy_codes`` overrides the default "anything below 400" rule. The
    PrimeMover API needs it: every /api/v1 route is behind ``auth:api``, so an
    unauthenticated probe can only ever prove liveness by getting a **401**.
    Getting that 401 also requires ``Accept: application/json`` -- otherwise
    Laravel redirects to a ``login`` route this API does not define and the
    probe sees a 404, which is what made this check cry wolf on a healthy API.
    """
    import httpx

    try:
        url = f"{base_url.rstrip('/')}{path}"
        response = httpx.get(url, timeout=5.0, headers=headers)
        healthy = (
            response.status_code in healthy_codes
            if healthy_codes is not None
            else response.status_code < 400
        )
        if healthy:
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
@click.option(
    "--lambda-local/--no-lambda-local", default=True, help="Start local Lambda server (for runner)"
)
@click.pass_context
def up(ctx: click.Context, api: bool, runner: bool, monitoring: bool, lambda_local: bool) -> None:
    """
    Start PrimeMover components.

    By default starts all components in correct order:
    1. Monitoring (Grafana, Loki, Prometheus, Tempo)
    2. Lambda Server (local Python service for video selection)
    3. API (Apache, PHP, MySQL, Redis)
    4. Runner (Java process)

    Examples:
        orchestrator up                   # Start all components
        orchestrator up --no-runner       # Start API and monitoring only
        orchestrator up --no-lambda-local # Use AWS Lambda instead of local
    """
    settings = ctx.obj["settings"]

    if not any([api, runner, monitoring, lambda_local]):
        console.print(
            "[yellow]No components selected."
            " Use --api, --runner, --monitoring or --lambda-local.[/yellow]"
        )
        return

    console.print("[bold]Starting PrimeMover components...[/bold]\n")

    manager = _get_component_manager(settings, lambda_server_enabled=lambda_local)
    system_status = manager.start(
        api=api, runner=runner, monitoring=monitoring, lambda_server=lambda_local
    )

    console.print()
    if system_status.overall.value == "running":
        console.print("[green bold]All requested components started successfully![/green bold]")
    elif system_status.overall.value == "error":
        console.print(
            "[red bold]Some components failed to start. Check the output above.[/red bold]"
        )
    else:
        console.print(f"[yellow]Components status: {system_status.overall.value}[/yellow]")


@cli.command()
@click.option("--api/--no-api", default=True, help="Stop API component")
@click.option("--runner/--no-runner", default=True, help="Stop Runner component")
@click.option("--monitoring/--no-monitoring", default=True, help="Stop Monitoring component")
@click.option("--lambda-local/--no-lambda-local", default=True, help="Stop local Lambda server")
@click.option("--volumes", is_flag=True, help="Also remove Docker volumes (data loss!)")
@click.pass_context
def down(
    ctx: click.Context, api: bool, runner: bool, monitoring: bool, lambda_local: bool, volumes: bool
) -> None:
    """
    Stop PrimeMover components.

    By default stops all components in reverse order:
    1. Runner (Java process)
    2. API (Apache, PHP, MySQL, Redis)
    3. Lambda Server (local Python service)
    4. Monitoring (Grafana, Loki, Prometheus, Tempo)

    Examples:
        orchestrator down                 # Stop all components
        orchestrator down --no-monitoring # Stop API and Runner only
        orchestrator down --volumes       # Stop all and remove data volumes
    """
    settings = ctx.obj["settings"]

    if not any([api, runner, monitoring, lambda_local]):
        console.print(
            "[yellow]No components selected."
            " Use --api, --runner, --monitoring or --lambda-local.[/yellow]"
        )
        return

    if volumes:
        console.print("[red bold]WARNING: This will remove Docker volumes![/red bold]")
        console.print("[dim]This means database data, metrics, and logs will be lost.[/dim]")
        if not click.confirm("Are you sure?"):
            console.print("Cancelled.")
            return

    console.print("[bold]Stopping PrimeMover components...[/bold]\n")

    manager = _get_component_manager(settings)
    system_status = manager.stop(
        api=api,
        runner=runner,
        monitoring=monitoring,
        lambda_server=lambda_local,
        remove_volumes=volumes,
    )

    console.print()
    if system_status.overall.value == "stopped":
        console.print("[green bold]All requested components stopped successfully![/green bold]")
    elif system_status.overall.value == "error":
        console.print(
            "[red bold]Some components failed to stop. Check the output above.[/red bold]"
        )


@cli.command()
@click.option("--api", is_flag=True, help="Restart API component")
@click.option("--runner", is_flag=True, help="Restart Runner component")
@click.option("--monitoring", is_flag=True, help="Restart Monitoring component")
@click.option("--lambda-local", is_flag=True, help="Restart Lambda Server component")
@click.pass_context
def restart(
    ctx: click.Context, api: bool, runner: bool, monitoring: bool, lambda_local: bool
) -> None:
    """
    Restart PrimeMover components.

    Stops then starts the specified components.

    Examples:
        orchestrator restart              # Restart all components
        orchestrator restart --runner     # Restart just the runner
        orchestrator restart --api --runner  # Restart API and runner
    """
    settings = ctx.obj["settings"]

    # If no specific components selected, restart all
    if not any([api, runner, monitoring, lambda_local]):
        api = runner = monitoring = lambda_local = True

    console.print("[bold]Restarting PrimeMover components...[/bold]\n")

    manager = _get_component_manager(settings)
    system_status = manager.restart(
        api=api, runner=runner, monitoring=monitoring, lambda_server=lambda_local
    )

    console.print()
    if system_status.overall.value == "running":
        console.print("[green bold]All requested components restarted successfully![/green bold]")
    elif system_status.overall.value == "error":
        console.print(
            "[red bold]Some components failed to restart. Check the output above.[/red bold]"
        )


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

    # Check Docker Compose (v2 or v1)
    compose_v2_result = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True
    )
    compose_v1_path = shutil.which("docker-compose")

    if compose_v2_result.returncode == 0:
        version = (
            compose_v2_result.stdout.strip().split()[-1] if compose_v2_result.stdout else "unknown"
        )
        console.print(f"  [green]✓[/green] Docker Compose v2 (plugin) version {version}")
    elif compose_v1_path:
        result = subprocess.run(["docker-compose", "--version"], capture_output=True, text=True)
        version = result.stdout.strip().split()[-1] if result.returncode == 0 else "unknown"
        console.print(f"  [green]✓[/green] Docker Compose v1 (standalone) version {version}")
    else:
        console.print("  [red]✗[/red] Docker Compose not found (install docker-compose-plugin)")
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
            console.print("  [green]✓[/green] Java found")
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
            console.print("  [yellow]⚠[/yellow] API path exists but missing docker-compose.yml")
            all_ok = False
    else:
        console.print(f"  [red]✗[/red] API path not found: {api_path}")
        all_ok = False

    # Check Runner path
    runner_path = settings.get_runner_path()
    if runner_path.exists():
        jars = (
            list((runner_path / "primemover-runner" / "target").glob("primemover-runner-*.jar"))
            if (runner_path / "primemover-runner" / "target").exists()
            else []
        )
        if jars:
            console.print(f"  [green]✓[/green] Runner path exists: {runner_path}")
        else:
            console.print(
                "  [yellow]⚠[/yellow] Runner path exists but JAR not found (run mvn package)"
            )
    else:
        console.print(f"  [red]✗[/red] Runner path not found: {runner_path}")
        all_ok = False

    # Check Orchestrator path
    orch_path = settings.get_orchestrator_path()
    compose_file = orch_path / "docker-compose.yml"
    if compose_file.exists():
        console.print(f"  [green]✓[/green] Orchestrator path: {orch_path}")
    else:
        console.print("  [red]✗[/red] Orchestrator docker-compose.yml not found")
        all_ok = False

    console.print("\n[bold]Checking ports...[/bold]\n")

    import socket

    def check_port(port: int, name: str):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(("localhost", port))
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
            console.print(
                f"  [yellow]⚠[/yellow] Port {port} in use ({name}) - component may be running"
            )

    # Systemd units.
    #
    # This exists because a failing unit is otherwise invisible.
    # primemover-health-check is a one-shot that runs at boot; when it failed it
    # stayed `failed` for over a week with nobody noticing, because the only
    # place that state is visible is `systemctl status`, which nobody runs
    # during normal work. `orchestrator doctor` is where people already look.
    console.print("\n[bold]Checking systemd units...[/bold]\n")

    if not shutil.which("systemctl"):
        console.print("  [dim]-[/dim] systemd not present, skipping (not a VM deployment)")
    else:
        units = [
            ("primemover-lambda", "Local Lambda service"),
            ("primemover-runner", "Java runner"),
            ("primemover-health-check", "Boot health check"),
            ("vncserver@1", "VNC display :1"),
            ("multilogin-desktop", "Multilogin X"),
        ]

        def _systemctl(*args: str) -> str:
            return subprocess.run(
                ["systemctl", *args], capture_output=True, text=True
            ).stdout.strip()

        installed_any = False
        for unit, label in units:
            enabled = _systemctl("is-enabled", unit)
            if enabled == "not-found":
                continue
            installed_any = True

            state = _systemctl("is-active", unit)
            if state == "failed":
                # When it last ran matters: a one-shot that failed at boot weeks
                # ago reads identically to one that failed a minute ago.
                # ActiveEnterTimestamp is empty for a unit that never went
                # active, which is exactly the failing-one-shot case, so fall
                # back to StateChangeTimestamp.
                when = _systemctl("show", "-p", "StateChangeTimestamp", "--value", unit)
                suffix = f", since {when}" if when else ""
                console.print(f"  [red]✗[/red] {label} ({unit}) FAILED{suffix}")
                console.print(f"      [dim]journalctl -u {unit} -n 50[/dim]")
                all_ok = False
            elif state == "active":
                console.print(f"  [green]✓[/green] {label} ({unit})")
            elif enabled == "disabled":
                console.print(f"  [dim]-[/dim] {label} ({unit}) not enabled: {state}")
            else:
                console.print(f"  [yellow]⚠[/yellow] {label} ({unit}) {state}")

        if not installed_any:
            console.print(
                "  [dim]-[/dim] no PrimeMover units installed "
                "(run scripts/install-systemd-services.sh on a VM deployment)"
            )

    # Configuration & credentials preflight.
    #
    # Everything above checks the machine; these check that THIS box/account is
    # actually configured. They exist because a fresh deployment previously only
    # discovered a missing symlink, an unset secret, or an unreachable job-data
    # store at runtime -- see docs/new-server-setup.md. Missing credentials are a
    # warning, not a hard failure: a given command may not need all of them.
    console.print("\n[bold]Checking configuration & credentials...[/bold]\n")

    # Secrets that have no default and block the thing named if unset.
    for value, label in (
        (settings.pm_api_email, "PM_API_EMAIL (API login)"),
        (settings.pm_api_password, "PM_API_PASSWORD (API login)"),
        (
            settings.grafana_admin_password,
            "GRAFANA_ADMIN_PASSWORD (Grafana won't start without it)",
        ),
    ):
        if value:
            console.print(f"  [green]✓[/green] {label.split(' (')[0]} set")
        else:
            console.print(f"  [yellow]⚠[/yellow] {label} not set")

    # The decision symlink: the local Lambda server refuses to start without it.
    decision = settings.get_orchestrator_path() / "orchestrator" / "lambda_service" / "decision"
    if (decision / "lambda_function.py").exists():
        console.print(
            f"  [green]✓[/green] decision symlink resolves ({decision} → bot-decision-EN)"
        )
    else:
        console.print(
            f"  [red]✗[/red] decision symlink broken or missing: {decision}\n"
            "      [dim]ln -s ../../../bot-decision-yt/bot-decision-EN "
            "orchestrator/lambda_service/decision[/dim]"
        )
        all_ok = False

    # The job-data (MinIO) reader: without a working key, `debug job` /
    # `lambda invocations` silently return nothing rather than failing.
    from orchestrator.logs.jobdata_s3 import store_from_settings

    store = store_from_settings(settings)
    if not store.configured:
        console.print(
            "  [yellow]⚠[/yellow] JOBDATA_S3 key not set — 'orchestrator debug job' and "
            "'lambda invocations' will be blind (mint a scoped read-only MinIO key)"
        )
    else:
        try:
            store.reachable()
            console.print(f"  [green]✓[/green] job-data store reachable ({store.endpoint})")
        except Exception as exc:  # noqa: BLE001 - report any failure, don't crash doctor
            console.print(
                f"  [red]✗[/red] job-data store NOT reachable ({store.endpoint}): "
                f"{str(exc)[:80]}"
            )
            all_ok = False

    # The standing treatment canary, if this deployment runs one.
    #
    # It is a fixed list of daily sessions rather than a recurring job, because
    # start_at already schedules work -- so it RUNS OUT, and when it does the
    # canary goes quiet. Silence from a canary reads as health, which is the one
    # failure it must not have. Warn while there is still time to top it up.
    console.print("\n[bold]Checking the treatment canary...[/bold]\n")

    from orchestrator.canary import find_canary
    from orchestrator.clients.api import APIClient

    try:
        client = APIClient()
        try:
            canary = find_canary(client)
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 - the API check above already reported reachability
        console.print(f"  [dim]-[/dim] could not check ({str(exc)[:60]})")
        canary = None

    if canary is None:
        pass
    elif canary.missing:
        # Not a failure: most deployments do not run one.
        console.print("  [dim]-[/dim] no treatment canary on this deployment")
    else:
        label = f'#{canary.experiment_id} "{canary.experiment_name}"'
        if canary.remaining == 0:
            console.print(f"  [red]✗[/red] canary {label} has NO sessions left — it is silent")
            all_ok = False
        elif canary.needs_attention:
            console.print(
                f"  [yellow]⚠[/yellow] canary {label}: {canary.remaining} session(s) left, "
                f"last on {canary.last_start:%Y-%m-%d}"
                if canary.last_start
                else f"  [yellow]⚠[/yellow] canary {label}: {canary.remaining} session(s) left"
            )
            all_ok = False
        else:
            console.print(
                f"  [green]✓[/green] canary {label}: {canary.remaining} session(s) left, "
                f"last on {canary.last_start:%Y-%m-%d}"
            )

        if canary.needs_attention:
            console.print(
                "      top up with: ./venv/bin/python scripts/treatment-canary.py "
                "--days 28 --out /tmp/canary.json"
            )
            console.print(
                "      then POST it to a NEW experiment (see CLAUDE.md, " '"standing canary")'
            )

        for problem in canary.problems:
            console.print(f"      [yellow]![/yellow] {problem}")

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
            console.print("  [red]✗[/red] API path exists but missing docker-compose.yml")
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
                console.print("  [yellow]⚠[/yellow] Runner path exists but JAR not built")
                warnings.append("Run 'mvn package' in the runner directory")
        else:
            console.print("  [yellow]⚠[/yellow] Runner path exists but target/ not found")
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
        console.print("  [red]✗[/red] Orchestrator docker-compose.yml not found")
        all_valid = False

    console.print("\n[bold]Checking ports...[/bold]\n")

    def check_port(port: int) -> tuple[bool, str]:
        """Check if port is available or in use."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(("localhost", port))
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
            seed_success, seed_output = manager.api.run_artisan(
                "db:seed --force" if force else "db:seed"
            )
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
        lines = output.strip().split("\n")
        for line in lines:
            if "Ran" in line or "✓" in line or "Yes" in line:
                console.print(f"  [green]✓[/green] {line.strip()}")
            elif "Pending" in line or "✗" in line or "No" in line:
                console.print(f"  [yellow]○[/yellow] {line.strip()}")
            elif line.strip():
                console.print(f"  {line.strip()}")
    else:
        console.print(f"[red]Failed to get status:[/red]\n{output}")


@db.command("init")
@click.option("--force", is_flag=True, help="Skip confirmation prompt")
@click.option("--skip-composer", is_flag=True, help="Skip composer install")
@click.pass_context
def db_init(ctx: click.Context, force: bool, skip_composer: bool) -> None:
    """
    Run full API initialization (fresh setup).

    This runs all Laravel initialization commands:
    - composer install (PHP dependencies)
    - key:generate (app encryption key)
    - jwt:secret (JWT authentication)
    - migrate (database tables)
    - db:seed (default data)
    - passport:install (OAuth)
    - storage:link (public storage)

    Use this after cloning the API repo or for a fresh start.
    """
    import subprocess

    settings = ctx.obj["settings"]
    manager = _get_component_manager(settings)

    if not manager.api.is_running():
        console.print("[red]API is not running. Start it first with 'orchestrator up --api'[/red]")
        return

    console.print("[bold]Full API Initialization[/bold]\n")
    console.print("[dim]This will run all Laravel setup commands.[/dim]")

    if not force:
        if not click.confirm("Proceed with initialization?"):
            console.print("Cancelled.")
            return

    # Full initialization sequence
    init_steps = [
        ("Installing PHP dependencies", "composer install --no-interaction", skip_composer),
        ("Generating app key", "key:generate --force", False),
        ("Generating JWT secret", "jwt:secret --force", False),
        ("Running migrations", "migrate --force", False),
        ("Seeding database", "db:seed --force", False),
        ("Installing Passport (OAuth)", "passport:install --force", False),
        ("Creating storage link", "storage:link", False),
    ]

    console.print()
    for step_name, command, skip in init_steps:
        if skip:
            console.print(f"  [dim]○[/dim] {step_name} (skipped)")
            continue

        console.print(f"  [cyan]⋯[/cyan] {step_name}...")

        # composer install needs special handling (not artisan)
        if command.startswith("composer"):
            from orchestrator.components.api import _get_compose_cmd

            api_path = settings.get_api_path()
            compose_cmd = _get_compose_cmd()
            api_compose_file = api_path / "docker-compose.yml"
            result = subprocess.run(
                compose_cmd
                + [
                    "-f",
                    str(api_compose_file),
                    "exec",
                    "-T",
                    "php",
                    "composer",
                    "install",
                    "--no-interaction",
                ],
                cwd=api_path,
                capture_output=True,
                text=True,
            )
            success = result.returncode == 0
            output = result.stdout + result.stderr
        else:
            success, output = manager.api.run_artisan(command)

        if success:
            console.print(f"  [green]✓[/green] {step_name}")
        else:
            # Some commands may "fail" but are OK
            if "already exists" in output.lower() or "nothing to migrate" in output.lower():
                console.print(f"  [green]✓[/green] {step_name} (already done)")
            else:
                console.print(f"  [yellow]⚠[/yellow] {step_name}")
                console.print(f"      [dim]{output[:200]}...[/dim]")

    console.print("\n[green bold]API initialization complete![/green bold]")
    console.print("[dim]Default login: admin@primemover.example / password[/dim]")


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
@click.option(
    "-f",
    "--format",
    "output_format",
    type=click.Choice(["table", "json", "plain"]),
    default="plain",
)
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

    console.print(
        f"[dim]Querying logs: component={component}, time={time_range}, level={level}[/dim]"
    )
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
                lvl = (
                    f"[{level_style}]{entry.level:5}[/{level_style}]"
                    if level_style
                    else entry.level
                )

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

        console.print("\n[bold]Top Error Messages:[/bold]")
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
@click.option(
    "-f",
    "--format",
    "output_format",
    type=click.Choice(["tree", "json", "summary"]),
    default="tree",
)
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
@click.option(
    "-f",
    "--format",
    "output_format",
    type=click.Choice(["tree", "json", "summary"]),
    default="tree",
)
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
@click.option(
    "-f",
    "--format",
    "output_format",
    type=click.Choice(["tree", "json", "summary"]),
    default="tree",
)
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

    def _status_style(status: str) -> str:
        return {
            "success": "green",
            "warning": "yellow",
            "skipped": "dim",
            "failed": "red",
            "running": "yellow",
            "pending": "dim",
            "unknown": "dim",
        }.get(status, "dim")

    def _add_span_to_tree(tree, span) -> None:
        status_icon = {
            "success": "[green]✓[/green]",
            # A warning span ran to completion with something degraded -- never
            # a red ✗ (it did not fail) and never a green ✓ (it is exactly the
            # silent degradation this state exists to make visible).
            "warning": "[yellow]⚠[/yellow]",
            "skipped": "[dim]⊘[/dim]",
            "failed": "[red]✗[/red]",
            "running": "[yellow]⋯[/yellow]",
            "pending": "[dim]○[/dim]",
            "unknown": "[dim]?[/dim]",
        }.get(span.status.value if hasattr(span.status, "value") else span.status, "?")

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

        # Show errors. On a warning span these are degradation notes, not
        # errors -- labelling them ERROR is how people learn to ignore them.
        if span.errors:
            span_status = span.status.value if hasattr(span.status, "value") else span.status
            if span_status == "warning":
                for err in span.errors[:3]:
                    branch.add(f"[yellow]DEGRADED: {err[:80]}[/yellow]")
            else:
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
                    logs_branch.add(
                        f"[dim]{time_str}[/dim] [{level_style}]{log.level}[/{level_style}] {msg}"
                    )
                else:
                    logs_branch.add(f"[dim]{time_str}[/dim] {log.level} {msg}")

        # Add children
        for child in span.children:
            _add_span_to_tree(branch, child)

    # Build the tree
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
                (
                    entry.message[17:67] if len(entry.message) > 17 else entry.message
                ),  # Skip "Lambda decision: "
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
            console.print("\n[bold]XPath:[/bold]")
            console.print(f"  {output.get('xpath', 'N/A')}")

            # Timestamps
            if "timestamps" in output:
                console.print("\n[bold]Execution Timeline:[/bold]")
                ts = output["timestamps"]
                if isinstance(ts, dict) and "progress" in ts:
                    for step in ts["progress"].values():
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
@click.option(
    "-f",
    "--format",
    "output_format",
    type=click.Choice(["plain", "json", "table"]),
    default="plain",
)
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
                req_id = (
                    f"[cyan]{entry.request_id[:8]}[/cyan]"
                    if entry.request_id
                    else "[dim]--------[/dim]"
                )
                lvl = (
                    f"[{level_style}]{entry.level:5}[/{level_style}]"
                    if level_style
                    else entry.level
                )

                console.print(f"[dim]{time_str}[/dim] {req_id} {lvl} {entry.message[:120]}")

        client.close()

    except Exception as e:
        console.print(f"[red]Error querying CloudWatch: {e}[/red]")
        import traceback

        console.print(f"[dim]{traceback.format_exc()}[/dim]")


@lambda_.command("metrics")
@click.option("-t", "--time-range", default="1h", help="Time range (15m, 1h, 24h, 7d)")
@click.option(
    "-f", "--format", "output_format", type=click.Choice(["summary", "json"]), default="summary"
)
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

            console.print("[bold]Invocations:[/bold]")
            console.print(f"  Total:       {metrics.get('invocation_count', 0)}")
            console.print(f"  Errors:      {metrics.get('error_count', 0)}")
            console.print(f"  Cold Starts: {metrics.get('cold_start_count', 0)}")

            console.print()
            console.print("[bold]Duration (ms):[/bold]")
            duration = metrics.get("duration", {})
            console.print(f"  Average:  {duration.get('avg', 0):.1f}")
            console.print(f"  Min:      {duration.get('min', 0):.1f}")
            console.print(f"  Max:      {duration.get('max', 0):.1f}")
            console.print(f"  P99:      {duration.get('p99', 0):.1f}")

            console.print()
            console.print("[bold]Memory (MB):[/bold]")
            memory = metrics.get("memory", {})
            console.print(f"  Configured: {memory.get('configured', 0)}")
            console.print(f"  Avg Used:   {memory.get('avg_used', 0):.1f}")
            console.print(f"  Max Used:   {memory.get('max_used', 0)}")

            if metrics.get("recent_errors"):
                console.print()
                console.print(
                    f"[bold red]Recent Errors ({len(metrics['recent_errors'])}):[/bold red]"
                )
                for err in metrics["recent_errors"][:5]:
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
@click.option(
    "-f", "--format", "output_format", type=click.Choice(["table", "json"]), default="table"
)
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
                error_data.append(
                    {
                        "request_id": inv.request_id,
                        "start_time": inv.start_time.isoformat() if inv.start_time else None,
                        "duration_ms": inv.duration_ms,
                        "error": inv.error,
                        "log_count": len(inv.logs),
                    }
                )
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
                "max_memory_used_mb": invocation.memory_used_mb,
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
                console.print(
                    f"  [cyan]Cold Start Init: {invocation.init_duration_ms:.1f} ms[/cyan]"
                )

            # Memory
            console.print()
            console.print("[bold]Memory:[/bold]")
            if invocation.memory_size_mb:
                console.print(f"  Configured: {invocation.memory_size_mb} MB")
            if invocation.memory_used_mb:
                console.print(f"  Max Used:   {invocation.memory_used_mb} MB")
                if invocation.memory_size_mb:
                    pct = (invocation.memory_used_mb / invocation.memory_size_mb) * 100
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
                    lvl = (
                        f"[{level_style}]{log.level:5}[/{level_style}]"
                        if level_style
                        else log.level
                    )
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
@click.option(
    "-f", "--format", "output_format", type=click.Choice(["text", "json"]), default="text"
)
@click.pass_context
def experiment_validate(
    ctx: click.Context, file: str, strict: bool, seed: int | None, output_format: str
) -> None:
    """Validate an experiment YAML specification."""
    import json
    from pathlib import Path

    try:
        import yaml as yaml_lib

        from experiment_designer.spec import SpecConverter, SpecParser, SpecValidator
        from experiment_designer.spec.design_expander import DesignExpander
        from experiment_designer.spec.file_loader import (
            create_file_tag_loader,
            resolve_file_references,
        )
    except ImportError as e:
        console.print(f"[red]Error importing experiment_designer: {e}[/red]")
        console.print("[dim]Install with: pip install -e .[/dim]")
        return

    file_path = Path(file)
    base_dir = file_path.parent

    with open(file) as f:
        content = f.read()

    # Create custom YAML loader with !file tag support
    FileTagLoader, file_loader = create_file_tag_loader(base_dir=base_dir, seed=seed)

    # Parse YAML first
    try:
        raw_spec = yaml_lib.load(content, Loader=FileTagLoader)
        raw_spec = resolve_file_references(raw_spec, file_loader)
    except yaml_lib.YAMLError as e:
        if output_format == "json":
            console.print(
                json.dumps({"valid": False, "errors": [f"YAML parse error: {e}"], "warnings": []})
            )
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
    all_errors = list(set(validation_result.errors + parsed.errors + result.errors))
    all_warnings = list(set(validation_result.warnings + parsed.warnings + result.warnings))

    if strict:
        all_errors.extend(all_warnings)
        all_warnings = []

    if output_format == "json":
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
@click.option(
    "--mode",
    # Hardcoded rather than importing experiment_designer.lock.upload.MODES at
    # module load time: that import alone costs ~150ms and would slow every
    # `orchestrator` invocation, not just this command. Keep in sync with
    # MODES there.
    type=click.Choice(("real", "test")),
    required=True,
    help="test = VM runner, test- name prefix; real = as in the lock",
)
@click.option("-e", "--experiment-id", type=int, help="Add to existing experiment")
@click.option("--dry-run", is_flag=True, help="Validate and convert without uploading")
@click.option("--seed", "-s", type=int, help="Random seed for reproducible randomization")
@click.option("--record", type=click.Path(), help="Append the one-line upload record to this file")
@click.option("--allow-dirty", is_flag=True)
@click.option("--allow-past", is_flag=True)
@click.option(
    "--deep", is_flag=True, help="Post-upload verify down to jobs and behaviours (uses the export)"
)
@click.pass_context
def experiment_upload(
    ctx: click.Context,
    file: str,
    mode: str,
    experiment_id: int | None,
    dry_run: bool,
    seed: int | None,
    record: str | None,
    allow_dirty: bool,
    allow_past: bool,
    deep: bool,
) -> None:
    """Upload a lock (compiling a spec first) and verify the result, same as `pmspec upload`."""
    from pathlib import Path

    from experiment_designer.lock.api import ApiError, ApiSession
    from experiment_designer.lock.build import compile_spec
    from experiment_designer.lock.io import dump_lock, is_lock_document, load_lock
    from experiment_designer.lock.secrets import (
        InsecureSecretsFile,
        MissingSecret,
        SecretStore,
        find_placeholders,
    )
    from experiment_designer.lock.upload import UploadOptions, UploadRefused, upload_lock
    from experiment_designer.lock.verify import verify_lock

    settings = ctx.obj["settings"]
    file_path = Path(file)
    lock_text = file_path.read_text()
    if not is_lock_document(lock_text):
        compiled = compile_spec(
            lock_text, base_dir=file_path.parent, spec_path=str(file_path), seed=seed
        )
        if compiled.lock is None:
            for error in compiled.errors:
                console.print(f"[red]  {error}[/red]")
            raise SystemExit(1)
        lock_path = file_path.with_name(f"{file_path.stem}.lock.yaml")
        lock_text = dump_lock(compiled.lock)
        lock_path.write_text(lock_text)
        console.print(f"[cyan]Compiled {lock_path} -- commit it[/cyan]")

    try:
        store = SecretStore.load()
    except FileNotFoundError:
        store = None
    except InsecureSecretsFile as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1) from e
    needed = sorted(find_placeholders(load_lock(lock_text).crawlers))
    missing = store.missing(needed) if store else needed
    for key in needed:
        console.print(f"  {'MISSING' if key in missing else 'OK':7} {key}")
    if missing:
        raise SystemExit(1)
    if dry_run:
        console.print("[cyan]Dry run - not uploading[/cyan]")
        return

    session = ApiSession(settings.pm_api_base_url, settings.pm_api_email, settings.pm_api_password)
    options = UploadOptions(
        mode=mode,
        experiment_id=experiment_id,
        allow_dirty=allow_dirty,
        allow_past=allow_past,
        contact="orchestrator CLI",
    )
    try:
        result = upload_lock(lock_text, session=session, store=store, options=options)
    except (UploadRefused, ApiError, MissingSecret) as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1) from e
    console.print(
        f"[green]Upload SUCCESS[/green] experiment {result.experiment_id} ({result.mode})"
    )
    if record:
        with open(record, "a") as f:
            f.write(result.record_line + "\n")
    console.print(result.record_line)

    try:
        verdict = verify_lock(
            lock_text, session=session, experiment_id=result.experiment_id, deep=deep
        )
    except ApiError as e:
        console.print(f"[red]Verify failed after upload: {e}[/red]")
        if e.detail:
            console.print(f"  {e.detail}")
        raise SystemExit(1) from e
    console.print(verdict.render())
    if not verdict.ok:
        raise SystemExit(1)


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
        from experiment_designer.spec import SpecConverter, SpecParser
    except ImportError as e:
        console.print(f"[red]Error importing experiment_designer: {e}[/red]")
        return

    file_path = Path(file)
    base_dir = file_path.parent

    with open(file) as f:
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
        with open(output, "w") as f:
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

    with open(file) as f:
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
    for factor in preview_info.get("factors", []):
        console.print(f"  [cyan]{factor['name']}[/cyan]: {factor['levels']}")
        if factor.get("maps_to"):
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
            headers={"Accept": "application/json"},
        )
        auth_response.raise_for_status()
        token = auth_response.json().get("access_token")

        # Fetch experiments
        list_response = requests.get(
            f"{api_url}/experiments",
            params={"per_page": limit},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
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
                str(exp.get("id", "")), exp.get("name", "")[:40], active, crawler_count, updated
            )

        console.print(table)

    except requests.exceptions.RequestException as e:
        console.print(f"[red]Error fetching experiments: {e}[/red]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


# =============================================================================
# DEBUG COMMANDS
# =============================================================================


@cli.group()
@click.pass_context
def debug(ctx: click.Context) -> None:
    """Debug failed jobs and diagnose issues."""
    pass


@debug.command("job")
@click.argument("job_id", type=int)
@click.option("-v", "--verbose", is_flag=True, help="Show detailed output")
@click.option("--no-har", is_flag=True, help="Skip HAR file analysis")
@click.option("--no-cloudwatch", is_flag=True, help="Skip CloudWatch logs")
@click.option("--no-logs", is_flag=True, help="Skip Loki/file logs")
@click.option(
    "-t",
    "--time-range",
    default="1h",
    help="Time range for log queries (default: 1h)",
)
@click.option("-e", "--export", type=click.Path(), help="Export report to JSON file")
@click.option(
    "-f",
    "--format",
    "output_format",
    type=click.Choice(["rich", "json", "plain"]),
    default="rich",
    help="Output format",
)
@click.pass_context
def debug_job(
    ctx: click.Context,
    job_id: int,
    verbose: bool,
    no_har: bool,
    no_cloudwatch: bool,
    no_logs: bool,
    time_range: str,
    export: str,
    output_format: str,
) -> None:
    """
    Debug a failed job by combining data from all sources.

    Collects and displays:

    \b
    - Job metadata from API (status, timing, crawler info)
    - Lambda decision output (selected video, xpath, warnings)
    - HAR file analysis (failed requests, slow requests)
    - Logs from Loki/files and CloudWatch

    \b
    Examples:
        orchestrator debug job 12345
        orchestrator debug job 12345 -v
        orchestrator debug job 12345 --export report.json
        orchestrator debug job 12345 --no-cloudwatch
    """
    from orchestrator.debug import JobDebugger

    console.print(f"[bold cyan]Debugging Job {job_id}[/bold cyan]\n")

    debugger = JobDebugger()

    with console.status("[bold green]Collecting debug information..."):
        report = debugger.debug_job(
            job_id=job_id,
            include_har=not no_har,
            include_cloudwatch=not no_cloudwatch,
            include_logs=not no_logs,
            log_time_range=time_range,
        )

    if output_format == "json" or export:
        import json

        report_dict = report.to_dict()
        json_output = json.dumps(report_dict, indent=2, default=str)

        if export:
            from pathlib import Path

            Path(export).write_text(json_output)
            console.print(f"[green]Report exported to {export}[/green]")

        if output_format == "json":
            console.print(json_output)
            return

    _print_debug_report(report, verbose)


def _print_debug_report(report, verbose: bool = False) -> None:
    """Print formatted debug report to console.

    Every attribute read here must exist on the debug dataclasses. Until
    2026-08-27 this renderer referenced four that did not (``experiment_name``,
    ``LambdaDecision.scoring``, ``CloudWatchInfo.errors``,
    ``Artifacts.screenshots``), so ``debug job`` raised AttributeError while
    ``--format json`` worked -- the default path was the broken one.
    """
    from rich.panel import Panel

    from orchestrator import status as status_contract
    from orchestrator.debug.har_parser import format_bytes, format_ms

    def _id_with_name(entity_id, name: str) -> str:
        """Render an id with its name when there is one -- GET /jobs/{id} does
        not carry the crawler or experiment name, so "42" is the honest answer
        there rather than "42 ()"."""
        if entity_id is None:
            return "N/A"
        return f"{entity_id} ({name})" if name else str(entity_id)

    # Job Metadata Section
    if report.metadata:
        meta = report.metadata
        status_color = {
            "success": "green",
            "warning": "yellow",
            "failure": "red",
            "skipped": "dim",
            "running": "cyan",
        }.get(meta.status, "dim")
        status_text = meta.status or "unknown"

        meta_table = Table(show_header=False, box=None, padding=(0, 2))
        meta_table.add_column("Key", style="dim")
        meta_table.add_column("Value")

        meta_table.add_row("Status", f"[{status_color}]{status_text}[/{status_color}]")
        # The runner's own diagnostic. It used to be dropped everywhere, and it
        # is the field most likely to say why.
        if meta.status_message:
            meta_table.add_row("Status Message", meta.status_message)
        meta_table.add_row("Experiment", _id_with_name(meta.experiment_id, meta.experiment_name))
        meta_table.add_row("Crawler", _id_with_name(meta.crawler_id, meta.crawler_name))
        meta_table.add_row("Queue ID", str(meta.queue_id) if meta.queue_id else "N/A")
        if meta.job_type:
            meta_table.add_row("Type", meta.job_type)

        if meta.started_at:
            meta_table.add_row("Started", meta.started_at.strftime("%Y-%m-%d %H:%M:%S"))
        if meta.completed_at:
            meta_table.add_row("Finished", meta.completed_at.strftime("%Y-%m-%d %H:%M:%S"))
        if meta.duration_seconds:
            meta_table.add_row("Duration", f"{meta.duration_seconds:.1f}s")

        if meta.diagnostic and not meta.diagnostic_is_duplicate:
            colour = "red" if meta.is_failure else "yellow"
            meta_table.add_row(meta.diagnostic_label, f"[{colour}]{meta.diagnostic}[/{colour}]")

        console.print(Panel(meta_table, title="[bold]Job Metadata[/bold]", border_style="blue"))
    else:
        console.print("[yellow]Job metadata not available (API unreachable?)[/yellow]\n")

    # Lambda Decision Section
    if report.lambda_decision:
        ld = report.lambda_decision

        lambda_table = Table(show_header=False, box=None, padding=(0, 2))
        lambda_table.add_column("Key", style="dim")
        lambda_table.add_column("Value")

        # `selection_mode` is a description, not a verdict. A control crawler
        # requests no LLM filter, draws uniformly, and correctly reports
        # "random": 236 of the 243 live crawler configurations on this box are
        # control-arm. Shouting "this arm behaved as a CONTROL arm" at every one
        # of them is the exact false alarm docs/integration-contract.md §7.3
        # warns against, and it trains people to ignore the row.
        #
        # `degraded` is the field that means "a human should look at this", so
        # the alarming wording is shown only when the decision itself says so.
        if ld.random_selection and ld.degraded:
            lambda_table.add_row(
                "Selection Mode",
                "[bold yellow]RANDOM — and this decision is degraded: no "
                "requested filter applied, so this arm behaved as a CONTROL "
                "arm[/bold yellow]",
            )
        elif ld.selection_mode:
            lambda_table.add_row(
                "Selection Mode",
                f"{ld.selection_mode}"
                + (" (expected for a control crawler)" if ld.random_selection else ""),
            )

        if ld.error:
            lambda_table.add_row("Error", f"[red]{str(ld.error)[:120]}[/red]")
        lambda_table.add_row(
            "Decision",
            "[green]ok[/green]" if ld.success else "[yellow]degraded[/yellow]",
        )
        lambda_table.add_row(
            "Selected Video", ld.selected_title[:60] if ld.selected_title else "N/A"
        )
        lambda_table.add_row("Video ID", ld.selected_id or "N/A")
        lambda_table.add_row("XPath", ld.xpath[:80] if ld.xpath else "N/A")

        if ld.llm and ld.llm.get("available") is False:
            llm_error = ld.llm.get("error") or ld.llm.get("error_type") or "unavailable"
            lambda_table.add_row("LLM", f"[yellow]{str(llm_error)[:80]}[/yellow]")

        border = "green" if ld.success else "yellow"
        console.print(
            Panel(lambda_table, title="[bold]Lambda Decision[/bold]", border_style=border)
        )

        failed_filters = ld.failed_filters
        if failed_filters:
            console.print("[yellow]Filters that did not apply:[/yellow]")
            for name, state in list(failed_filters.items())[:8]:
                console.print(f"  [yellow]⚠[/yellow] {name}: {str(state)[:100]}")
            console.print()

        if ld.warnings:
            console.print("[yellow]Warnings:[/yellow]")
            for w in ld.warnings[:5]:
                console.print(f"  [yellow]⚠[/yellow] {w[:100]}")
            console.print()
    elif report.artifacts and report.artifacts.lambda_zip:
        console.print("[yellow]Lambda output file found but could not be parsed[/yellow]\n")

    # HAR Analysis Section
    if report.har_summary:
        har = report.har_summary

        har_table = Table(show_header=False, box=None, padding=(0, 2))
        har_table.add_column("Key", style="dim")
        har_table.add_column("Value")

        har_table.add_row("Total Requests", str(har.total_requests))
        har_table.add_row(
            "Failed Requests", f"[red]{har.failed_count}[/red]" if har.failed_count else "0"
        )
        har_table.add_row(
            "Slow Requests", f"[yellow]{har.slow_count}[/yellow]" if har.slow_count else "0"
        )
        # Say what the total covers. A request the recorder could not time is
        # not a zero-length request, and quietly leaving it out of the total
        # while counting it in "Total Requests" would overstate how complete
        # the picture is.
        if har.untimed_count:
            har_table.add_row(
                "Total Time",
                f"{format_ms(har.total_time_ms)} "
                f"[dim](over {har.total_requests - har.untimed_count} of "
                f"{har.total_requests} requests)[/dim]",
            )
            har_table.add_row(
                "Untimed Requests",
                f"[yellow]{har.untimed_count}[/yellow] [dim](no response received; "
                f"the recorder reports no usable duration)[/dim]",
            )
        else:
            har_table.add_row("Total Time", format_ms(har.total_time_ms))
        har_table.add_row("Total Transfer", format_bytes(har.total_transfer_bytes))

        console.print(Panel(har_table, title="[bold]HAR Analysis[/bold]", border_style="cyan"))

        # Failed requests details
        if har.failed_requests:
            console.print("[red]Failed Requests:[/red]")
            for req in har.failed_requests[:5]:
                console.print(f"  [red]{req.status}[/red] {req.method} {req.url[:80]}")
                if verbose and req.response_body:
                    console.print(f"      [dim]{req.response_body[:200]}[/dim]")
            if len(har.failed_requests) > 5:
                console.print(f"  [dim]... and {len(har.failed_requests) - 5} more[/dim]")
            console.print()

        # Slow requests
        if har.slow_requests and verbose:
            console.print("[yellow]Slow Requests:[/yellow]")
            for req in har.slow_requests[:3]:
                console.print(
                    f"  [yellow]{format_ms(req.time_ms)}[/yellow] {req.method} {req.url[:70]}"
                )
            console.print()

        # Domain breakdown (verbose)
        if verbose and har.domain_stats:
            console.print("[dim]Domain Breakdown:[/dim]")
            for domain, stats in list(har.domain_stats.items())[:5]:
                failed_str = (
                    f" [red]({stats.failed_count} failed)[/red]" if stats.failed_count else ""
                )
                console.print(f"  {domain}: {stats.request_count} requests{failed_str}")
            console.print()

    elif report.artifacts and report.artifacts.har_file:
        console.print("[yellow]HAR file found but could not be parsed[/yellow]\n")

    # CloudWatch Info Section
    if report.cloudwatch:
        cw = report.cloudwatch

        cw_table = Table(show_header=False, box=None, padding=(0, 2))
        cw_table.add_column("Key", style="dim")
        cw_table.add_column("Value")

        cw_table.add_row("Request ID", cw.request_id or "N/A")
        if cw.duration_ms:
            cw_table.add_row("Duration", f"{cw.duration_ms:.0f}ms")
        if cw.billed_duration_ms:
            cw_table.add_row("Billed Duration", f"{cw.billed_duration_ms:.0f}ms")
        if cw.memory_used_mb:
            cw_table.add_row("Memory Used", f"{cw.memory_used_mb:.0f}MB")
        if cw.init_duration_ms:
            cw_table.add_row("Cold Start", f"{cw.init_duration_ms:.0f}ms")
        if cw.error:
            cw_table.add_row("Collector", f"[yellow]{str(cw.error)[:80]}[/yellow]")

        console.print(
            Panel(cw_table, title="[bold]CloudWatch Metrics[/bold]", border_style="magenta")
        )

        # There is no `cw.errors` list: CloudWatch gives one stream, and its
        # errors are the error-level entries in it.
        cw_errors = cw.error_entries
        if cw_errors:
            console.print("[red]Lambda Errors:[/red]")
            for entry in cw_errors[:3]:
                console.print(f"  [red]✗[/red] {entry.message[:150]}")
            console.print()

    # Logs Section
    if report.logs:
        console.print(f"[bold]Related Logs[/bold] ({len(report.logs)} entries)")

        log_table = Table(show_header=True, box=None)
        log_table.add_column("Time", style="dim", width=19)
        log_table.add_column("Source", width=8)
        log_table.add_column("Level", width=7)
        log_table.add_column("Message")

        display_count = 10 if verbose else 5
        for entry in report.logs[:display_count]:
            level_color = {"error": "red", "warning": "yellow", "info": "green"}.get(
                entry.level.lower(), "white"
            )
            log_table.add_row(
                entry.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                entry.component,
                f"[{level_color}]{entry.level}[/{level_color}]",
                entry.message[:80],
            )

        console.print(log_table)

        if len(report.logs) > display_count:
            console.print(f"[dim]... and {len(report.logs) - display_count} more entries[/dim]")
        console.print()

    # Artifacts Section
    if report.artifacts:
        art = report.artifacts
        console.print("[bold]Artifacts[/bold]")

        if art.job_dir:
            console.print(f"  [dim]Job Directory:[/dim] {art.job_dir}")
        if art.har_file:
            console.print(f"  [dim]HAR File:[/dim] {art.har_file}")
        if art.lambda_zip:
            console.print(f"  [dim]Lambda Output:[/dim] {art.lambda_zip}")
        if art.screenshot_files:
            console.print(f"  [dim]Screenshots:[/dim] {len(art.screenshot_files)} files")
            if verbose:
                for ss in art.screenshot_files[:3]:
                    console.print(f"    - {ss}")
        console.print()

    # Summary.
    #
    # Split into failures and degradations. The old version compared the status
    # against "completed" -- a value the API never sends -- so every job,
    # including a perfectly green one, was listed as an issue, which is the
    # fastest way to teach someone to skip this section.
    issues: list[str] = []
    degradations: list[str] = []

    meta = report.metadata
    if meta:
        detail = meta.status_message or meta.error_message
        if status_contract.is_failure(meta.status):
            issues.append(f"Job status: {meta.status}")
            if detail:
                issues.append(f"Runner said: {detail[:120]}")
        elif status_contract.is_warning(meta.status):
            degradations.append(f"Job status: {meta.status}")
            if detail:
                degradations.append(f"Runner said: {detail[:120]}")
        elif meta.error_message:
            issues.append(f"Error: {meta.error_message[:50]}")

    if report.har_summary and report.har_summary.failed_count > 0:
        issues.append(f"{report.har_summary.failed_count} failed HTTP requests")

    ld = report.lambda_decision
    if ld:
        if ld.error:
            issues.append(f"Lambda error: {str(ld.error)[:80]}")
        if ld.random_selection:
            degradations.append(
                "Lambda selected the video at RANDOM — this arm behaved as a control arm"
            )
        elif ld.degraded:
            degradations.append("Lambda decision degraded: not every filter applied")
        if ld.failed_filters:
            degradations.append(f"{len(ld.failed_filters)} Lambda filter(s) did not apply")
        if ld.warnings:
            degradations.append(f"{len(ld.warnings)} Lambda warnings")

    if report.cloudwatch:
        cw_error_count = len(report.cloudwatch.error_entries)
        if cw_error_count:
            issues.append(f"{cw_error_count} CloudWatch errors")

    if issues:
        console.print("[bold red]Issues Found:[/bold red]")
        for issue in issues:
            console.print(f"  [red]•[/red] {issue}")

    if degradations:
        console.print("[bold yellow]Degraded (ran, but not as designed):[/bold yellow]")
        for item in degradations:
            console.print(f"  [yellow]•[/yellow] {item}")

    if not issues and not degradations:
        console.print("[bold green]No obvious issues found[/bold green]")


# =============================================================================
# TEST COMMAND (E2E)
# =============================================================================


@cli.command()
@click.option("-v", "--verbose", is_flag=True, help="Verbose output")
@click.option("--quick", is_flag=True, help="Run quick subset of tests")
@click.option("-k", "--filter", "test_filter", help="Filter tests by name pattern")
@click.pass_context
def test(ctx: click.Context, verbose: bool, quick: bool, test_filter: str) -> None:
    """
    Run end-to-end tests to verify system setup.

    Tests verify all components are running and can communicate:

    \b
    - API stack (Laravel, MySQL, Redis)
    - Monitoring stack (Grafana, Loki, Prometheus)
    - Log aggregation
    - Runner setup (JAR exists)

    \b
    Note: These tests do NOT require Multilogin X.
          Browser automation tests are not included.

    \b
    Examples:
        orchestrator test              # Run all e2e tests
        orchestrator test -v           # Verbose output
        orchestrator test --quick      # Quick subset
        orchestrator test -k "api"     # Filter by name
    """
    import subprocess
    import sys
    from pathlib import Path

    console.print("[bold cyan]Running End-to-End Tests[/bold cyan]\n")

    # Build pytest command
    test_file = Path(__file__).parent / "tests" / "test_e2e_new.py"

    if not test_file.exists():
        console.print(f"[red]Test file not found: {test_file}[/red]")
        raise SystemExit(1)

    # `orchestrator test` verifies the setup without Multilogin; the full browser
    # automation run is `orchestrator e2e`.
    cmd = [sys.executable, "-m", "pytest", str(test_file), "--e2e", "-m", "not multilogin"]

    if verbose:
        cmd.append("-v")
    else:
        cmd.append("-q")

    if quick:
        # Run only critical tests
        cmd.extend(["-k", "health or status or accessible"])

    if test_filter:
        cmd.extend(["-k", test_filter])

    # Add color output
    cmd.append("--color=yes")

    console.print(f"[dim]Running: {' '.join(cmd)}[/dim]\n")

    try:
        result = subprocess.run(cmd, cwd=Path(__file__).parent.parent)
        sys.exit(result.returncode)
    except FileNotFoundError:
        console.print("[red]pytest not found. Install with: pip install pytest[/red]")
        raise SystemExit(1) from None


# =============================================================================
# E2E COMMAND (FULL SYSTEM TEST WITH MULTILOGIN)
# =============================================================================


@cli.command()
@click.option(
    "--timeout",
    default=None,
    type=int,
    help=(
        "Timeout in seconds for job execution, measured from when the runner picks up "
        "the queue (default: 180 for basic/chrome, 600 for youtube)"
    ),
)
@click.option(
    "--pickup-timeout",
    default=None,
    type=int,
    help=(
        "Timeout in seconds for the runner to pick up the queue at all (default: 180). "
        "The runner polls on runner.UpdateInterval, so pickup latency is variable."
    ),
)
@click.option("-v", "--verbose", is_flag=True, help="Show detailed output")
@click.option(
    "--youtube",
    is_flag=True,
    help="Run YouTube-specific e2e test with Lambda selection workflow",
)
@click.option(
    "--youtube-treatment",
    is_flag=True,
    help=(
        "Run the YouTube e2e on the TREATED arm (pi=1, kappa=1) and fail unless the "
        "treatment actually applied. --youtube is a control crawler and cannot."
    ),
)
@click.option(
    "--chrome",
    is_flag=True,
    help="Run e2e test with plain Chrome (no Multilogin X, no proxy)",
)
@click.pass_context
def e2e(
    ctx: click.Context,
    timeout: int | None,
    pickup_timeout: int | None,
    verbose: bool,
    youtube: bool,
    youtube_treatment: bool,
    chrome: bool,
) -> None:
    """
    Run a complete end-to-end test with browser automation.

    This test verifies the entire PrimeMover system including:

    \b
    - API and database connectivity
    - Experiment upload and scheduling
    - Runner executing jobs in a browser
    - Job completion and reporting

    \b
    Test types:
    - Basic (default): Simple visit/wait jobs via Multilogin X (~3 min)
    - Chrome (--chrome): Same workflow using plain Chrome, no MLX or proxy (~3 min)
    - YouTube (--youtube): Full YouTube workflow with Lambda video selection (~10 min)
    - YouTube treated (--youtube-treatment): the same workflow on a TREATED bot
      (pi=1, kappa=1), which additionally fails unless the treatment applied (~10 min)

    \b
    Why the treated variant exists:
    --youtube runs a control crawler (pi=psi=kappa=tau=0). It calls OpenAI, but
    only for the always-on children_entertainment rejection filter, so
    selection_mode comes back 'random' and no filter that WEIGHTS the draw is
    ever exercised. A corrupted mainstream_media_channels.csv, a wrong lambda
    cwd, or the known IndexError in the per-card xpath all degrade a treatment
    silently -- HTTP 200, plausible recommendations, no failed job -- and a
    control crawler never reaches any of those branches. --youtube-treatment
    asks for two filters a control never requests and fails the run if they did
    not apply.

    \b
    Prerequisites:
    - All components running (orchestrator up)
    - For basic/youtube: Multilogin X installed and logged in
    - For chrome: Only Chrome/Chromium and ChromeDriver needed
    - For YouTube test: Lambda function (bot-decision-yt) must be deployed

    \b
    Timeouts:
    The wait is split into two separately budgeted phases, because the runner
    polls for new work on its own schedule (runner.UpdateInterval) and that
    latency has nothing to do with how long the jobs take:
    - --pickup-timeout: waiting for the runner to claim the queue (no jobs yet)
    - --timeout: executing the jobs, counted from the moment it claimed it
    Worst-case wall time is therefore pickup + execution, and a failure names
    which of the two phases ran out.

    \b
    Examples:
        orchestrator e2e                  # Run basic e2e test (requires MLX)
        orchestrator e2e --chrome         # Run with plain Chrome (no MLX needed)
        orchestrator e2e --chrome -v      # Chrome test with verbose output
        orchestrator e2e --youtube        # Run YouTube workflow test (control arm)
        orchestrator e2e --youtube-treatment   # Treated arm; fails if untreated
        orchestrator e2e --timeout 300 -v # Basic test with a bigger execution budget
        orchestrator e2e --pickup-timeout 900  # Runner polls on a long UpdateInterval
    """
    import sys

    from orchestrator.e2e import E2ETestRunner

    if chrome:
        test_type = "chrome"
    elif youtube_treatment:
        test_type = "youtube_treatment"
    elif youtube:
        test_type = "youtube"
    else:
        test_type = "basic"

    runner = E2ETestRunner(
        timeout=timeout,
        verbose=verbose,
        test_type=test_type,
        pickup_timeout=pickup_timeout,
    )

    result = runner.run()
    sys.exit(0 if result.success else 1)


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
