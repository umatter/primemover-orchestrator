"""
Tests for Phase 4 - External Data Integration features.

Tests cover:
- File loading (CSV, JSON, YAML) with !file tag
- Parameter distributions (uniform, discrete, normal, gumbel, etc.)
- Data pools with scoring expressions
- Stratified sampling with proportional factor levels
- Safe expression evaluation

Run with: python -m pytest pmspec/tests/test_external_data.py -v
Or: python pmspec/tests/test_external_data.py
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from pmspec.spec.data_pools import DataPoolManager
from pmspec.spec.design_expander import DesignExpander
from pmspec.spec.distributions import (
    BetaDistribution,
    DiscreteDistribution,
    DistributionFactory,
    FixedValue,
    GumbelDistribution,
    NormalDistribution,
    ParameterSampler,
    TruncatedNormalDistribution,
    UniformDistribution,
)
from pmspec.spec.expression import ExpressionError, SafeExpressionEvaluator
from pmspec.spec.file_loader import FileLoader, FileReference
from pmspec.spec.parser import SpecParser


class TestFileLoader(unittest.TestCase):
    """Tests for FileLoader."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.loader = FileLoader(base_dir=Path(self.temp_dir), seed=42)

    def test_load_csv_file(self):
        """Test loading a CSV file."""
        csv_path = Path(self.temp_dir) / "test.csv"
        csv_path.write_text("name,value,pi\nitem1,100,-0.5\nitem2,200,0.3\n")

        ref = FileReference(path="test.csv")
        data = self.loader.load(ref)

        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["name"], "item1")
        self.assertEqual(data[0]["value"], 100)
        self.assertEqual(data[0]["pi"], -0.5)

    def test_load_json_file(self):
        """Test loading a JSON file."""
        json_path = Path(self.temp_dir) / "test.json"
        json_path.write_text(
            json.dumps([{"name": "item1", "value": 100}, {"name": "item2", "value": 200}])
        )

        ref = FileReference(path="test.json")
        data = self.loader.load(ref)

        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["name"], "item1")

    def test_load_yaml_file(self):
        """Test loading a YAML file."""
        yaml_path = Path(self.temp_dir) / "test.yaml"
        yaml_path.write_text("""
- name: item1
  value: 100
- name: item2
  value: 200
""")

        ref = FileReference(path="test.yaml")
        data = self.loader.load(ref)

        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["name"], "item1")

    def test_select_random(self):
        """Test random selection from file."""
        csv_path = Path(self.temp_dir) / "test.csv"
        csv_path.write_text("name\n" + "\n".join(f"item{i}" for i in range(100)))

        ref = FileReference(path="test.csv", select="random", count=10)
        data = self.loader.load(ref)

        self.assertEqual(len(data), 10)

    def test_select_first(self):
        """Test first N selection from file."""
        csv_path = Path(self.temp_dir) / "test.csv"
        csv_path.write_text("name\nitem1\nitem2\nitem3\nitem4\nitem5\n")

        ref = FileReference(path="test.csv", select="first", count=3)
        data = self.loader.load(ref)

        self.assertEqual(len(data), 3)
        self.assertEqual(data[0]["name"], "item1")
        self.assertEqual(data[2]["name"], "item3")

    def test_filter_records(self):
        """Test filtering records by field value."""
        csv_path = Path(self.temp_dir) / "test.csv"
        csv_path.write_text("name,region\nitem1,US\nitem2,EU\nitem3,US\n")

        ref = FileReference(path="test.csv", filter={"region": "US"})
        data = self.loader.load(ref)

        self.assertEqual(len(data), 2)
        self.assertTrue(all(d["region"] == "US" for d in data))

    def test_column_mapping(self):
        """Test column renaming."""
        csv_path = Path(self.temp_dir) / "test.csv"
        csv_path.write_text("old_name,old_value\nitem1,100\n")

        ref = FileReference(path="test.csv", columns={"old_name": "name", "old_value": "value"})
        data = self.loader.load(ref)

        self.assertEqual(data[0]["name"], "item1")
        self.assertEqual(data[0]["value"], 100)


