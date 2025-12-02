#!/bin/bash
# Run the MCP server for Claude Code integration

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

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

# Run MCP server
exec python -m orchestrator.mcp_server.server
