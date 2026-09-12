"""
Tests for the experiment_designer spec module.

Run with: python -m pytest experiment_designer/tests/test_spec.py -v
Or: python experiment_designer/tests/test_spec.py
"""

import json
import sys
import unittest
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from experiment_designer.spec import (
    DesignExpander,
    JobRegistry,
    SpecConverter,
    SpecParser,
    SpecValidator,
)


class TestJobRegistry(unittest.TestCase):
    """Tests for JobRegistry."""

    def test_list_all_returns_jobs(self):
        """Test that list_all returns job types."""
        jobs = JobRegistry.list_all()
        self.assertGreater(len(jobs), 10)

    def test_get_schema_for_valid_type(self):
        """Test getting schema for a valid job type."""
        schema = JobRegistry.get_schema("visit")
        self.assertIsNotNone(schema)
        self.assertEqual(schema.api_type, "visitjob")

    def test_get_schema_for_invalid_type(self):
        """Test getting schema for an invalid job type."""
        schema = JobRegistry.get_schema("nonexistent")
        self.assertIsNone(schema)

    def test_get_api_type_mapping(self):
        """Test API type mappings."""
        self.assertEqual(JobRegistry.get_api_type("visit"), "visitjob")
        self.assertEqual(JobRegistry.get_api_type("wait"), "waitjob")
        self.assertEqual(JobRegistry.get_api_type("click"), "clickjob")
        self.assertEqual(JobRegistry.get_api_type("type"), "entertextfieldjob")
        self.assertEqual(JobRegistry.get_api_type("scroll"), "scrollbydurationjob")


class TestSpecParser(unittest.TestCase):
    """Tests for SpecParser."""

    def setUp(self):
        self.parser = SpecParser()

    def test_parse_minimal_spec(self):
        """Test parsing a minimal valid spec."""
        yaml_content = """
experiment:
  name: Test Experiment

crawlers:
  - name: Test Crawler
"""
        parsed = self.parser.parse(yaml_content)
        self.assertEqual(len(parsed.errors), 0)
        self.assertEqual(parsed.experiment["name"], "Test Experiment")
        self.assertEqual(len(parsed.crawlers), 1)

    def test_parse_invalid_yaml(self):
        """Test parsing invalid YAML."""
        yaml_content = "invalid: yaml: content:"
        parsed = self.parser.parse(yaml_content)
        self.assertGreater(len(parsed.errors), 0)

    def test_parse_missing_experiment_name(self):
        """Test that missing experiment name causes error."""
        yaml_content = """
experiment:
  description: No name

crawlers:
  - name: Test
"""
        parsed = self.parser.parse(yaml_content)
        self.assertIn("experiment.name is required", parsed.errors)

    def test_duration_parsing(self):
        """Test duration string parsing."""
        yaml_content = """
experiment:
  name: Test

crawlers:
  - name: Test
    session:
      workflow:
        - wait: 10s
        - wait: 5m
        - wait: 1h
        - wait: 30
"""
        parsed = self.parser.parse(yaml_content)
        jobs = parsed.crawlers[0]["queues"][0]["jobs"]

        # Check that durations are converted correctly
        self.assertEqual(len(jobs), 4)

        # Get the seconds behavior for each wait job
        def get_seconds(job):
            for b in job.get("behaviors", []):
                if b["name"] == "seconds":
                    return b["value"]
            return None

        self.assertEqual(get_seconds(jobs[0]), 10)  # 10s
        self.assertEqual(get_seconds(jobs[1]), 300)  # 5m
        self.assertEqual(get_seconds(jobs[2]), 3600)  # 1h
        self.assertEqual(get_seconds(jobs[3]), 30)  # 30

    def test_shorthand_expansion(self):
        """Test shorthand syntax expansion."""
        yaml_content = """
experiment:
  name: Test

crawlers:
  - name: Test
    session:
      workflow:
        - visit: https://example.com
        - click: //button[@id='submit']
"""
        parsed = self.parser.parse(yaml_content)
        jobs = parsed.crawlers[0]["queues"][0]["jobs"]

        self.assertEqual(jobs[0]["type"], "visitjob")
        self.assertEqual(jobs[1]["type"], "clickjob")

    def test_template_expansion(self):
        """Test template reference expansion."""
        yaml_content = """
experiment:
  name: Test

templates:
  my_template:
    - visit: https://example.com
    - wait: 5s

crawlers:
  - name: Test
    session:
      workflow:
        - template: my_template
"""
        parsed = self.parser.parse(yaml_content)
        jobs = parsed.crawlers[0]["queues"][0]["jobs"]

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["type"], "visitjob")
        self.assertEqual(jobs[1]["type"], "waitjob")

    def test_template_with_variables(self):
        """Test template expansion with variable substitution."""
        yaml_content = """
experiment:
  name: Test

templates:
  search:
    - type:
        text: "${query}"
        selector: "//input"
        submit: true

crawlers:
  - name: Test
    session:
      workflow:
        - template: search
          with:
            query: "hello world"
"""
        parsed = self.parser.parse(yaml_content)
        jobs = parsed.crawlers[0]["queues"][0]["jobs"]

        # Find the text behavior
        text_value = None
        for b in jobs[0].get("behaviors", []):
            if b["name"] == "text":
                text_value = b["value"]
                break

        self.assertEqual(text_value, "hello world")