class TestDistributions(unittest.TestCase):
    """Tests for probability distributions."""

    def test_uniform_distribution(self):
        """Test uniform distribution sampling."""
        dist = UniformDistribution(min=-1, max=1)
        import random

        rng = random.Random(42)

        samples = [dist.sample(rng) for _ in range(1000)]

        self.assertTrue(all(-1 <= s <= 1 for s in samples))
        # Check roughly uniform
        mean = sum(samples) / len(samples)
        self.assertAlmostEqual(mean, 0, delta=0.15)

    def test_discrete_distribution(self):
        """Test discrete distribution sampling."""
        dist = DiscreteDistribution(values={0: 0.25, 1: 0.25, 2: 0.50})
        import random

        rng = random.Random(42)

        samples = [dist.sample(rng) for _ in range(1000)]

        # Count occurrences
        counts = {0: 0, 1: 0, 2: 0}
        for s in samples:
            counts[s] += 1

        # Check roughly proportional
        self.assertAlmostEqual(counts[2] / len(samples), 0.5, delta=0.1)

    def test_normal_distribution(self):
        """Test normal distribution sampling."""
        dist = NormalDistribution(mean=0.35, std=0.1)
        import random

        rng = random.Random(42)

        samples = [dist.sample(rng) for _ in range(1000)]

        mean = sum(samples) / len(samples)
        self.assertAlmostEqual(mean, 0.35, delta=0.05)

    def test_gumbel_distribution(self):
        """Test Gumbel distribution sampling."""
        dist = GumbelDistribution(mu=0, beta=1)
        import random

        rng = random.Random(42)

        samples = [dist.sample(rng) for _ in range(1000)]

        # Gumbel distribution has mean = mu + beta * euler_gamma
        # Euler's constant is approximately 0.5772
        expected_mean = 0 + 1 * 0.5772
        mean = sum(samples) / len(samples)
        self.assertAlmostEqual(mean, expected_mean, delta=0.15)

    def test_truncated_normal_distribution(self):
        """Test truncated normal distribution sampling."""
        dist = TruncatedNormalDistribution(mean=0.5, std=0.5, min=0, max=1)
        import random

        rng = random.Random(42)

        samples = [dist.sample(rng) for _ in range(1000)]

        # All samples should be within bounds
        self.assertTrue(all(0 <= s <= 1 for s in samples))

    def test_beta_distribution(self):
        """Test beta distribution sampling."""
        dist = BetaDistribution(alpha=2, beta=5)
        import random

        rng = random.Random(42)

        samples = [dist.sample(rng) for _ in range(1000)]

        # Beta(2,5) has mean = alpha/(alpha+beta) = 2/7 ≈ 0.286
        mean = sum(samples) / len(samples)
        self.assertAlmostEqual(mean, 2 / 7, delta=0.05)

    def test_fixed_value(self):
        """Test fixed value distribution."""
        dist = FixedValue(value=42)
        import random

        rng = random.Random(42)

        samples = [dist.sample(rng) for _ in range(10)]

        self.assertTrue(all(s == 42 for s in samples))

    def test_distribution_factory(self):
        """Test creating distributions from config dicts."""
        # Uniform
        dist = DistributionFactory.create({"distribution": "uniform", "min": -1, "max": 1})
        self.assertIsInstance(dist, UniformDistribution)

        # Discrete
        dist = DistributionFactory.create(
            {"distribution": "discrete", "values": {"0": 0.5, "1": 0.5}}
        )
        self.assertIsInstance(dist, DiscreteDistribution)

        # Simple value
        dist = DistributionFactory.create(42)
        self.assertIsInstance(dist, FixedValue)

    def test_discrete_distribution_rejects_non_numeric_proportion(self):
        """A bad proportion is an actionable ValueError, not a re-raise/TypeError."""
        # Non-numeric proportion: the error must name both the key and the value
        # so the user can find the offending line in a large `values:` mapping.
        with self.assertRaises(ValueError) as ctx:
            DistributionFactory.create({"distribution": "discrete", "values": {"0.5": "abc"}})
        self.assertIn("0.5", str(ctx.exception))
        self.assertIn("abc", str(ctx.exception))

        # Int-ish key takes the other branch; same contract.
        with self.assertRaises(ValueError) as ctx:
            DistributionFactory.create({"distribution": "discrete", "values": {"0": "abc"}})
        self.assertIn("abc", str(ctx.exception))

        # `values: {"0":}` in YAML yields None, which used to escape as TypeError.
        with self.assertRaises(ValueError):
            DistributionFactory.create({"distribution": "discrete", "values": {"0": None}})

        # Key-conversion semantics are unchanged: a non-numeric key is kept as-is.
        dist = DistributionFactory.create(
            {"distribution": "discrete", "values": {"1.2.3": 1.0, "control": 1.0}}
        )
        self.assertIsInstance(dist, DiscreteDistribution)
        self.assertEqual(sorted(dist.values), ["1.2.3", "control"])

        # Via the sampler, it surfaces as a collected error rather than raising.
        sampler = ParameterSampler(seed=42)
        sampler.add_parameter("kappa", {"distribution": "discrete", "values": {"0.5": "abc"}})
        self.assertEqual(len(sampler.errors), 1)
        self.assertIn("kappa", sampler.errors[0])
        self.assertIn("0.5", sampler.errors[0])

    def test_parameter_sampler(self):
        """Test sampling multiple parameters at once."""
        sampler = ParameterSampler(seed=42)
        sampler.add_parameters(
            {
                "pi": {"distribution": "uniform", "min": -1, "max": 1},
                "kappa": {"distribution": "discrete", "values": {0: 0.5, 1: 0.5}},
                "tau": 1.0,  # Fixed value
            }
        )

        values = sampler.sample()

        self.assertIn("pi", values)
        self.assertIn("kappa", values)
        self.assertIn("tau", values)
        self.assertEqual(values["tau"], 1.0)

    def test_reproducible_sampling(self):
        """Test that same seed produces same results."""
        sampler1 = ParameterSampler(seed=42)
        sampler1.add_parameter("pi", {"distribution": "uniform", "min": -1, "max": 1})

        sampler2 = ParameterSampler(seed=42)
        sampler2.add_parameter("pi", {"distribution": "uniform", "min": -1, "max": 1})

        v1 = sampler1.sample()
        v2 = sampler2.sample()

        self.assertEqual(v1["pi"], v2["pi"])


