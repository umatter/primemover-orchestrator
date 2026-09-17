"""
Local Lambda Server - HTTP wrapper for the project decision functions.

Provides a local HTTP endpoint that mirrors AWS Lambda invocation,
allowing the runner to call the decision function without AWS costs.

Usage:
    python -m orchestrator.lambda_service.server [--port PORT] [--host HOST]

Endpoints:
    GET  /health  - Health check (registry, per-function state, open fds)
    POST /invoke  - Invoke a decision function (expects JSON body)

Dispatch
--------
The runner has always addressed a decision by name -- the ``lambda_select``
behaviour's ``function`` field, which ``LambdaCaller.invokeAws(functionName)``
uses verbatim in AWS mode. Locally that name used to be dropped and one
directory served, so a box could host exactly one study.

It now dispatches on the ``X-Function-Name`` header against the registry in
:mod:`orchestrator.lambda_service.registry`. No header means the registry's
``default`` (so an un-rebuilt runner keeps working), and an unknown name is
answered with HTTP 200 and the degraded report shape -- never a 5xx, because a
5xx is the one answer the runner turns into a retry loop rather than a recorded
verdict.

Working directory
-----------------
The server no longer chdirs per package, and a function must not depend on the
cwd: data files are read relative to the function's own file
(``Path(__file__).resolve().parent / ...``), which bot-decision-yt now does. A
cwd-relative read is how the ``pi=1`` mainstream-media arm silently became a
second control arm under systemd for months -- the read raised, an
``except Exception`` in the decision function degraded it to a warning, and the
job still returned 200 with plausible recommendations.

The one exception is the no-registry fallback, which still chdirs into the
symlinked decision directory once (:func:`ensure_decision_cwd`) so a checkout of
bot-decision-yt that predates its file-relative paths keeps working through the
transition.

Observability
-------------
A degraded decision is invisible in the HTTP status code -- the decision
function answers 200 whether its filters applied or not. :func:`invoke` reads
the response through ``orchestrator.status`` and logs:

* **ERROR** when ``treatment_lost`` -- a bot that was configured to be treated
  produced control-arm data, so that session's data is scientifically void.
* **WARNING** when ``degraded`` -- a *requested* filter did not apply.
* **INFO** otherwise, one summary line per decision.
* **DEBUG** for the neutral observation that the video was picked at random.

That last point is the whole reason this docstring exists. An earlier version
raised the ERROR on ``selection_mode == "random"`` alone, and ``random`` is the
correct, healthy mode for a **control** crawler: no LLM filter was ever
requested. On this box 82% of real lambda payloads (295 of 358) come from
control-arm bots, so that alarm would have fired on ~82% of all decisions and
been permanently on. Gate on ``treatment_lost``; never on ``selection_mode``.

The response body and status code are passed through unchanged: the runner
decides what to do with a degraded decision.
"""

import argparse
import json
import logging
import os
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from orchestrator.lambda_service.registry import (
    FALLBACK_NAME,
    Registry,
    UnknownFunctionError,
    hook_problem,
    load_registry,
    registry_path,
)
from orchestrator.status import lambda_verdict

# Configure logging.
#
# stderr stays the primary sink, so `journalctl -u primemover-lambda` keeps
# working under systemd and `orchestrator up` keeps showing output. A rotating
# file is added alongside it because promtail scrapes files, not journald: until
# now the service's own account of a degraded decision -- the WARNING on
# `degraded` and the ERROR on `treatment_lost`, the two most important signals
# this system produces -- never reached Loki at all, so no Grafana rule could be
# written against them. The lambda alerts had to key off the *runner's* log
# lines instead, which only see a degradation once a job consumes it.
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_FILE = Path(__file__).resolve().parents[2] / "log" / "lambda-server.log"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("lambda_service")