class TestDesignExpander(unittest.TestCase):
    """Tests for DesignExpander."""

    def setUp(self):
        self.expander = DesignExpander()

    def test_expand_simple_design(self):
        """Test expanding a simple 2x2 design."""
        design = {
            "type": "factorial",
            "factors": {
                "a": {"levels": ["a1", "a2"]},
                "b": {"levels": ["b1", "b2"]},
            },
            "replications": 1,
        }
        crawler_template = {"name": "test_{a}_{b}", "description": "Test crawler"}

        crawlers = self.expander.expand(design, crawler_template)

        self.assertEqual(len(crawlers), 4)  # 2 x 2 = 4
        names = [c["name"] for c in crawlers]
        self.assertIn("test_a1_b1", names)
        self.assertIn("test_a1_b2", names)
        self.assertIn("test_a2_b1", names)
        self.assertIn("test_a2_b2", names)

    def test_expand_with_replications(self):
        """Test design expansion with replications."""
        design = {
            "type": "factorial",
            "factors": {
                "condition": {"levels": ["control", "treatment"]},
            },
            "replications": 3,
        }
        crawler_template = {
            "name": "crawler_{condition}_{replica}",
        }

        crawlers = self.expander.expand(design, crawler_template)

        self.assertEqual(len(crawlers), 6)  # 2 x 3 = 6

    def test_expand_with_maps_to(self):
        """Test design expansion with maps_to parameter mapping."""
        design = {
            "type": "factorial",
            "factors": {
                "condition": {
                    "levels": ["control", "treated"],
                    "maps_to": {"configuration.params.pi": {"control": 0, "treated": 1}},
                }
            },
            "replications": 1,
        }
        crawler_template = {"name": "test_{condition}", "configuration": {"params": {}}}

        crawlers = self.expander.expand(design, crawler_template)

        self.assertEqual(len(crawlers), 2)

        # Check parameter values were mapped correctly (pi is a 0/1 switch)
        for c in crawlers:
            if c["name"] == "test_control":
                self.assertEqual(c["configuration"]["params"]["pi"], 0)
            elif c["name"] == "test_treated":
                self.assertEqual(c["configuration"]["params"]["pi"], 1)

    def test_preview_design(self):
        """Test design preview."""
        design = {
            "type": "factorial",
            "factors": {
                "a": {"levels": ["a1", "a2", "a3"], "maps_to": {"x": {}}},
                "b": {"levels": ["b1", "b2"]},
            },
            "replications": 5,
        }

        preview = self.expander.preview(design)

        self.assertTrue(preview["has_design"])
        self.assertEqual(preview["num_factors"], 2)
        self.assertEqual(preview["total_cells"], 6)  # 3 x 2
        self.assertEqual(preview["replications"], 5)
        self.assertEqual(preview["total_crawlers"], 30)  # 6 x 5


