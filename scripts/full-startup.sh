#!/bin/bash
#
# PrimeMover Full Startup Script
#
# Starts all PrimeMover components in the correct order:
# 1. VNC Server (required for Multilogin)
# 2. Docker containers (API, MySQL, Redis, etc.)
# 3. Monitoring stack (Grafana, Loki, Prometheus)
# 4. Multilogin Agent and Desktop
# 5. Lambda Service
# 6. Runner (if not using systemd)
#
# Usage:
#   ./full-startup.sh                  # Start all components
#   ./full-startup.sh --no-runner      # Start without runner
#   ./full-startup.sh --no-multilogin  # Chrome-only dev mode: skip VNC + Multilogin
#                                      # and run the runner with the chrome-dev profile
#   ./full-startup.sh --check          # Only check, don't start
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log() { echo -e "${CYAN}[$(date +%H:%M:%S)]${NC} $1"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }
log_error() { echo -e "${RED}✗${NC} $1"; }
log_warn() { echo -e "${YELLOW}⚠${NC} $1"; }

START_RUNNER=true
CHECK_ONLY=false
# Chrome-only dev mode: no Multilogin account, no proxy, no S3. The runner's
# chrome-dev profile turns Multilogin off and runs headless Chrome, so neither
# the VNC display nor the Multilogin agent/desktop is needed.
USE_MULTILOGIN=true
RUNNER_PROFILES="local"

while [[ $# -gt 0 ]]; do
    case $1 in
        --no-runner)
            START_RUNNER=false
            shift
            ;;
        --no-multilogin)
            USE_MULTILOGIN=false
            RUNNER_PROFILES="local,chrome-dev"
            shift
            ;;
        --check)
            CHECK_ONLY=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo ""
echo -e "${CYAN}╔═══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║              PrimeMover Full Startup                          ║${NC}"
echo -e "${CYAN}╚═══════════════════════════════════════════════════════════════╝${NC}"
echo ""

# =============================================================================
# Step 1: VNC Server
# =============================================================================
log "Step 1: VNC Server"
if [[ "$USE_MULTILOGIN" == "false" ]]; then
    log_success "Skipped (chrome-dev mode: headless Chrome needs no display)"
elif systemctl is-active --quiet vncserver@1; then
    log_success "VNC Server already running"
else
    if [[ "$CHECK_ONLY" == "false" ]]; then
        sudo systemctl start vncserver@1
        sleep 5
        if systemctl is-active --quiet vncserver@1; then
            log_success "VNC Server started"
        else
            log_error "Failed to start VNC Server"
            exit 1
        fi
    else
        log_error "VNC Server not running"
    fi
fi

# =============================================================================
# Step 2: Docker Containers
# =============================================================================
log "Step 2: Docker Containers"

cd "$PROJECT_DIR"

# Check if Docker is running
if ! systemctl is-active --quiet docker; then
    if [[ "$CHECK_ONLY" == "false" ]]; then
        sudo systemctl start docker
        sleep 3
    else
        log_error "Docker not running"
    fi
fi

# Start containers
if [[ "$CHECK_ONLY" == "false" ]]; then
    docker compose up -d
    sleep 10
fi

# Verify containers
for container in primemover-apache primemover-mysql primemover-redis; do
    if docker ps | grep -q "$container"; then
        log_success "$container"
    else
        log_error "$container not running"
    fi
done

# =============================================================================
# Step 3: Monitoring Stack
# =============================================================================
log "Step 3: Monitoring Stack"

for service in grafana loki prometheus; do
    container="pm-orchestrator-$service"
    if docker ps | grep -q "$container"; then
        log_success "$service"
    else
        log_warn "$service not running"
    fi
done

# =============================================================================
# Step 4: Multilogin
# =============================================================================
log "Step 4: Multilogin"

if [[ "$USE_MULTILOGIN" == "false" ]]; then
    log_success "Skipped (chrome-dev mode: the runner does not sign in to Multilogin)"
else  # --- the whole Multilogin step, to the "end Multilogin step" marker below

# Start agent
if pgrep -f "agent.bin" > /dev/null 2>&1; then
    log_success "Multilogin Agent already running"
