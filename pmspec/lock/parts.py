"""Content-addressed parts: factor a fleet of crawlers into distinct queue and
agent bodies plus small per-crawler references, and materialise the inverse.

A part's key is the sha256 of its canonical JSON, prefixed by its kind and
truncated to 16 hex characters. Two different bodies hashing to the same key
is a compile error, not a coincidence to shrug off -- ``factor_crawlers``
checks it on every insert.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

_CRAWLER_KEY_ORDER = ("name", "description", "configuration", "proxy", "agent", "queues")


class PartCollision(ValueError):
    """Two distinct queue or agent bodies hashed to the same content-addressed key."""


class DanglingRefError(KeyError):
    """A crawler references a part the lock does not carry.

    A ``KeyError`` so an existing ``except KeyError`` still catches it, but one
    that knows *where* the ref sits: ``Lock.from_dict`` turns it into a
    ``LockError`` naming ``crawlers[i].queues[j].ref`` / ``crawlers[i].agent.ref``.
    """

    def __init__(self, key: str, crawler_index: int, queue_index: int | None = None) -> None:
        super().__init__(key)
        self.key = key
        self.crawler_index = crawler_index
        self.queue_index = queue_index

    @property
    def path(self) -> str:
        if self.queue_index is None:
            return f"crawlers[{self.crawler_index}].agent.ref"
        return f"crawlers[{self.crawler_index}].queues[{self.queue_index}].ref"

    def __str__(self) -> str:
        return f"{self.path}: unknown part {self.key}"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def part_key(obj: Any, prefix: str) -> str:
    digest = hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest[:16]}"


def _ordered_crawler(crawler: dict[str, Any]) -> dict[str, Any]:
    ordered: dict[str, Any] = {}
    for key in _CRAWLER_KEY_ORDER:
        if key in crawler:
            ordered[key] = crawler[key]
    for key, value in crawler.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def factor_crawlers(
    crawlers: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Split every crawler's queue and agent bodies out into content-addressed
    parts, replacing them in the returned crawlers with ``{"ref": key, ...}``.

    Returns ``(parts, refs)`` where ``parts`` is ``{"queues": {key: body},
    "agents": {key: body}}`` and ``refs`` is the crawler list with ``agent``
    reduced to ``{"ref": key, "name": ...}`` and each queue reduced to
    ``{"ref": key, "start_offset": ...}``.
    """
    queues: dict[str, dict[str, Any]] = {}
    agents: dict[str, dict[str, Any]] = {}
    refs: list[dict[str, Any]] = []

    for crawler in crawlers:
        new_crawler = dict(crawler)

        agent = crawler.get("agent")
        if agent is not None:
            body = {k: v for k, v in agent.items() if k != "name"}
            key = part_key(body, "a")
            existing = agents.get(key)
            if existing is not None and existing != body:
                raise PartCollision(f"agent part collision at {key}")
            agents[key] = body
            new_crawler["agent"] = {"ref": key, "name": agent.get("name")}

        if "queues" in crawler:
            new_queues = []
            for queue in crawler["queues"]:
                body = {k: v for k, v in queue.items() if k != "start_offset"}
                key = part_key(body, "q")
                existing = queues.get(key)
                if existing is not None and existing != body:
                    raise PartCollision(f"queue part collision at {key}")
                queues[key] = body
                new_queues.append({"ref": key, "start_offset": queue.get("start_offset")})
            new_crawler["queues"] = new_queues

        refs.append(_ordered_crawler(new_crawler))

    return {"queues": queues, "agents": agents}, refs


def materialise(
    parts: dict[str, dict[str, Any]], refs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Inverse of ``factor_crawlers``: expand crawler refs back into full
    crawlers carrying complete ``agent`` and ``queues`` bodies. An unresolvable
    ref raises :class:`DanglingRefError` (a ``KeyError``) naming the dangling ref
    and where it sits."""
    queues = parts.get("queues", {})
    agents = parts.get("agents", {})
    crawlers: list[dict[str, Any]] = []

    for i, ref in enumerate(refs):
        crawler = dict(ref)

        agent_ref = ref.get("agent")
        if agent_ref is not None:
            key = agent_ref["ref"]
            if key not in agents:
                raise DanglingRefError(key, i)
            # Every crawler referencing this part must get its own copy: a
            # shallow dict() here would leave nested structures (e.g. a
            # multilogin_profile dict) aliased across every crawler sharing
            # the part, so mutating one bot's materialised agent would
            # corrupt its siblings.
            agent = copy.deepcopy(agents[key])
            agent["name"] = agent_ref.get("name")
            crawler["agent"] = agent

        if "queues" in ref:
            new_queues = []
            for j, queue_ref in enumerate(ref["queues"]):
                key = queue_ref["ref"]
                if key not in queues:
                    raise DanglingRefError(key, i, j)
                # Same aliasing concern as the agent above, and sharper here:
                # the body includes the `jobs` list, so without a deep copy
                # every crawler sharing this queue part would alias the same
                # job/behavior dicts.
                queue = copy.deepcopy(queues[key])
                queue["start_offset"] = queue_ref.get("start_offset")
                new_queues.append(queue)
            crawler["queues"] = new_queues

        crawlers.append(crawler)

    return crawlers
