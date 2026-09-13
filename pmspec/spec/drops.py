"""The strict contract's one mechanism.

A *drop* is a construct the pipeline would silently discard, ignore or leave
unresolved -- an unknown params key, a treatment without a strategy, an
unresolved ${var}. Every layer reports drops here; strict mode (the default)
turns them into errors, lax mode into warnings plus a count that the lock and
the upload record carry (provenance.lax / pmspec.lax)."""

from __future__ import annotations


class Drops:
    def __init__(self, lax: bool = False) -> None:
        self.lax = lax
        self._messages: list[str] = []

    def report(self, path: str, what: str, fix: str) -> None:
        self._messages.append(f"{path}: {what}; {fix}")

    @property
    def messages(self) -> list[str]:
        return list(self._messages)

    @property
    def count(self) -> int:
        return len(self._messages)

    def merge_into(self, errors: list[str], warnings: list[str]) -> None:
        if not self._messages:
            return
        if self.lax:
            warnings.extend(self._messages)
            warnings.append(f"{self.count} construct(s) would have been dropped (lax mode)")
        else:
            errors.extend(self._messages)
