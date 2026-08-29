#!/bin/bash
#
# har-drain.sh — off-host archival of runner job-data / HAR output.
#
# WHAT CHANGED, AND WHY THE NAME NO LONGER QUITE FITS
# ---------------------------------------------------
# This script used to drain a local buffer: the runner's FileStorageService
# wrote each job's data to out/har/<jobId>/, and this moved it off-host and
# deleted the local copy.
#
# That buffer no longer exists. primemover_runner cadb15e set
# storage.local.enabled=false ("run S3-only on the VM"), so the local writer
# bean is never constructed and out/har is never created. This script therefore
# ran against an empty directory and shipped nothing — silently, because "no
# settled job folders to drain" is also what a healthy idle run looks like.
#
# The job data goes to the object store the runner config calls "awsS3", which
# is NOT AWS: awsS3.endpoint is https://s3.qamelab.org, the MinIO on this box.
# So the source here is now that bucket.
#
# The consequence worth understanding: MinIO is the system of record, not a
# buffer. This script is an ARCHIVER, not a drain — it copies, and it never
# deletes from the source. Retention in MinIO is MinIO's business.
#
# Why aggregate (tar) mode is still the default for Nextcloud:
#   Nextcloud stores one DB row per file and is served over WebDAV; hundreds of
#   thousands of tiny per-job objects degrade its metadata/listing badly. Each
#   run bundles one batch into a single dated archive. Bulk re-analysis pulls a
#   date range and extracts.
#
# Incremental selection is by watermark: the UTC time of the last successful
# upload, kept in STATE_FILE and advanced ONLY after the upload is confirmed. A
# failed run therefore re-archives rather than skips. SLACK_MIN is re-scanned
# each time so an object written during the previous run is not missed.
#
# This script mutates nothing until configured: it refuses to run without a
# readable rclone config. Use --dry-run to preview.
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
# SOURCE_REMOTE is an rclone S3 remote pointing at the MinIO bucket the runner
# writes to (awsS3.bucketName=primemoverrunner).
SOURCE_REMOTE="${SOURCE_REMOTE:-minio:primemoverrunner}"
STAGING_DIR="${STAGING_DIR:-/home/primemover/primemover-orchestrator/log/har-drain-staging}"
STATE_FILE="${STATE_FILE:-$STAGING_DIR/.last-archived}"
LOG_FILE="${LOG_FILE:-/home/primemover/primemover-orchestrator/log/har-drain.log}"
RCLONE_CONFIG="${RCLONE_CONFIG:-$PROJECT_DIR/infrastructure/har-drain/rclone.conf}"
RCLONE_REMOTE="${RCLONE_REMOTE:-nextcloud:primemover/har}"
MIN_AGE_MIN="${MIN_AGE_MIN:-2}"           # ignore objects still being written
SLACK_MIN="${SLACK_MIN:-30}"              # re-scan window, so a run never leaves a gap
INITIAL_MAX_AGE="${INITIAL_MAX_AGE:-7d}"  # first run only: how far back to reach
BWLIMIT="${BWLIMIT:-20M}"                 # rclone bandwidth cap
TRANSFERS="${TRANSFERS:-2}"
STAGING_CAP_GB="${STAGING_CAP_GB:-20}"    # warn (do not delete) when staging exceeds this

if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
fi

mkdir -p "$STAGING_DIR" "$(dirname "$LOG_FILE")"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [har-drain] $*" | tee -a "$LOG_FILE" >&2; }

# ---- single-instance lock so overlapping timer fires never collide ----
exec 9>"$STAGING_DIR/.lock"
if ! flock -n 9; then
    log "another archive run holds the lock; exiting"
    exit 0
fi

# Leftovers from a run that died after staging. The lock guarantees we are the
# only instance, so discarding them is safe: the watermark was not advanced, so
# the same objects are selected again below.
rm -rf "${STAGING_DIR:?}/batch" 2>/dev/null || true
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
if ! rclone --config "$RCLONE_CONFIG" lsd "$SOURCE_REMOTE" >/dev/null 2>&1; then
    log "ERROR: source $SOURCE_REMOTE not reachable. Check the [minio] remote in $RCLONE_CONFIG (endpoint https://s3.qamelab.org) and that the bucket exists."
    exit 1