def _add_file_handler() -> None:
    """Mirror the log to ``log/lambda-server.log`` for promtail to scrape.

    Best-effort: a service that cannot open its log file should still serve
    decisions, so any failure here is reported once and otherwise ignored.
    """
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(LOG_FILE, maxBytes=20 * 1024 * 1024, backupCount=5)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.getLogger().addHandler(handler)
    except OSError as exc:  # pragma: no cover - depends on the filesystem
        logger.warning("could not open %s for logging: %s", LOG_FILE, exc)


_add_file_handler()

app = Flask(__name__)

#: The symlink into ``bot-decision-yt/bot-decision-EN``. It is the *fallback*
#: function when no registry file exists, and what ``LambdaServerComponent``
#: validates on a dev box.
DECISION_DIR = Path(__file__).resolve().parent / "decision"

#: Header the runner sends to name the function it wants.
FUNCTION_NAME_HEADER = "X-Function-Name"

#: Data file backing the mainstream-media-aversion treatment arm. Still used by
#: ``LambdaServerComponent.validate()`` and ``check_mainstream_media_csv``; it
#: is no longer part of /health, which asks each function's own ``health()``.
CSV_NAME = "mainstream_media_channels.csv"

# The registry, read once per process (lazy, so a WSGI deployment gets it too).
_registry: Registry | None = None
_registry_lock = threading.Lock()

# Whether the resolved cwd has already been announced at INFO once.
_cwd_announced = False

# Whether the "caller supplied no correlation id" gap has been reported once.
_missing_correlation_id_logged = False


# ---------------------------------------------------------------------------
# Startup checks: working directory and data-file integrity
# ---------------------------------------------------------------------------


def ensure_decision_cwd(decision_dir: Path | None = None) -> Path | None:
    """Make the decision directory the process cwd -- fallback path only.

    A registered function must read its data files relative to its own file, so
    with a registry this is never called: N functions cannot share one cwd, and
    chdiring per request would be a race in a threaded server. It survives for
    the no-registry fallback, where the decision package may still be a
    bot-decision-yt checkout that predates file-relative reads.

    Idempotent, so it is safe to call from startup *and* from
    :func:`get_decision_function` (a WSGI deployment never runs :func:`main`).

    Returns the resolved directory, or ``None`` when it could not be used -- in
    which case an ERROR has been logged, because every relative data read in the
    decision function will then silently miss its file.
    """
    global _cwd_announced

    target = Path(decision_dir) if decision_dir is not None else DECISION_DIR

    if not target.is_dir():
        logger.error(
            "Decision directory not found: %s. Recreate the symlink from the repo root: "
            "ln -s ../../../bot-decision-yt/bot-decision-EN "
            "orchestrator/lambda_service/decision",
            target,
        )
        return None

    resolved = target.resolve()

    if not (resolved / "lambda_function.py").exists():
        logger.error("lambda_function.py not found in %s - /invoke cannot work", resolved)
        return None

    try:
        if Path.cwd() != resolved:
            os.chdir(resolved)
    except OSError as exc:
        logger.error("Failed to chdir to %s: %s", resolved, exc)
        return None

    if not _cwd_announced:
        logger.info(
            "Decision working directory: %s (relative data reads such as %s resolve here)",
            resolved,
            CSV_NAME,
        )
        _cwd_announced = True
    else:
        logger.debug("Decision working directory already set: %s", resolved)

    return resolved


def _load_csv_checkers() -> tuple[Any, Any, str | None]:
    """Import ``check_csv`` / ``check_parsed`` from the sibling bot-decision-yt repo.

    ``bot-decision-yt/tests/test_mainstream_media_csv.py`` exposes those helpers
    deliberately so this gate reuses the rules instead of re-implementing them.
    The import is guarded on purpose: a deployment may have only the decision
    code symlinked, without the sibling repo's test tree or pytest installed.

    Returns ``(check_csv, check_parsed, reason)``; on failure both callables are
    ``None`` and ``reason`` says why.

    The lookup is anchored on this package's own symlink rather than on the
    directory being checked: the rules are global, so validating some other
    decision directory must not silently fall back to checking nothing.
    """
    package_decision_dir = (Path(__file__).resolve().parent / "decision").resolve()
    module_path = package_decision_dir.parent / "tests" / "test_mainstream_media_csv.py"

    if not module_path.exists():
        return None, None, f"{module_path} not found"

    try:
        import importlib.util

        # Loaded by path under a non-test name: importing it as
        # ``test_mainstream_media_csv`` would collide with pytest's own
        # collection of that module.
        spec = importlib.util.spec_from_file_location("pm_mainstream_media_csv_checks", module_path)
        if spec is None or spec.loader is None:
            return None, None, f"could not load {module_path}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.check_csv, module.check_parsed, None
    except Exception as exc:  # pragma: no cover - depends on the sibling checkout
        return None, None, f"{type(exc).__name__}: {exc}"