class TestExpressionEvaluator(unittest.TestCase):
    """Tests for safe expression evaluator."""

    def setUp(self):
        self.evaluator = SafeExpressionEvaluator()

    def test_simple_arithmetic(self):
        """Test basic arithmetic operations."""
        self.assertEqual(self.evaluator.evaluate("2 + 3"), 5)
        self.assertEqual(self.evaluator.evaluate("10 - 4"), 6)
        self.assertEqual(self.evaluator.evaluate("3 * 4"), 12)
        self.assertEqual(self.evaluator.evaluate("10 / 2"), 5)
        self.assertEqual(self.evaluator.evaluate("2 ** 3"), 8)

    def test_variable_substitution(self):
        """Test variable substitution."""
        result = self.evaluator.evaluate("{alpha} - {tau}", {"alpha": 0.35, "tau": 1})
        self.assertAlmostEqual(result, -0.65)

    def test_nested_variable_access(self):
        """Test accessing nested dict values."""
        result = self.evaluator.evaluate(
            "{crawler.pi} + {item.pi}", {"crawler": {"pi": -0.5}, "item": {"pi": 0.3}}
        )
        self.assertAlmostEqual(result, -0.2)

    def test_math_functions(self):
        """Test safe math functions."""
        self.assertEqual(self.evaluator.evaluate("abs(-5)"), 5)
        self.assertEqual(self.evaluator.evaluate("pow(2, 3)"), 8)
        self.assertAlmostEqual(self.evaluator.evaluate("sqrt(4)"), 2)
        self.assertEqual(self.evaluator.evaluate("min(3, 5)"), 3)
        self.assertEqual(self.evaluator.evaluate("max(3, 5)"), 5)

    def test_scoring_expression(self):
        """Test a realistic scoring expression."""
        expr = "{alpha} - {tau} * pow(abs({crawler.pi} - {item.pi}), 2)"
        context = {"alpha": 0.35, "tau": 1, "crawler": {"pi": -0.5}, "item": {"pi": 0.2}}
        result = self.evaluator.evaluate(expr, context)

        # Expected: 0.35 - 1 * pow(abs(-0.5 - 0.2), 2) = 0.35 - 0.49 = -0.14
        self.assertAlmostEqual(result, -0.14, places=5)

    def test_comparison_operators(self):
        """Test comparison operators."""
        self.assertTrue(self.evaluator.evaluate("5 > 3"))
        self.assertFalse(self.evaluator.evaluate("5 < 3"))
        self.assertTrue(self.evaluator.evaluate("5 >= 5"))

    def test_ternary_expression(self):
        """Test ternary if-else expression."""
        result = self.evaluator.evaluate("10 if 5 > 3 else 20")
        self.assertEqual(result, 10)

    def test_disallowed_operations(self):
        """Test that dangerous operations are blocked."""
        with self.assertRaises(ExpressionError):
            self.evaluator.evaluate("__import__('os').system('ls')")

    def test_extract_variables(self):
        """Test extracting variable names from expression."""
        expr = "{alpha} - {tau} * pow(abs({crawler.pi} - {item.pi}), 2)"
        variables = self.evaluator.extract_variables(expr)

        self.assertIn("alpha", variables)
        self.assertIn("tau", variables)
        self.assertIn("crawler.pi", variables)
        self.assertIn("item.pi", variables)


