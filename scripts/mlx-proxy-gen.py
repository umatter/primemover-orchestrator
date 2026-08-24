#!/usr/bin/env python3
"""Generate Multilogin sticky-session proxy credentials, one per bot.

Multilogin's proxy service (part of the workspace plan; verified live on
2026-08-24) issues ordinary ``host:port:username:password`` HTTP proxy
credentials via ``POST profile-proxy.multilogin.com/v1/proxy/connection_url``.
Each generated session carries a sticky session id with a TTL, so every bot can
run on its own stable residential exit IP -- which is what a personalization
experiment needs, and what a single static proxy cannot provide.

Credentials for the Multilogin account are read from the runner's config
(the same files the runner itself authenticates with), so this script adds no
new secret storage.

Usage:
    scripts/mlx-proxy-gen.py --count 15 --country us            # JSON to stdout
    scripts/mlx-proxy-gen.py --count 4 --ttl 86400 --out proxies.json

Output: a JSON array of {hostname, port, username, password, exit_ip?}.
Pass --verify to resolve each session's exit IP through the proxy and fail if
any two sessions share an exit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import httpx

RUNNER_CONFIG = (
    Path(__file__).resolve().parent.parent.parent
    / "primemover_runner"
    / "primemover-runner"
    / "config"
)
SIGNIN_URL = "https://api.multilogin.com/user/signin"
GENERATE_URL = "https://profile-proxy.multilogin.com/v1/proxy/connection_url"


def _read_credentials() -> tuple[str, str]:
    email = password = None
    for line in (RUNNER_CONFIG / "application.properties").read_text().splitlines():
        if line.startswith("multilogin.apiUser="):
            email = line.split("=", 1)[1].strip()
    for line in (RUNNER_CONFIG / "secrets.properties").read_text().splitlines():
        if line.startswith("multilogin.apiPassword="):
            password = line.split("=", 1)[1].strip()
    if not email or not password:
        raise SystemExit("multilogin.apiUser / multilogin.apiPassword not found in runner config")
    return email, password


def _token(email: str, password: str) -> str:
    # The Multilogin API takes the MD5 of the password, not the password itself.
    digest = hashlib.md5(password.encode()).hexdigest()  # noqa: S324 -- their API contract
    r = httpx.post(SIGNIN_URL, json={"email": email, "password": digest}, timeout=30)
    r.raise_for_status()
    token: str = r.json()["data"]["token"]
    return token


def generate(token: str, country: str, ttl: int) -> dict[str, str | int]:
    r = httpx.post(
        GENERATE_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"country": country, "protocol": "http", "sessionType": "sticky", "IPTTL": ttl},
        timeout=30,
    )
    r.raise_for_status()
    host, port, user, password = r.json()["data"].rsplit(":", 3)
    return {"hostname": host, "port": int(port), "username": user, "password": password}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--count", type=int, required=True, help="number of sticky sessions to generate"
    )
    ap.add_argument("--country", default="us", help="ISO country code for the exit (default: us)")
    ap.add_argument("--ttl", type=int, default=86400, help="session TTL in seconds (default: 24h)")
    ap.add_argument(
        "--verify", action="store_true", help="resolve each exit IP; fail on duplicates"
    )
    ap.add_argument("--out", help="write JSON here instead of stdout")
    args = ap.parse_args()

    token = _token(*_read_credentials())
    sessions = [generate(token, args.country, args.ttl) for _ in range(args.count)]

    if args.verify:
        exits: list[str] = []
        for s in sessions:
            proxy = f"http://{s['username']}:{s['password']}@{s['hostname']}:{s['port']}"
            ip = httpx.get("https://api.ipify.org", proxy=proxy, timeout=30).text.strip()
            s["exit_ip"] = ip
            exits.append(ip)
        dupes = {ip for ip in exits if exits.count(ip) > 1}
        if dupes:
            print(f"ERROR: duplicate exit IPs across sessions: {sorted(dupes)}", file=sys.stderr)
            return 1
        print(f"verified: {len(set(exits))}/{len(sessions)} distinct exit IPs", file=sys.stderr)

    payload = json.dumps(sessions, indent=2)
    if args.out:
        Path(args.out).write_text(payload)
        print(f"wrote {len(sessions)} sessions to {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