else
    if [[ "$CHECK_ONLY" == "false" ]]; then
        log "Starting Multilogin Agent..."
        DISPLAY=:1 nohup /opt/mlx/agent.bin > /dev/null 2>&1 &
        sleep 10
        if pgrep -f "agent.bin" > /dev/null 2>&1; then
            log_success "Multilogin Agent started"
        else
            log_error "Failed to start Multilogin Agent"
        fi
    else
        log_error "Multilogin Agent not running"
    fi
fi

# Start desktop
if pgrep -f "desktop.bin" > /dev/null 2>&1; then
    log_success "Multilogin Desktop already running"
else
    if [[ "$CHECK_ONLY" == "false" ]]; then
        log "Starting Multilogin Desktop..."
        DISPLAY=:1 nohup /opt/mlxapp/desktop.bin > /dev/null 2>&1 &
        sleep 15
        if pgrep -f "desktop.bin" > /dev/null 2>&1; then
            log_success "Multilogin Desktop started"
        else
            log_warn "Multilogin Desktop may need manual login via VNC"
        fi
    else
        log_warn "Multilogin Desktop not running"
    fi
fi

# Check launcher API
sleep 5
if curl -sf -k https://launcher.mlx.yt:45001/api/v2 > /dev/null 2>&1; then
    log_success "Multilogin Launcher API responding"
else
    log_warn "Multilogin Launcher API not responding - manual login may be required"
fi

fi  # --- end Multilogin step

# =============================================================================
# Step 5: Lambda Service
# =============================================================================
log "Step 5: Lambda Service"

if pgrep -f "lambda_service.server" > /dev/null 2>&1; then
    log_success "Lambda Service already running"
else
    if [[ "$CHECK_ONLY" == "false" ]]; then
        log "Starting Lambda Service..."
        cd "$PROJECT_DIR"
        source venv/bin/activate
        nohup python -m orchestrator.lambda_service.server --port 5001 --host 127.0.0.1 > log/lambda-server.log 2>&1 &
        sleep 5
        if curl -sf http://127.0.0.1:5001/health > /dev/null 2>&1; then
            log_success "Lambda Service started"
        else
            log_error "Failed to start Lambda Service"
        fi
    else
        log_error "Lambda Service not running"
    fi
fi

# =============================================================================
# Step 6: Runner (optional)
# =============================================================================
if [[ "$START_RUNNER" == "true" ]]; then
    log "Step 6: Runner"

    if pgrep -f "primemover-runner.*jar" > /dev/null 2>&1; then
        log_success "Runner already running"
    else
        if [[ "$CHECK_ONLY" == "false" ]]; then
            log "Starting Runner..."
            RUNNER_DIR="/home/primemover/primemover_runner/primemover-runner"
            cd "$RUNNER_DIR"
            nohup java -Xmx2g -Xms512m -jar target/primemover-runner-*.jar \
                --spring.profiles.active="$RUNNER_PROFILES" \
                --spring.config.location=config/ \
                > log/runner-stdout.log 2>&1 &
            sleep 10
            if pgrep -f "primemover-runner.*jar" > /dev/null 2>&1; then
                log_success "Runner started"
            else
                log_error "Failed to start Runner"
            fi
        else
            log_error "Runner not running"
        fi
    fi
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
log "Startup complete!"
echo ""
echo "Services:"
echo "  Grafana:     http://localhost:3000"
echo "  API:         http://localhost:8090"
echo "  Lambda:      http://localhost:5001"
echo ""
echo "To verify everything works:"
echo "  orchestrator e2e"
echo ""
echo "To monitor memory usage:"
echo "  $SCRIPT_DIR/monitor-memory.sh --watch"
echo ""

# Check if Multilogin needs manual login
if [[ "$USE_MULTILOGIN" == "true" ]] && ! curl -sf -k https://launcher.mlx.yt:45001/api/v2 > /dev/null 2>&1; then
    echo -e "${YELLOW}NOTE: Multilogin may require manual login via VNC.${NC}"
    echo "Connect to VNC and log in to Multilogin X to enable browser automation."
    echo ""
fi
