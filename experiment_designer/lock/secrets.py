"""Secret placeholders in locks and the file they resolve from at upload time.

A lock says ``${secret:oxylabs.us_nc.password}``; the machine doing the upload
holds a mode-600 TOML file with that key. The value exists in memory for the
duration of one HTTP request and is never written or printed."""

from __future__ import annotations

import copy
import os
import re
import stat
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on the 3.10 CI job
    import tomli as tomllib

PLACEHOLDER_RE = re.compile(r"\$\{secret:([A-Za-z0-9_.-]+)\}")
SELF_CONFIGURING_PROXY_TYPES = frozenset({"oxylabs"})
DEFAULT_SECRETS_FILE = Path.home() / ".config" / "primemover" / "secrets.toml"
_CREDENTIAL_FIELDS = ("username", "password")


class MissingSecret(KeyError):
    pass


class InsecureSecretsFile(PermissionError):
    pass


def find_placeholders(obj: Any) -> set[str]:
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            found.update(PLACEHOLDER_RE.findall(node))
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    return found


def find_literal_credentials(crawlers: list[dict[str, Any]]) -> list[str]:
    """Paths of proxy credentials that are non-empty literals (not placeholders)."""
    paths = []
    for i, crawler in enumerate(crawlers):
        proxy = crawler.get("proxy") or {}
        if not isinstance(proxy, dict) or proxy.get("type") in SELF_CONFIGURING_PROXY_TYPES:
            continue
        for field in _CREDENTIAL_FIELDS:
            value = proxy.get(field)
            if isinstance(value, str) and value and not PLACEHOLDER_RE.fullmatch(value):
                paths.append(f"crawlers[{i}].proxy.{field}")
    return paths


class SecretStore:
    def __init__(self, data: dict[str, Any], source: str) -> None:
        self._data = data
        self.source = source

    @classmethod
    def load(cls, path: Path | None = None) -> SecretStore:
        resolved = path or Path(os.environ.get("PMSPEC_SECRETS_FILE") or DEFAULT_SECRETS_FILE)
        mode = stat.S_IMODE(resolved.stat().st_mode)
        if mode & 0o077:
            raise InsecureSecretsFile(
                f"{resolved} is readable by group/other (mode {mode:o}); run: chmod 600 {resolved}"
            )
        with open(resolved, "rb") as f:
            return cls(tomllib.load(f), str(resolved))

    def get(self, key: str) -> str:
        node: Any = self._data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise MissingSecret(f"{key} (not in {self.source})")
            node = node[part]
        if not isinstance(node, str):
            raise MissingSecret(f"{key} is not a string in {self.source}")
        return node

    def missing(self, keys: Iterable[str]) -> list[str]:
        out = []
        for key in sorted(set(keys)):
            try:
                self.get(key)
            except MissingSecret:
                out.append(key)
        return out


def resolve_secrets(obj: Any, store: SecretStore) -> Any:
    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return PLACEHOLDER_RE.sub(lambda m: store.get(m.group(1)), node)
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(copy.deepcopy(obj))


def mask_secrets(obj: Any) -> Any:
    """Locks already hold placeholders; this exists so every printer calls the
    same function and a future resolved-object printer cannot forget to."""
    return obj