fi

# ---- staging-cap watch (alert only; never drops data) ----
staging_kb=$(du -sk "$STAGING_DIR" 2>/dev/null | cut -f1)
cap_kb=$(( STAGING_CAP_GB * 1024 * 1024 ))
if (( staging_kb > cap_kb )); then
    log "WARNING: staging $((staging_kb/1024/1024))GB exceeds cap ${STAGING_CAP_GB}GB — uploads are failing or falling behind; investigate the Nextcloud remote."
fi

# ---- select the window to archive ----
# --max-age reaches back from now. On the first run there is no watermark, so
# the window is INITIAL_MAX_AGE rather than the whole bucket: that bucket holds
# tens of thousands of objects and a full first pass is a deliberate act, not a
# default. Widen INITIAL_MAX_AGE (or delete STATE_FILE) to backfill further.
if [[ -f "$STATE_FILE" ]] && last_epoch=$(cat "$STATE_FILE" 2>/dev/null) && [[ "$last_epoch" =~ ^[0-9]+$ ]]; then
    now_epoch=$(date -u +%s)
    age_sec=$(( now_epoch - last_epoch + SLACK_MIN * 60 ))
    max_age="${age_sec}s"
else
    max_age="$INITIAL_MAX_AGE"
    log "no watermark at $STATE_FILE; first run archives the last $INITIAL_MAX_AGE"
fi

run_started=$(date -u +%s)
batch_dir="$STAGING_DIR/batch"
mkdir -p "$batch_dir"

log "copying objects newer than $max_age (settled ${MIN_AGE_MIN}m) from $SOURCE_REMOTE"
if ! rclone_cmd copy "$SOURCE_REMOTE" "$batch_dir" \
        --max-age "$max_age" --min-age "${MIN_AGE_MIN}m"; then
    log "ERROR: copy from $SOURCE_REMOTE failed; watermark not advanced, nothing uploaded"
    rm -rf "${batch_dir:?}"
    exit 1
fi

if [[ "$DRY_RUN" == 1 ]]; then
    log "DRY-RUN: would archive the copied window to $RCLONE_REMOTE and advance the watermark"
    rm -rf "${batch_dir:?}"
    exit 0
fi

object_count=$(find "$batch_dir" -type f | wc -l)
if (( object_count == 0 )); then
    log "no new objects in the window; nothing to archive"
    rm -rf "${batch_dir:?}"
    # Advance anyway: an empty window is a successful, complete run.
    echo "$run_started" > "$STATE_FILE"
    exit 0
fi

# ---- archive and upload ----
ts="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$STAGING_DIR/har-$ts.tar.zst"
remote_path="$RCLONE_REMOTE/$(date -u +%Y/%m/%d)/har-$ts.tar.zst"

# Objects are already zip-compressed inside; zstd at a low level just bundles cheaply.
tar_compress="zstd -1 -T2"
if ! command -v zstd >/dev/null 2>&1; then
    tar_compress="gzip -1"
    archive="${archive%.zst}.gz"
    remote_path="${remote_path%.zst}.gz"
fi

log "archiving $object_count object(s) -> $(basename "$archive")"
if ! tar -C "$batch_dir" -cf - . | $tar_compress > "$archive"; then
    log "ERROR: archive creation failed; watermark not advanced"
    rm -f "$archive"; rm -rf "${batch_dir:?}"
    exit 1
fi

if ! rclone_cmd copyto "$archive" "$remote_path"; then
    log "ERROR: upload failed; archive staged at $archive, watermark not advanced (this window re-archives next run)"
    rm -rf "${batch_dir:?}"
    exit 1
fi

# Upload confirmed. Only staging is removed — the MinIO objects are the system
# of record and are never deleted from here.
rm -f "$archive"
rm -rf "${batch_dir:?}"
echo "$run_started" > "$STATE_FILE"
log "uploaded $(basename "$remote_path") ($object_count object(s)); source retained in $SOURCE_REMOTE"
exit 0
