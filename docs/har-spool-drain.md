# Job-data archival to Nextcloud

Copies the runner's job data (HAR, final page source, cookies, lambda payload)
from **MinIO** to the institute Nextcloud, as an off-host archive.

> **This used to be a spool-and-drain sidecar and no longer is.** It drained a
> local buffer at `out/har/<jobId>/` and deleted it after upload. That buffer
> stopped existing when `primemover_runner` `cadb15e` set
> `storage.local.enabled=false` ("run S3-only on the VM"), so the script ran
> against a directory that is never created and shipped nothing — silently,
> because an empty buffer is also what a healthy idle run looks like.
>
> The source is now the MinIO bucket the runner actually writes to. **MinIO is
> the system of record, so this script copies and never deletes.** Retention in
> MinIO is MinIO's business. See "Where job data goes" in `CLAUDE.md` for the
> full destination map — in particular, the runner's `awsS3.*` keys are not AWS.

## Why archive to Nextcloud at all

MinIO lives on the same box as the runner. An archive elsewhere is what makes
job data survive losing that box, and gives collaborators without tailnet
access a route to bulk data. It runs from a **separate, throttled process**
(systemd `CPUQuota` / idle IO / `--bwlimit`) so it can never contend with the
runner or Chrome, and it retries independently — a Nextcloud outage delays the
archive and touches nothing else.

## How the incremental window works

Selection is by watermark: the UTC time of the last **successful** upload, in
`STATE_FILE`, advanced only after the upload is confirmed. A failed run
re-archives rather than skips. `SLACK_MIN` (default 30m) is re-scanned each run
so an object written during the previous run is not missed — duplicates are
cheap, gaps need a manual backfill.

On the first run there is no watermark, so the window is `INITIAL_MAX_AGE`
(default 7d) rather than the whole bucket: it holds tens of thousands of
objects and a full backfill should be a deliberate act. Widen that value, or
delete `STATE_FILE`, when you want one.

## Why aggregate mode (default) for Nextcloud

Nextcloud keeps one DB row per file and is served over WebDAV; hundreds of
thousands of tiny per-job objects degrade its metadata and listing. Each run
copies its window into a staging directory, tars it into one dated archive and
uploads that, collapsing tens of thousands of objects into a handful per day
(`har/YYYY/MM/DD/har-<ts>.tar.zst`). Bulk re-analysis pulls a date range of
archives and extracts. Objects still being written are excluded by
`MIN_AGE_MIN`, so a half-written upload is never archived.

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
   cp har-drain.env.template har-drain.env      # adjust RCLONE_REMOTE, BWLIMIT, INITIAL_MAX_AGE
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

- **Staging cap**: `STAGING_CAP_GB` only *warns* (never deletes). Staging is
  transient — a run copies its window in, tars it, uploads, and removes it — so
  a large staging directory means a run is stuck or Nextcloud is unreachable.
  Job data itself is never at risk: it stays in MinIO, which this never deletes.
- **A failed run loses nothing.** The watermark advances only after a confirmed
  upload, so the same window is re-archived next run. The cost of failure is a
  duplicate archive, never a gap.
- **Footprint**: the drain adds ~20–50 MB RSS (rclone) plus bursty CPU capped at
  50% of one core; the runner JVM gets *lighter* (no in-process PUT). See the
  resource analysis in the deploy notes.
- **Ordering safety**: only folders untouched for `MIN_AGE_MIN` minutes are
  drained, and in aggregate mode a batch's source folders are deleted only after
  its archive upload returns success.
