"""
Local Lambda Server - HTTP wrapper for bot-decision-yt Lambda function.

Provides a local HTTP endpoint that mirrors AWS Lambda invocation,
allowing the runner to call the decision function without AWS costs.

Usage:
    python -m orchestrator.lambda_service.server [--port PORT] [--host HOST]

Endpoints:
    GET  /health  - Health check (reports the resolved cwd and CSV integrity)
    POST /invoke  - Invoke the Lambda function (expects JSON body)

Working directory
-----------------
``lambda_function.py`` reads ``mainstream_media_channels.csv`` *relative to the
process cwd*, mirroring how AWS Lambda runs a function from its task root. Any
other cwd silently disables the mainstream-media-aversion treatment arm: the
read raises, an ``except Exception`` inside the decision function degrades it to
a warning, and the job still returns HTTP 200 with plausible recommendations --
the treated crawler simply behaves like a control.

This server therefore chdirs into the decision directory itself
(:func:`ensure_decision_cwd`) rather than trusting whoever started it. That is
not belt-and-braces: on the production VM the ``primemover-lambda`` unit's
``WorkingDirectory`` pointed at the orchestrator repo, where the CSV does not
exist, so the treatment arm had never worked under systemd.

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
import sys
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

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

#: The symlink into ``bot-decision-yt/bot-decision-EN``. Both the decision code
#: and its data files live here, and the process cwd must be this directory.
DECISION_DIR = Path(__file__).resolve().parent / "decision"

#: Data file backing the mainstream-media-aversion treatment arm.
CSV_NAME = "mainstream_media_channels.csv"

# Global reference to the decision function (lazy loaded)
_select_recommendation = None

# Whether the resolved cwd has already been announced at INFO once.
_cwd_announced = False

# Whether the "caller supplied no correlation id" gap has been reported once.
_missing_correlation_id_logged = False


# ---------------------------------------------------------------------------
# Startup checks: working directory and data-file integrity
# ---------------------------------------------------------------------------


def ensure_decision_cwd(decision_dir: Path | None = None) -> Path | None:
    """Make the decision directory the process cwd.

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


def run_startup_checks() -> dict[str, Any]:
    """chdir into the decision directory and validate its data files."""
    cwd = ensure_decision_cwd()
    csv_state = check_mainstream_media_csv()
    _log_csv_state(csv_state)
    return {"cwd": str(cwd) if cwd else None, "mainstream_media_csv": csv_state}


def get_decision_function():
    """
    Lazy load the select_recommendation function from bot-decision-yt.

    The decision module is symlinked into this package, so we import it
    dynamically to avoid import errors if the symlink doesn't exist yet.
    """
    global _select_recommendation

    if _select_recommendation is not None:
        return _select_recommendation

    # The decision function reads its data files relative to the cwd, so fix the
    # cwd before the module is imported or called - not only in main(), because
    # a WSGI deployment never runs main().
    ensure_decision_cwd()

    # Try to import from the symlinked decision module
    try:
        # Add the decision directory to path if needed
        decision_path = Path(__file__).parent / "decision"
        if decision_path.exists() and str(decision_path) not in sys.path:
            sys.path.insert(0, str(decision_path))

        from lambda_function import select_recommendation

        _select_recommendation = select_recommendation
        logger.info("Successfully loaded select_recommendation function")
        return _select_recommendation
    except ImportError as e:
        logger.error(f"Failed to import lambda_function: {e}")
        logger.error(
            "Make sure the 'decision' symlink exists and points to bot-decision-yt/bot-decision-EN"
        )
        raise


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

    Always answers 200 (the caller uses it for liveness), but reports the
    resolved cwd and the CSV verdict, because a wrong cwd or a corrupted CSV
    disables a treatment arm without failing anything.
    """
    csv_state = check_mainstream_media_csv()
    cwd = os.getcwd()
    expected_cwd = str(DECISION_DIR.resolve()) if DECISION_DIR.exists() else None
    fds = _fd_stats()

    healthy = csv_state.get("ok") and cwd == expected_cwd and not fds.get("near_limit")

    return jsonify(
        {
            "status": "healthy" if healthy else "degraded",
            "service": "lambda_service",
            "cwd": cwd,
            "expected_cwd": expected_cwd,
            "cwd_ok": cwd == expected_cwd,
            "decision_dir": str(DECISION_DIR),
            "mainstream_media_csv": csv_state,
            "open_files": fds,
        }
    )


@app.route("/invoke", methods=["POST"])
def invoke():
    """
    Invoke the Lambda function with the provided payload.

    Expects JSON body matching the Lambda event format.
    Returns JSON response matching Lambda response format.
    """
    started = time.monotonic()
    event = request.get_json(silent=True)
    request_id = _correlation_id(event)

    try:
        # Get the decision function
        select_recommendation = get_decision_function()

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
        _log(logging.INFO, request_id, f"invoke: video selection requested (payload keys={keys})")

        # Call the decision function (it expects a JSON string)
        result = select_recommendation(json.dumps(event))

        elapsed_ms = (time.monotonic() - started) * 1000
        _report_decision(request_id, result, elapsed_ms)

        return jsonify(result)

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
    args = parser.parse_args()

    logger.info(f"Starting Local Lambda Server on {args.host}:{args.port}")

    # Fix the cwd and validate the data files before anything imports or calls
    # the decision function.
    run_startup_checks()

    # Verify decision function can be loaded
    try:
        get_decision_function()
    except ImportError:
        logger.warning("Decision function not available - /invoke will fail until fixed")

    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