class TestSpecConverter(unittest.TestCase):
    """Tests for SpecConverter."""

    def setUp(self):
        self.parser = SpecParser()
        self.converter = SpecConverter()

    def test_convert_minimal_spec(self):
        """Test converting a minimal spec to JSON."""
        yaml_content = """
experiment:
  name: Test

crawlers:
  - name: Test Crawler
    proxy:
      hostname: proxy.example.com
      port: 8080
    session:
      workflow:
        - visit: https://example.com
"""
        parsed = self.parser.parse(yaml_content)
        result = self.converter.convert(parsed)

        self.assertEqual(len(result.errors), 0)
        self.assertEqual(result.crawler_count, 1)

        # Check JSON structure
        crawler = result.json_data[0]
        self.assertEqual(crawler["name"], "Test Crawler")
        self.assertIn("configuration", crawler)
        self.assertIn("proxy", crawler)
        self.assertIn("agent", crawler)
        self.assertIn("queues", crawler)

    def test_convert_passes_through_unknown_params(self):
        """Non-standard params survive conversion instead of being dropped."""
        yaml_content = """
experiment:
  name: Test

crawlers:
  - name: Test Crawler
    configuration:
      params:
        pi: 1
        epsilon: 0.7
    proxy:
      hostname: proxy.example.com
      port: 8080
    session:
      workflow:
        - visit: https://example.com
"""
        parsed = self.parser.parse(yaml_content)
        result = self.converter.convert(parsed)
        params = result.json_data[0]["configuration"]["params"]
        self.assertEqual(params["pi"], 1)  # standard key kept
        self.assertEqual(params["epsilon"], 0.7)  # unknown key passed through

    def test_try_scroll_is_parsed_not_dropped(self):
        """try_scroll / try_scroll_length must map to their API job types."""
        yaml_content = """
experiment:
  name: Test

crawlers:
  - name: Test Crawler
    session:
      workflow:
        - try_scroll: 30s
        - try_scroll_length: 500
"""
        parsed = self.parser.parse(yaml_content)
        result = self.converter.convert(parsed)
        jobs = result.json_data[0]["queues"][0]["jobs"]
        types = [j["type"] for j in jobs]
        self.assertIn("tryscrollbydurationjob", types)
        self.assertIn("tryscrollbylengthjob", types)

    def test_convert_with_design(self):
        """Test converting a spec with factorial design."""
        yaml_content = """
experiment:
  name: Test

design:
  type: factorial
  factors:
    condition:
      levels: [control, treatment]
  replications: 2

crawlers:
  - from_design: true
    name: "test_{condition}_{replica}"
    proxy:
      hostname: localhost
      port: 8080
    session:
      workflow:
        - visit: https://example.com
"""
        parsed = self.parser.parse(yaml_content)
        result = self.converter.convert(parsed)

        self.assertEqual(len(result.errors), 0)
        self.assertEqual(result.crawler_count, 4)  # 2 conditions x 2 replications

    def test_json_output_format(self):
        """Test that JSON output is properly formatted."""
        yaml_content = """
experiment:
  name: Test

crawlers:
  - name: Test
    configuration:
      params:
        pi: 1
        alpha: 1
    proxy:
      hostname: localhost
      port: 8080
    session:
      workflow:
        - visit: https://example.com
"""
        parsed = self.parser.parse(yaml_content)
        result = self.converter.convert(parsed)

        # Check it's valid JSON
        json_str = result.to_json()
        json_data = json.loads(json_str)

        self.assertIsInstance(json_data, list)
        self.assertEqual(len(json_data), 1)

    def test_convert_carries_runner_assignment(self):
        yaml_content = """
experiment:
  name: VM study
  runner_user_id: 3

crawlers:
  - name: Test Crawler
    proxy:
      hostname: proxy.example.com
      port: 8080
    session:
      workflow:
        - visit: https://example.com
"""
        result = self.converter.convert(self.parser.parse(yaml_content))
        self.assertEqual(result.errors, [])
        self.assertEqual(result.experiment_runner_user_id, 3)

    def test_convert_defaults_runner_assignment_to_none(self):
        yaml_content = """
experiment:
  name: Default study

crawlers:
  - name: Test Crawler
    proxy:
      hostname: proxy.example.com
      port: 8080
    session:
      workflow:
        - visit: https://example.com
"""
        result = self.converter.convert(self.parser.parse(yaml_content))
        self.assertIsNone(result.experiment_runner_user_id)

    def test_convert_rejects_non_integer_runner_assignment(self):
        yaml_content = """
experiment:
  name: Bad
  runner_user_id: vm

crawlers:
  - name: Test Crawler
    proxy:
      hostname: proxy.example.com
      port: 8080
    session:
      workflow:
        - visit: https://example.com
"""
        result = self.converter.convert(self.parser.parse(yaml_content))
        self.assertTrue(any("runner_user_id" in e for e in result.errors))
        # Verify short-circuit: no JSON crawlers generated
        self.assertEqual(result.json_data, [])
        self.assertEqual(result.crawler_count, 0)
        self.assertIsNone(result.experiment_runner_user_id)

    def test_convert_rejects_boolean_runner_assignment(self):
        yaml_content = """
experiment:
  name: Bad
  runner_user_id: true

crawlers:
  - name: Test Crawler
    proxy:
      hostname: proxy.example.com
      port: 8080
    session:
      workflow:
        - visit: https://example.com
"""
        result = self.converter.convert(self.parser.parse(yaml_content))
        self.assertTrue(any("runner_user_id" in e for e in result.errors))
        # Verify short-circuit: no JSON crawlers generated
        self.assertEqual(result.json_data, [])
        self.assertEqual(result.crawler_count, 0)
        self.assertIsNone(result.experiment_runner_user_id)


