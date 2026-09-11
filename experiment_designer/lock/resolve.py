# experiment_designer/lock/resolve.py
"""Turn a lock into the API's upload JSON: anchor -> start_at, placeholders -> values."""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from .anchor import resolve_anchor, resolve_start_at
from .model import Lock
from .secrets import SecretStore, resolve_secrets


def resolve_for_upload(
    lock: Lock, *, store: SecretStore | None, now: datetime | None = None
) -> tuple[list[dict[str, Any]], datetime]:
    anchor_dt = resolve_anchor(lock.schedule["anchor"], now=now)
    crawlers = copy.deepcopy(lock.crawlers)
    for crawler in crawlers:
        for queue in crawler.get("queues", []):
            queue["start_at"] = resolve_start_at(anchor_dt, queue.pop("start_offset"))
    if store is not None:
        crawlers = resolve_secrets(crawlers, store)
    return crawlers, anchor_dt
