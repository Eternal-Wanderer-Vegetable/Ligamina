from __future__ import annotations

import unittest

from lab.automation.p7_mutations import MUTANTS, run_campaign


class P7MutationTests(unittest.TestCase):
    def test_small_mutation_regression_detects_every_self_owned_mutant(self) -> None:
        report = run_campaign()

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["counts"]["mutants"], len(MUTANTS))
        self.assertEqual(report["counts"]["detected"], len(MUTANTS))
        self.assertEqual(report["counts"]["survived"], 0)
        self.assertEqual(report["counts"]["mutation_score"], 1.0)
        self.assertEqual(report["counts"]["detected_by_layer"], {
            "fixture": 1,
            "contract": 3,
            "differential": 5,
            "stateful": 1,
        })
        self.assertTrue(all(row["detected_by"] for row in report["mutants"]))

    def test_mutation_campaign_declares_observation_only_scope(self) -> None:
        scope = run_campaign()["scope"]

        self.assertFalse(scope["third_party_artifacts_modified"])
        self.assertFalse(scope["production_core_host_modified_for_injection"])
        self.assertIn("not a mutation score", scope["limitation"])


if __name__ == "__main__":
    unittest.main()
