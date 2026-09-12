"""Track D phase 3: the DSL side of the behaviours treatment channel.

A lambda_select job can now carry a ``strategy`` marker and a ``treatment`` block;
the parser emits them as job behaviours (the channel the runner now forwards and
the decision function reads), and the validator checks the treatment against the
strategy's JSON schema when it can find it.

The strategy schema lives in the sibling bot-decision-yt repo, which is NOT
present in this repo's CI, so the validation-logic tests inject a schema rather
than depend on the sibling; the soft-dependency path is tested with a strategy
whose schema genuinely cannot be found.
"""

import unittest

from experiment_designer.spec.parser import SpecParser
from experiment_designer.spec.validator import SpecValidator

_YT_SCHEMA = {
    "type": "object",
    "properties": {
        "strategy": {"const": "youtube_v1"},
        "pi": {"enum": [0, 1]},
        "tau": {"enum": [0, 1]},
    },
    "additionalProperties": True,
}


class TestLambdaSelectTreatmentParsing(unittest.TestCase):
    def setUp(self):
        self.parser = SpecParser()

    def _behaviours(self, lambda_select_yaml):
        parsed = self.parser.parse(
            "experiment:\n  name: Test\ncrawlers:\n  - name: Bot\n"
            "    session:\n      workflow:\n" + lambda_select_yaml
        )
        job = parsed.crawlers[0]["queues"][0]["jobs"][0]
        return {b["name"]: b["value"] for b in job["behaviors"]}

    def test_strategy_and_treatment_become_behaviours(self):
        behaviours = self._behaviours(
            "        - lambda_select:\n"
            "            task: youtube_video_select\n"
            "            function: bot-decision-yt\n"
            "            strategy: youtube_v1\n"
            "            treatment:\n"
            "              pi: 1\n"
            "              tau: 0\n"
        )
        assert behaviours["strategy"] == "youtube_v1"
        assert behaviours["pi"] == 1
        assert behaviours["tau"] == 0
        assert behaviours["task"] == "youtube_video_select"  # job-control still there

    def test_treatment_cannot_clobber_job_control(self):
        behaviours = self._behaviours(
            "        - lambda_select:\n"
            "            task: real_task\n"
            "            function: bot-decision-yt\n"
            "            strategy: youtube_v1\n"
            "            treatment:\n"
            "              task: hijacked\n"
        )
        assert behaviours["task"] == "real_task"  # job-control wins


class TestLambdaSelectTreatmentValidation(unittest.TestCase):
    def setUp(self):
        self.validator = SpecValidator()

    def _spec(self, lambda_select):
        # `start` is required: a session without one is a drop (strict contract),
        # and these tests are about the treatment block, not the schedule.
        return {
            "experiment": {"name": "Test"},
            "crawlers": [
                {
                    "name": "Bot",
                    "session": {"start": "now", "workflow": [{"lambda_select": lambda_select}]},
                }
            ],
        }

    def test_valid_treatment_passes(self):
        self.validator._strategy_schema = lambda name: _YT_SCHEMA
        spec = self._spec(
            {
                "task": "t",
                "function": "f",
                "strategy": "youtube_v1",
                "treatment": {"pi": 1, "tau": 0},
            }
        )
        result = self.validator.validate(spec)
        assert result.valid, result.errors

    def test_invalid_treatment_errors(self):
        self.validator._strategy_schema = lambda name: _YT_SCHEMA
        spec = self._spec(
            {
                "task": "t",
                "function": "f",
                "strategy": "youtube_v1",
                "treatment": {"pi": 0.5},  # not a 0/1 switch
            }
        )
        result = self.validator.validate(spec)
        assert not result.valid
        assert any("treatment" in e and "pi" in e for e in result.errors)

    def test_treatment_without_strategy_is_a_drop(self):
        # The decision function ignores a treatment with no strategy marker, so
        # strict mode errors; lax mode keeps it as a warning plus a count.
        spec = self._spec({"task": "t", "function": "f", "treatment": {"pi": 1}})
        result = self.validator.validate(spec)
        assert not result.valid
        assert any("'strategy' is not" in e for e in result.errors), result.errors
        assert result.dropped == 1

        lax = SpecValidator(lax=True).validate(spec)
        assert lax.valid, lax.errors
        assert any("'strategy' is not" in w for w in lax.warnings), lax.warnings
        assert lax.dropped == 1

    def test_missing_strategy_schema_warns_soft(self):
        # A real strategy name whose schema is not found (sibling absent) -> soft
        # warning, not a hard failure.
        spec = self._spec(
            {
                "task": "t",
                "function": "f",
                "strategy": "definitely_not_a_real_strategy",
                "treatment": {"pi": 1},
            }
        )
        result = self.validator.validate(spec)
        assert result.valid, result.errors
        assert any("schema was not found" in w for w in result.warnings)


if __name__ == "__main__":
    unittest.main()
