#!/bin/bash
# Check health of all orchestrator services

check_service() {
    local name=$1
    local url=$2

    if curl -s -f "$url" > /dev/null 2>&1; then
        echo "  ✓ $name is healthy"
        return 0
    else
        echo "  ✗ $name is not responding"
        return 1
    fi
}

echo "Service Health Check:"
echo ""

check_service "Grafana" "http://localhost:3000/api/health"
check_service "Loki" "http://localhost:3100/ready"
check_service "Prometheus" "http://localhost:9090/-/healthy"
check_service "Tempo" "http://localhost:3200/ready"

echo ""