class TestDataPoolManager(unittest.TestCase):
    """Tests for DataPoolManager."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.manager = DataPoolManager(base_dir=Path(self.temp_dir), seed=42)

        # Create test data files
        outlets_path = Path(self.temp_dir) / "outlets.csv"
        outlets_path.write_text(
            "domain,url,pi\n"
            "cnn.com,https://cnn.com,-0.3\n"
            "foxnews.com,https://foxnews.com,0.5\n"
            "bbc.com,https://bbc.com,0.0\n"
            "reuters.com,https://reuters.com,0.1\n"
            "msnbc.com,https://msnbc.com,-0.4\n"
        )

    def test_load_pool(self):
        """Test loading a data pool."""
        result = self.manager.load_pool("outlets", {"source": "outlets.csv"})

        self.assertTrue(result)
        self.assertIn("outlets", self.manager.pools)
        self.assertEqual(len(self.manager.pools["outlets"]), 5)

    def test_resolve_all_selection(self):
        """Test selecting all records from pool."""
        self.manager.load_pool("outlets", {"source": "outlets.csv"})

        ref = {"from_pool": "outlets", "select": "all"}
        data = self.manager.resolve(ref, {})

        self.assertEqual(len(data), 5)

    def test_resolve_random_selection(self):
        """Test random selection from pool."""
        self.manager.load_pool("outlets", {"source": "outlets.csv"})

        ref = {"from_pool": "outlets", "select": "random", "count": 2}
        data = self.manager.resolve(ref, {})

        self.assertEqual(len(data), 2)

    def test_resolve_top_with_scoring(self):
        """Test top-N selection with scoring expression."""
        self.manager.load_pool("outlets", {"source": "outlets.csv"})

        # Select outlets closest to crawler's pi
        ref = {
            "from_pool": "outlets",
            "select": "top",
            "count": 2,
            "score": "{alpha} - {tau} * pow(abs({crawler.pi} - {item.pi}), 2)",
        }
        context = {"alpha": 0.35, "tau": 1, "crawler": {"pi": -0.5}}
        data = self.manager.resolve(ref, context)

        self.assertEqual(len(data), 2)
        # msnbc (-0.4) and cnn (-0.3) should be closest to crawler pi (-0.5)
        domains = [d["domain"] for d in data]
        self.assertIn("msnbc.com", domains)
        self.assertIn("cnn.com", domains)

    def test_resolve_with_filter(self):
        """Test filtering pool records."""
        # Create pool with region column
        outlets_path = Path(self.temp_dir) / "outlets_region.csv"
        outlets_path.write_text(
            "domain,url,pi,region\n"
            "cnn.com,https://cnn.com,-0.3,US\n"
            "bbc.com,https://bbc.com,0.0,UK\n"
            "reuters.com,https://reuters.com,0.1,US\n"
        )

        self.manager.load_pool("outlets_region", {"source": "outlets_region.csv"})

        ref = {"from_pool": "outlets_region", "filter": {"region": "US"}}
        data = self.manager.resolve(ref, {})

        self.assertEqual(len(data), 2)
        self.assertTrue(all(d["region"] == "US" for d in data))

    def test_is_pool_reference(self):
        """Test detecting pool references."""
        self.assertTrue(self.manager.is_pool_reference({"from_pool": "outlets"}))
        self.assertFalse(self.manager.is_pool_reference({"url": "https://example.com"}))
        self.assertFalse(self.manager.is_pool_reference("string"))

    def test_is_pool_reference_requires_a_string_from_pool(self):
        """A boolean `from_pool` (e.g. `proxy: {from_pool: true, ...}`) is a different,
        unimplemented shape -- must agree with resolve_pool_references, which passes it
        through untouched rather than resolving it."""
        self.assertFalse(self.manager.is_pool_reference({"from_pool": True}))


class TestDesignExpanderWithDistributions(unittest.TestCase):
    """Tests for DesignExpander with distribution sampling."""

    def test_expand_with_parameter_distributions(self):
        """Test design expansion with parameter distributions."""
        expander = DesignExpander(seed=42)

        design = {
            "type": "factorial",
            "parameters": {
                "pi": {"distribution": "uniform", "min": -1, "max": 1},
                "kappa": {"distribution": "discrete", "values": {0: 0.5, 1: 0.5}},
                "tau": 1.0,
            },
            "factors": {"condition": {"levels": ["control", "treatment"]}},
            "replications": 3,
        }

        template = {"name": "Bot {condition} {replica}"}
        crawlers = expander.expand(design, template)

        self.assertEqual(len(crawlers), 6)  # 2 conditions × 3 replications

        # Check that each crawler has sampled parameters in config
        for crawler in crawlers:
            params = crawler.get("configuration", {}).get("params", {})
            self.assertIn("pi", params)
            self.assertIn("kappa", params)
            self.assertIn("tau", params)

    def test_expand_with_proportional_factors(self):
        """Test stratified design with proportional factor levels."""
        expander = DesignExpander(seed=42)

        design = {
            "type": "factorial",  # Will be auto-converted to stratified
            "factors": {
                "usage_type": {
                    "levels": [
                        {"id": "only_visits", "proportion": 0.25},
                        {"id": "only_searches", "proportion": 0.25},
                        {"id": "searches_and_visits", "proportion": 0.50},
                    ]
                }
            },
            "total_crawlers": 100,
        }

        template = {"name": "Bot {usage_type}"}
        crawlers = expander.expand(design, template)

        self.assertEqual(len(crawlers), 100)

        # Count by usage type
        counts = {"only_visits": 0, "only_searches": 0, "searches_and_visits": 0}
        for crawler in crawlers:
            # Extract usage type from name
            for ut in counts.keys():
                if ut in crawler["name"]:
                    counts[ut] += 1

        # Check proportions (allowing some tolerance)
        self.assertAlmostEqual(counts["only_visits"], 25, delta=3)
        self.assertAlmostEqual(counts["only_searches"], 25, delta=3)
        self.assertAlmostEqual(counts["searches_and_visits"], 50, delta=3)

    def test_reproducible_expansion(self):
        """Test that same seed produces same results."""
        design = {
            "type": "factorial",
            "seed": 42,
            "parameters": {"pi": {"distribution": "uniform", "min": -1, "max": 1}},
            "factors": {"condition": {"levels": ["a", "b"]}},
            "replications": 3,
        }

        template = {"name": "Bot"}

        expander1 = DesignExpander(seed=42)
        crawlers1 = expander1.expand(design, template)

        expander2 = DesignExpander(seed=42)
        crawlers2 = expander2.expand(design, template)

        # Compare pi values
        for i in range(len(crawlers1)):
            pi1 = crawlers1[i]["configuration"]["params"]["pi"]
            pi2 = crawlers2[i]["configuration"]["params"]["pi"]
            self.assertEqual(pi1, pi2)


class TestSpecParserWithExternalData(unittest.TestCase):
    """Tests for SpecParser with external data features."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def test_parse_with_file_tag(self):
        """Test parsing YAML with !file tag."""
        # Create test data file
        data_path = Path(self.temp_dir) / "terms.csv"
        data_path.write_text("term,pi\nclimate change,-0.2\neconomy,0.1\n")

        yaml_content = f"""
experiment:
  name: Test Experiment

crawlers:
  - name: Test Crawler
    configuration:
      params:
        search_terms: !file {data_path}
"""
        parser = SpecParser(base_dir=Path(self.temp_dir))
        parsed = parser.parse(yaml_content)

        self.assertEqual(len(parsed.errors), 0)
        # File reference should be resolved
        terms = parsed.crawlers[0]["configuration"]["params"]["search_terms"]
        self.assertEqual(len(terms), 2)
        self.assertEqual(terms[0]["term"], "climate change")

    def test_parse_with_data_pools(self):
        """Test parsing YAML with data_pools section."""
        # Create test data file
        data_path = Path(self.temp_dir) / "outlets.csv"
        data_path.write_text("domain,pi\ncnn.com,-0.3\nfox.com,0.5\n")

        yaml_content = f"""
experiment:
  name: Test Experiment

data_pools:
  outlets:
    source: {data_path}

crawlers:
  - name: Test Crawler
"""
        parser = SpecParser(base_dir=Path(self.temp_dir))
        parsed = parser.parse(yaml_content)

        self.assertEqual(len(parsed.errors), 0)
        self.assertIn("outlets", parsed.data_pools)

        # Pool should be loaded in pool manager
        pool_manager = parser.get_pool_manager()
        self.assertIsNotNone(pool_manager)
        self.assertIn("outlets", pool_manager.pools)


