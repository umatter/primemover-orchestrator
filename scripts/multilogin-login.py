#!/usr/bin/env python3
"""
Multilogin X API Authentication Script

This script authenticates with Multilogin X via the API and ensures the agent
is running and connected. It can be used for automated startup after reboot.

Usage:
    python multilogin-login.py                    # Interactive (prompts for password)
    python multilogin-login.py --env              # Use credentials from environment
    python multilogin-login.py --check-only       # Only check if already authenticated

Environment variables:
    MLX_EMAIL    - Multilogin account email
    MLX_PASSWORD - Multilogin account password

References:
    - https://multilogin.com/help/en_US/custom-api-scripts-with-python/log-in-automatically
    - https://multilogin.com/help/en_US/api/retrieving-the-token
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

try:
    import requests
    import urllib3
except ImportError:
    print("Error: 'requests' library not installed.")
    print("Install with: pip install requests")
    sys.exit(1)

# The launcher serves HTTPS with a self-signed cert on localhost, so every probe
# is deliberately unverified; the warning would otherwise drown the script's own
# output on each run.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# Multilogin API endpoints
MLX_BASE = "https://api.multilogin.com"
MLX_LAUNCHER = "https://launcher.mlx.yt:45001/api/v2"
MLX_LAUNCHER_LOCAL = "https://127.0.0.1:45001/api/v2"

# Paths
# Multilogin X v12 embeds the agent in the desktop app -- there is no
# standalone agent.bin, and /opt/mlx does not exist. The desktop app is
# managed by the multilogin-desktop systemd unit.
MLX_DESKTOP_BIN = "/opt/mlxapp/desktop.bin"
MLX_DESKTOP_UNIT = "multilogin-desktop"
TOKEN_CACHE_FILE = Path.home() / ".mlx_token_cache"

# Timeouts
AGENT_START_TIMEOUT = 60
API_TIMEOUT = 30


def log(msg: str, level: str = "INFO"):
    """Print log message with timestamp."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {msg}")


def get_md5_hash(password: str) -> str:
    """Hash password with MD5 as required by Multilogin API."""
    return hashlib.md5(password.encode()).hexdigest()


def sign_in(email: str, password: str) -> dict:
    """
    Sign in to Multilogin API and retrieve authentication token.

    Returns:
        dict with 'token' and 'refresh_token' on success
        dict with 'error' on failure
    """
    url = f"{MLX_BASE}/user/signin"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {
        "email": email,
        "password": get_md5_hash(password),
    }

    try:
        log(f"Signing in as {email}...")
        response = requests.post(url, json=payload, headers=headers, timeout=API_TIMEOUT)

        if response.status_code == 200:
            data = response.json()
            if "data" in data and "token" in data["data"]:
                log("Successfully authenticated with Multilogin API")
                return {
                    "token": data["data"]["token"],
                    "refresh_token": data["data"].get("refresh_token", ""),
                }
            else:
                return {"error": f"Unexpected response format: {data}"}
        else:
            return {"error": f"HTTP {response.status_code}: {response.text}"}

    except requests.exceptions.RequestException as e:
        return {"error": f"Request failed: {str(e)}"}


def refresh_token(refresh_token: str) -> dict:
    """Refresh an expired token."""
    url = f"{MLX_BASE}/user/refresh_token"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {"refresh_token": refresh_token}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=API_TIMEOUT)
        if response.status_code == 200:
            data = response.json()
            return {
                "token": data["data"]["token"],
                "refresh_token": data["data"].get("refresh_token", refresh_token),
            }
        else:
            return {"error": f"Token refresh failed: {response.text}"}
    except requests.exceptions.RequestException as e:
        return {"error": f"Token refresh request failed: {str(e)}"}


def check_agent_running() -> bool:
    """Whether the Multilogin launcher API is up.

    Liveness only: ANY HTTP response means something is listening and serving,
    which is what "the agent is running" actually means here. Only a failure to
    connect counts as down.

    This used to compare against [200, 404] and `return` that comparison from the
    FIRST probe -- so a local reply of 400 returned False immediately and the
    working external probe was never reached. It fell through on an exception but
    not on an unexpected status, which is exactly what happened: the local URL was
    http:// against an HTTPS port, so it answered 400 and the check reported the
    agent down while it was serving normally.
    """
    for url in (MLX_LAUNCHER_LOCAL, MLX_LAUNCHER):
        try:
            response = requests.get(f"{url}/", timeout=5, verify=False)
        except requests.RequestException:
            continue  # try the next endpoint, do not conclude anything yet
        log(f"Launcher API responded on {url} (HTTP {response.status_code})", "DEBUG")
        return True
    return False


