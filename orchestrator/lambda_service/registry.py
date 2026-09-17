"""The decision-function registry: one local server, N project decision functions.

The runner has always addressed a decision by *name* -- the ``lambda_select``
behaviour's ``function`` field, e.g. ``bot-decision-yt``, which
``LambdaCaller.invokeAws(functionName)`` uses verbatim in AWS mode. The local
path dropped the name and served one directory, so a box could host exactly one
study's decision function. This module restores the name: a registry maps each
name to the directory holding its ``lambda_function.py``, and the server
dispatches on the ``X-Function-Name`` header.

Registry file
-------------
Box-specific, outside git, like ``secrets.toml``: ``DECISION_FUNCTIONS_FILE``
(env or the orchestrator ``.env``), default
``~/.config/primemover/decision-functions.toml``::

    default = "bot-decision-yt"

    [functions.bot-decision-yt]
    path = "/opt/primemover/bot-decision-yt/bot-decision-EN"
    # entry = "lambda_function:select_recommendation"   # the default

**No file is not an error.** It means exactly today's behaviour: the
``orchestrator/lambda_service/decision`` symlink is the one function, named
``bot-decision-yt``, and ``/health`` says the registry is absent. A *malformed*
file is a different thing entirely and must never fall back silently -- running
some other study's decision function would be invisible in every indicator the
system has -- so it loads to a registry with no functions and a recorded
problem, and every ``/invoke`` then answers the degraded unknown-function shape.

Module isolation
----------------
Every package is called ``lambda_function`` and several carry same-named helper
modules next to it. Importing the second package the obvious way would hand it
the first package's already-cached modules -- the same silent-wrong-answer class
of failure as the wrong cwd: a plausible recommendation from the wrong code.

So each package is imported under a unique module name
(``pm_decision_<slug>``) with its directory on ``sys.path`` only for the
duration of that import, and every module the import pulled *out of that
directory* is then re-keyed in ``sys.modules`` under the same unique prefix and
removed from its bare name. The module objects stay alive (the importing module
holds references), the bare names are free for the next package, and nothing
imported from site-packages (pandas, openai, ...) is touched.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on the 3.10 CI job
    import tomli as tomllib

logger = logging.getLogger("lambda_service")

#: Where the registry lives when ``DECISION_FUNCTIONS_FILE`` is unset.
DEFAULT_REGISTRY_FILE = Path.home() / ".config" / "primemover" / "decision-functions.toml"

#: The name the symlinked decision directory answers to when no registry exists.
FALLBACK_NAME = "bot-decision-yt"

#: ``module:attribute``. The module is the ``.py`` file inside the package dir.
DEFAULT_ENTRY = "lambda_function:select_recommendation"

#: Prefix for the unique ``sys.modules`` name each package is imported under.
MODULE_PREFIX = "pm_decision_"


class UnknownFunctionError(LookupError):
    """No function of that name is registered (or no default is configured)."""


def _slug(name: str) -> str:
    """A module-name-safe rendering of a function name (``bot-decision-yt``)."""
    return re.sub(r"\W", "_", name)


@dataclass
class DecisionFunction:
    """One registered decision function: a name, a directory, an entry point."""

    name: str
    path: Path
    entry: str = DEFAULT_ENTRY

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _callable: Callable[[str], Any] | None = field(default=None, repr=False)
    _module: ModuleType | None = field(default=None, repr=False)
    _error: str | None = field(default=None, repr=False)

    # -- introspection ------------------------------------------------------

    @property
    def module_name(self) -> str:
        """The module half of ``entry`` (``lambda_function``)."""
        return self.entry.partition(":")[0] or "lambda_function"

    @property
    def attribute(self) -> str:
        """The attribute half of ``entry`` (``select_recommendation``)."""
        return self.entry.partition(":")[2] or "select_recommendation"

    @property
    def module_file(self) -> Path:
        """The file that is imported for this function."""
        return self.path / f"{self.module_name}.py"

    @property
    def loaded(self) -> bool:
        return self._callable is not None

    @property
    def error(self) -> str | None:
        return self._error

    def path_problem(self) -> str | None:
        """Why this entry cannot work, judged from the filesystem alone.

        Deliberately free of imports so ``orchestrator doctor`` and
        ``LambdaServerComponent.validate()`` can use it on a box that cannot
        import the package's dependencies.
        """
        if not self.path.is_dir():
            return f"path not found: {self.path}"
        if not self.module_file.is_file():
            return f"{self.module_file.name} not found in {self.path}"
        return None

    # -- loading ------------------------------------------------------------

    def load(self) -> Callable[[str], Any]:
        """Import the package (once) and return its entry callable.

        Raises ``ImportError`` with a message naming this function. A failure is
        recorded but not cached as terminal: a fixed deployment is picked up by
        the next request rather than needing a restart.
        """
        with self._lock:
            if self._callable is not None:
                return self._callable

            problem = self.path_problem()
            if problem:
                self._error = problem
                raise ImportError(f"decision function '{self.name}': {problem}")

            try:
                module = self._import_isolated()
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {exc}"
                raise ImportError(
                    f"decision function '{self.name}' failed to import "
                    f"({self.module_file}): {type(exc).__name__}: {exc}"
                ) from exc

            func = getattr(module, self.attribute, None)
            if not callable(func):
                self._error = f"{self.module_name}.py defines no callable {self.attribute}()"
                raise ImportError(f"decision function '{self.name}': {self._error}")

            typed: Callable[[str], Any] = func
            self._module = module
            self._callable = typed
            self._error = None
            logger.info(
                "loaded decision function '%s' from %s (entry %s)",
                self.name,
                self.module_file,
                self.entry,
            )
            return typed

    def _import_isolated(self) -> ModuleType:
        """Import ``module_file`` under a unique name, then free the bare names.

        See the module docstring: two packages both called ``lambda_function``,
        both with a ``helpers`` next to it, must not shadow each other.
        """
        import importlib.util

        unique = f"{MODULE_PREFIX}{_slug(self.name)}"
        spec = importlib.util.spec_from_file_location(unique, self.module_file)
        if spec is None or spec.loader is None:
            raise ImportError(f"could not build an import spec for {self.module_file}")

        module = importlib.util.module_from_spec(spec)
        before = set(sys.modules)
        sys.modules[unique] = module
        sys.path.insert(0, str(self.path))
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(unique, None)
            raise
        finally:
            try:
                sys.path.remove(str(self.path))
            except ValueError:  # pragma: no cover - only if someone else removed it
                pass
            self._rekey_package_modules(before, unique)
        return module

    def _rekey_package_modules(self, before: set[str], unique: str) -> None:
        """Move modules imported *from this package's directory* under ``unique``.

        Anything else the import pulled in (pandas, openai, ...) is left where
        it is: it is genuinely shared, and re-importing it per package would be
        both slow and wrong.
        """
        root = str(self.path.resolve())
        for name in list(sys.modules):
            if name in before or name == unique or name.startswith(f"{unique}."):
                continue
            module = sys.modules.get(name)
            origin = getattr(module, "__file__", None)
            if not origin:
                continue
            try:
                resolved = str(Path(origin).resolve())
            except OSError:  # pragma: no cover - a synthetic __file__
                continue
            if resolved == root or resolved.startswith(root + "/"):
                sys.modules[f"{unique}.{name}"] = sys.modules.pop(name)

    # -- the optional health hook ------------------------------------------

    def health(self) -> dict[str, Any] | None:
        """The package's own ``health()``, or ``None`` if it defines none.

        The hook is the project's, not the server's: whether
        ``mainstream_media_channels.csv`` is intact is a fact about
        bot-decision-yt, and hard-coding it here is why this server could only
        ever host one study.
        """
        module = self._module
        if module is None:
            return None
        hook = getattr(module, "health", None)
        if not callable(hook):
            return None
        try:
            result = hook()
        except Exception as exc:  # noqa: BLE001 - a broken hook must not break /health
            return {"ok": False, "problems": [f"health() raised {type(exc).__name__}: {exc}"]}
        if not isinstance(result, dict):
            return {
                "ok": False,
                "problems": [f"health() returned {type(result).__name__}, expected a dict"],
            }
        return result


def hook_problem(result: dict[str, Any] | None) -> bool:
    """Whether a hook result reports a problem.

    ``ok`` is the contract; ``problems`` is accepted as well so a hook that
    only lists findings still counts. Absent (no hook) is *not* a problem --
    absence of a report is never evidence of health, but it is also not
    evidence of a fault, and marking every hookless package degraded would make
    the field useless.
    """
    if not isinstance(result, dict):
        return False
    if result.get("ok") is False:
        return True
    return bool(result.get("problems"))


@dataclass
class Registry:
    """The registered decision functions, plus where they were read from."""

    functions: dict[str, DecisionFunction] = field(default_factory=dict)
    default: str | None = None
    source: Path | None = None
    fallback: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return sorted(self.functions)

    def get(self, name: str | None) -> DecisionFunction:
        """Resolve a requested name (``None`` = the default).

        Raises :class:`UnknownFunctionError` with a message naming what *is*
        registered -- the server passes that text back to the runner, and a
        bare "not found" would send whoever reads it to the wrong box.
        """
        registered = ", ".join(self.names) or "none"
        if name is None:
            if self.default is None:
                raise UnknownFunctionError(
                    f"no default decision function configured; registered: {registered}"
                )
            name = self.default
        function = self.functions.get(name)
        if function is None:
            raise UnknownFunctionError(
                f"unknown decision function '{name}'; registered: {registered}"
            )
        return function

    def describe(self) -> dict[str, Any]:
        """Per-function state for ``/health``: loaded, error, hook result.

        This *attempts* the import, because "registered" and "importable" are
        different claims and only the second one is worth reporting.
        """
        described: dict[str, Any] = {}
        for name in self.names:
            function = self.functions[name]
            try:
                function.load()
            except ImportError as exc:
                logger.debug("decision function '%s' not importable: %s", name, exc)
            described[name] = {
                "path": str(function.path),
                "entry": function.entry,
                "loaded": function.loaded,
                "error": function.error,
                "health": function.health(),
            }
        return described

    def healthy(self, described: dict[str, Any] | None = None) -> bool:
        """Degraded if any function failed to import or its hook reports a problem."""
        if self.problems:
            return False
        described = self.describe() if described is None else described
        for state in described.values():
            if not state.get("loaded") or state.get("error"):
                return False
            if hook_problem(state.get("health")):
                return False
        return True


def registry_path(settings: Any | None = None) -> Path:
    """The configured registry path (``DECISION_FUNCTIONS_FILE``), expanded."""
    if settings is None:
        from orchestrator.config.settings import get_settings

        settings = get_settings()
    configured = getattr(settings, "decision_functions_file", None)
    if configured is None:
        return DEFAULT_REGISTRY_FILE

    raw = str(configured).strip()
    if not raw or raw == ".":
        # An empty ``DECISION_FUNCTIONS_FILE=`` in .env parses as ``Path('.')``.
        # Read that as unset rather than as "the registry is this directory",
        # which would report a nonsense path and hide the real default.
        return DEFAULT_REGISTRY_FILE
    return Path(raw).expanduser()


def fallback_registry(decision_dir: Path) -> Registry:
    """The no-registry case: the symlinked decision dir, named ``bot-decision-yt``."""
    return Registry(
        functions={FALLBACK_NAME: DecisionFunction(name=FALLBACK_NAME, path=Path(decision_dir))},
        default=FALLBACK_NAME,
        source=None,
        fallback=True,
    )


def load_registry(path: Path | None, decision_dir: Path) -> Registry:
    """Read the registry file, or fall back to the symlinked decision directory.

    Never raises: a registry this server cannot read is reported through
    ``problems`` (and so through ``/health`` and every ``/invoke``) rather than
    taking the service down or, worse, quietly serving the fallback.
    """
    if path is None or not Path(path).is_file():
        return fallback_registry(decision_dir)

    path = Path(path)
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except Exception as exc:  # noqa: BLE001 - any unreadable file is one problem
        return Registry(
            source=path, problems=[f"could not read {path}: {type(exc).__name__}: {exc}"]
        )

    problems: list[str] = []
    functions: dict[str, DecisionFunction] = {}

    table = data.get("functions")
    if not isinstance(table, dict):
        problems.append(f"{path}: no [functions.<name>] tables")
        table = {}

    for name, entry in table.items():
        if not isinstance(entry, dict):
            problems.append(f"{path}: [functions.{name}] is not a table")
            continue
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            problems.append(f"{path}: [functions.{name}] has no 'path'")
            continue
        entry_point = entry.get("entry", DEFAULT_ENTRY)
        if not isinstance(entry_point, str) or ":" not in entry_point:
            problems.append(f"{path}: [functions.{name}] entry must look like 'module:attribute'")
            continue
        functions[name] = DecisionFunction(
            name=name,
            path=Path(raw_path).expanduser(),
            entry=entry_point,
        )

    default = data.get("default")
    if default is None:
        if len(functions) == 1:
            default = next(iter(functions))
        else:
            problems.append(
                f"{path}: no 'default' and {len(functions)} functions registered, so a "
                "request without an X-Function-Name header cannot be served"
            )
    elif not isinstance(default, str) or default not in functions:
        problems.append(f"{path}: default = {default!r} is not a registered function")
        default = None

    return Registry(
        functions=functions,
        default=default if isinstance(default, str) else None,
        source=path,
        fallback=False,
        problems=problems,
    )
