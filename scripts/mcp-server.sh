#!/bin/bash
# Run the MCP server for Claude Code integration
#
# Usage:
#   ./mcp-server.sh              # stdio mode (default, for local Claude Code)
#   ./mcp-server.sh --http       # HTTP mode on port 8100, localhost only (for local access)
#   ./mcp-server.sh --http --port 9000   # HTTP mode on custom port
#   ./mcp-server.sh --http --host 0.0.0.0  # HTTP mode, allow external connections
#
# Environment variables:
#   MCP_TRANSPORT=http           # Alternative to --http flag
#   MCP_PORT=8100                # Port for HTTP mode (default: 8100)
#   MCP_HOST=127.0.0.1           # Host to bind to (default: 127.0.0.1, use 0.0.0.0 for external access)
#
# SECURITY NOTE: By default, HTTP mode binds to localhost only. Use --host 0.0.0.0
#                to allow external connections, but ensure proper network security.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR" || { echo "cannot cd to $PROJECT_DIR" >&2; exit 1; }

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# Load environment variables
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Run MCP server with all arguments passed through
exec python3 -m orchestrator.mcp_server.server "$@"
