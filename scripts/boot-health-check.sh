#!/bin/bash
#
# Boot Health Check Script
#
# This script runs after system boot to verify all PrimeMover components
# are running correctly. It waits for services to start and optionally
# runs an e2e test.
#
# Usage:
#   ./boot-health-check.sh              # Full check with e2e test
#   ./boot-health-check.sh --quick      # Quick check (no e2e test)
#   ./boot-health-check.sh --wait 120   # Custom wait time for services
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="$PROJECT_DIR/log/boot-health-check.log"

# Default settings
WAIT_TIME=90
RUN_E2E=true
NOTIFY_EMAIL=""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    local msg="[$(date -Iseconds)] $1"
    echo -e "$msg"
    echo "$msg" >> "$LOG_FILE"
}

log_success() { log "${GREEN}✓${NC} $1"; }
log_error() { log "${RED}✗${NC} $1"; }
log_warn() { log "${YELLOW}⚠${NC} $1"; }
log_info() { log "${CYAN}→${NC} $1"; }

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --quick)
            RUN_E2E=false
            shift
            ;;
        --wait)
            WAIT_TIME="$2"
            shift 2
            ;;
        --notify)
            NOTIFY_EMAIL="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

log "=========================================="
log "PrimeMover Boot Health Check"
log "=========================================="
log "Started at: $(date)"
log "Wait time: ${WAIT_TIME}s"

# Function to check if a service is running
check_service() {
    local name="$1"
    local check_cmd="$2"

    if eval "$check_cmd" > /dev/null 2>&1; then
        log_success "$name"
        return 0
    else
        log_error "$name"
        return 1
    fi
}

# Wait for services to initialize
log_info "Waiting ${WAIT_TIME}s for services to initialize..."
sleep "$WAIT_TIME"

FAILED=0

# Check VNC Server
log ""
log "Checking infrastructure services..."
if ! check_service "VNC Server" "systemctl is-active --quiet vncserver@1"; then
    ((FAILED++))
fi

# Check Docker containers
if ! check_service "Docker daemon" "systemctl is-active --quiet docker"; then
    ((FAILED++))
fi

# Check API container
if ! check_service "API (Docker)" "docker ps | grep -q primemover-apache"; then
    ((FAILED++))
fi

# Check MySQL container
if ! check_service "MySQL (Docker)" "docker ps | grep -q primemover-mysql"; then
    ((FAILED++))
fi

# Check monitoring stack
log ""
log "Checking monitoring services..."
check_service "Grafana" "curl -sf http://localhost:3000/api/health" || ((FAILED++))
check_service "Loki" "curl -sf http://localhost:3100/ready" || ((FAILED++))
check_service "Prometheus" "curl -sf http://localhost:9090/-/ready" || ((FAILED++))

# Check PrimeMover services
log ""
log "Checking PrimeMover services..."

# Check API health (verify it responds - any HTTP code means it's running)
if ! check_service "PrimeMover API" "curl -s -o /dev/null -w '%{http_code}' http://localhost:8090/ | grep -qE '[0-9]{3}'"; then
    ((FAILED++))
fi

# Check Lambda service
if ! check_service "Lambda Service" "curl -sf http://127.0.0.1:5001/health"; then
    ((FAILED++))
fi

# Check Runner process
if ! check_service "Runner (Java)" "pgrep -f 'primemover-runner.*jar'"; then
    ((FAILED++))
fi

# Check Multilogin
log ""
log "Checking Multilogin..."

# Check agent (pre-v12 only).
#
# Multilogin X v12 embeds the agent in the desktop app: there is no separate
# agent.bin and /opt/mlx does not exist on this host at all -- only /opt/mlxapp.
# This block used to warn, then try to launch /opt/mlx/agent.bin, then count a
# FAILED when that non-existent binary predictably failed to start, so the boot
# health check could never pass no matter how healthy Multilogin was. The real
# functional test is the launcher API check below, which desktop.bin serves.
#
# Kept as a positive-only report so an older standalone-agent install is still
# recognised, without failing the v12 layout.
if pgrep -f "agent.bin" > /dev/null 2>&1; then
    log_success "Multilogin Agent (standalone, pre-v12)"
fi

# Check desktop app
if pgrep -f "desktop.bin" > /dev/null 2>&1; then
    log_success "Multilogin Desktop (process)"
else
    log_warn "Multilogin Desktop not running"
    # Try to start it
    log_info "Attempting to start Multilogin desktop..."
    DISPLAY=:1 nohup /opt/mlxapp/desktop.bin > /dev/null 2>&1 &
    sleep 15
    if pgrep -f "desktop.bin" > /dev/null 2>&1; then
        log_success "Multilogin Desktop started"
    else
        log_error "Failed to start Multilogin Desktop"
        ((FAILED++))
    fi
fi

# Check launcher API.
#
# Liveness only: ANY HTTP response means the launcher is listening and serving.
# This used to require a 2xx from /api/v2 (curl -sf), but that path returns 404
# on Multilogin X v12 -- the launcher answers, just not on that route. The check
# therefore reported "not responding" and told you to log in via VNC while
# Multilogin was working perfectly and running jobs. A version-specific route is
# the wrong thing to gate a boot check on; curl's %{http_code} is 000 only when
# the connection itself fails, which is the condition actually worth alerting on.
launcher_code=$(curl -s -k -m 5 -o /dev/null -w '%{http_code}' \
    https://launcher.mlx.yt:45001/api/v2 2>/dev/null)
if [[ -z "$launcher_code" || "$launcher_code" == "000" ]]; then
    launcher_code=$(curl -s -k -m 5 -o /dev/null -w '%{http_code}' \
        https://127.0.0.1:45001/api/v2 2>/dev/null)
fi

if [[ -n "$launcher_code" && "$launcher_code" != "000" ]]; then
    log_success "Multilogin Launcher API (HTTP $launcher_code)"
else
    log_error "Multilogin Launcher API not responding"
    log_warn "Manual login via VNC may be required"
    ((FAILED++))
fi

# Memory check
log ""
log "Checking system resources..."
AVAILABLE_MEM=$(free -m | awk '/^Mem:/{print $7}')
if [[ $AVAILABLE_MEM -gt 2000 ]]; then
    log_success "Available memory: ${AVAILABLE_MEM} MB"
else
    log_warn "Low available memory: ${AVAILABLE_MEM} MB"
fi

DISK_AVAIL=$(df -h / | awk 'NR==2 {print $4}')
log_success "Available disk: $DISK_AVAIL"

# Run E2E test if requested
if [[ "$RUN_E2E" == "true" ]]; then
    log ""
    log "Running E2E test..."

    cd "$PROJECT_DIR"
    source venv/bin/activate

    if orchestrator e2e --timeout 300 >> "$LOG_FILE" 2>&1; then
        log_success "E2E test passed"
    else
        log_error "E2E test failed"
        ((FAILED++))
    fi
fi

# Summary
log ""
log "=========================================="
if [[ $FAILED -eq 0 ]]; then
    log "${GREEN}All checks passed!${NC}"
    EXIT_CODE=0
else
    log "${RED}$FAILED check(s) failed${NC}"
    EXIT_CODE=1

    # Send notification if configured
    if [[ -n "$NOTIFY_EMAIL" ]]; then
        echo "PrimeMover boot health check failed. $FAILED checks failed. See $LOG_FILE for details." | \
            mail -s "PrimeMover Health Check Failed" "$NOTIFY_EMAIL" 2>/dev/null || true
    fi
fi
log "=========================================="
log "Completed at: $(date)"
log "Log file: $LOG_FILE"

exit $EXIT_CODE
