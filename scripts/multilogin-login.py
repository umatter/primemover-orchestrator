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
import subprocess
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: 'requests' library not installed.")
    print("Install with: pip install requests")
    sys.exit(1)


# Multilogin API endpoints
MLX_BASE = "https://api.multilogin.com"
MLX_LAUNCHER = "https://launcher.mlx.yt:45001/api/v2"
MLX_LAUNCHER_LOCAL = "http://127.0.0.1:45001/api/v2"

# Paths
MLX_AGENT_BIN = "/opt/mlx/agent.bin"
MLX_DESKTOP_BIN = "/opt/mlxapp/desktop.bin"
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
    """Check if Multilogin agent is running and responding."""
    try:
        # Try local launcher first
        response = requests.get(f"{MLX_LAUNCHER_LOCAL}/", timeout=5, verify=False)
        return response.status_code in [200, 404]  # 404 is OK, means agent is running
    except:
        pass

    try:
        # Try external launcher
        response = requests.get(f"{MLX_LAUNCHER}/", timeout=5)
        return response.status_code in [200, 404]
    except:
        return False


def start_agent() -> bool:
    """Start the Multilogin agent if not running."""
    if check_agent_running():
        log("Multilogin agent is already running")
        return True

    log("Starting Multilogin agent...")

    # Check if agent binary exists
    if not os.path.exists(MLX_AGENT_BIN):
        log(f"Agent binary not found at {MLX_AGENT_BIN}", "ERROR")
        return False

    # Start agent in background
    # Note: Agent requires a display, so DISPLAY must be set
    env = os.environ.copy()
    if "DISPLAY" not in env:
        env["DISPLAY"] = ":1"

    try:
        subprocess.Popen(
            [MLX_AGENT_BIN],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        log(f"Failed to start agent: {e}", "ERROR")
        return False

    # Wait for agent to start
    log("Waiting for agent to initialize...")
    for i in range(AGENT_START_TIMEOUT):
        time.sleep(1)
        if check_agent_running():
            log(f"Agent started successfully (took {i+1}s)")
            return True

    log("Agent failed to start within timeout", "ERROR")
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
            data = json.loads(TOKEN_CACHE_FILE.read_text())
            # Token expires in 30 minutes, but we'll try to use it anyway
            # and refresh if needed
            return data
    except:
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
    except:
        return False


def get_credentials_from_env() -> tuple[str, str]:
    """Get credentials from environment variables."""
    email = os.environ.get("MLX_EMAIL", "")
    password = os.environ.get("MLX_PASSWORD", "")

    # Also check the runner config as fallback
    if not email or not password:
        config_file = Path("/home/primemover/primemover_runner/primemover-runner/config/application.properties")
        if config_file.exists():
            content = config_file.read_text()
            for line in content.splitlines():
                if line.startswith("multilogin.apiUser=") and not email:
                    email = line.split("=", 1)[1].strip()
                elif line.startswith("multilogin.apiPassword=") and not password:
                    password = line.split("=", 1)[1].strip()

    return email, password


def main():
    parser = argparse.ArgumentParser(description="Multilogin X API Authentication")
    parser.add_argument("--env", action="store_true", help="Use credentials from environment/config")
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

    print(f"\nAuthentication successful!")
    print(f"Token: {result['token'][:50]}...")
    print(f"\nToken cached to: {TOKEN_CACHE_FILE}")
    print("Token expires in 30 minutes. Use refresh_token to extend.")


if __name__ == "__main__":
    main()
