#!/bin/bash
#
# Reap orphaned Multilogin/Mimic browser processes.
#
# Over a long-running deployment with churn (bots stopping and starting for
# weeks), an occasional failed teardown -- the runner's stopProfile() retry loop
# gives up after ~10 rounds and only logs the failure -- can leave a browser
# process alive with no owning session. Each is ~1.8 GB, so a slow accumulation
# eventually eats the memory headroom. This sweeps them up.
#
# SAFETY: it must never kill a live browser. "Orphan" is defined strictly by AGE:
# a Mimic browser whose main process has been running longer than any real
# session possibly could. Production sessions are bounded (the longest tested is
# ~1h plus teardown), so the default threshold of 120 minutes cannot match a
# browser that is legitimately mid-session. Age is the only signal used, because
# linking a process to a live joblist is fragile and getting it wrong would fail
# a running experiment.
#
# Usage:
#   ./reap-orphan-browsers.sh                 # DRY RUN: list what it would kill
#   ./reap-orphan-browsers.sh --kill          # actually kill (SIGTERM, then SIGKILL)
#   ./reap-orphan-browsers.sh --older-than 90 # change the age threshold (minutes)
#
# Intended to run from cron/systemd-timer every 30-60 min. Dry-run by default so
# a misconfiguration lists rather than kills.

set -euo pipefail

OLDER_THAN_MIN=120
DO_KILL=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --kill) DO_KILL=true; shift ;;
        --older-than) OLDER_THAN_MIN="$2"; shift 2 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

now=$(date +%s)
threshold_s=$((OLDER_THAN_MIN * 60))
killed=0
found=0

# Main browser processes only: the top-level Mimic chrome with a --user-data-dir.
# Sub-processes (--type=renderer/gpu-process/...) die with their parent, so we
# target the roots and let the children go with them.
while read -r pid; do
    [[ -n "$pid" ]] || continue
    args=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
    case "$args" in
        *mimic_151*--user-data-dir=*)
            case "$args" in *--type=*) continue ;; esac  # a child, skip
            ;;
        *) continue ;;
    esac

    start_s=$(stat -c %Y "/proc/$pid" 2>/dev/null || echo "$now")
    age_s=$((now - start_s))
    [[ $age_s -ge $threshold_s ]] || continue

    found=$((found + 1))
    age_min=$((age_s / 60))
    udir=$(printf '%s' "$args" | grep -oE -- '--user-data-dir=[^ ]+' | head -1)
    if [[ "$DO_KILL" == "true" ]]; then
        echo "reaping orphan pid $pid (age ${age_min}m) $udir"
        kill -TERM "$pid" 2>/dev/null || true
        sleep 5
        kill -KILL "$pid" 2>/dev/null || true
        killed=$((killed + 1))
    else
        echo "[dry-run] would reap pid $pid (age ${age_min}m > ${OLDER_THAN_MIN}m) $udir"
    fi
done < <(pgrep -f 'mimic_151' 2>/dev/null || true)

if [[ "$DO_KILL" == "true" ]]; then
    echo "reaped $killed orphan browser(s) older than ${OLDER_THAN_MIN}m"
else
    echo "[dry-run] $found orphan browser(s) older than ${OLDER_THAN_MIN}m; pass --kill to reap"
fi
