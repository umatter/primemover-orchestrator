"""`summarize_yaml_spec` must report what `validate_experiment_spec` reports.

The MCP summary is the same `SummaryGenerator.from_yaml_string` the CLI uses,
so the domain refusals `SpecValidator` owns -- the 0/1 switch rule, the
strict-contract drops -- have to reach its `validation` block. Otherwise an
agent composing a study gets "success: true" plus a plausible report for a spec
the very next tool call refuses.
"""

from __future__ import annotations

import json

from orchestrator.mcp_server import server as s

VALID = """version: "1.0"
experiment:
  name: "A Usable Study"
crawlers:
  - name: "b1"
    session:
      name: "Session 1"
      start_at: "+1h"
      workflow:
        - visit: https://example.com
        - wait: 5s
"""

FRACTIONAL_PI = """version: "1.0"
experiment:
  name: "Half a treatment"
crawlers:
  - name: "b1"
    configuration:
      params:
        pi: 0.7
    session:
      name: "Session 1"
      start_at: "+1h"
      workflow:
        - visit: https://example.com
"""


def test_a_domain_error_reaches_the_validation_block():
    out = json.loads(s.summarize_yaml_spec(FRACTIONAL_PI))
    assert out["success"] is True  # the tool ran; the spec is what is invalid
    validation = out["summary"]["validation"]
    assert validation["is_valid"] is False
    assert any("is not 0 or 1" in e for e in validation["errors"]), validation["errors"]


def test_a_usable_spec_still_summarises():
    out = json.loads(s.summarize_yaml_spec(VALID))
    validation = out["summary"]["validation"]
    assert validation["is_valid"] is True, validation["errors"]
    assert out["summary"]["summary"]["crawler_count"] == 1
