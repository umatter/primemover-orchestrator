#!/bin/bash
# Stop PrimeMover Orchestrator monitoring stack

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "Stopping PrimeMover Orchestrator..."
docker-compose down

echo "Done."
