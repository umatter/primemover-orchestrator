#!/usr/bin/env bash
# Nightly whole-machine restic snapshot of a runner-only box -> Nextcloud.
#
# Mirrors /opt/ex63-infra/scripts/backup.sh on ex63 (same tool, same remote
# kind, same retention), minus the database dumps: a runner-only box has no
# database, and its job data lives in ex63's MinIO (which ex63's own backup
# covers). What this protects is the box's *state*: runner + orchestrator
# config and secrets, the Multilogin profile cache, systemd units, Promtail
# config -- everything needed to rebuild the box without redoing
# docs/new-server-setup.md by hand.
#
# Requires, as root:
#   - an rclone remote named "nextcloud" (WebDAV) in /root/.config/rclone/rclone.conf
#   - /etc/primemover-backup.env with RESTIC_PASSWORD (and optional overrides)
# Install with infrastructure/backup/install.sh. Run manually: sudo vm-backup.sh
set -uo pipefail
[ "$EUID" -eq 0 ] || { echo "run as root (sudo): a whole-machine backup has to read every user's files" >&2; exit 1; }

ENV_FILE="${BACKUP_ENV:-/etc/primemover-backup.env}"
[ -r "$ENV_FILE" ] || { echo "missing $ENV_FILE (RESTIC_PASSWORD=...)" >&2; exit 1; }
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a
case "${RESTIC_PASSWORD:-}" in ""|change-me*) echo "Set a real RESTIC_PASSWORD in $ENV_FILE. Losing it = losing the backups; keep a copy in your password manager." >&2; exit 1;; esac
export RESTIC_PASSWORD

HOST="$(hostname -s)"
REMOTE="${RESTIC_REMOTE:-nextcloud}"
REMOTE_DIR="${RESTIC_REMOTE_DIR:-Backups/${HOST}}"
export RESTIC_REPOSITORY="rclone:${REMOTE}:${REMOTE_DIR}"
export RESTIC_COMPRESSION="${RESTIC_COMPRESSION:-auto}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXCLUDES="${BACKUP_EXCLUDES:-$SCRIPT_DIR/vm-backup-excludes.txt}"
LOG_DIR="${BACKUP_LOG_DIR:-/var/log/primemover-backup}"
LOG_FILE="$LOG_DIR/backup.log"
mkdir -p "$LOG_DIR"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "${HOST} backup -> ${RESTIC_REPOSITORY}"
if ! rclone listremotes | grep -qx "${REMOTE}:"; then
    log "ERROR: rclone remote '${REMOTE}' not configured for root. See infrastructure/backup/README.md"
    exit 1
fi
if ! restic cat config >/dev/null 2>>"$LOG_FILE"; then
    log "repo not initialised -- running restic init"
    restic init >>"$LOG_FILE" 2>&1 || { log "ERROR: restic init failed"; exit 1; }
fi

log "Starting restic backup of / (excludes: $EXCLUDES)"
restic backup / \
    --one-file-system \
    --exclude-file "$EXCLUDES" \
    --exclude-caches \
    --tag nightly \
    --host "$HOST" \
    >>"$LOG_FILE" 2>&1
backup_rc=$?
if [ "$backup_rc" -ne 0 ] && [ "$backup_rc" -ne 3 ]; then
    # 3 = "some files could not be read" (warnings, snapshot still made)
    log "ERROR: restic backup exited $backup_rc -- skipping prune."
    exit "$backup_rc"
fi
[ "$backup_rc" -eq 3 ] && log "WARNING: restic reported unreadable files (exit 3); snapshot created."

log "Applying retention (daily ${KEEP_DAILY:-14} / weekly ${KEEP_WEEKLY:-8} / monthly ${KEEP_MONTHLY:-6})"
restic forget \
    --keep-daily "${KEEP_DAILY:-14}" \
    --keep-weekly "${KEEP_WEEKLY:-8}" \
    --keep-monthly "${KEEP_MONTHLY:-6}" \
    --prune >>"$LOG_FILE" 2>&1 || log "WARNING: restic forget/prune exited $?"

restic snapshots --latest 1 --host "$HOST" >>"$LOG_FILE" 2>&1
log "Done."