def start_agent() -> bool:
    """Ensure the Multilogin launcher is up, starting the desktop app if needed.

    On Multilogin X v12 there is no standalone agent to launch: the agent is
    embedded in the desktop app, which is managed by the multilogin-desktop
    systemd unit. This used to exec /opt/mlx/agent.bin -- a path that does not
    exist on a v12 install -- so it could only ever fail.

    Restarting the unit needs root, so this reports what to run rather than
    pretending it can do it.
    """
    if check_agent_running():
        log("Multilogin launcher is already running")
        return True

    log("Multilogin launcher is not responding", "ERROR")

    if not os.path.exists(MLX_DESKTOP_BIN):
        log(f"Desktop app not found at {MLX_DESKTOP_BIN}", "ERROR")
        return False

    log(f"Start it with:  sudo systemctl start {MLX_DESKTOP_UNIT}", "ERROR")
    log("It needs the VNC display (DISPLAY=:1); the unit sets that up.", "ERROR")
    return False


def cache_token(token_data: dict):
    """Cache token to file for reuse."""
    try:
        TOKEN_CACHE_FILE.write_text(json.dumps(token_data))
        TOKEN_CACHE_FILE.chmod(0o600)  # Secure permissions
    except Exception as e:
        log(f"Warning: Could not cache token: {e}", "WARN")


def load_cached_token() -> dict | None:
    """Load cached token if available and not expired."""
    try:
        if TOKEN_CACHE_FILE.exists():
            data: dict[Any, Any] = json.loads(TOKEN_CACHE_FILE.read_text())
            # Token expires in 30 minutes, but we'll try to use it anyway
            # and refresh if needed
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def verify_token(token: str) -> bool:
    """Verify that a token is still valid."""
    url = f"{MLX_BASE}/user/me"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    try:
        response = requests.get(url, headers=headers, timeout=API_TIMEOUT)
        return response.status_code == 200
    except requests.RequestException:
        return False


def get_credentials_from_env() -> tuple[str, str]:
    """Get credentials from environment variables."""
    email = os.environ.get("MLX_EMAIL", "")
    password = os.environ.get("MLX_PASSWORD", "")

    # Also check the runner config as fallback. The user lives in the tracked
    # application.properties; the password in the gitignored secrets.properties,
    # which application.properties imports and which overrides it -- so read
    # secrets.properties last and let it win.
    env_email, env_password = email, password
    if not email or not password:
        config_dir = Path("/home/primemover/primemover_runner/primemover-runner/config")
        for name in ("application.properties", "secrets.properties"):
            config_file = config_dir / name
            if not config_file.exists():
                continue
            for line in config_file.read_text().splitlines():
                if line.startswith("multilogin.apiUser=") and not env_email:
                    email = line.split("=", 1)[1].strip() or email
                elif line.startswith("multilogin.apiPassword=") and not env_password:
                    password = line.split("=", 1)[1].strip() or password

    return email, password


def main():
    parser = argparse.ArgumentParser(description="Multilogin X API Authentication")
    parser.add_argument(
        "--env", action="store_true", help="Use credentials from environment/config"
    )
    parser.add_argument("--check-only", action="store_true", help="Only check if agent is running")
    parser.add_argument("--start-agent", action="store_true", help="Start agent if not running")
    parser.add_argument("--email", help="Multilogin account email")
    parser.add_argument("--password", help="Multilogin account password")
    args = parser.parse_args()

    # Check-only mode
    if args.check_only:
        if check_agent_running():
            log("Multilogin agent is running")
            sys.exit(0)
        else:
            log("Multilogin agent is NOT running", "ERROR")
            sys.exit(1)

    # Start agent mode
    if args.start_agent:
        if start_agent():
            sys.exit(0)
        else:
            sys.exit(1)

    # Get credentials
    if args.email and args.password:
        email, password = args.email, args.password
    elif args.env:
        email, password = get_credentials_from_env()
        if not email or not password:
            log("Credentials not found in environment or config", "ERROR")
            sys.exit(1)
    else:
        # Interactive mode
        import getpass

        email = input("Multilogin email: ")
        password = getpass.getpass("Multilogin password: ")

    # Try cached token first
    cached = load_cached_token()
    if cached and "token" in cached:
        log("Found cached token, verifying...")
        if verify_token(cached["token"]):
            log("Cached token is valid")
            # Ensure agent is running
            if start_agent():
                print(f"\nToken: {cached['token'][:50]}...")
                sys.exit(0)
        elif "refresh_token" in cached:
            log("Token expired, attempting refresh...")
            result = refresh_token(cached["refresh_token"])
            if "token" in result:
                cache_token(result)
                if start_agent():
                    print(f"\nToken: {result['token'][:50]}...")
                    sys.exit(0)

    # Sign in with credentials
    result = sign_in(email, password)

    if "error" in result:
        log(f"Authentication failed: {result['error']}", "ERROR")
        sys.exit(1)

    # Cache the token
    cache_token(result)

    # Ensure agent is running
    if not start_agent():
        log("Warning: Agent may not be running", "WARN")

    print("\nAuthentication successful!")
    print(f"Token: {result['token'][:50]}...")
    print(f"\nToken cached to: {TOKEN_CACHE_FILE}")
    print("Token expires in 30 minutes. Use refresh_token to extend.")


if __name__ == "__main__":
    main()
