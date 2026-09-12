# experiment_designer/tests/test_lock_cli_compile.py
from pathlib import Path

from click.testing import CliRunner

from experiment_designer.cli import cli
from experiment_designer.lock.io import is_lock_document, load_lock

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
