#!/bin/bash
# Start PrimeMover Orchestrator monitoring stack

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "Starting PrimeMover Orchestrator..."

# Create .env from example if it doesn't exist
if [ ! -f .env ]; then
    echo "Creating .env from template..."
    cp .env.example .env
    echo "Please review and update .env with your settings"
fi

# Start Docker Compose
docker-compose up -d

echo ""
echo "Waiting for services to start..."
sleep 5

# Check health
echo ""
echo "Checking service health..."
./scripts/health-check.sh

echo ""
echo "=========================================="
echo "PrimeMover Orchestrator is running!"
echo "=========================================="
echo ""
echo "Services:"
echo "  Grafana:     http://localhost:3000 (admin/admin)"
echo "  Loki:        http://localhost:3100"
echo "  Prometheus:  http://localhost:9090"
echo "  Tempo:       http://localhost:3200"
echo ""
echo "CLI Usage:"
echo "  orchestrator status          # Check system status"
echo "  orchestrator logs query      # Query logs"
echo "  orchestrator watch           # Watch logs in real-time"
echo "  orchestrator dashboard       # Open Grafana"
echo ""