def check_mainstream_media_csv(decision_dir: Path | None = None) -> dict[str, Any]:
    """Report the state of ``mainstream_media_channels.csv``.

    A missing or corrupted copy does not crash anything -- it turns the
    mainstream-media treatment arm into a second control arm -- so this is
    reported explicitly rather than left to be inferred from a failure that
    never comes.

    Returns a dict with ``path``, ``present``, ``ok``, ``problems`` and
    ``checked_by`` (plus ``note`` when only presence could be verified).
    """
    target = Path(decision_dir) if decision_dir is not None else DECISION_DIR
    csv_path = target / CSV_NAME

    state: dict[str, Any] = {
        "path": str(csv_path),
        "present": csv_path.is_file(),
        "ok": False,
        "problems": [],
        "checked_by": "none",
    }

    if not state["present"]:
        state["problems"] = [f"{CSV_NAME} not found next to lambda_function.py"]
        return state

    check_csv, check_parsed, reason = _load_csv_checkers()
    if check_csv is None or check_parsed is None:
        # Presence only. Deliberately not a second implementation of the
        # integrity rules: bot-decision-yt owns those.
        state["ok"] = True
        state["checked_by"] = "presence-only"
        state["note"] = f"integrity helpers unavailable ({reason}); only existence was checked"
        return state

    problems: list[str] = []
    try:
        problems.extend(check_csv(csv_path.read_bytes()))
    except Exception as exc:
        problems.append(f"unreadable: {type(exc).__name__}: {exc}")

    try:
        import pandas

        problems.extend(check_parsed(pandas.read_csv(csv_path)))
    except Exception as exc:
        # This is the call production makes; if it raises here it raises there.
        problems.append(f"pandas.read_csv failed: {type(exc).__name__}: {exc}")

    state["problems"] = problems
    state["ok"] = not problems
    state["checked_by"] = "bot-decision-yt/tests/test_mainstream_media_csv.py"
    return state


def _log_csv_state(state: dict[str, Any]) -> None:
    """Log the CSV verdict, at ERROR when it would silently degrade a treatment arm."""
    if not state.get("present"):
        logger.error(
            "%s is MISSING (%s). The mainstream-media aversion arm (pi=1) will silently "
            "behave like a control arm - the decision function swallows the read error.",
            CSV_NAME,
            state.get("path"),
        )
        return

    problems = state.get("problems") or []
    if problems:
        logger.error(
            "%s is malformed (%s): %s. The mainstream-media aversion arm (pi=1) will "
            "silently behave like a control arm.",
            CSV_NAME,
            state.get("path"),
            "; ".join(str(p) for p in problems),
        )
        return

    logger.info("%s OK (checked by %s)", CSV_NAME, state.get("checked_by"))


# ---------------------------------------------------------------------------
# The registry and the functions it names
# ---------------------------------------------------------------------------


def get_registry(reload: bool = False) -> Registry:
    """The decision-function registry, read once per process.

    Lazy on purpose: a WSGI deployment never runs :func:`main`, and the first
    ``/invoke`` must not find an unconfigured server.
    """
    global _registry

    with _registry_lock:
        if _registry is None or reload:
            _registry = load_registry(registry_path(), DECISION_DIR)
            _announce_registry(_registry)
            if _registry.fallback:
                # Transitional, and it has to happen *before* the package is
                # imported: an un-updated bot-decision-yt may read a data file
                # at import time by bare relative path. /health imports every
                # function too, so doing this only in get_decision_function()
                # would leave the very first request a coin toss.
                ensure_decision_cwd(_registry.functions[FALLBACK_NAME].path)
        return _registry