class TestSpecValidator(unittest.TestCase):
    """Tests for SpecValidator."""

    def setUp(self):
        self.validator = SpecValidator()

    def test_validate_valid_spec(self):
        """Test validating a valid spec."""
        spec = {"experiment": {"name": "Test"}, "crawlers": [{"name": "Test Crawler"}]}
        result = self.validator.validate(spec)
        self.assertTrue(result.valid)

    def test_validate_missing_experiment_name(self):
        """Test validation catches missing experiment name."""
        spec = {"experiment": {}, "crawlers": [{"name": "Test"}]}
        result = self.validator.validate(spec)
        self.assertFalse(result.valid)
        self.assertTrue(any("experiment.name" in e for e in result.errors))

    def test_validate_missing_crawlers(self):
        """Test validation catches missing crawlers."""
        spec = {"experiment": {"name": "Test"}, "crawlers": []}
        result = self.validator.validate(spec)
        self.assertFalse(result.valid)
        self.assertTrue(any("crawler" in e.lower() for e in result.errors))

    def test_validate_design_factor_levels(self):
        """Test validation of design factors."""
        spec = {
            "experiment": {"name": "Test"},
            "design": {"factors": {"empty_factor": {"levels": []}}},  # Should fail
            "crawlers": [{"name": "Test"}],
        }
        result = self.validator.validate(spec)
        self.assertFalse(result.valid)

    def test_validate_template_reference(self):
        """Test validation of template references."""
        spec = {
            "experiment": {"name": "Test"},
            "templates": {},
            "crawlers": [
                {"name": "Test", "session": {"workflow": [{"template": "nonexistent_template"}]}}
            ],
        }
        result = self.validator.validate(spec)
        self.assertTrue(any("template" in e.lower() for e in result.errors))

    def test_switch_param_rejects_fractional_in_maps_to(self):
        """pi/psi/kappa/tau are 0/1 switches; a fractional maps_to value errors."""
        spec = {
            "experiment": {"name": "Test"},
            "design": {
                "factors": {
                    "orientation": {
                        "levels": ["left", "center", "right"],
                        "maps_to": {
                            "configuration.params.pi": {"left": -0.5, "center": 0, "right": 0.5}
                        },
                    }
                }
            },
            "crawlers": [{"name": "Test", "from_design": True}],
        }
        result = self.validator.validate(spec)
        self.assertFalse(result.valid)
        self.assertTrue(any("switch" in e.lower() and "pi" in e for e in result.errors))

    def test_switch_param_rejects_fractional_direct(self):
        """A fractional switch param on a crawler errors."""
        spec = {
            "experiment": {"name": "Test"},
            "crawlers": [{"name": "Test", "configuration": {"params": {"tau": 0.5}}}],
        }
        result = self.validator.validate(spec)
        self.assertFalse(result.valid)
        self.assertTrue(any("tau" in e for e in result.errors))

    def test_switch_param_accepts_zero_and_one(self):
        """0 and 1 are the valid switch values and must not error."""
        spec = {
            "experiment": {"name": "Test"},
            "design": {
                "factors": {
                    "condition": {
                        "levels": ["control", "treated"],
                        "maps_to": {"configuration.params.pi": {"control": 0, "treated": 1}},
                    }
                }
            },
            "crawlers": [
                {"name": "Test", "from_design": True, "configuration": {"params": {"psi": 1}}}
            ],
        }
        result = self.validator.validate(spec)
        self.assertTrue(result.valid, result.errors)

    def test_unknown_param_is_a_drop(self):
        """A non-standard param is dropped by the pipeline: an error in strict
        mode, a warning plus a count under lax."""
        spec = {
            "experiment": {"name": "Test"},
            "crawlers": [{"name": "Test", "configuration": {"params": {"epsilon": 0.7}}}],
        }
        result = self.validator.validate(spec)
        self.assertFalse(result.valid)
        self.assertTrue(any("epsilon" in e for e in result.errors), result.errors)
        self.assertEqual(result.dropped, 1)

        lax = SpecValidator(lax=True).validate(spec)
        self.assertTrue(lax.valid, lax.errors)
        self.assertTrue(any("epsilon" in w for w in lax.warnings), lax.warnings)
        self.assertEqual(lax.dropped, 1)

    def _validate_workflow_item(self, item):
        """Validate a spec whose single crawler runs one workflow item."""
        spec = {
            "experiment": {"name": "Test"},
            # A session without a start is a drop since the strict contract, so
            # these fixtures carry one; the item under test is the only variable.
            "crawlers": [{"name": "Test", "session": {"start": "now", "workflow": [item]}}],
        }
        return self.validator.validate(spec)

    @staticmethod
    def _required_behavior_warnings(result):
        return [w for w in result.warnings if "required behavior" in w]

    def test_warns_on_dict_shorthand_missing_required_behavior(self):
        """Dict shorthand omitting a registry-required behavior warns, stays valid."""
        cases = [
            ({"scroll": {"direction": "UP"}}, "duration"),
            ({"scroll_length": {"direction": "DOWN"}}, "length"),
            ({"single_select": {}}, "selector"),
            ({"single_select_url": {}}, "selector"),
            ({"switch": {}}, "target"),
            ({"try_switch": {}}, "target"),
            ({"handle_alert": {}}, "action"),
            ({"try_handle_alert": {}}, "action"),
        ]
        for item, behavior in cases:
            with self.subTest(item=item):
                result = self._validate_workflow_item(item)
                # Warning, never an error: the parser substitutes a default today.
                self.assertTrue(result.valid, f"unexpected errors: {result.errors}")
                warnings = self._required_behavior_warnings(result)
                self.assertTrue(
                    any(f"'{behavior}'" in w for w in warnings),
                    f"expected a warning naming '{behavior}', got {result.warnings}",
                )

    def test_click_missing_selector_warns_independently_of_jsonschema(self):
        """click/try_click gain a check that does not depend on the jsonschema package.

        These two are already rejected by the optional JSON Schema layer
        (clickOptions.required=['selector']), which is silently skipped when
        `jsonschema` is not installed. The registry-driven warning is the
        dependency-independent backstop, so here it coexists with an error.
        """
        for item in [{"click": {"task": "x"}}, {"try_click": {"task": "x"}}]:
            with self.subTest(item=item):
                result = self._validate_workflow_item(item)
                warnings = self._required_behavior_warnings(result)
                self.assertTrue(
                    any("'selector'" in w for w in warnings),
                    f"expected a warning naming 'selector', got {result.warnings}",
                )

    def test_scalar_shorthand_does_not_warn(self):
        """Scalar shorthand supplies its required behavior positionally."""
        for item in [
            {"scroll": "30s"},
            {"single_select": "//a"},
            {"switch": "//iframe"},
            {"click": "//button"},
            {"handle_alert": "dismiss"},
            {"visit": "https://example.com"},
            {"wait": "10s"},
        ]:
            with self.subTest(item=item):
                result = self._validate_workflow_item(item)
                self.assertEqual(self._required_behavior_warnings(result), [])

    def test_behavior_key_aliases_accepted(self):
        """YAML keys the parser renames on expansion satisfy the registry name."""
        for item in [
            # registry says 'target', parser reads 'target' or 'selector'
            {"switch": {"target": "//iframe"}},
            {"switch": {"selector": "//iframe"}},
            # registry says 'selector', parser reads 'selector' or 'target'
            {"single_select": {"target": "//a"}},
            # registry says 'length', parser reads 'pixels' or 'length'
            {"scroll_length": {"pixels": 500}},
            {"scroll_length": {"length": 500}},
            # registry says 'linkValue', parser reads 'function' or 'link_value'
            {"lambda_select": {"task": "pick", "function": "chooser"}},
            {"lambda_select": {"task": "pick", "link_value": "chooser"}},
        ]:
            with self.subTest(item=item):
                result = self._validate_workflow_item(item)
                self.assertEqual(self._required_behavior_warnings(result), [])

    def test_hardcoded_errors_not_duplicated_as_warnings(self):
        """Items the hardcoded checks reject do not also collect warnings."""
        result = self._validate_workflow_item({"type": {}})
        self.assertFalse(result.valid)
        self.assertTrue(any("'text'" in e for e in result.errors))
        self.assertTrue(any("'selector'" in e for e in result.errors))
        self.assertEqual(self._required_behavior_warnings(result), [])

        result = self._validate_workflow_item({"lambda_select": {}})
        self.assertFalse(result.valid)
        self.assertEqual(self._required_behavior_warnings(result), [])

    def test_zero_is_a_value(self):
        """Falsy-but-legitimate numeric values must not trigger the warning."""
        for item in [{"scroll_length": {"pixels": 0}}, {"scroll": {"duration": 0}}]:
            with self.subTest(item=item):
                result = self._validate_workflow_item(item)
                self.assertEqual(self._required_behavior_warnings(result), [])


