"""Proportional factors are randomised independently, with exact marginals.

Until 2026-09-16 `_generate_stratified_cells` gave every bot the *first* level
with a count remaining of each proportional factor, so with two such factors
bots 1..k carried level A of both and bots k+1.. level B of both: a perfectly
confounded, deterministic assignment with no warning. Found while designing
websearch_polarization experiment 3 (10 cities x 5 privacy settings x ...).
"""

import collections
import unittest

from pmspec.spec.design_expander import DesignExpander

DESIGN = {
    "type": "factorial",
    "factors": {
        "arm": {"levels": [{"id": "a", "proportion": 0.5}, {"id": "b", "proportion": 0.5}]},
        "privacy": {"levels": [{"id": "p", "proportion": 0.5}, {"id": "q", "proportion": 0.5}]},
    },
    "total_crawlers": 100,
}
TEMPLATE = {"name": "bot", "arm": "{arm}", "privacy": "{privacy}"}


def _joint(crawlers):
    return collections.Counter((c["arm"], c["privacy"]) for c in crawlers)


class TestStratifiedRandomization(unittest.TestCase):
    def test_two_proportional_factors_are_not_confounded(self):
        crawlers = DesignExpander(seed=1).expand(DESIGN, TEMPLATE)
        joint = _joint(crawlers)
        # Block-wise assignment gives {(a,p): 50, (b,q): 50} and nothing else.
        self.assertEqual(sum(joint.values()), 100)
        self.assertEqual(len(joint), 4, joint)
        for cell, n in joint.items():
            self.assertTrue(10 <= n <= 40, f"{cell}: {n} looks confounded")

    def test_marginals_stay_exact(self):
        crawlers = DesignExpander(seed=1).expand(DESIGN, TEMPLATE)
        self.assertEqual(collections.Counter(c["arm"] for c in crawlers), {"a": 50, "b": 50})
        self.assertEqual(collections.Counter(c["privacy"] for c in crawlers), {"p": 50, "q": 50})

    def test_assignment_is_seeded(self):
        one = [(c["arm"], c["privacy"]) for c in DesignExpander(seed=7).expand(DESIGN, TEMPLATE)]
        same = [(c["arm"], c["privacy"]) for c in DesignExpander(seed=7).expand(DESIGN, TEMPLATE)]
        other = [(c["arm"], c["privacy"]) for c in DesignExpander(seed=8).expand(DESIGN, TEMPLATE)]
        self.assertEqual(one, same)
        self.assertNotEqual(one, other)

    def test_proportional_factor_is_independent_of_factorial_factors(self):
        design = {
            "type": "factorial",
            "factors": {
                "arm": {"levels": [{"id": "a", "proportion": 0.5}, {"id": "b", "proportion": 0.5}]},
                "city": {"levels": ["x", "y", "z"]},
            },
            "total_crawlers": 90,
        }
        crawlers = DesignExpander(seed=3).expand(
            design, {"name": "bot", "arm": "{arm}", "privacy": "{city}"}
        )
        joint = _joint(crawlers)
        self.assertEqual(len(joint), 6, joint)
        for cell, n in joint.items():
            self.assertTrue(5 <= n <= 25, f"{cell}: {n}")


if __name__ == "__main__":
    unittest.main()