def _announce_registry(registry: Registry) -> None:
    """Say which functions this process will serve, once, at INFO."""
    if registry.fallback:
        logger.info(
            "no decision registry (%s); serving the %s symlink as '%s'",
            registry_path(),
            DECISION_DIR,
            registry.default,
        )
        return

    for problem in registry.problems:
        logger.error("decision registry: %s", problem)

    logger.info(
        "decision registry %s: functions=%s default=%s",
        registry.source,
        registry.names or "none",
        registry.default,
    )


def run_startup_checks() -> dict[str, Any]:
    """Resolve the registry, import each function, and report what is wrong.

    In the no-registry fallback this also chdirs into the symlinked decision
    directory and checks its CSV, which is exactly the old behaviour: that path
    exists for a bot-decision-yt checkout predating file-relative data reads.
    """
    registry = get_registry(reload=True)

    cwd = None
    csv_state = None
    if registry.fallback:
        cwd = ensure_decision_cwd()
        csv_state = check_mainstream_media_csv()
        _log_csv_state(csv_state)

    described = registry.describe()
    for name, state in described.items():
        if state.get("error"):
            logger.error("decision function '%s' is NOT importable: %s", name, state["error"])
        elif hook_problem(state.get("health")):
            logger.error(
                "decision function '%s' reports a problem: %s", name, _trim(state["health"])
            )
        else:
            logger.info("decision function '%s' ready (%s)", name, state["path"])

    return {
        "registry": str(registry.source) if registry.source else None,
        "functions": described,
        "cwd": str(cwd) if cwd else None,
        "mainstream_media_csv": csv_state,
    }


def get_decision_function(name: str | None = None):
    """Return the entry callable for *name* (``None`` = the registry default).

    Raises :class:`UnknownFunctionError` when no such function is registered and
    ``ImportError`` when the registered package cannot be imported. The caller
    turns each into a different answer: an unknown name is the caller's
    mistake and comes back as a degraded 200, an unimportable package is this
    box's fault and comes back as a 500.
    """
    registry = get_registry()
    function = registry.get(name)

    if registry.fallback:
        # Transitional only: the fallback is an un-updated bot-decision-yt that
        # may still read its data files relative to the cwd.
        ensure_decision_cwd(function.path)

    return function.load()


# ---------------------------------------------------------------------------
# Per-request logging
# ---------------------------------------------------------------------------


def _log(level: int, request_id: str, message: str) -> None:
    """Log one line tagged with the correlation id of the request it belongs to.

    Every line for a request carries the id both in the text (so ``grep`` and
    Loki find it) and as a ``correlation_id`` record attribute.
    """
    logger.log(level, "[req %s] %s", request_id, message, extra={"correlation_id": request_id})


def _trim(value: Any, limit: int = 300) -> str:
    """Render *value* as one truncated line.

    The decision function's ``warnings`` can be several hundred characters with
    embedded newlines; a multi-line log record breaks per-line log shipping.
    """
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "..."


def _correlation_id(event: Any) -> str:
    """Correlation id for this request.

    Read, in order, from the ``X-Correlation-Id`` / ``X-Job-Id`` request headers
    (what ``LambdaCaller`` sends), then from a top-level ``correlation_id`` or
    ``job_id`` key in the posted JSON, then -- only then -- from a generated id.

    Both sources are supported on purpose: the header is the runner's, but the
    decision event is also written to the job's HAR archive, so an id carried in
    the body survives where a header does not.

    A generated id is prefixed ``anon-`` so that a log line whose id cannot be
    joined to a job is recognisable as such at a glance rather than looking like
    a job id nobody can find.
    """
    for header in ("X-Correlation-Id", "X-Job-Id"):
        value = request.headers.get(header)
        if value and value.strip():
            return value.strip()[:120]

    if isinstance(event, dict):
        for key in ("correlation_id", "job_id"):
            value = event.get(key)
            if value not in (None, ""):
                return str(value).strip()[:120]

    generated = f"anon-{uuid.uuid4().hex[:8]}"
    _note_missing_correlation_id(generated)
    return generated


