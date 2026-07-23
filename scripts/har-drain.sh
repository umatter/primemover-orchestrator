#!/bin/bash
#
# har-drain.sh — spool-and-drain sidecar for runner job-data / HAR output.
#
# The runner's FileStorageService writes each job's data (HAR, final source,
# cookies, storage, screenshot) to a local buffer directory
# (export.FileTargetFolder, default out/har/<jobId>/). This script drains that
# buffer to an off-host object store — here a Nextcloud WebDAV remote via rclone
# — and removes the local copy once the upload is confirmed.
#
# Why a sidecar instead of the runner's in-process S3 write:
#   * the runner never blocks browser automation on network storage; a slow or
#     offline store just grows the local buffer instead of failing/slowing jobs
#   * uploads are throttleable (systemd CPUQuota / IOSchedulingClass + --bwlimit)
#     so they never contend with the runner or Chrome on the shared host
#   * one code path, independent retry/backfill, automatic local cleanup
#
# Why AGGREGATE mode is the default for Nextcloud:
#   Nextcloud stores one DB row per file and is served over WebDAV; hundreds of
#   thousands of tiny per-job objects degrade its metadata/listing badly. This
#   script tars each batch of *settled* job folders into a single dated archive
#   and uploads that, collapsing tens of thousands of objects into a handful per
#   day. Bulk re-analysis pulls a date range of archives and extracts. Set
#   MODE=direct to upload per-job files verbatim instead (fine for an
#   S3-compatible store, not recommended for Nextcloud at scale).
#
# This script mutates nothing until configured: it refuses to run without a
# readable rclone config and a remote. Use --dry-run to preview.
#
# Usage:
#   scripts/har-drain.sh [--dry-run] [--once] [--env FILE]
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# ---- configuration (override via env file; see infrastructure/har-drain/har-drain.env.template) ----
ENV_FILE="${HAR_DRAIN_ENV:-$PROJECT_DIR/infrastructure/har-drain/har-drain.env}"
DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --once)    : ;;  # accepted for symmetry; this script is already single-shot
        --env)     shift; ENV_FILE="${1:-$ENV_FILE}" ;;
    esac
done

# Defaults — every one is overridable in the env file.
BUFFER_DIR="${BUFFER_DIR:-/home/primemover/primemover_runner/primemover-runner/out/har}"
STAGING_DIR="${STAGING_DIR:-/home/primemover/primemover-orchestrator/log/har-drain-staging}"
LOG_FILE="${LOG_FILE:-/home/primemover/primemover-orchestrator/log/har-drain.log}"
RCLONE_CONFIG="${RCLONE_CONFIG:-$PROJECT_DIR/infrastructure/har-drain/rclone.conf}"
RCLONE_REMOTE="${RCLONE_REMOTE:-nextcloud:primemover/har}"
MODE="${MODE:-aggregate}"                 # aggregate | direct
MIN_AGE_MIN="${MIN_AGE_MIN:-2}"           # only touch job folders untouched for this long (avoid racing the runner)
BWLIMIT="${BWLIMIT:-20M}"                 # rclone bandwidth cap
TRANSFERS="${TRANSFERS:-2}"
BUFFER_CAP_GB="${BUFFER_CAP_GB:-20}"      # warn (do not delete) when the local buffer exceeds this
MAX_DIRS_PER_ARCHIVE="${MAX_DIRS_PER_ARCHIVE:-2000}"  # cap archive size / memory

if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
fi

mkdir -p "$STAGING_DIR" "$(dirname "$LOG_FILE")"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [har-drain] $*" | tee -a "$LOG_FILE" >&2; }

# ---- single-instance lock so overlapping timer fires never collide ----
exec 9>"$STAGING_DIR/.lock"
if ! flock -n 9; then
    log "another drain run holds the lock; exiting"
    exit 0
fi

# Orphaned staging archives are leftovers from a previous run that died or failed
# after tar but before/at upload. The lock guarantees we are the only instance, so
# anything staged now is safe to discard — the source folders were retained and
# will be re-archived below.
rm -f "$STAGING_DIR"/har-*.tar.* 2>/dev/null || true

