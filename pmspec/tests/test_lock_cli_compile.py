# pmspec/tests/test_lock_cli_compile.py
from pathlib import Path

from click.testing import CliRunner

from pmspec.cli import cli
from pmspec.lock.io import is_lock_document, load_lock

SPEC = """
version: "1.0"
experiment:
  name: CLI
crawlers:
  - name: bot
    session:
      start: "+5m"
      workflow:
        - visit: https://example.com
"""


def _write(tmp_path: Path) -> Path:
    p = tmp_path / "study.yaml"
    p.write_text(SPEC)
    return p


def test_compile_writes_sibling_lock(tmp_path: Path):
    spec = _write(tmp_path)
    out = CliRunner().invoke(cli, ["compile", str(spec), "--seed", "3"])
    assert out.exit_code == 0, out.output
    lock_path = tmp_path / "study.lock.yaml"
    assert is_lock_document(lock_path.read_text())
    assert load_lock(lock_path.read_text()).provenance.seed == 3
    assert "study.lock.yaml" in out.output


def test_compile_without_seed_fails_loudly(tmp_path: Path):
    out = CliRunner().invoke(cli, ["compile", str(_write(tmp_path))])
    assert out.exit_code == 1 and "seed" in out.output


def test_validate_accepts_a_lock(tmp_path: Path):
    spec = _write(tmp_path)
    CliRunner().invoke(cli, ["compile", str(spec), "--seed", "3"])
    out = CliRunner().invoke(cli, ["validate", str(tmp_path / "study.lock.yaml")])
    assert out.exit_code == 0, out.output
    assert "Lock" in out.output and "Crawlers:   1" in out.output


def test_validate_reports_lock_schema_errors(tmp_path: Path):
    bad = tmp_path / "bad.lock.yaml"
    bad.write_text("pmspec_lock: 1\nprovenance: {}\n")
    out = CliRunner().invoke(cli, ["validate", str(bad)])
    assert out.exit_code == 1 and "provenance" in out.output


def test_generate_with_output_flag_prints_the_job_count(tmp_path: Path):
    spec = _write(tmp_path)
    CliRunner().invoke(cli, ["compile", str(spec), "--seed", "3"])
    out_json = tmp_path / "study.json"
    out = CliRunner().invoke(
        cli, ["generate", str(tmp_path / "study.lock.yaml"), "-o", str(out_json)]
    )
    assert out.exit_code == 0, out.output
    assert "Crawlers: 1" in out.output
    assert "Jobs: 1" in out.output


def test_generate_from_lock_resolves_start_at_and_keeps_placeholders(tmp_path: Path):
    spec = (
        _write(tmp_path)
        .read_text()
        .replace(
            "  - name: bot\n",
            '  - name: bot\n    proxy: {type: http, hostname: h, port: 1, username: "${secret:a.u}", password: "${secret:a.p}"}\n',
        )
    )
    (tmp_path / "s.yaml").write_text(spec)
    CliRunner().invoke(cli, ["compile", str(tmp_path / "s.yaml"), "--seed", "1"])
    out = CliRunner().invoke(cli, ["generate", str(tmp_path / "s.lock.yaml")])
    assert out.exit_code == 0, out.output
    assert (
        "${secret:a.u}" in out.output
        and "start_at" in out.output
        and "start_offset" not in out.output
    )
    assert "unresolved" in out.output.lower() or "placeholder" in out.output.lower()


# --- runs locks ------------------------------------------------------------

FLEET = """
version: "1.0"
experiment:
  name: Fleet CLI
crawlers:
  - name: bot
    session:
      start: "+5m"
      workflow:
        - visit: https://example.com
"""

RUNS = """
version: "1.0"
runs_for: fleet.lock.yaml
crawlers:
  - name: bot
    session:
      start: "2026-10-01T10:00:00"
      workflow:
        - visit: https://example.com/day1
"""


def _fleet_and_runs(tmp_path: Path) -> Path:
    (tmp_path / "fleet.yaml").write_text(FLEET)
    out = CliRunner().invoke(cli, ["compile", str(tmp_path / "fleet.yaml"), "--seed", "1"])
    assert out.exit_code == 0, out.output
    (tmp_path / "day.yaml").write_text(RUNS)
    return tmp_path / "day.yaml"