def _note_missing_correlation_id(generated: str) -> None:
    """Say -- once -- that the caller supplied no id, instead of hiding it.

    Correlation-id logging is worthless if nobody notices that the production
    caller never sends one: every request then lands under its own random id and
    joins to nothing, while the logs still *look* correlated. Until the runner
    ships ``X-Correlation-Id`` this is the state of every real request, so it is
    logged at WARNING the first time and at DEBUG afterwards (one line per
    process, not one per decision).
    """
    global _missing_correlation_id_logged

    message = (
        "no correlation id supplied by the caller: none of the X-Correlation-Id / X-Job-Id "
        "headers and no top-level correlation_id / job_id key was present, so these log "
        f"lines cannot be joined to a runner job. Using the generated id {generated}."
    )

    if _missing_correlation_id_logged:
        logger.debug("%s", message)
        return

    _missing_correlation_id_logged = True
    logger.warning("%s This is logged once per process.", message)


def _report_decision(request_id: str, result: Any, elapsed_ms: float) -> None:
    """Log what the decision function actually did, not merely that it answered.

    The decision function returns 200 even when every LLM filter failed, so an
    INFO success line is exactly what a silently degraded treatment arm used to
    look like.

    The gating is the amended contract, and getting it wrong in either direction
    is expensive: ``degraded`` (a *requested* filter failed) is a WARNING, and
    ``treatment_lost`` (a treated bot produced control-arm data) is the ERROR.
    ``selection_mode == "random"`` is neither -- it is the healthy, expected mode
    for a control crawler, which is 82% of this box's traffic.
    """
    if not isinstance(result, dict):
        _log(
            logging.WARNING,
            request_id,
            f"decision returned {type(result).__name__}, expected a dict "
            f"(elapsed_ms={elapsed_ms:.0f})",
        )
        return

    verdict = lambda_verdict(result)
    mode = verdict.selection_mode
    failed = sorted(verdict.failed_filters)
    skipped = verdict.skipped_filters
    warnings = verdict.warnings

    # An unavailable LLM is the usual cause of a lost treatment; name it in the
    # same line rather than making someone correlate two logs.
    llm = result.get("llm")
    llm_note = ""
    if isinstance(llm, dict) and llm.get("available") is False:
        detail = llm.get("error_type") or llm.get("error") or "unknown"
        llm_note = f" llm_unavailable={_trim(detail, 120)}"

    xpath = str(result.get("xpath") or "")
    summary = (
        f"selection_mode={mode or 'unknown'} degraded={verdict.degraded} "
        f"treatment_lost={verdict.treatment_lost} "
        f"report={'present' if verdict.reported else 'absent(legacy)'} "
        f"elapsed_ms={elapsed_ms:.0f} xpath={xpath[:50]!r}{llm_note}"
    )

    if verdict.error:
        _log(
            logging.ERROR,
            request_id,
            f"decision reported an error: {_trim(verdict.error)} ({summary})",
        )

    if verdict.degraded:
        _log(
            logging.WARNING,
            request_id,
            f"decision DEGRADED: a requested filter did not apply. "
            f"failed_filters={failed or 'none'} "
            f"not_requested={skipped or 'none'} "
            f"warnings={_trim(warnings) if warnings else 'none'} ({summary})",
        )

    if verdict.treatment_lost:
        _log(
            logging.ERROR,
            request_id,
            "TREATMENT LOST: this bot was configured to be treated but produced "
            "control-arm data - no requested treatment applied, so this session is "
            f"scientifically void. failed_filters={failed or 'none'} "
            f"warnings={_trim(warnings) if warnings else 'none'} ({summary})",
        )

    if not verdict.degraded:
        if mode == "random":
            # Neutral, and the normal state of a control crawler. Kept at DEBUG
            # on purpose: raising it any higher is the false alarm this contract
            # was amended to remove.
            _log(
                logging.DEBUG,
                request_id,
                "selection_mode=random with no failed filter: no LLM rating was "
                "requested, which is the expected, correct mode for a control crawler",
            )
        _log(logging.INFO, request_id, f"decision completed: {summary}")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _fd_stats() -> dict[str, Any]:
    """Open file descriptors and the soft limit the process runs under.

    This is here because a file-descriptor leak in the decision function's
    OpenAI client (fixed in bot-decision-yt) went undiagnosed for an hour: the
    process silently crossed its 1024 fd ceiling mid-run, after which every
    OpenAI call failed with APIConnectionError and the children/entertainment
    filter stopped applying -- with nothing in the API, the job status, or the
    health check to show it. Surfacing the count here turns that from forensics
    into a glance: ``near_limit`` is the thing to watch.

    Best-effort: never raises, because /health must answer even if these cannot
    be read (a non-Linux host has no /proc/self/fd).
    """
    stats: dict[str, Any] = {"open": None, "soft_limit": None, "near_limit": None}
    try:
        stats["open"] = len(os.listdir("/proc/self/fd"))
    except OSError:
        pass
    try:
        import resource

        stats["soft_limit"] = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except (ImportError, ValueError, OSError):
        pass

    open_n, limit = stats["open"], stats["soft_limit"]
    if isinstance(open_n, int) and isinstance(limit, int) and limit > 0:
        # 80% is early enough to act before calls start failing.
        stats["near_limit"] = open_n >= 0.8 * limit
    return stats


