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

    # Sum PROPORTIONAL set size (PSS), not RSS. PSS counts each shared page once
    # (split among the processes sharing it), so summing it across a multi-process
    # app gives an honest total. Summing RSS -- what this did before -- counts the
    # shared browser binary, libraries and /dev/shm once PER sub-process, which
    # inflated the browser total ~5x at 19 concurrent browsers (it read 60 GB of
    # "browser" on a 62 GB box while 33 GB was actually free). smaps_rollup is
    # cheap and present on this kernel; fall back to VmRSS where it is unreadable
    # (older kernel or a cross-user process), which at worst restores the old
    # behaviour for that one process rather than failing.
    local total_kb=0 pid pss rss
    for pid in $(pgrep -f "$pattern" 2>/dev/null); do
        pss=$(awk '/^Pss:/{s+=$2} END{print s+0}' "/proc/$pid/smaps_rollup" 2>/dev/null || echo 0)
        if [[ "$pss" -gt 0 ]]; then
            total_kb=$((total_kb + pss))
        else
            rss=$(awk '/^VmRSS:/{print $2}' "/proc/$pid/status" 2>/dev/null || echo 0)
            total_kb=$((total_kb + rss))
        fi
    done

    echo $((total_kb / 1024))
}

get_process_count() {
    local pattern="$1"
    ps aux | grep -E "$pattern" | grep -v grep | wc -l
}

# Count distinct live browser INSTANCES (not sub-processes). Each Multilogin
# Mimic browser runs with a unique --user-data-dir; counting those is the signal
# that matters for churn: at a quiet moment between sessions this should be ~0,
# and a number that climbs over a churn soak is an orphan-process leak.
get_browser_instances() {
    local n
    n=$(ps -eo args | grep -oE -- '--user-data-dir=[^ ]+' | grep -iE 'mimic|mlx|multilogin' | sort -u | wc -l)
    echo "$n"
}

# Disk usage of the paths that grow over weeks: the MLX profile store and the
# runner's log dir. HAR now ships to MinIO off-host, so it is not tracked here.
dir_mb() {
    local d="$1"
    [[ -d "$d" ]] || { echo 0; return; }
    du -sm "$d" 2>/dev/null | awk '{print $1+0}'
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
    local browser_instances
    browser_instances=$(get_browser_instances)

    # Disk (weeks-scale creep)
    local mlx_profiles_mb log_dir_mb root_avail_gb
    mlx_profiles_mb=$(dir_mb "/home/primemover/mlx/profiles")
    log_dir_mb=$(dir_mb "$LOG_DIR")
    root_avail_gb=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')

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
    "_metric": "pss_mb",
    "vnc_mb": $vnc_mem,
    "multilogin_mb": $multilogin_mem,
    "chrome_mb": $chrome_mem,
    "chrome_processes": $chrome_count,
    "runner_mb": $runner_mem,
    "lambda_mb": $lambda_mem,
    "total_browser_mb": $total_browser_mem,
    "browser_instances": $browser_instances
  },
  "disk": {
    "mlx_profiles_mb": $mlx_profiles_mb,
    "log_dir_mb": $log_dir_mb,
    "root_avail_gb": $root_avail_gb
  }
}
EOF
    else
        echo -e "\n${CYAN}=== Memory Monitor - $timestamp ===${NC}\n"

        echo -e "${CYAN}System Memory:${NC}"
        printf "  Total: %'d MB | Used: %'d MB (%d%%) | Available: %'d MB\n" \
            "$total_mem" "$used_mem" "$mem_percent" "$available_mem"

        echo -e "\n${CYAN}Component Memory (PSS -- shared pages counted once):${NC}"

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

        echo -e "\n  ${CYAN}Total Browser:  ${total_browser_mem} MB  (${browser_instances} live instance(s))${NC}"
        echo -e "  ${CYAN}Disk: MLX profiles ${mlx_profiles_mb} MB | logs ${log_dir_mb} MB | root free ${root_avail_gb} GB${NC}"

        # Log to file
        echo "$timestamp | avail:${available_mem}MB | browsers:${total_browser_mem}MB/${browser_instances}inst | runner:${runner_mem}MB | mlxdisk:${mlx_profiles_mb}MB | rootfree:${root_avail_gb}GB" >> "$LOG_FILE"

        # Capacity signal is system available memory, not a per-component RSS
        # sum. The old "browser memory high -> split into batches" warning was
        # OLD-VM guidance that fired on a handful of healthy bots and no longer
        # applies (62 GB host, ~0.85 GB PSS per bot). available_mem is the truth.
        if [[ $available_mem -lt 4000 ]]; then
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
