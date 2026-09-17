"""One seed, several streams: the parser's `!file` selections and its data-pool
selections must not draw the same sequence.

Both are seeded from the parser's seed (`parser_seed(seed)` == seed + 1) and both
select with `rng.sample(data, count)`, so over lists of the same length they used
to return *the same rows* -- a `!file {select: random}` and a pool
`{select: random}` over one CSV picked the same terms every time. Correlated
draws in an experiment spec are not a cosmetic bug: two things a design meant to
vary independently move together.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pmspec.lock.build import parser_seed
from pmspec.spec.converter import SpecConverter
from pmspec.spec.parser import SpecParser, stream_seed

SPEC = """
version: "1.0"
experiment:
  name: Streams
data_pools:
  terms:
    source: ./terms.csv
crawlers:
  - name: bot
    configuration:
      params:
        search_terms: !file
          path: ./terms.csv
          select: random
          count: 3
        media_outlet_urls:
          from_pool: terms
          select: random
          count: 3
    session:
      start: "+1m"
      workflow:
        - visit: https://example.com
"""


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    (tmp_path / "terms.csv").write_text("term\n" + "\n".join(f"t{i:02d}" for i in range(10)) + "\n")
    return tmp_path


def _draws(spec_dir: Path, seed: int) -> tuple[list[str], list[str]]:
    parsed = SpecParser(base_dir=spec_dir, seed=parser_seed(seed)).parse(SPEC)
    assert not parsed.errors, parsed.errors
    result = SpecConverter().convert(parsed)
    assert not result.errors, result.errors
    params = result.json_data[0]["configuration"]["params"]
    return (
        [row["term"] for row in params["search_terms"]],
        [row["term"] for row in params["media_outlet_urls"]],
    )


@pytest.mark.parametrize("seed", [0, 7, 42, 1234])
def test_the_file_and_pool_streams_are_not_the_same_sequence(spec_dir, seed):
    files, pools = _draws(spec_dir, seed)
    assert len(files) == len(pools) == 3
    assert files != pools, f"correlated draws: both streams picked {files}"


def test_both_streams_are_still_deterministic(spec_dir):
    assert _draws(spec_dir, 7) == _draws(spec_dir, 7)


def test_a_different_seed_moves_both_streams(spec_dir):
    assert _draws(spec_dir, 7) != _draws(spec_dir, 8)


class TestStreamSeed:
    def test_none_stays_none(self):
        """`seed=None` is "unseeded"; a stream of it must not become a number."""
        assert stream_seed(None, "files") is None

    def test_a_stream_is_a_function_of_the_seed_and_the_name(self):
        assert stream_seed(7, "files") == stream_seed(7, "files")
        assert stream_seed(7, "files") != stream_seed(7, "pools")
        assert stream_seed(7, "files") != stream_seed(8, "files")

    def test_zero_is_a_seed_not_a_falsy_value(self):
        assert isinstance(stream_seed(0, "files"), int)
        assert stream_seed(0, "files") != stream_seed(0, "pools")
