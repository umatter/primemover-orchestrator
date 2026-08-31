"""Tests for `orchestrator debug job` reading artifacts from the object store.

Two silent failures are pinned here, both found while diagnosing a real stuck
bot (experiment 147, job 12691) on 2026-08-29.

1. ``_find_artifacts`` looked only in ``har_path/<job_id>/``, a directory the
   runner stopped creating in ``primemover_runner`` cadb15e. The command
   reported no artifacts, no lambda decision and no HAR -- indistinguishable
   from a job that genuinely produced none.

2. The stored object is the runner's *invocation envelope*, not the decision
   body. Its ``success`` and ``statusCode`` describe the HTTP call, which is 200
   even when every requested filter failed. Reading them as the decision's
   verdict reported ``degraded: true, xpath: ""`` as **"ok"** -- the silent
   success the degradation report exists to prevent, in the tool meant to
   diagnose it.
"""

import io
import json
import zipfile

import pytest

from orchestrator.debug.job_debugger import JobDebugger
from orchestrator.logs.jobdata_s3 import JobDataStore

# The real degradation report from job 12691, in the envelope the runner stores.
DECISION_BODY = {
    "xpath": "",
    "degraded": True,
    "treatment_lost": False,
    "selection_mode": "random",
    "warnings": [
        "UserWarning (line 1277): No recommendation selected.",
        "UserWarning (line 1344): Output not assembled.",
    ],
}
ENVELOPE = {
    "success": True,  # the HTTP call succeeded; the decision did not
    "statusCode": 200,
    "functionError": None,
    "payload": json.dumps(DECISION_BODY),
}


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return buf.getvalue()


class FakeS3Client:
    def __init__(self, objects):
        self.objects = objects

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):  # noqa: N803
        keys = [k for k in self.objects if k.startswith(Prefix)]
        return {"Contents": [{"Key": k, "Size": len(self.objects[k])} for k in keys]}

    def get_object(self, Bucket, Key):  # noqa: N803
        return {"Body": io.BytesIO(self.objects[Key])}


JOB = 12691
OBJECTS = {
    f"reports/lambda-jobdata-lambdaselectjob-sel-{JOB}": _zip(
        {f"2026_lambda_output_jobdata_{JOB}.json": json.dumps(ENVELOPE).encode()}
    ),
    f"reports/static-jobdata-lambdaselectjob-sel-{JOB}": _zip(
        {
            f"2026_trafic_har_{JOB}.json": b'{"log": {"entries": []}}',
            f"2026_page_screenshot_{JOB}.png": b"\x89PNG fake",
            f"2026_page_cookies_{JOB}.json": b"[]",
        }
    ),
    # Routinely zero bytes in production; must not break materialisation.
    f"reports/static-captcha-lambdaselectjob-sel-{JOB}": b"",
}


@pytest.fixture
def debugger(mock_settings, tmp_path, monkeypatch):
    dbg = JobDebugger(har_path=str(tmp_path / "nonexistent-out-har"))
    dbg.jobdata_store = JobDataStore(
        endpoint="https://s3.qamelab.org",
        bucket="primemoverrunner",
        access_key="k",
        secret_key="s",
        client=FakeS3Client(OBJECTS),
    )
    dbg.jobdata_cache = tmp_path / "cache"
    return dbg


class TestUnwrapInvocation:
    def test_unwraps_the_runner_envelope(self):
        assert JobDebugger.unwrap_invocation(ENVELOPE)["degraded"] is True

    def test_leaves_a_bare_decision_body_alone(self):
        assert JobDebugger.unwrap_invocation(DECISION_BODY) == DECISION_BODY

    def test_a_non_json_payload_falls_back_to_the_original(self):
        data = {"payload": "not json at all", "statusCode": 200}
        assert JobDebugger.unwrap_invocation(data) == data

    def test_a_non_dict_payload_falls_back_to_the_original(self):
        data = {"payload": json.dumps([1, 2, 3])}
        assert JobDebugger.unwrap_invocation(data) == data


class TestMaterialiseFromStore:
    def test_lays_out_the_files_the_parsers_expect(self, debugger):
        job_dir = debugger._materialise_from_store(JOB)
        assert job_dir is not None
        names = sorted(p.name for p in job_dir.iterdir())
        # The three globs _find_artifacts uses must all hit.
        assert any(n.endswith(".zip") and "lambda" in n for n in names), names
        assert any(n.endswith(".har") for n in names), names
        assert any(n.endswith(".png") for n in names), names

    def test_zero_byte_objects_are_skipped_not_fatal(self, debugger):
        # static-captcha is empty in production for most jobs.
        assert debugger._materialise_from_store(JOB) is not None

    def test_returns_none_when_the_store_is_unconfigured(self, debugger):
        debugger.jobdata_store = JobDataStore("https://s3", "b", None, None)
        assert debugger._materialise_from_store(JOB) is None

    def test_returns_none_when_the_store_has_nothing_for_the_job(self, debugger):
        assert debugger._materialise_from_store(999999) is None

    def test_an_unreachable_store_returns_none_rather_than_raising(self, debugger):
        class Broken(JobDataStore):
            def objects_for_job(self, job_id):
                raise OSError("connection refused")

        debugger.jobdata_store = Broken("https://s3", "b", "k", "s")
        assert debugger._materialise_from_store(JOB) is None


class TestArtifactsAndDecision:
    def test_find_artifacts_falls_back_to_the_store(self, debugger):
        artifacts = debugger._find_artifacts(JOB)
        assert artifacts.job_dir is not None
        assert artifacts.har_file is not None
        assert artifacts.lambda_zip is not None
        assert artifacts.screenshot_files

    def test_a_degraded_decision_is_not_reported_as_ok(self, debugger):
        """The regression that matters: HTTP 200 is not a verdict."""
        artifacts = debugger._find_artifacts(JOB)
        decision = debugger._extract_lambda_decision(artifacts.lambda_zip)
        assert decision.success is False, "a degraded decision must not read as ok"
        assert decision.selection_mode == "random"
        assert decision.warnings
