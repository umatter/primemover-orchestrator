"""`pmspec summary` on a spec the pipeline cannot use must say so and exit 1.

The report it prints for such a spec is fiction -- "Crawlers: 0 total", "Total
Jobs: 0" -- because nothing was expanded. Printing it under exit 0 makes an
unusable spec look like an empty-but-fine one, which is exactly what the strict
contract exists to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from pmspec.cli import cli

NON_MAPPING_ROOT = "- just\n- a\n- list\n"

NO_NAME = """version: "1.0"
experiment:
  description: "a spec with no name"
crawlers:
  - name: "b1"
    session:
      workflow:
        - visit: https://example.com
"""

UNKNOWN_JOB = """version: "1.0"
experiment:
  name: "Unknown job"
crawlers:
  - name: "b1"
    session:
      workflow:
        - frobnicate: https://example.com
"""

# A runs spec is summarised against its base fleet lock; without one there is
# nothing to summarise, and the same rule applies.
RUNS_WITHOUT_BASE = """version: "1.0"
runs_for: design/nowhere.lock.yaml
crawlers:
  - name: "b1"
    session:
      name: "day 1"
      start_at: "+1h"
      workflow:
        - visit: https://example.com
"""

VALID = """version: "1.0"
experiment:
  name: "A Usable Study"
  description: "one bot, one visit"
crawlers:
  - name: "b1"
    session:
      name: "Session 1"
      start_at: "+1h"
      workflow:
        - visit: https://example.com
        - wait: 5s
"""

FABRICATED = ("Crawlers: 0 total", "Total Jobs: 0")


def _write(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text)
    return str(path)


class TestInvalidSpecExitsOne:
    def test_non_mapping_root(self, tmp_path):
        out = CliRunner().invoke(cli, ["summary", _write(tmp_path, "s.yaml", NON_MAPPING_ROOT)])
        assert out.exit_code == 1, out.output
        assert "YAML root must be an object" in out.output
        for fake in FABRICATED:
            assert fake not in out.output

    def test_missing_experiment_name(self, tmp_path):
        out = CliRunner().invoke(cli, ["summary", _write(tmp_path, "s.yaml", NO_NAME)])
        assert out.exit_code == 1, out.output
        assert "experiment.name is required" in out.output
        for fake in FABRICATED:
            assert fake not in out.output

    def test_unknown_job_type(self, tmp_path):
        out = CliRunner().invoke(cli, ["summary", _write(tmp_path, "s.yaml", UNKNOWN_JOB)])
        assert out.exit_code == 1, out.output
        assert "frobnicate" in out.output
        for fake in FABRICATED:
            assert fake not in out.output

    def test_runs_spec_without_its_base_lock(self, tmp_path):
        out = CliRunner().invoke(cli, ["summary", _write(tmp_path, "s.yaml", RUNS_WITHOUT_BASE)])
        assert out.exit_code == 1, out.output
        assert "nowhere.lock.yaml" in out.output
        for fake in FABRICATED:
            assert fake not in out.output

    def test_it_says_no_summary_is_available(self, tmp_path):
        out = CliRunner().invoke(cli, ["summary", _write(tmp_path, "s.yaml", NO_NAME)])
        assert "Summary not available: spec is invalid" in out.output
        # The errors come first: nobody should have to scroll past a report to
        # find out the report is fiction.
        assert out.output.index("experiment.name is required") < out.output.index(
            "Summary not available"
        )

    def test_json_form_reports_invalid(self, tmp_path):
        out = CliRunner().invoke(
            cli, ["summary", _write(tmp_path, "s.yaml", NO_NAME), "-f", "json"]
        )
        assert out.exit_code == 1, out.output
        payload = json.loads(out.output)
        assert payload["valid"] is False
        assert any("experiment.name is required" in e for e in payload["errors"])
        assert "summary" not in payload

    def test_html_form_writes_no_report(self, tmp_path):
        target = tmp_path / "report.html"
        out = CliRunner().invoke(
            cli,
            ["summary", _write(tmp_path, "s.yaml", NO_NAME), "-f", "html", "-o", str(target)],
        )
        assert out.exit_code == 1, out.output
        assert not target.exists()


class TestValidSpecIsUnchanged:
    def test_text_output_is_the_summary_model_verbatim(self, tmp_path):
        from pmspec.summary import SummaryGenerator

        path = _write(tmp_path, "ok.yaml", VALID)
        out = CliRunner().invoke(cli, ["summary", path])
        assert out.exit_code == 0, out.output
        expected = SummaryGenerator().from_yaml_file(path).to_text()
        assert out.output == expected + "\n"
        assert "Crawlers: 1 total" in out.output

    def test_example_spec_still_passes(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "websearch-study.yaml"
        out = CliRunner().invoke(cli, ["summary", str(example)])
        assert out.exit_code == 0, out.output
        assert "Experiment Summary: Web Search Polarization Study 2025" in out.output
        assert "VALIDATION" not in out.output or "  ERROR:" not in out.output

    def test_json_output_keeps_its_shape(self, tmp_path):
        out = CliRunner().invoke(cli, ["summary", _write(tmp_path, "ok.yaml", VALID), "-f", "json"])
        assert out.exit_code == 0, out.output
        payload = json.loads(out.output)
        assert payload["metadata"]["name"] == "A Usable Study"
        assert payload["summary"]["crawler_count"] == 1
        assert payload["validation"]["is_valid"] is True
        assert "valid" not in payload