@app.route("/health", methods=["GET"])
def health():
    """Health check.

    Always answers 200 (the caller uses it for liveness), but reports every
    registered function's state, because a package that does not import, or one
    whose own ``health()`` hook reports corrupted data, disables a treatment arm
    without failing anything.

    What that data *is* is the project's business, not this server's: the
    mainstream-media CSV check now lives behind bot-decision-yt's hook. Hosting
    N studies means this file can no longer know any of them.
    """
    registry = get_registry()
    described = registry.describe()
    fds = _fd_stats()

    healthy = registry.healthy(described) and not fds.get("near_limit")

    body: dict[str, Any] = {
        "status": "healthy" if healthy else "degraded",
        "service": "lambda_service",
        "registry": str(registry.source) if registry.source else "absent",
        "default": registry.default,
        "functions": described,
        "open_files": fds,
        # Informational: no function may depend on the cwd any more, but the
        # fallback still chdirs for an un-updated bot-decision-yt.
        "cwd": os.getcwd(),
    }
    if registry.problems:
        body["registry_problems"] = registry.problems

    return jsonify(body)


def _requested_function() -> str | None:
    """The function name the caller asked for, or ``None`` for the default.

    An empty or whitespace header is treated as absent rather than as the name
    ``""``: a header the runner failed to fill in must land on the default, not
    on an unknown-function report that hides the real problem.
    """
    value = request.headers.get(FUNCTION_NAME_HEADER)
    if value and value.strip():
        return value.strip()[:120]
    return None


def _unknown_function_response(request_id: str, message: str):
    """Answer a request for a function this box does not host.

    Deliberately HTTP **200** with the degraded report, not a 4xx/5xx. The
    runner reads the verdict out of the body and writes it onto the job row; a
    non-2xx is a transport failure to it, which it retries and records as a
    failed job rather than as a decision that never applied. ``treatment_lost``
    is true because nothing requested was applied -- whatever this session
    produced, it is not the arm the experiment asked for.
    """
    _log(logging.WARNING, request_id, f"unknown decision function requested: {message}")
    return (
        jsonify(
            {
                "degraded": True,
                "treatment_lost": True,
                "error": message,
                "xpath": "",
            }
        ),
        200,
    )


