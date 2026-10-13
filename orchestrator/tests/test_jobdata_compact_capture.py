"""Reading a compact capture (runner ``capture_profile=compact``).

A compact job-data ZIP stores the screenshot as ``.jpg`` and, when the session
storage did not change since an earlier job of the same session, a pointer
``_page_sessionstorage_ref_<id>.json`` = ``{"same_as_job_id", "same_as_object",
"sha256"}`` instead of the content. Following the pointer must give back exactly
the bytes the earlier job stored -- checked against the hash -- or nothing:
never a near miss presented as this job's storage.
"""

import hashlib
import io
import json
import zipfile

import pytest

from orchestrator.debug.job_debugger import JobDebugger
from orchestrator.logs.jobdata_s3 import JobDataStore

FULL_JOB = 717526
POINTER_JOB = 717527
STORAGE = b'{"SerpCache":"' + b"x" * 2000 + b'"}'


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def _pointer(sha: str | None = None, obj: str | None = None, job: int = FULL_JOB) -> bytes:
    return json.dumps(
        {
            "same_as_job_id": job,
            "same_as_object": obj or f"static-jobdata-visitjob-NULL-{FULL_JOB}",
            "sha256": sha or hashlib.sha256(STORAGE).hexdigest(),
        }
    ).encode()


class FakeS3Client:
    def __init__(self, objects):
        self.objects = objects

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None, MaxKeys=None):  # noqa: N803
        keys = [k for k in self.objects if k.startswith(Prefix)]
        return {"Contents": [{"Key": k, "Size": len(self.objects[k])} for k in keys]}

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise KeyError(Key)  # boto3 raises NoSuchKey; any exception will do
        return {"Body": io.BytesIO(self.objects[Key])}


OBJECTS = {
    f"reports/static-jobdata-visitjob-NULL-{FULL_JOB}": _zip(
        {
            f"20261012-1303_page_sessionstorage_{FULL_JOB}.json": STORAGE,
            f"20261012-1303_page_screenshot_{FULL_JOB}.jpg": b"\xff\xd8 fake jpeg",
        }
    ),
    f"reports/static-jobdata-waitjob-NULL-{POINTER_JOB}": _zip(
        {
            f"20261012-1304_page_sessionstorage_ref_{POINTER_JOB}.json": _pointer(),
            f"20261012-1304_page_screenshot_{POINTER_JOB}.jpg": b"\xff\xd8 fake jpeg",
            f"20261012-1304_page_finalsource_{POINTER_JOB}.html": b"<html>serp</html>",
        }
    ),
}


def _store(objects=OBJECTS) -> JobDataStore:
    return JobDataStore(
        "https://s3.qamelab.org", "primemoverrunner", "k", "s", client=FakeS3Client(objects)
    )


class TestResolveSessionStoragePointer:
    def test_follows_the_pointer_to_the_full_copy(self):
        assert _store().resolve_session_storage(_pointer()) == STORAGE

    def test_a_hash_mismatch_is_nothing(self):
        assert _store().resolve_session_storage(_pointer(sha="0" * 64)) is None

    def test_a_missing_object_is_nothing(self):
        assert (
            _store().resolve_session_storage(_pointer(obj="static-jobdata-visitjob-NULL-1")) is None
        )

    def test_an_object_without_the_entry_is_nothing(self):
        # points at the right object but names a job whose entry is not in it
        assert _store().resolve_session_storage(_pointer(job=999)) is None

    def test_a_malformed_pointer_is_nothing(self):
        assert _store().resolve_session_storage(b"not json") is None
        assert _store().resolve_session_storage(b'{"same_as_job_id": 1}') is None


@pytest.fixture
def debugger(mock_settings, tmp_path):
    dbg = JobDebugger(har_path=str(tmp_path / "nonexistent-out-har"))
    dbg.jobdata_store = _store()
    dbg.jobdata_cache = tmp_path / "cache"
    return dbg


class TestDebuggerReadsACompactJob:
    def test_the_pointer_is_resolved_next_to_itself(self, debugger):
        job_dir = debugger._materialise_from_store(POINTER_JOB)

        assert job_dir is not None
        resolved = job_dir / f"20261012-1304_page_sessionstorage_{POINTER_JOB}.json"
        assert resolved.read_bytes() == STORAGE
        # the pointer stays too, so a reader can see the storage was shared
        assert (job_dir / f"20261012-1304_page_sessionstorage_ref_{POINTER_JOB}.json").exists()
        assert (job_dir / f"20261012-1304_page_screenshot_{POINTER_JOB}.jpg").exists()

    def test_an_unresolvable_pointer_leaves_only_the_pointer(self, debugger):
        objects = dict(OBJECTS)
        del objects[f"reports/static-jobdata-visitjob-NULL-{FULL_JOB}"]
        debugger.jobdata_store = _store(objects)

        job_dir = debugger._materialise_from_store(POINTER_JOB)

        assert job_dir is not None
        assert not (job_dir / f"20261012-1304_page_sessionstorage_{POINTER_JOB}.json").exists()
        assert (job_dir / f"20261012-1304_page_sessionstorage_ref_{POINTER_JOB}.json").exists()
