"""Tests for reading runner job data out of the MinIO object store.

The regression these guard against is silent emptiness: when the runner stopped
writing job data to local disk, every HAR-backed reader returned ``[]`` instead
of failing, so the tooling looked healthy while answering nothing.
"""

import io
import zipfile

import pytest

from orchestrator.logs.jobdata_s3 import JobDataObject, JobDataStore, store_from_settings


class FakeS3Client:
    """Minimal stand-in for the boto3 S3 client, with paging."""

    def __init__(self, objects: dict[str, bytes], page_size: int = 1000):
        self.objects = objects
        self.page_size = page_size
        self.list_calls = 0

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None, MaxKeys=None):  # noqa: N803
        self.list_calls += 1
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken) if ContinuationToken else 0
        page = keys[start : start + self.page_size]
        end = start + len(page)
        response = {"Contents": [{"Key": k, "Size": len(self.objects[k])} for k in page]}
        if end < len(keys):
            response["IsTruncated"] = True
            response["NextContinuationToken"] = str(end)
        return response

    def get_object(self, Bucket, Key):  # noqa: N803
        return {"Body": io.BytesIO(self.objects[Key])}


def _zip_with(name: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, payload)
    return buf.getvalue()


# The real layout: two flat prefixes, job id as the trailing token.
JOB = "lambdaselectjob-youtube_video_select"
SAMPLE = {
    f"reports/lambda-jobdata-{JOB}-12691": b"z1",
    f"reports/static-jobdata-{JOB}-12691": b"z2",
    f"reports/static-captcha-{JOB}-12691": b"z3",
    f"finalsource/20260828-230139-page-finalsource-{JOB}-12691": b"z4",
    f"reports/lambda-jobdata-{JOB}-12687": b"z5",
    "reports/lambda-jobdata-visitjob-NULL-700": b"z6",
}


def make_store(objects=None, page_size=1000, **kwargs):
    client = FakeS3Client(objects if objects is not None else SAMPLE, page_size=page_size)
    store = JobDataStore(
        endpoint="https://s3.qamelab.org",
        bucket="primemoverrunner",
        access_key="key",
        secret_key="secret",
        client=client,
        **kwargs,
    )
    return store, client


class TestJobDataObject:
    @pytest.mark.parametrize(
        "key,expected",
        [
            ("reports/lambda-jobdata-lambdaselectjob-x-1", "lambda-jobdata"),
            ("reports/static-jobdata-lambdaselectjob-x-1", "static-jobdata"),
            ("reports/static-captcha-lambdaselectjob-x-1", "static-captcha"),
            ("finalsource/2026-page-finalsource-lambdaselectjob-sel-1", "page-finalsource"),
            ("reports/something-else-1", "unknown"),
        ],
    )
    def test_kind_from_key(self, key, expected):
        assert JobDataObject(key=key, job_id=1, size=0).kind == expected


class TestJobDataStore:
    def test_not_configured_without_credentials(self):
        store = JobDataStore("https://s3", "bucket", access_key=None, secret_key=None)
        assert store.configured is False

    def test_configured_with_credentials(self):
        store, _ = make_store()
        assert store.configured is True

    def test_lists_across_both_prefixes(self):
        store, _ = make_store()
        assert len(store.list_objects()) == len(SAMPLE)

    def test_skips_keys_without_a_job_id(self):
        objects = dict(SAMPLE)
        objects["reports/no-trailing-id-here"] = b"x"
        store, _ = make_store(objects)
        # The unparseable key is dropped rather than guessed at.
        assert len(store.list_objects()) == len(SAMPLE)

    def test_follows_pagination(self):
        store, client = make_store(page_size=2)
        assert len(store.list_objects()) == len(SAMPLE)
        assert client.list_calls > 2

    def test_listing_is_cached_until_refresh(self):
        store, client = make_store()
        store.list_objects()
        calls = client.list_calls
        store.list_objects()
        assert client.list_calls == calls
        store.list_objects(refresh=True)
        assert client.list_calls > calls

    def test_objects_for_job_matches_only_that_job(self):
        store, _ = make_store()
        keys = {o.key for o in store.objects_for_job(12691)}
        assert len(keys) == 4
        assert all(k.endswith("-12691") for k in keys)

    def test_job_id_12687_not_matched_by_12691_prefix(self):
        """A trailing-token match must not confuse ids that share a prefix."""
        store, _ = make_store()
        assert [o.job_id for o in store.objects_for_job(12687)] == [12687]

    def test_recent_job_ids_descending_and_limited(self):
        store, _ = make_store()
        assert store.recent_job_ids() == [12691, 12687, 700]
        assert store.recent_job_ids(limit=2) == [12691, 12687]

    def test_read_returns_object_bytes(self):
        store, _ = make_store()
        key = f"reports/lambda-jobdata-{JOB}-12691"
        assert store.read(key) == b"z1"


class TestStoreFromSettings:
    def test_parses_prefix_list(self, mock_settings):
        store = store_from_settings(mock_settings)
        assert store.prefixes == ("reports/", "finalsource/")

    def test_falls_back_to_defaults_on_empty_prefixes(self, mock_settings):
        mock_settings.jobdata_s3_prefixes = "  , "
        assert store_from_settings(mock_settings).prefixes == ("reports/", "finalsource/")


class TestAggregatorUsesTheStore:
    def test_lambda_output_read_from_store_when_configured(self, mock_settings, monkeypatch):
        """With credentials set, the reader must not touch the dead local path."""
        from orchestrator.logs.aggregator import LogAggregator

        payload = b'{"selected": {"title": "a video"}, "xpath": ""}'
        objects = {f"reports/lambda-jobdata-{JOB}-12691": _zip_with("output.json", payload)}
        monkeypatch.setattr(mock_settings, "jobdata_s3_access_key", "key", raising=False)
        monkeypatch.setattr(mock_settings, "jobdata_s3_secret_key", "secret", raising=False)

        aggregator = LogAggregator(har_path="/nonexistent/out/har")
        aggregator.jobdata_store = JobDataStore(
            endpoint="https://s3.qamelab.org",
            bucket="primemoverrunner",
            access_key="key",
            secret_key="secret",
            client=FakeS3Client(objects),
        )

        entries = aggregator._query_lambda_output(job_id=12691)
        assert entries, "expected a decision entry from the object store"
        assert all(e.job_id == 12691 for e in entries if e.job_id is not None)

    def test_unreachable_store_returns_empty_not_raises(self, mock_settings):
        from orchestrator.logs.aggregator import LogAggregator

        class Broken:
            configured = True
            endpoint = "https://s3.qamelab.org"

            def recent_job_ids(self, limit=50):
                raise OSError("connection refused")

            def objects_for_job(self, job_id):
                raise OSError("connection refused")

        aggregator = LogAggregator(har_path="/nonexistent/out/har")
        aggregator.jobdata_store = Broken()
        assert aggregator._query_lambda_output() == []


class TestReachable:
    def test_reachable_returns_true_when_the_store_answers(self):
        store, _ = make_store()
        assert store.reachable() is True

    def test_reachable_true_on_an_empty_but_answering_bucket(self):
        # A fresh bucket with nothing under the prefix is reachable, not broken.
        store, _ = make_store(objects={})
        assert store.reachable() is True

    def test_reachable_propagates_a_connection_error(self):
        # doctor catches this and reports the reason; reachable must not swallow it.
        class Broken:
            def list_objects_v2(self, **kw):
                raise OSError("connection refused")

        store = JobDataStore("https://s3", "b", "k", "s", client=Broken())
        import pytest

        with pytest.raises(OSError):
            store.reachable()
