#!/usr/bin/env bash
# Weekly integrity check of the runner-only box's restic repository.
# Mirrors /opt/ex63-infra/scripts/backup-check.sh: reads a subset of the data
# back from Nextcloud so a silently corrupted repository is noticed in a week,
# not on restore day.
set -uo pipefail
[ "$EUID" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }

ENV_FILE="${BACKUP_ENV:-/etc/primemover-backup.env}"
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a
export RESTIC_PASSWORD
HOST="$(hostname -s)"
export RESTIC_REPOSITORY="rclone:${RESTIC_REMOTE:-nextcloud}:${RESTIC_REMOTE_DIR:-Backups/${HOST}}"
LOG_DIR="${BACKUP_LOG_DIR:-/var/log/primemover-backup}"
LOG_FILE="$LOG_DIR/check.log"
mkdir -p "$LOG_DIR"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "=== restic check --read-data-subset=${CHECK_READ_SUBSET:-2.5%} on $RESTIC_REPOSITORY ==="
restic check --read-data-subset="${CHECK_READ_SUBSET:-2.5%}" >>"$LOG_FILE" 2>&1
rc=$?
[ "$rc" -eq 0 ] && log "Integrity check PASSED." || log "ALERT: restic check FAILED (exit $rc) -- inspect $LOG_FILE."
exit "$rc"
