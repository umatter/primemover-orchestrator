"""
Local Lambda Server - HTTP wrapper for bot-decision-yt Lambda function.

Provides a local HTTP endpoint that mirrors AWS Lambda invocation,
allowing the runner to call the decision function without AWS costs.

Usage:
    python -m orchestrator.lambda_service.server [--port PORT] [--host HOST]

Endpoints:
    GET  /health  - Health check
    POST /invoke  - Invoke the Lambda function (expects JSON body)
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from flask import Flask, request, jsonify

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("lambda_service")

app = Flask(__name__)

# Global reference to the decision function (lazy loaded)
_select_recommendation = None


def get_decision_function():
    """
    Lazy load the select_recommendation function from bot-decision-yt.

    The decision module is symlinked into this package, so we import it
    dynamically to avoid import errors if the symlink doesn't exist yet.
    """
    global _select_recommendation

    if _select_recommendation is not None:
        return _select_recommendation

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
        logger.error("Make sure the 'decision' symlink exists and points to bot-decision-yt/bot-decision-EN")
        raise


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "service": "lambda_service",
    })


@app.route("/invoke", methods=["POST"])
def invoke():
    """
    Invoke the Lambda function with the provided payload.

    Expects JSON body matching the Lambda event format.
    Returns JSON response matching Lambda response format.
    """
    try:
        # Get the decision function
        select_recommendation = get_decision_function()

        # Get the request payload
        if not request.is_json:
            return jsonify({
                "error": "Request must be JSON",
                "xpath": "",
            }), 400

        event = request.json
        logger.info(f"Received invoke request for video selection")
        logger.debug(f"Request payload keys: {list(event.keys()) if event else 'None'}")

        # Call the decision function (it expects a JSON string)
        result = select_recommendation(json.dumps(event))

        logger.info(f"Decision completed, xpath: {result.get('xpath', '')[:50]}...")

        return jsonify(result)

    except ImportError as e:
        logger.error(f"Import error: {e}")
        return jsonify({
            "error": f"Failed to load decision function: {str(e)}",
            "xpath": "",
        }), 500
    except Exception as e:
        logger.error(f"Error invoking decision function: {e}", exc_info=True)
        return jsonify({
            "error": str(e),
            "xpath": "",
        }), 500


@app.route("/", methods=["GET"])
def root():
    """Root endpoint with service info."""
    return jsonify({
        "service": "PrimeMover Local Lambda Service",
        "endpoints": {
            "/health": "GET - Health check",
            "/invoke": "POST - Invoke Lambda function",
        },
    })


def main():
    """Run the Flask server."""
    parser = argparse.ArgumentParser(description="Local Lambda Server")
    parser.add_argument("--port", type=int, default=5001, help="Port to listen on")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    args = parser.parse_args()

    logger.info(f"Starting Local Lambda Server on {args.host}:{args.port}")

    # Verify decision function can be loaded
    try:
        get_decision_function()
    except ImportError:
        logger.warning("Decision function not available - /invoke will fail until fixed")

    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
