"""Read runner job data from the object store it is actually written to.

The runner writes every job's HAR, page source and lambda payload as a zip to
an S3-compatible store. Despite the ``awsS3.*`` config keys this is not AWS:
``awsS3.endpoint`` in the runner's ``secrets.properties`` is
``https://s3.qamelab.org``, which resolves to the box itself and terminates at
the local MinIO.

Local disk copies stopped in ``primemover_runner`` cadb15e, which set
``storage.local.enabled=false`` to run "S3-only on the VM". The write moved and
no reader followed: ``LogAggregator`` still looked in
``out/har``, a directory that is never created any more, so every HAR-backed
query returned an empty list rather than an error.

**The layout is flat, not a directory per job.** Objects live under two
prefixes and carry the job id as the last dash-separated token::

    reports/lambda-jobdata-lambdaselectjob-youtube_video_select-12691
    reports/static-jobdata-lambdaselectjob-youtube_video_select-12691
    reports/static-captcha-lambdaselectjob-youtube_video_select-12691
    finalsource/20260828-230139-page-finalsource-lambdaselectjob-...-12691

There is no prefix that selects one job, so finding a job's objects means
listing and matching the suffix. Listings are cached per instance because a
debug session asks for several jobs in a row and the bucket already holds tens
of thousands of keys.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    pass


@dataclass(frozen=True)
class JobDataObject:
    """One stored object, with the job it belongs to."""

    key: str
    job_id: int
    size: int

    @property
    def kind(self) -> str:
        """Coarse type of the object, from its name.

        ``lambda-jobdata`` carries the decision payload, ``page-finalsource``
        the rendered HTML, ``static-jobdata`` the HAR.
        """
        name = self.key.rsplit("/", 1)[-1]
        for marker in ("lambda-jobdata", "static-jobdata", "static-captcha", "page-finalsource"):
            if marker in name:
                return marker
        return "unknown"


# The job id is the trailing token: "...-youtube_video_select-12691".
_JOB_ID_SUFFIX = re.compile(r"-(\d+)$")


class JobDataStore:
    """Lists and fetches runner job data from the MinIO bucket.

    Constructed with explicit values rather than reading settings itself so
    tests can drive it without touching the settings cache.
    """

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key: str | None,
        secret_key: str | None,
        prefixes: tuple[str, ...] = ("reports/", "finalsource/"),
        client: Any | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.prefixes = prefixes
        self._client = client
        self._listing: list[JobDataObject] | None = None

    @property
    def configured(self) -> bool:
        """True when credentials are present, so the store can be used."""
        return bool(self.access_key and self.secret_key)

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3  # lazy: keeps CLI startup fast, matches the CLI convention

            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
            )
        return self._client

    def list_objects(self, refresh: bool = False) -> list[JobDataObject]:
        """List every job-data object under the configured prefixes.

        Objects whose name does not end in a job id are skipped rather than
        guessed at -- an unparseable key is not evidence about any job.
        """
        if self._listing is not None and not refresh:
            return self._listing

        client = self._get_client()
        found: list[JobDataObject] = []
        for prefix in self.prefixes:
            token: str | None = None
            while True:
                kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
                if token:
                    kwargs["ContinuationToken"] = token
                response = client.list_objects_v2(**kwargs)
                for item in response.get("Contents", []):
                    key = item["Key"]
                    match = _JOB_ID_SUFFIX.search(key)
                    if not match:
                        continue
                    found.append(
                        JobDataObject(key=key, job_id=int(match.group(1)), size=item.get("Size", 0))
                    )
                if not response.get("IsTruncated"):
                    break
                token = response.get("NextContinuationToken")
                if not token:
                    break

        self._listing = found
        return found

    def objects_for_job(self, job_id: int) -> list[JobDataObject]:
        """Every stored object belonging to one job."""
        return [obj for obj in self.list_objects() if obj.job_id == job_id]

    def recent_job_ids(self, limit: int = 50) -> list[int]:
        """Job ids present in the store, highest first.

        Ids ascend with time, so this stands in for the mtime ordering the
        local-directory reader used.
        """
        return sorted({obj.job_id for obj in self.list_objects()}, reverse=True)[:limit]

    def reachable(self) -> bool:
        """Cheap liveness probe for `orchestrator doctor`.

        One list call capped at a single key -- confirms the endpoint answers
        and the credentials are accepted, without pulling the whole bucket
        (which holds tens of thousands of objects). Raises are the caller's to
        catch; a False here would hide the reason.
        """
        client = self._get_client()
        prefix = self.prefixes[0] if self.prefixes else ""
        resp = client.list_objects_v2(Bucket=self.bucket, Prefix=prefix, MaxKeys=1)
        return "Contents" in resp or resp.get("KeyCount", 0) == 0

    def read(self, key: str) -> bytes:
        """Fetch one object's bytes."""
        response = self._get_client().get_object(Bucket=self.bucket, Key=key)
        body = response["Body"]
        try:
            data: bytes = body.read()
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        return data


def store_from_settings(settings: Any) -> JobDataStore:
    """Build a store from a Settings instance.

    Read with ``getattr`` defaults rather than attribute access: callers pass
    lightweight settings stand-ins (a ``SimpleNamespace`` carrying only the few
    fields a test needs), and a store that is merely unconfigured must not
    become an ``AttributeError`` in code that never touches job data.
    """
    raw_prefixes = getattr(settings, "jobdata_s3_prefixes", "") or ""
    prefixes = tuple(p.strip() for p in raw_prefixes.split(",") if p.strip())
    return JobDataStore(
        endpoint=getattr(settings, "jobdata_s3_endpoint", "https://s3.qamelab.org"),
        bucket=getattr(settings, "jobdata_s3_bucket", "primemoverrunner"),
        access_key=getattr(settings, "jobdata_s3_access_key", None),
        secret_key=getattr(settings, "jobdata_s3_secret_key", None),
        prefixes=prefixes or ("reports/", "finalsource/"),
    )