def test_compile_prints_the_kind_and_base_sha_for_a_runs_lock(tmp_path: Path):
    from pmspec.lock.io import sha256_text

    day = _fleet_and_runs(tmp_path)
    out = CliRunner().invoke(cli, ["compile", str(day), "--seed", "2"])
    assert out.exit_code == 0, out.output
    base_sha = sha256_text((tmp_path / "fleet.lock.yaml").read_text())
    assert f"Kind: runs  Base: {base_sha[:12]}" in out.output
    lock = load_lock((tmp_path / "day.lock.yaml").read_text())
    assert lock.kind == "runs" and lock.experiment["name"] == "Fleet CLI"


def test_compile_of_a_fleet_spec_says_nothing_about_a_base(tmp_path: Path):
    spec = _write(tmp_path)
    out = CliRunner().invoke(cli, ["compile", str(spec), "--seed", "3"])
    assert "Kind:" not in out.output and "Base:" not in out.output


def test_preview_on_a_runs_lock_names_the_base(tmp_path: Path):
    from pmspec.lock.io import sha256_text

    day = _fleet_and_runs(tmp_path)
    CliRunner().invoke(cli, ["compile", str(day), "--seed", "2"])
    out = CliRunner().invoke(cli, ["preview", str(tmp_path / "day.lock.yaml")])
    assert out.exit_code == 0, out.output
    base_sha = sha256_text((tmp_path / "fleet.lock.yaml").read_text())
    assert f"Runs lock for Fleet CLI (base {base_sha[:12]})" in out.output
    assert "Crawlers:    1" in out.output and "Queues:      1" in out.output


def test_summary_on_a_runs_lock_names_the_base(tmp_path: Path):
    from pmspec.lock.io import sha256_text

    day = _fleet_and_runs(tmp_path)
    CliRunner().invoke(cli, ["compile", str(day), "--seed", "2"])
    out = CliRunner().invoke(cli, ["summary", str(tmp_path / "day.lock.yaml")])
    assert out.exit_code == 0, out.output
    base_sha = sha256_text((tmp_path / "fleet.lock.yaml").read_text())
    assert f"Runs lock for Fleet CLI (base {base_sha[:12]})" in out.output


def test_validate_accepts_a_runs_spec(tmp_path: Path):
    day = _fleet_and_runs(tmp_path)
    out = CliRunner().invoke(cli, ["validate", str(day)])
    assert out.exit_code == 0, out.output


def test_validate_reports_a_runs_spec_refusal(tmp_path: Path):
    day = _fleet_and_runs(tmp_path)
    day.write_text(
        RUNS.replace("crawlers:", "design:\n  factors:\n    a:\n      levels: [x]\ncrawlers:")
    )
    out = CliRunner().invoke(cli, ["validate", str(day)])
    assert out.exit_code == 1 and "design" in out.output


def test_diff_refuses_a_runs_lock_against_a_fleet_lock(tmp_path: Path):
    day = _fleet_and_runs(tmp_path)
    CliRunner().invoke(cli, ["compile", str(day), "--seed", "2"])
    out = CliRunner().invoke(
        cli, ["diff", str(tmp_path / "day.lock.yaml"), str(tmp_path / "fleet.lock.yaml")]
    )
    assert out.exit_code == 1
    assert "cannot diff a runs lock against a fleet lock" in out.output


def test_preview_on_a_runs_spec_counts_crawlers_and_queues(tmp_path: Path):
    day = _fleet_and_runs(tmp_path)
    out = CliRunner().invoke(cli, ["preview", str(day), "--seed", "2"])
    assert out.exit_code == 0, out.output
    assert "Runs spec for Fleet CLI (base " in out.output
    assert "Crawlers:    1" in out.output
    assert "Queues:      1" in out.output
    assert "Jobs:        1" in out.output


def test_summary_on_a_runs_spec_names_the_base_experiment(tmp_path: Path):
    day = _fleet_and_runs(tmp_path)
    out = CliRunner().invoke(cli, ["summary", str(day), "--seed", "2"])
    assert out.exit_code == 0, out.output
    assert "Unnamed Experiment" not in out.output
    assert "Fleet CLI" in out.output
    assert "0 crawlers" not in out.output.lower()


def test_validate_refuses_a_fleet_spec_with_no_experiment(tmp_path: Path):
    spec = tmp_path / "no_exp.yaml"
    spec.write_text(SPEC.replace("experiment:\n  name: CLI\n", ""))
    out = CliRunner().invoke(cli, ["validate", str(spec)])
    assert out.exit_code == 1
    assert "experiment.name is required" in out.output