class TestIntegration(unittest.TestCase):
    """Integration tests combining all features."""

    def test_full_websearch_style_spec(self):
        """Test a complete websearch-style specification."""
        temp_dir = tempfile.mkdtemp()

        # Create data files
        outlets_path = Path(temp_dir) / "outlets.csv"
        outlets_path.write_text(
            "domain,url,pi\n"
            "cnn.com,https://cnn.com,-0.3\n"
            "fox.com,https://fox.com,0.5\n"
            "bbc.com,https://bbc.com,0.0\n"
        )

        terms_path = Path(temp_dir) / "terms.csv"
        terms_path.write_text("term,pi\n" "climate change,-0.2\n" "immigration,0.3\n")

        yaml_content = f"""
experiment:
  name: Web Search Polarization Study

data_pools:
  outlets:
    source: {outlets_path}
  terms:
    source: {terms_path}

design:
  seed: 42
  parameters:
    pi:
      distribution: uniform
      min: -1
      max: 1
    kappa:
      distribution: discrete
      values:
        0: 0.25
        1: 0.25
        2: 0.50
  factors:
    browser:
      levels: [chrome, firefox]
  replications: 2

crawlers:
  - from_design: true
    name: "bot_{{browser}}_{{replica:02d}}"
    description: "PI={{pi}}"
    configuration:
      params:
        alpha: 0.35
        tau: 1.0
    agent:
      identification: mlx
    proxy:
      hostname: proxy.example.com
      port: 8080
    session:
      workflow:
        - visit: https://google.com
        - wait: 5s
"""
        parser = SpecParser(base_dir=Path(temp_dir), seed=42)
        parsed = parser.parse(yaml_content)

        self.assertEqual(len(parsed.errors), 0, f"Errors: {parsed.errors}")

        # Convert to verify full pipeline
        from pmspec.spec.converter import SpecConverter

        converter = SpecConverter()
        result = converter.convert(parsed)

        self.assertEqual(len(result.errors), 0, f"Conversion errors: {result.errors}")
        # 2 browsers × 2 replications = 4 crawlers
        self.assertEqual(result.crawler_count, 4)

        # Check that crawlers have sampled parameters
        for crawler in result.json_data:
            params = crawler["configuration"]["params"]
            self.assertIn("pi", params)
            self.assertIn("kappa", params)


if __name__ == "__main__":
    unittest.main()
