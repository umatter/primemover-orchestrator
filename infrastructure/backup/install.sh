#!/usr/bin/env bash
# One-time install of the runner-only box backup (mirrors ex63's backup-setup.sh).
#
#   sudo infrastructure/backup/install.sh
#
# Expects, before running:
#   1. an rclone remote "nextcloud" for ROOT:  sudo rclone config  (webdav,
#      url https://<host>/remote.php/dav/files/<user>/, vendor nextcloud, an
#      APP password) -- or copy the [nextcloud] section from another machine's
#      rclone.conf into /root/.config/rclone/rclone.conf (mode 600).
#   2. nothing else: this script creates /etc/primemover-backup.env with a fresh
#      RESTIC_PASSWORD if it does not exist. KEEP A COPY OF THAT PASSWORD in
#      your password manager -- without it the backups are unreadable.
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
cd "$(dirname "$0")"
command -v restic >/dev/null || apt-get install -y -q restic
command -v rclone >/dev/null || apt-get install -y -q rclone

ENV_FILE=/etc/primemover-backup.env
if [ ! -f "$ENV_FILE" ]; then
    umask 077
    printf 'RESTIC_PASSWORD=%s\n# RESTIC_REMOTE=nextcloud\n# RESTIC_REMOTE_DIR=Backups/%s\n' "$(openssl rand -base64 32)" "$(hostname -s)" > "$ENV_FILE"
    echo "created $ENV_FILE with a new RESTIC_PASSWORD -- store it in your password manager: sudo cat $ENV_FILE"
fi
chmod 600 "$ENV_FILE"

rclone listremotes | grep -qx 'nextcloud:' || { echo "rclone remote 'nextcloud' is not configured for root -- see the header of this script"; exit 1; }
rclone lsd nextcloud: >/dev/null && echo "WebDAV login OK."

chmod 755 vm-backup.sh vm-backup-check.sh
for u in primemover-backup primemover-backup-check; do
    install -m 644 "$u.service" "$u.timer" /etc/systemd/system/
done
systemctl daemon-reload
systemctl enable --now primemover-backup.timer primemover-backup-check.timer
systemctl list-timers --no-pager primemover-backup.timer primemover-backup-check.timer
echo "Run the first backup now with:  sudo systemctl start primemover-backup.service && sudo tail -f /var/log/primemover-backup/backup.log"
