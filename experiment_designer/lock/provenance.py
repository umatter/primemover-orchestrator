"""Facts about where a lock came from. Cheap, best-effort, never raises."""

from __future__ import annotations

import subprocess
from datetime import datetime
from importlib import metadata
from pathlib import Path

from experiment_designer.spec.time_utils import API_TIMEZONE, format_iso8601


def git_commit(path: Path) -> str | None:
    try:
        head = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return f"{head}-dirty" if status.strip() else head


def pmspec_version() -> str:
    try:
        return metadata.version("primemover-orchestrator")
    except metadata.PackageNotFoundError:
        return "unknown"


def now_iso() -> str:
    return format_iso8601(datetime.now(API_TIMEZONE).replace(microsecond=0))