rclone_cmd() {
    local extra=()
    [[ "$DRY_RUN" == 1 ]] && extra+=(--dry-run)
    rclone --config "$RCLONE_CONFIG" \
           --bwlimit "$BWLIMIT" --transfers "$TRANSFERS" \
           --low-level-retries 5 --retries 3 --retries-sleep 10s \
           "${extra[@]}" "$@"
}

# ---- preflight ----
if ! command -v rclone >/dev/null 2>&1; then
    log "ERROR: rclone not installed. See docs/har-spool-drain.md (Setup step 1)."
    exit 1
fi
if [[ ! -r "$RCLONE_CONFIG" ]]; then
    log "ERROR: rclone config not readable at $RCLONE_CONFIG. Copy the template and fill it in."
    exit 1
fi
if [[ ! -d "$BUFFER_DIR" ]]; then
    log "buffer dir $BUFFER_DIR does not exist yet (recorder off / no jobs); nothing to do"
    exit 0
fi

# ---- buffer-cap watch (alert only; never drops data) ----
buffer_kb=$(du -sk "$BUFFER_DIR" 2>/dev/null | cut -f1)
cap_kb=$(( BUFFER_CAP_GB * 1024 * 1024 ))
if (( buffer_kb > cap_kb )); then
    log "WARNING: buffer $((buffer_kb/1024/1024))GB exceeds cap ${BUFFER_CAP_GB}GB — the drain is falling behind or the remote is unreachable. Data is retained locally; investigate the remote."
fi

# ---- collect SETTLED job folders (no writes for MIN_AGE_MIN minutes = job complete) ----
mapfile -t settled < <(find "$BUFFER_DIR" -mindepth 1 -maxdepth 1 -type d -mmin +"$MIN_AGE_MIN" -printf '%f\n' 2>/dev/null | sort)
if (( ${#settled[@]} == 0 )); then
    log "no settled job folders to drain"
    exit 0
fi
log "found ${#settled[@]} settled job folder(s) (mode=$MODE, dry_run=$DRY_RUN)"

if [[ "$MODE" == "direct" ]]; then
    # Per-job upload, structure preserved. Simple; not recommended for Nextcloud at scale.
    if rclone_cmd move "$BUFFER_DIR" "$RCLONE_REMOTE/$(date -u +%Y/%m/%d)/" \
            --min-age "${MIN_AGE_MIN}m" --delete-empty-src-dirs; then
        log "direct drain complete"
    else
        log "ERROR: direct drain failed; local buffer retained"
        exit 1
    fi
    exit 0
fi

# ---- AGGREGATE mode ----
# Tar a bounded batch of settled folders into one dated archive, upload it, and
# only then delete exactly the folders that went into that archive.
batch=("${settled[@]:0:$MAX_DIRS_PER_ARCHIVE}")
ts="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$STAGING_DIR/har-$ts.tar.zst"
remote_path="$RCLONE_REMOTE/$(date -u +%Y/%m/%d)/har-$ts.tar.zst"

# Files are already zip-compressed inside; zstd at a low level just bundles cheaply.
tar_compress="zstd -1 -T2"
command -v zstd >/dev/null 2>&1 || { tar_compress="gzip -1"; archive="${archive%.zst}.gz"; remote_path="${remote_path%.zst}.gz"; }

if [[ "$DRY_RUN" == 1 ]]; then
    log "DRY-RUN: would archive ${#batch[@]} folder(s) -> $remote_path and delete them on success"
    exit 0
fi

log "archiving ${#batch[@]} folder(s) -> $(basename "$archive")"
if ! tar -C "$BUFFER_DIR" -cf - "${batch[@]}" | $tar_compress > "$archive"; then
    log "ERROR: archive creation failed; nothing deleted"
    rm -f "$archive"
    exit 1
fi

if ! rclone_cmd copyto "$archive" "$remote_path"; then
    log "ERROR: upload failed; archive staged at $archive, source folders retained"
    exit 1
fi

# Upload confirmed — remove staged archive and the exact source folders it contained.
rm -f "$archive"
for d in "${batch[@]}"; do
    rm -rf "${BUFFER_DIR:?}/${d:?}"
done
log "uploaded $(basename "$remote_path") and removed ${#batch[@]} drained folder(s)"

remaining=$(( ${#settled[@]} - ${#batch[@]} ))
(( remaining > 0 )) && log "$remaining more settled folder(s) will drain on the next run"
exit 0
