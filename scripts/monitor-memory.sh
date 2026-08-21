#!/bin/bash
#
# Memory Monitor for PrimeMover Components
#
# Monitors memory usage of VNC, Multilogin, and browser processes.
# Logs to file and optionally alerts when thresholds are exceeded.
#
# Usage:
#   ./monitor-memory.sh              # Run once
#   ./monitor-memory.sh --watch      # Continuous monitoring (every 30s)
#   ./monitor-memory.sh --json       # JSON output for programmatic use
#

set -e

LOG_DIR="/home/primemover/primemover-orchestrator/log"
LOG_FILE="$LOG_DIR/memory-monitor.log"
ALERT_THRESHOLD_MB=2000  # Alert if any process exceeds this

mkdir -p "$LOG_DIR"

# Colors
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

get_process_memory() {
    local pattern="$1"
    # $2 is a human label passed by callers for readability at the call site;
    # it is deliberately not bound here, since nothing in this function uses it.

    # Get RSS memory in KB, sum all matching processes
    local mem_kb
    mem_kb=$(ps aux | grep -E "$pattern" | grep -v grep | awk '{sum += $6} END {print sum+0}')
    local mem_mb
    mem_mb=$((mem_kb / 1024))

    echo "$mem_mb"
}

get_process_count() {
    local pattern="$1"
    ps aux | grep -E "$pattern" | grep -v grep | wc -l
}

monitor_once() {
    local json_mode="$1"
    local timestamp
    timestamp=$(date -Iseconds)

    # Get memory for each component
    local vnc_mem
    vnc_mem=$(get_process_memory "Xtigervnc|Xvnc" "VNC")
    local multilogin_mem
    multilogin_mem=$(get_process_memory "multilogin|mlx|desktop.bin|agent.bin" "Multilogin")
    local chrome_mem
    chrome_mem=$(get_process_memory "chrome|chromium" "Chrome")
    local runner_mem
    runner_mem=$(get_process_memory "primemover-runner.*jar" "Runner")
    local lambda_mem
    lambda_mem=$(get_process_memory "lambda_service" "Lambda")
    local total_browser_mem
    total_browser_mem=$((multilogin_mem + chrome_mem))

    # Get process counts
    local chrome_count
    chrome_count=$(get_process_count "chrome|chromium")

    # System memory
    local total_mem
    total_mem=$(free -m | awk '/^Mem:/{print $2}')
    local used_mem
    used_mem=$(free -m | awk '/^Mem:/{print $3}')
    local available_mem
    available_mem=$(free -m | awk '/^Mem:/{print $7}')
    local mem_percent
    mem_percent=$((used_mem * 100 / total_mem))

    if [[ "$json_mode" == "true" ]]; then
        cat << EOF
{
  "timestamp": "$timestamp",
  "system": {
    "total_mb": $total_mem,
    "used_mb": $used_mem,
    "available_mb": $available_mem,
    "percent": $mem_percent
  },
  "components": {
    "vnc_mb": $vnc_mem,
    "multilogin_mb": $multilogin_mem,
    "chrome_mb": $chrome_mem,
    "chrome_processes": $chrome_count,
    "runner_mb": $runner_mem,
    "lambda_mb": $lambda_mem,
    "total_browser_mb": $total_browser_mem
  }
}
EOF
    else
        echo -e "\n${CYAN}=== Memory Monitor - $timestamp ===${NC}\n"

        echo -e "${CYAN}System Memory:${NC}"
        printf "  Total: %'d MB | Used: %'d MB (%d%%) | Available: %'d MB\n" \
            "$total_mem" "$used_mem" "$mem_percent" "$available_mem"

        echo -e "\n${CYAN}Component Memory:${NC}"

        # VNC
        if [[ $vnc_mem -gt $ALERT_THRESHOLD_MB ]]; then
            echo -e "  VNC Server:     ${RED}${vnc_mem} MB${NC} (HIGH)"
        elif [[ $vnc_mem -gt 0 ]]; then
            echo -e "  VNC Server:     ${GREEN}${vnc_mem} MB${NC}"
        else
            echo -e "  VNC Server:     ${YELLOW}not running${NC}"
        fi

        # Multilogin
        if [[ $multilogin_mem -gt $ALERT_THRESHOLD_MB ]]; then
            echo -e "  Multilogin:     ${RED}${multilogin_mem} MB${NC} (HIGH)"
        elif [[ $multilogin_mem -gt 0 ]]; then
            echo -e "  Multilogin:     ${GREEN}${multilogin_mem} MB${NC}"
        else
            echo -e "  Multilogin:     ${YELLOW}not running${NC}"
        fi

        # Chrome
        if [[ $chrome_mem -gt $ALERT_THRESHOLD_MB ]]; then
            echo -e "  Chrome:         ${RED}${chrome_mem} MB${NC} ($chrome_count processes) (HIGH)"
        elif [[ $chrome_mem -gt 0 ]]; then
            echo -e "  Chrome:         ${GREEN}${chrome_mem} MB${NC} ($chrome_count processes)"
        else
            echo -e "  Chrome:         ${YELLOW}not running${NC}"
        fi

        # Runner
        if [[ $runner_mem -gt 0 ]]; then
            echo -e "  Runner (Java):  ${GREEN}${runner_mem} MB${NC}"
        else
            echo -e "  Runner (Java):  ${YELLOW}not running${NC}"
        fi

        # Lambda
        if [[ $lambda_mem -gt 0 ]]; then
            echo -e "  Lambda Server:  ${GREEN}${lambda_mem} MB${NC}"
        else
            echo -e "  Lambda Server:  ${YELLOW}not running${NC}"
        fi

        echo -e "\n  ${CYAN}Total Browser:  ${total_browser_mem} MB${NC}"

        # Log to file
        echo "$timestamp | System: ${used_mem}/${total_mem}MB (${mem_percent}%) | VNC: ${vnc_mem}MB | MLX: ${multilogin_mem}MB | Chrome: ${chrome_mem}MB (${chrome_count}) | Runner: ${runner_mem}MB | Lambda: ${lambda_mem}MB" >> "$LOG_FILE"

        # Alert check
        if [[ $total_browser_mem -gt $ALERT_THRESHOLD_MB ]]; then
            echo -e "\n${RED}WARNING: Browser memory usage is high (${total_browser_mem} MB)${NC}"
            echo -e "${YELLOW}Consider restarting Multilogin or splitting long sessions into batches.${NC}"
        fi

        if [[ $available_mem -lt 1000 ]]; then
            echo -e "\n${RED}WARNING: Low system memory (${available_mem} MB available)${NC}"
        fi
    fi
}

# Parse arguments
WATCH_MODE=false
JSON_MODE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --watch|-w)
            WATCH_MODE=true
            shift
            ;;
        --json|-j)
            JSON_MODE=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--watch] [--json]"
            exit 1
            ;;
    esac
done

if [[ "$WATCH_MODE" == "true" ]]; then
    echo "Memory monitoring started. Press Ctrl+C to stop."
    echo "Logging to: $LOG_FILE"
    while true; do
        clear
        monitor_once "$JSON_MODE"
        sleep 30
    done
else
    monitor_once "$JSON_MODE"
fi
