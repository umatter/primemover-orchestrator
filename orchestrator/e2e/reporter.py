"""
E2E Test Reporter.

Pretty output formatting for e2e test results.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

from orchestrator.e2e.job_status import job_duration_seconds, job_status, job_succeeded

if TYPE_CHECKING:
    from .runner import E2EResult, ValidationResult


class E2EReporter:
    """Formats and displays e2e test output."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.console = Console()

    def print_header(self) -> None:
        """Print test header."""
        self.console.print()
        self.console.print("[bold cyan]PrimeMover End-to-End Test[/bold cyan]")
        self.console.print("[dim]" + "─" * 40 + "[/dim]")
        self.console.print()

    def print_step(self, step: int, total: int, message: str) -> None:
        """Print step header."""
        self.console.print(f"[bold]\\[{step}/{total}] {message}[/bold]")

    def print_validation(self, result: ValidationResult) -> None:
        """Print validation results."""
        for check, status in result.checks.items():
            if status["ok"]:
                self.console.print(f"  [green]✓[/green] {check}: {status.get('message', 'OK')}")
            else:
                self.console.print(f"  [red]✗[/red] {check}: {status.get('message', 'Failed')}")
        self.console.print()

    def print_upload(
        self,
        experiment_id: int,
        experiment_name: str,
        crawler_count: int,
        job_count: int,
        start_time: str,
    ) -> None:
        """Print upload results."""
        self.console.print(
            f'  [green]✓[/green] Created experiment #{experiment_id} "{experiment_name}"'
        )
        self.console.print(
            f"  [green]✓[/green] Created {crawler_count} crawler(s) with {job_count} jobs"
        )
        self.console.print(f"  [green]✓[/green] Queue scheduled for {start_time}")
        self.console.print()

    def print_waiting(self, message: str) -> None:
        """Print waiting message."""
        self.console.print(f"  [yellow]⏳[/yellow] {message}")

    def print_job_started(self, job_type: str) -> None:
        """Print job started message."""
        self.console.print(f"  [blue]▶[/blue] Running: {job_type}")

    def print_job_completed(self, job_type: str, duration: float) -> None:
        """Print job completed message."""
        self.console.print(f"  [green]✓[/green] {job_type} - {duration:.1f}s")

    def print_job_failed(self, job_type: str, error: str) -> None:
        """Print job failed message."""
        self.console.print(f"  [red]✗[/red] {job_type} - {error}")

    def print_progress(self, completed: int, total: int, current_status: str) -> None:
        """Print progress indicator."""
        pct = (completed / total * 100) if total > 0 else 0
        self.console.print(
            f"  [dim]Progress: {completed}/{total} ({pct:.0f}%) - {current_status}[/dim]", end="\r"
        )

    def clear_line(self) -> None:
        """Clear current line."""
        self.console.print(" " * 80, end="\r")

    def print_results(self, result: E2EResult) -> None:
        """Print final results."""
        self.console.print()

        if result.success:
            self.console.print(
                f"  [bold green]✓ E2E test passed in {result.duration:.1f}s[/bold green]"
            )
        else:
            self.console.print(f"  [bold red]✗ E2E test failed: {result.error}[/bold red]")

        self.console.print()

        # Job summary table
        if result.jobs:
            table = Table(show_header=True, header_style="bold")
            table.add_column("Job", style="dim")
            table.add_column("Type")
            table.add_column("Status")
            table.add_column("Duration", justify="right")

            for job in result.jobs:
                status = job_status(job)
                status_style = "green" if job_succeeded(job) else "red"
                seconds = job_duration_seconds(job)
                duration = f"{seconds:.1f}s" if seconds is not None else "-"
                table.add_row(
                    str(job.get("id", "")),
                    job.get("type", "unknown"),
                    f"[{status_style}]{status}[/{status_style}]",
                    duration,
                )

            self.console.print(table)
            self.console.print()

        # Links
        if result.experiment_id:
            settings_url = "http://localhost:8090"
            self.console.print(
                f"  [dim]Experiment:[/dim] {settings_url}/experiments/{result.experiment_id}"
            )
            self.console.print(
                f"  [dim]Trace:[/dim] orchestrator trace experiment {result.experiment_id}"
            )

        self.console.print()

    def print_error(self, message: str) -> None:
        """Print error message."""
        self.console.print(f"  [red]Error:[/red] {message}")

    def print_troubleshooting(self) -> None:
        """Print troubleshooting suggestions."""
        self.console.print()
        self.console.print("[bold]Troubleshooting:[/bold]")
        self.console.print("  • Check component status: [cyan]orchestrator status -v[/cyan]")
        self.console.print("  • View runner logs: [cyan]orchestrator logs -c runner -t 5m[/cyan]")
        self.console.print("  • Run diagnostics: [cyan]orchestrator doctor[/cyan]")
        self.console.print("  • Check Multilogin X is running: [cyan]ps aux | grep mlx[/cyan]")
        self.console.print()
