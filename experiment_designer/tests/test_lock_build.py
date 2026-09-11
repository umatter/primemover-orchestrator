"""compile: spec + seed -> lock. Deterministic, refuses what cannot be committed."""

import subprocess
from pathlib import Path

import pytest

from experiment_designer.lock.build import compile_spec
from experiment_designer.lock.io import dump_lock
from experiment_designer.lock.provenance import git_commit

SPEC = """
version: "1.0"
experiment:
  name: Compiled
  generator: build_spec.R
design:
  factors:
    arm:
      levels: [control, treated]
      maps_to:
        params.pi: {control: 0, treated: 1}
  parameters:
    alpha:
      distribution: uniform
      min: 0
      max: 1
  replications: 2
data_pools:
  outlets:
    type: csv
    source: outlets.csv
crawlers:
  - from_design: true
    name: "bot_{arm}_{replica:02d}"
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
        - wait: 2s
"""


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    (tmp_path / "outlets.csv").write_text("domain,lean\nnyt.com,L\nfox.com,R\n")
    (tmp_path / "spec.yaml").write_text(SPEC)
    return tmp_path


def _compile(spec_dir: Path, text: str = SPEC, **kw):
    kw.setdefault("seed", 42)
    return compile_spec(text, base_dir=spec_dir, spec_path="spec.yaml", **kw)


def test_compile_is_byte_deterministic(spec_dir: Path):
    a = _compile(spec_dir)
    b = _compile(spec_dir)
    assert not a.errors and a.lock is not None
    a.lock.provenance.compiled_at = b.lock.provenance.compiled_at = "fixed"
    assert dump_lock(a.lock) == dump_lock(b.lock)


def test_different_seed_changes_draws_not_structure(spec_dir: Path):
    a = _compile(spec_dir, seed=1).lock
    b = _compile(spec_dir, seed=2).lock
    names = lambda lk: [c["name"] for c in lk.crawlers]  # noqa: E731
    draws = lambda lk: [c["configuration"]["params"]["alpha"] for c in lk.crawlers]  # noqa: E731
    assert names(a) == names(b) and len(names(a)) == 4
    assert draws(a) != draws(b)


def test_unseeded_compile_is_refused(spec_dir: Path):
    result = _compile(spec_dir, seed=None)
    assert result.lock is None
    assert any("seed" in e for e in result.errors)


def test_design_seed_is_used_when_no_flag(spec_dir: Path):
    text = SPEC.replace("design:\n", "design:\n  seed: 7\n")
    result = _compile(spec_dir, text, seed=None)
    assert result.lock is not None and result.lock.provenance.seed == 7


def test_literal_credential_is_refused(spec_dir: Path):
    text = SPEC.replace('password: "${secret:oxylabs.p}"', 'password: "hunter2"')
    result = _compile(spec_dir, text)
    assert result.lock is None
    assert any("crawlers[0].proxy.password" in e for e in result.errors)


def test_relative_starts_become_offsets_from_upload(spec_dir: Path):
    lock = _compile(spec_dir).lock
    assert lock.schedule == {"anchor": "upload"}
    assert {q["start_offset"] for c in lock.crawlers for q in c["queues"]} == {"+5m"}
    assert all("start_at" not in q for c in lock.crawlers for q in c["queues"])


def test_mixed_starts_are_a_compile_error(spec_dir: Path):
    text = SPEC + """
  - name: fixed_bot
    session:
      start: "2026-10-01T10:00:00"
      workflow:
        - wait: 1s
"""
    result = _compile(spec_dir, text)
    assert result.lock is None and any("mixed" in e for e in result.errors)


def test_provenance_header(spec_dir: Path):
    lock = _compile(spec_dir).lock
    p = lock.provenance
    assert p.spec_path == "spec.yaml"
    assert len(p.spec_sha256) == 64
    assert p.generator == "build_spec.R"
    assert p.seed == 42 and p.lax is False
    assert p.git_commit is None  # tmp_path is not a repo


def test_generator_flag_overrides_spec(spec_dir: Path):
    lock = _compile(spec_dir, generator="hand").lock
    assert lock.provenance.generator == "hand"


def test_git_commit_reports_dirty(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    assert git_commit(tmp_path) is not None and not git_commit(tmp_path).endswith(  # type: ignore[union-attr]
        "-dirty"
    )
    (tmp_path / "x").write_text("x")
    assert git_commit(tmp_path).endswith("-dirty")  # type: ignore[union-attr]


def test_git_commit_none_outside_a_repo(tmp_path: Path):
    assert git_commit(tmp_path) is None


def test_experiment_block_and_runner(spec_dir: Path):
    text = SPEC.replace("  name: Compiled\n", "  name: Compiled\n  runner_user_id: 3\n")
    lock = _compile(spec_dir, text).lock
    assert lock.experiment == {"name": "Compiled", "description": "", "runner_user_id": 3}
