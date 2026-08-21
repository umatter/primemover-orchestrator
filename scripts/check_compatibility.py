#!/usr/bin/env python3
"""
Check for potentially breaking changes in integrated repositories.

This script checks the primemover_api and primemover_runner repositories
for recent changes to files that could break the orchestrator's integration.

Usage:
    python scripts/check_compatibility.py [--days N] [--token TOKEN]

Environment:
    GITHUB_TOKEN: GitHub API token for authentication (optional, increases rate limit)
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError  # noqa: F811
from urllib.request import Request, urlopen

# =============================================================================
# Critical files that could break orchestrator integration
# =============================================================================

CRITICAL_FILES = {
    "primemover_api": {
        "high": [
            "docker-compose.yml",
            "docker-compose.yaml",
            "config/logging.php",
            "routes/api.php",
            "app/Http/Controllers/Auth",
        ],
        "medium": [
            ".env.example",
            "database/migrations",
            "app/Http/Controllers/Api",
        ],
        "description": "Laravel API - log format, Docker, API endpoints",
    },
    "primemover_runner": {
        "high": [
            "primemover-runner/src/main/resources/logback",
            "primemover-runner/pom.xml",
            "pom.xml",
        ],
        "medium": [
            "primemover-runner/src/main/resources/application",
            "primemover-runner/config",
        ],
        "description": "Java Runner - log format, JAR naming, config",
    },
    "bot-decision-yt": {
        "high": [
            "serverless.yml",
            "handler.py",
        ],
        "medium": [
            "src/",
        ],
        "description": "Lambda - function config, output format",
    },
}


@dataclass
class Change:
    """A potentially breaking change."""

    repo: str
    file: str
    severity: str
    commit_sha: str
    commit_message: str
    commit_date: str
    author: str


def github_request(url: str, token: str | None = None) -> dict[str, Any] | list[dict[str, Any]]:
    """Make a GitHub API request.

    Returns whatever the endpoint sends: the commits list endpoint returns a
    JSON array, commits/{sha} returns an object, and the error paths below
    return an object. Callers must narrow before indexing -- this was
    annotated ``-> dict`` while returning a list, which mypy caught.
    """
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "primemover-orchestrator-compat-check",
    }
    if token:
        headers["Authorization"] = f"token {token}"

    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            payload: dict[str, Any] | list[dict[str, Any]] = json.loads(response.read().decode())
            return payload
    except HTTPError as e:
        if e.code == 404:
            return {"error": "not_found"}
        elif e.code == 403:
            return {"error": "rate_limited"}
        raise


def get_recent_commits(
    owner: str,
    repo: str,
    since: datetime,
    token: str | None = None,
) -> list[dict]:
    """Get commits since a given date."""
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"https://api.github.com/repos/{owner}/{repo}/commits?since={since_str}&per_page=100"

    result = github_request(url, token)
    # A dict here is either the error envelope or an unexpected object response;
    # neither is a commit list.
    if isinstance(result, dict):
        return []
    return result


def get_commit_files(
    owner: str,
    repo: str,
    sha: str,
    token: str | None = None,
) -> list[str]:
    """Get list of files changed in a commit."""
    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}"

    result = github_request(url, token)
    # This endpoint returns an object; a list would be unexpected.
    if not isinstance(result, dict) or "error" in result:
        return []

    files = result.get("files", [])
    return [f["filename"] for f in files]


def check_file_match(changed_file: str, critical_patterns: Sequence[str]) -> bool:
    """Check if a changed file matches any critical pattern."""
    for pattern in critical_patterns:
        if changed_file.startswith(pattern) or pattern in changed_file:
            return True
    return False


def check_repository(
    owner: str,
    repo: str,
    since: datetime,
    token: str | None = None,
) -> list[Change]:
    """Check a repository for potentially breaking changes."""
    changes: list[Change] = []

    if repo not in CRITICAL_FILES:
        return changes

    critical = CRITICAL_FILES[repo]
    high_patterns = critical.get("high", [])
    medium_patterns = critical.get("medium", [])

    print(f"  Checking {owner}/{repo}...", end=" ", flush=True)

    commits = get_recent_commits(owner, repo, since, token)
    if not commits:
        print("no commits or not accessible")
        return changes

    print(f"found {len(commits)} commits")

    for commit in commits:
        sha = commit["sha"]
        message = commit["commit"]["message"].split("\n")[0][:80]
        date = commit["commit"]["author"]["date"]
        author = commit["commit"]["author"]["name"]

        files = get_commit_files(owner, repo, sha, token)

        for f in files:
            if check_file_match(f, high_patterns):
                changes.append(
                    Change(
                        repo=repo,
                        file=f,
                        severity="HIGH",
                        commit_sha=sha[:8],
                        commit_message=message,
                        commit_date=date,
                        author=author,
                    )
                )
            elif check_file_match(f, medium_patterns):
                changes.append(
                    Change(
                        repo=repo,
                        file=f,
                        severity="MEDIUM",
                        commit_sha=sha[:8],
                        commit_message=message,
                        commit_date=date,
                        author=author,
                    )
                )

    return changes


def main():
    parser = argparse.ArgumentParser(
        description="Check for potentially breaking changes in integrated repositories"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days to look back (default: 7)",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub API token (or set GITHUB_TOKEN env var)",
    )
    parser.add_argument(
        "--owner",
        type=str,
        default="umatter",
        help="GitHub organization/owner (default: umatter)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    print(f"Checking for changes since {since.strftime('%Y-%m-%d')} ({args.days} days)")
    print()

    all_changes = []

    for repo in CRITICAL_FILES.keys():
        changes = check_repository(args.owner, repo, since, args.token)
        all_changes.extend(changes)

    print()

    if args.json:
        output = {
            "since": since.isoformat(),
            "changes": [
                {
                    "repo": c.repo,
                    "file": c.file,
                    "severity": c.severity,
                    "commit": c.commit_sha,
                    "message": c.commit_message,
                    "date": c.commit_date,
                    "author": c.author,
                }
                for c in all_changes
            ],
        }
        print(json.dumps(output, indent=2))
        return 1 if any(c.severity == "HIGH" for c in all_changes) else 0

    if not all_changes:
        print("✅ No potentially breaking changes detected")
        return 0

    # Group by severity
    high = [c for c in all_changes if c.severity == "HIGH"]
    medium = [c for c in all_changes if c.severity == "MEDIUM"]

    if high:
        print("🔴 HIGH SEVERITY CHANGES (likely to break orchestrator):")
        print("-" * 70)
        for c in high:
            print(f"  [{c.repo}] {c.file}")
            print(f"    Commit: {c.commit_sha} by {c.author}")
            print(f"    Message: {c.commit_message}")
            print()

    if medium:
        print("🟡 MEDIUM SEVERITY CHANGES (may affect orchestrator):")
        print("-" * 70)
        for c in medium:
            print(f"  [{c.repo}] {c.file}")
            print(f"    Commit: {c.commit_sha} by {c.author}")
            print(f"    Message: {c.commit_message}")
            print()

    print("-" * 70)
    print(f"Total: {len(high)} high, {len(medium)} medium severity changes")
    print()
    print("📚 Review the integration contract: docs/integration-contract.md")
    print("🧪 Run orchestrator tests: make test")

    return 1 if high else 0


if __name__ == "__main__":
    sys.exit(main())