class TestEndToEnd(unittest.TestCase):
    """End-to-end tests using real spec files."""

    def test_youtube_factorial_study(self):
        """Test the example YouTube factorial study spec."""
        spec_path = (
            Path(__file__).parent.parent.parent
            / "examples"
            / "specs"
            / "youtube-factorial-study.yaml"
        )

        if not spec_path.exists():
            self.skipTest(f"Example spec not found: {spec_path}")

        with open(spec_path) as f:
            yaml_content = f.read()

        parser = SpecParser()
        parsed = parser.parse(yaml_content)

        self.assertEqual(len(parsed.errors), 0, f"Parse errors: {parsed.errors}")

        converter = SpecConverter()
        result = converter.convert(parsed)

        self.assertEqual(len(result.errors), 0, f"Convert errors: {result.errors}")
        self.assertEqual(result.crawler_count, 30)  # 3 x 2 x 5

        # Verify JSON is valid
        json_str = result.to_json()
        json_data = json.loads(json_str)
        self.assertEqual(len(json_data), 30)


def run_tests():
    """Run all tests."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # Add all test classes
    suite.addTests(loader.loadTestsFromTestCase(TestJobRegistry))
    suite.addTests(loader.loadTestsFromTestCase(TestSpecParser))
    suite.addTests(loader.loadTestsFromTestCase(TestDesignExpander))
    suite.addTests(loader.loadTestsFromTestCase(TestSpecConverter))
    suite.addTests(loader.loadTestsFromTestCase(TestSpecValidator))
    suite.addTests(loader.loadTestsFromTestCase(TestEndToEnd))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_tests())


class TestVisitConsentBehavior:
    """The visit job advertises the runner's ``consent`` behaviour."""

    def test_visit_lists_consent_behavior(self):
        from experiment_designer.spec.job_registry import JobRegistry

        schema = JobRegistry.get_schema("visit")
        assert schema is not None
        names = {b.name for b in schema.behaviors}
        assert "consent" in names
        consent = next(b for b in schema.behaviors if b.name == "consent")
        assert consent.required is False
        assert "reject" in consent.description


