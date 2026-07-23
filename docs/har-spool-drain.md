# HAR spool-and-drain sidecar

Drains the runner's local job-data / HAR buffer to the institute Nextcloud,
off the runner's critical path.

## Why this instead of the in-process AWS S3 write

The runner's `AwsS3StorageService` uploaded each job's data synchronously from a
browser-automation executor thread. On a bad/rotated key it 403'd and (before
the storage short-circuit fix) suppressed the local write too; under load it
blocks job throughput and retry-storms when the store is slow. The sidecar
instead:

- lets the runner write only to fast local disk (`FileStorageService`) and return;
- uploads from a **separate, throttled process** (systemd `CPUQuota` / idle IO /
  `--bwlimit`) that can never contend with the runner or Chrome;
- retries/backfills independently — an outage just grows the local buffer;
- cleans up local files automatically once an upload is confirmed.

## Why aggregate mode (default) for Nextcloud

Nextcloud keeps one DB row per file and is served over WebDAV; hundreds of
thousands of tiny per-job objects degrade its metadata and listing. The drain
tars each batch of **settled** job folders (untouched for `MIN_AGE_MIN` minutes,
i.e. the job has finished writing) into one dated archive and uploads that,
collapsing tens of thousands of objects into a handful per day
(`har/YYYY/MM/DD/har-<ts>.tar.zst`). Bulk re-analysis pulls a date range of
archives and extracts. `MODE=direct` uploads per-job files verbatim — only for
an S3-compatible store, not recommended for Nextcloud at scale.

## Components

| File | Purpose |
|------|---------|
| `scripts/har-drain.sh` | the drain (single-shot, self-locking, `--dry-run`) |
| `infrastructure/systemd/primemover-har-drain.service` | oneshot unit with resource caps |
| `infrastructure/systemd/primemover-har-drain.timer` | runs it every 5 min |
| `infrastructure/har-drain/har-drain.env.template` | tunables (buffer dir, remote, caps) |
| `infrastructure/har-drain/rclone.conf.template` | Nextcloud WebDAV remote |

The real `har-drain.env` and `rclone.conf` are gitignored — only templates are
tracked.

## Setup

1. **Install rclone** (only `rsync` is present today):
   ```bash
   sudo apt-get install -y rclone zstd     # zstd optional; falls back to gzip
   ```
2. **Create a Nextcloud app password** (Settings → Security → *Create new app
   password*, name it `primemover-har-drain`). Never use your login password.
3. **Configure rclone**:
   ```bash
   cd infrastructure/har-drain
   cp rclone.conf.template rclone.conf
   rclone obscure 'THE_APP_PASSWORD'        # paste result as `pass`
   # edit rclone.conf: url/user/pass
   rclone --config rclone.conf lsd nextcloud:   # verify it lists
   ```
4. **Configure the drain**:
   ```bash
   cp har-drain.env.template har-drain.env      # adjust RCLONE_REMOTE, BWLIMIT, BUFFER_CAP_GB
   ```
5. **Dry-run before enabling anything**:
   ```bash
   scripts/har-drain.sh --dry-run
   ```
6. **Install + enable the timer**:
   ```bash
   sudo cp infrastructure/systemd/primemover-har-drain.{service,timer} /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now primemover-har-drain.timer
   systemctl list-timers primemover-har-drain.timer
   journalctl -u primemover-har-drain.service -f
   ```

## Runner-side changes (separate, in primemover_runner)

The sidecar works as-is: `FileStorageService` already writes the buffer, and the
storage short-circuit fix (commit `82d6e72`) ensures the local write happens even
while the dead S3 beans 403. But you should also stop the runner from attempting
the dead in-process S3 write, so jobs stop landing in `WARNING_JOBDATA` and the
retry storms end:

- Remove/disable the two `AwsS3*StorageService` beans in
  `primemover-runner/src/main/java/primemover/config/Config.java` (or gate them
  behind a property), leaving `FileStorageService`. Rebuild.
- Enable recording when you want HAR: `recorder.enabled=true` in
  `application.properties` (verified working — MITM chains through the
  residential proxy). Mind the recorder cert `cert/2024/ca-cert.p12` shelf life.

## Operational notes

- **Buffer cap**: `BUFFER_CAP_GB` only *warns* (never deletes). A growing buffer
  means the drain is behind or Nextcloud is unreachable — data is safe locally.
  Size the disk for `worst-case outage hours × job rate × ~1 MB/job`; there is
  ~30 GB free today. For sustained hundreds-of-thousands/day, use a dedicated
  buffer volume.
- **Footprint**: the drain adds ~20–50 MB RSS (rclone) plus bursty CPU capped at
  50% of one core; the runner JVM gets *lighter* (no in-process PUT). See the
  resource analysis in the deploy notes.
- **Ordering safety**: only folders untouched for `MIN_AGE_MIN` minutes are
  drained, and in aggregate mode a batch's source folders are deleted only after
  its archive upload returns success.