@app.route("/invoke", methods=["POST"])
def invoke():
    """
    Invoke the Lambda function with the provided payload.

    Expects JSON body matching the Lambda event format.
    Returns JSON response matching Lambda response format.

    Which function runs comes from the ``X-Function-Name`` header; without one,
    the registry's default runs, so a runner built before the header existed
    keeps working unchanged.
    """
    started = time.monotonic()
    event = request.get_json(silent=True)
    request_id = _correlation_id(event)
    requested = _requested_function()

    try:
        # Get the decision function
        select_recommendation = get_decision_function(requested)

        # Get the request payload
        if not request.is_json:
            _log(logging.WARNING, request_id, "rejected: request body was not JSON")
            return (
                jsonify(
                    {
                        "error": "Request must be JSON",
                        "xpath": "",
                    }
                ),
                400,
            )

        keys = sorted(event.keys()) if isinstance(event, dict) else None
        _log(
            logging.INFO,
            request_id,
            f"invoke: video selection requested from function="
            f"{requested or '<default>'} (payload keys={keys})",
        )

        # Call the decision function (it expects a JSON string)
        result = select_recommendation(json.dumps(event))

        elapsed_ms = (time.monotonic() - started) * 1000
        _report_decision(request_id, result, elapsed_ms)

        return jsonify(result)

    except UnknownFunctionError as e:
        return _unknown_function_response(request_id, str(e))
    except ImportError as e:
        elapsed_ms = (time.monotonic() - started) * 1000
        _log(logging.ERROR, request_id, f"import error after {elapsed_ms:.0f} ms: {e}")
        return (
            jsonify(
                {
                    "error": f"Failed to load decision function: {str(e)}",
                    "xpath": "",
                }
            ),
            500,
        )
    except Exception as e:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.error(
            "[req %s] error invoking decision function after %.0f ms: %s",
            request_id,
            elapsed_ms,
            e,
            exc_info=True,
            extra={"correlation_id": request_id},
        )
        return (
            jsonify(
                {
                    "error": str(e),
                    "xpath": "",
                }
            ),
            500,
        )


@app.route("/", methods=["GET"])
def root():
    """Root endpoint with service info."""
    return jsonify(
        {
            "service": "PrimeMover Local Lambda Service",
            "endpoints": {
                "/health": "GET - Health check",
                "/invoke": "POST - Invoke Lambda function",
            },
        }
    )


def main():
    """Run the Flask server."""
    parser = argparse.ArgumentParser(description="Local Lambda Server")
    parser.add_argument("--port", type=int, default=5001, help="Port to listen on")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    parser.add_argument(
        "--threads", type=int, default=16, help="waitress worker threads (production mode)"
    )
    args = parser.parse_args()

    logger.info(f"Starting Local Lambda Server on {args.host}:{args.port}")

    # Resolve the registry and import every function before serving, so a
    # misconfigured box says so at startup rather than on the first decision.
    run_startup_checks()

    # Verify the default decision function can be loaded
    try:
        get_decision_function()
    except (ImportError, UnknownFunctionError) as exc:
        logger.warning("Default decision function not available - /invoke will fail: %s", exc)

    if args.debug:
        # The werkzeug reloader/debugger only exists on app.run(); keep it for
        # local debugging where concurrency and longevity do not matter.
        app.run(host=args.host, port=args.port, debug=True)
        return

    # Production: serve with waitress, not werkzeug's development server.
    #
    # The dev server leaks an epoll fd under concurrency -- reproduced in
    # isolation with a bare threaded Flask app and a trivial route: 0 -> 17 epoll
    # over 90 concurrent requests, climbing monotonically. On this VM, 12 bots
    # calling /invoke concurrently drove the process to 844 leaked epoll fds in a
    # single soak round (round 9), which is what silently disabled the
    # children/entertainment filter once the fd ceiling was hit before
    # LimitNOFILE was raised. waitress uses a fixed thread pool and a single
    # asyncore loop, so it holds no per-connection epoll: verified flat at 0
    # across 200 concurrent requests. This is the actual fix; the client
    # singleton and the raised fd limit only reduced and delayed the leak.
    from waitress import serve

    serve(app, host=args.host, port=args.port, threads=args.threads)


if __name__ == "__main__":
    main()
