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


POOL_SPEC = """
version: "1.0"
experiment:
  name: Pooled
design:
  factors:
    arm:
      levels: [control, treated]
      maps_to:
        params.pi: {control: 0, treated: 1}
  replications: 2
data_pools:
  outlets:
    type: csv
    source: outlets.csv
crawlers:
  - from_design: true
    name: "bot_{arm}_{replica:02d}"
    configuration:
      params:
        media_outlet_urls:
          from_pool: outlets
          select: random
          count: 2
    proxy:
      type: http
      hostname: pr.oxylabs.io
      port: 7777
      username: "${secret:oxylabs.u}"
      password: "${secret:oxylabs.p}"
    session:
      start: "+5m"
      workflow:
        - visit: https://example.com
"""


@pytest.fixture
def pool_dir(tmp_path):
    rows = "\n".join(f"domain{i}.com,https://domain{i}.com" for i in range(12))
    (tmp_path / "outlets.csv").write_text(f"domain,url\n{rows}\n")
    return tmp_path


def _urls(crawlers):
    return [c["configuration"]["params"]["media_outlet_urls"] for c in crawlers]


@pytest.mark.parametrize("seed", [7, 0])
def test_compile_and_parse_and_convert_draw_the_same_random_pool_rows(pool_dir, seed):
    """One seed rule for everyone: the parser's pool stream is seed + 1, the
    converter's design stream is seed. A summary (parse_and_convert) and a lock
    (compile_spec) built from the same seed pick the same random outlets --
    and seed 0 is a seed, not "no seed"."""
    from experiment_designer.lock.build import parse_and_convert

    result = compile_spec(POOL_SPEC, base_dir=pool_dir, spec_path="spec.yaml", seed=seed)
    assert result.lock is not None, result.errors
    parsed, converted = parse_and_convert(POOL_SPEC, base_dir=pool_dir, seed=seed)
    assert not converted.errors, converted.errors
    from_lock = _urls(result.lock.crawlers)
    from_parse = _urls(converted.json_data)
    assert from_lock == from_parse
    assert all(len(u) == 2 for u in from_lock)
    # Not a degenerate comparison: a different seed draws differently.
    other = compile_spec(POOL_SPEC, base_dir=pool_dir, spec_path="spec.yaml", seed=seed + 100)
    assert _urls(other.lock.crawlers) != from_lock


@pytest.mark.parametrize("seed", [7, 0])
def test_summary_from_yaml_goes_through_the_one_seed_helper(pool_dir, seed, monkeypatch):
    """The summary side cannot show pool draws (CrawlerSummary keeps only the six
    numeric params), so pin the mechanism: from_yaml_string hands its seed --
    including 0 -- to parse_and_convert, the same helper compile_spec uses."""
    from experiment_designer.lock import build

    seen = []
    real = build.parse_and_convert

    def spy(spec_text, **kw):
        seen.append(kw["seed"])
        return real(spec_text, **kw)

    monkeypatch.setattr(build, "parse_and_convert", spy)
    SummaryGenerator().from_yaml_string(POOL_SPEC, base_dir=pool_dir, seed=seed)
    assert seen == [seed]
    seen.clear()
    SummaryGenerator(seed=seed).from_yaml_string(POOL_SPEC, base_dir=pool_dir)
    assert seen == [seed]
