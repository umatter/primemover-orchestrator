"""Filling a scenario's proxy placeholders from untracked configuration.

Lives here rather than on the e2e runner because two things upload these
scenarios now: the e2e itself, and the standing canary in
``scripts/treatment-canary.py``. An unsubstituted placeholder fails the same way
for both -- Multilogin validates a profile's proxy on start by fetching
proxy.multilogin.com through it and refuses to launch the browser if that fails,
so the crawler never gets past setup and reports
``GET_PROXY_CONNECTION_IP_ERROR`` from the launcher rather than anything about
credentials.
"""

from __future__ import annotations

from typing import Any

from orchestrator.config.settings import Settings

PLACEHOLDER_SUFFIX = "_PLACEHOLDER"


def substitute_proxy(
    crawler: dict[str, Any],
    settings: Settings,
    *,
    exempt: bool = False,
) -> None:
    """Replace the scenario's proxy placeholders in place.

    Does nothing when the crawler has no proxy, or when a real one is already
    baked in -- a scenario someone has filled in by hand is left alone.

    ``exempt`` is for callers that drive plain Chrome without Multilogin, where
    there is no proxy validation to fail; everyone else gets a RuntimeError
    naming what to set, because the alternative is a crawler that starts, hangs
    and reports an error about IP lookup.
    """
    proxy = crawler.get("proxy")
    if not proxy:
        return

    placeholders = {str(v) for v in proxy.values() if isinstance(v, str)}
    if not any(p.endswith(PLACEHOLDER_SUFFIX) for p in placeholders):
        return  # a real proxy is already baked in; leave it alone

    if not settings.e2e_proxy_configured:
        if exempt:
            return  # no Multilogin, so no proxy validation to fail
        raise RuntimeError(
            "This scenario needs a residential proxy, but its credentials are "
            "placeholders and none are configured.\n"
            "Set E2E_PROXY_HOST, E2E_PROXY_PORT, E2E_PROXY_USERNAME and "
            "E2E_PROXY_PASSWORD in .env (untracked), or run `orchestrator e2e "
            "--chrome`, which drives plain Chrome and needs no proxy.\n"
            "Without this, Multilogin fails the profile's proxy check with "
            "GET_PROXY_CONNECTION_IP_ERROR and the crawler never starts."
        )

    proxy["hostname"] = settings.e2e_proxy_host
    proxy["username"] = settings.e2e_proxy_username
    proxy["password"] = settings.e2e_proxy_password
    if settings.e2e_proxy_port:
        proxy["port"] = settings.e2e_proxy_port