class TestMultiloginProfileDefaults(unittest.TestCase):
    """The converter's profile template must stay creatable on Multilogin X."""

    def test_default_core_version_is_accepted_by_multilogin(self):
        # Multilogin rejects profile creation with "Can't set core older than 147"
        # (seen 2026-09-02 on a spec-uploaded bot); a stale default here makes
        # every spec-created bot fail at setup on every runner.
        from experiment_designer.spec.converter import DEFAULT_MULTILOGIN_PROFILE

        self.assertGreaterEqual(DEFAULT_MULTILOGIN_PROFILE["core_version"], 147)
        self.assertTrue(DEFAULT_MULTILOGIN_PROFILE["auto_update_core"])

    def test_default_core_version_matches_the_e2e_template(self):
        import json
        from pathlib import Path

        from experiment_designer.spec.converter import DEFAULT_MULTILOGIN_PROFILE

        e2e = (
            Path(__file__).resolve().parents[2]
            / "orchestrator"
            / "e2e"
            / "test_experiment_youtube.json"
        )
        profile = json.loads(e2e.read_text())[0]["agent"]["multilogin_profile"]
        if isinstance(profile, str):
            profile = json.loads(profile)
        self.assertEqual(DEFAULT_MULTILOGIN_PROFILE["core_version"], profile["core_version"])
