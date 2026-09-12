"""§6: spec -> lock -> summary equals spec -> summary, for every example spec that compiles."""

from pathlib import Path

import pytest

from experiment_designer.lock.build import compile_spec
from experiment_designer.summary.generator import SummaryGenerator

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
COMPILABLE = [
    "simple-distribution-study.yaml",
    "websearch-study.yaml",
]  # the two that pass strict validation


@pytest.mark.parametrize("name", COMPILABLE)
def test_spec_and_lock_summaries_agree(name):
    path = EXAMPLES / name
    gen = SummaryGenerator()
    from_spec = gen.from_yaml_file(path, seed=7)
    result = compile_spec(path.read_text(), base_dir=path.parent, spec_path=name, seed=7)
    assert result.lock is not None, result.errors
    from_lock = gen.from_lock(result.lock)
    assert [c.name for c in from_lock.crawlers] == [c.name for c in from_spec.crawlers]
    assert (
        from_lock.total_jobs == from_spec.total_jobs
        and from_lock.total_queues == from_spec.total_queues
    )
    assert from_lock.jobs_by_type == from_spec.jobs_by_type
    assert [c.params for c in from_lock.crawlers] == [c.params for c in from_spec.crawlers]
    assert from_lock.design_seed == 7


def test_lock_summary_text_names_its_source():
    path = EXAMPLES / COMPILABLE[0]
    result = compile_spec(path.read_text(), base_dir=path.parent, spec_path="x", seed=7)
    text = SummaryGenerator().from_lock(result.lock).to_text()
    assert "lock" in text.lower() and "seed" in text.lower()


def test_cli_preview_and_summary_accept_a_lock(tmp_path):
    from click.testing import CliRunner

    from experiment_designer.cli import cli
    from experiment_designer.lock.io import dump_lock

    path = EXAMPLES / COMPILABLE[0]
    result = compile_spec(path.read_text(), base_dir=path.parent, spec_path="x", seed=7)
    lock_path = tmp_path / "s.lock.yaml"
    lock_path.write_text(dump_lock(result.lock))
    out = CliRunner().invoke(cli, ["preview", str(lock_path)])
    assert out.exit_code == 0 and "Crawlers" in out.output and "anchor" in out.output.lower()
    out = CliRunner().invoke(cli, ["summary", str(lock_path)])
    assert out.exit_code == 0, out.output
