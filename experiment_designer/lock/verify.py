"""Is the live experiment the one this lock describes? Two answers: bound (hash)
and identical (structure). Execution is not this module's business."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .anchor import resolve_anchor, resolve_start_at
from .io import load_lock, sha256_text
from .model import Lock
from .secrets import PLACEHOLDER_RE

_PARAM_COLUMNS = ("pi", "psi", "alpha", "tau", "kappa", "beta")
_PAGE_SIZE = 500


@dataclass(frozen=True)
class Difference:
    path: str
    expected: Any
    actual: Any


@dataclass
class VerifyResult:
    experiment_id: int
    bound: bool
    local_sha256: str
    live_sha256s: list[str]
    anchor: str | None
    differences: list[Difference] = field(default_factory=list)
    deep: bool = False
    # Append mode (`upload --experiment-id`) adds crawlers to an experiment that
    # already has some from an earlier lock; those are not this lock's business.
    extra_live_crawlers: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.bound and not self.differences

    def render(self) -> str:
        lines = []
        if self.bound:
            lines.append(f"BOUND  experiment {self.experiment_id} <- lock {self.local_sha256[:12]}")
        else:
            live = ", ".join(h[:12] for h in self.live_sha256s) or "no pmspec.lock_sha256"
            lines.append(f"UNBOUND  local lock {self.local_sha256[:12]}; server has {live}")
        if self.anchor:
            lines.append(f"anchor {self.anchor}")
        else:
            lines.append("no pmspec.anchor on the server: start times not compared")
        if self.extra_live_crawlers:
            names = ", ".join(self.extra_live_crawlers)
            lines.append(
                f"note: {len(self.extra_live_crawlers)} live crawler(s) not in this lock: {names}"
            )
        if self.differences:
            lines.append(f"structure: {len(self.differences)} difference(s)")
        else:
            lines.append("structure: identical" + (" (deep)" if self.deep else ""))
        for d in self.differences:
            lines.append(f"  {d.path}: expected {d.expected!r}, live {d.actual!r}")
        return "\n".join(lines)


def redact_differences(differences: list[Difference]) -> list[Difference]:
    """A Difference's ``actual`` side is a value read straight off the live
    experiment. When ``expected`` is a secret placeholder (a lock keeps
    ``${secret:...}`` for anything outside ``proxy.*`` too, e.g. a preference or
    behaviour value), ``actual`` is that placeholder's *resolved* value -- the
    literal secret. ``verify_lock`` applies this before the VerifyResult exists,
    so nothing that serialises one (``render()``, a JSON tool result, a CLI
    print) can carry it; a printer of Differences from any other source (a lock
    diff) calls it itself."""
    return [
        (
            Difference(d.path, d.expected, "<redacted>")
            if isinstance(d.expected, str) and PLACEHOLDER_RE.search(d.expected)
            else d
        )
        for d in differences
    ]


def _num(value: Any) -> Any:
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _params(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    out = {}
    for k in _PARAM_COLUMNS:
        if raw.get(k) is not None:
            out[k] = _num(raw[k])
    for k in ("search_terms", "media_outlet_urls"):
        value = raw.get(k)
        if isinstance(value, str):
            # The API's Param model casts search_terms to an array but serves
            # media_outlet_urls as the JSON string it is stored as.
            try:
                value = json.loads(value)
            except ValueError:
                pass
        if value:
            out[k] = list(value) if isinstance(value, (list, tuple)) else value
    return out


def _wall(value: str) -> str:
    return value.replace(" ", "T")[:19]


def lock_shape(lock: Lock, anchor_dt: datetime, *, deep: bool) -> dict[str, Any]:
    crawlers: dict[str, Any] = {}
    for c in lock.crawlers:
        proxy = c.get("proxy") or {}
        config = c.get("configuration") or {}
        entry: dict[str, Any] = {
            "params": _params(config.get("params")),
            "preferences": {p["name"]: str(p["value"]) for p in config.get("preferences", [])},
            "location": (c.get("agent") or {}).get("location", ""),
            "proxy": {
                "type": proxy.get("type"),
                "hostname": proxy.get("hostname"),
                "port": proxy.get("port"),
            },
            "queues": [],
        }
        for q in c.get("queues", []):
            queue: dict[str, Any] = {
                "name": q["name"],
                "start_at": _wall(resolve_start_at(anchor_dt, q["start_offset"])),
            }
            if deep:
                queue["jobs"] = [
                    {
                        "type": j["type"],
                        "behaviors": {b["name"]: str(b["value"]) for b in j.get("behaviors", [])},
                    }
                    for j in q.get("jobs", [])
                ]
            entry["queues"].append(queue)
        crawlers[c["name"]] = entry
    return {"crawlers": crawlers}


def _paginate(session: Any, path: str) -> list[dict[str, Any]]:
    """Every row of a paginated listing.

    Both real sessions unwrap ``data`` before returning, so the page's ``meta``
    (and its ``last_page``) never reaches this function: a page is a plain list,
    and the listing ends at the first page shorter than what was asked for."""
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        body = session.get(path, params={"per_page": _PAGE_SIZE, "page": page})
        data = body.get("data", body) if isinstance(body, dict) else body
        data = list(data or [])
        rows.extend(data)
        if len(data) < _PAGE_SIZE:
            return rows
        page += 1


def _export_jobs(session: Any, experiment_id: int) -> dict[int, list[dict[str, Any]]]:
    """queue_id -> jobs (ordered) with behaviours, from the experiment download zip."""
    blob = session.get_bytes(f"experiments/{experiment_id}/download")
    jobs_by_queue: dict[int, list[dict[str, Any]]] = defaultdict(list)
    behaviours: dict[int, dict[str, str]] = defaultdict(dict)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = z.namelist()
        beh_name = next(n for n in names if n.endswith("_behaviors.csv"))
        job_name = next(n for n in names if n.endswith("_jobs.csv"))
        for row in csv.DictReader(io.TextIOWrapper(z.open(beh_name), encoding="utf-8")):
            behaviours[int(row["Job ID"])][row["Name"]] = row["Value"]
        for row in csv.DictReader(io.TextIOWrapper(z.open(job_name), encoding="utf-8")):
            jobs_by_queue[int(row["Queue ID"])].append(
                {"id": int(row["ID"]), "order": int(row["Order"] or 0), "type": row["Type"]}
            )
    return {
        qid: [
            {"type": j["type"], "behaviors": behaviours.get(j["id"], {})}
            for j in sorted(jobs, key=lambda j: (j["order"], j["id"]))
        ]
        for qid, jobs in jobs_by_queue.items()
    }


def live_shape(
    session: Any, experiment_id: int, *, deep: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    exp = session.get(f"experiments/{experiment_id}")
    queues = _paginate(session, f"experiments/{experiment_id}/queues-unprocessed") + _paginate(
        session, f"experiments/{experiment_id}/queues-processed"
    )
    by_crawler: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for q in queues:
        by_crawler[int(q["crawler_id"])].append(q)
    jobs = _export_jobs(session, experiment_id) if deep else {}

    crawlers: dict[str, Any] = {}
    for c in exp.get("crawlers", []):
        config = c.get("configuration") or {}
        params_rows: list[dict[str, Any]] = []
        if config.get("id") is not None:
            params_rows = (session.get(f"configurations/{config['id']}") or {}).get("params") or []
        proxy = c.get("proxy") or {}
        entry: dict[str, Any] = {
            "params": _params(params_rows[0] if params_rows else None),
            "preferences": {p["name"]: str(p["value"]) for p in config.get("preferences", [])},
            "location": (c.get("agent") or {}).get("location", ""),
            "proxy": {
                "type": proxy.get("type"),
                "hostname": proxy.get("hostname"),
                "port": proxy.get("port"),
            },
            "queues": [],
        }
        for q in sorted(
            by_crawler.get(int(c["id"]), []), key=lambda q: (_wall(q["start_at"]), q["name"])
        ):
            queue: dict[str, Any] = {"name": q["name"], "start_at": _wall(q["start_at"])}
            if deep:
                queue["jobs"] = jobs.get(int(q["id"]), [])
            entry["queues"].append(queue)
        crawlers[c["name"]] = entry
    return {"crawlers": crawlers}, exp


def compare_shapes(expected: Any, actual: Any, path: str = "") -> list[Difference]:
    diffs: list[Difference] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            sub = f"{path}.{key}" if path else str(key)
            if key not in expected:
                diffs.append(Difference(sub, None, "<present>"))
            elif key not in actual:
                diffs.append(Difference(sub, "<present>", None))
            else:
                diffs.extend(compare_shapes(expected[key], actual[key], sub))
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            diffs.append(Difference(f"{path}.length", len(expected), len(actual)))
        for i, (e, a) in enumerate(zip(expected, actual, strict=False)):
            diffs.extend(compare_shapes(e, a, f"{path}[{i}]"))
    elif expected != actual:
        diffs.append(Difference(path, expected, actual))
    return diffs


def _live_anchor(prefs: list[dict[str, Any]], local_sha: str) -> str | None:
    """The anchor that goes with this lock's upload.

    Each upload writes its provenance preferences with the same ``description``
    label ("upload 1", "upload 2", ...), so the anchor to resolve this lock's
    offsets against is the one labelled like the ``pmspec.lock_sha256`` entry
    that matches it. Unbound, the last anchor written is the best guess."""
    anchors = [p for p in prefs if p.get("name") == "pmspec.anchor"]
    if not anchors:
        return None
    labels = {
        p.get("description")
        for p in prefs
        if p.get("name") == "pmspec.lock_sha256" and p.get("value") == local_sha
    }
    for p in reversed(anchors):
        if p.get("description") in labels:
            return str(p["value"])
    return str(anchors[-1]["value"])


def _drop_start_times(shape: dict[str, Any]) -> None:
    for entry in shape["crawlers"].values():
        for queue in entry["queues"]:
            queue.pop("start_at", None)


def verify_lock(
    lock_text: str, *, session: Any, experiment_id: int, deep: bool = False
) -> VerifyResult:
    lock = load_lock(lock_text)
    local_sha = sha256_text(lock_text)
    actual, exp = live_shape(session, experiment_id, deep=deep)
    prefs = exp.get("preferences", [])
    live_shas = [p["value"] for p in prefs if p.get("name") == "pmspec.lock_sha256"]
    anchor_iso = _live_anchor(prefs, local_sha)
    anchor_dt = (
        resolve_anchor(anchor_iso[:19]) if anchor_iso else resolve_anchor(lock.schedule["anchor"])
    )
    expected = lock_shape(lock, anchor_dt, deep=deep)
    # Queue order inside a crawler: lock order vs. live sorted by start; sort the lock side too.
    for entry in expected["crawlers"].values():
        entry["queues"].sort(key=lambda q: (q["start_at"], q["name"]))
    if anchor_iso is None:
        # Without the server's anchor the lock's offsets resolve to nothing
        # comparable; the rest of the structure still is.
        _drop_start_times(expected)
        _drop_start_times(actual)
    # Only this lock's crawlers are compared: in append mode the experiment
    # legitimately holds crawlers from earlier uploads.
    extra = sorted(set(actual["crawlers"]) - set(expected["crawlers"]))
    actual = {
        "crawlers": {
            name: c for name, c in actual["crawlers"].items() if name in expected["crawlers"]
        }
    }
    return VerifyResult(
        experiment_id=experiment_id,
        bound=local_sha in live_shas,
        local_sha256=local_sha,
        live_sha256s=live_shas,
        anchor=anchor_iso,
        differences=redact_differences(compare_shapes(expected, actual)),
        deep=deep,
        extra_live_crawlers=extra,
    )
