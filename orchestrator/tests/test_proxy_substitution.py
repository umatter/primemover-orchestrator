"""Tests for e2e scenario proxy substitution.

The scenario JSONs ship PROXY_*_PLACEHOLDER instead of credentials (commit
9477749, "Keep proxy credentials out of experiment specs"). Nothing filled them
back in, so the Multilogin-backed e2e paths silently broke: MLX validates a
profile's proxy on start and refuses to launch the browser when that check
fails. These tests pin the substitution and the fail-fast behaviour.
"""

from typing import cast

import pytest

from orchestrator.config.settings import Settings
from orchestrator.e2e.runner import E2ETestRunner, E2ETestTypeConfig


def _crawler() -> dict:
    return {
        "proxy": {
            "hostname": "PROXY_HOST_PLACEHOLDER",
            "port": 5432,
            "username": "PROXY_USER_PLACEHOLDER",
            "password": "PROXY_PASSWORD_PLACEHOLDER",
            "type": "http",
        }
    }


def _runner(*, chrome: bool, configured: bool) -> E2ETestRunner:
    """Build a runner with only what _substitute_proxy touches.

    Uses a real Settings so the `e2e_proxy_configured` property under test is the
    production one rather than a stand-in.
    """
    runner = E2ETestRunner.__new__(E2ETestRunner)
    runner.test_config = cast(
        E2ETestTypeConfig, {"identification_override": "chrome"} if chrome else {}
    )
    creds = (
        {
            "E2E_PROXY_HOST": "proxy.example.net",
            "E2E_PROXY_PORT": "7000",
            "E2E_PROXY_USERNAME": "user",
            "E2E_PROXY_PASSWORD": "pass",
        }
        if configured
        else {}
    )
    # _env_file=None is essential: Settings reads .env by default, so on a machine
    # where the e2e proxy IS configured the "unconfigured" case would silently pick
    # up real credentials and these tests would fail -- on exactly the machines that
    # can run e2e. Isolate from the environment so the fixture means what it says.
    # call-arg: _env_file is a pydantic-settings runtime kwarg, not a declared field.
    # arg-type: the **creds keys are env aliases rather than field names.
    runner.settings = Settings(_env_file=None, **creds)  # type: ignore[call-arg, arg-type]
    return runner


def test_placeholders_are_replaced_when_configured():
    runner = _runner(chrome=False, configured=True)
    crawler = _crawler()
    runner._substitute_proxy(crawler)

    proxy = crawler["proxy"]
    assert proxy["hostname"] == "proxy.example.net"
    assert proxy["username"] == "user"
    assert proxy["password"] == "pass"
    assert proxy["port"] == 7000
    assert not any(str(v).endswith("_PLACEHOLDER") for v in proxy.values())


def test_multilogin_path_fails_fast_when_unconfigured():
    """Better a named error at upload than GET_PROXY_CONNECTION_IP_ERROR later."""
    runner = _runner(chrome=False, configured=False)
    with pytest.raises(RuntimeError, match="E2E_PROXY_HOST"):
        runner._substitute_proxy(_crawler())


def test_chrome_path_is_exempt():
    """--chrome drives plain Chrome, so there is no proxy check to fail."""
    runner = _runner(chrome=True, configured=False)
    crawler = _crawler()
    runner._substitute_proxy(crawler)  # must not raise
    assert crawler["proxy"]["hostname"] == "PROXY_HOST_PLACEHOLDER"


def test_real_proxy_is_left_alone():
    """A scenario with baked-in credentials is not overwritten."""
    runner = _runner(chrome=False, configured=True)
    crawler = {"proxy": {"hostname": "real.proxy", "username": "u", "password": "p", "port": 1}}
    runner._substitute_proxy(crawler)
    assert crawler["proxy"]["hostname"] == "real.proxy"
