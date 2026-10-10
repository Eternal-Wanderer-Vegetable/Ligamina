from __future__ import annotations

import unittest

from lab.stability import HOSTS, resource_budget


def samples_with_growth(final_growth: int, points_per_window: int = 30) -> list[dict]:
    baseline = 100 * 1024 * 1024
    samples = []
    elapsed_points = [1800 + i * 60 for i in range(points_per_window)]
    elapsed_points += [3600 + i * 60 for i in range(points_per_window)]
    for elapsed in elapsed_points:
        final = elapsed >= 3600
        components = {
            "core": {"pid": 10, "rss_bytes": baseline + (final_growth if final else 0)},
            HOSTS[0]: {"pid": 20, "rss_bytes": baseline + (final_growth if final else 0)},
            HOSTS[1]: {"pid": 30, "rss_bytes": baseline + (final_growth if final else 0)},
        }
        samples.append({"elapsed_seconds": elapsed, "components": components})
    return samples


class ResourceBudgetTests(unittest.TestCase):
    def test_short_run_is_skipped_instead_of_claiming_resource_acceptance(self) -> None:
        report = resource_budget(samples_with_growth(0), 120, 1800, 60)
        self.assertEqual(report["status"], "skipped")

    def test_required_windows_pass_when_rss_growth_stays_within_budget(self) -> None:
        report = resource_budget(samples_with_growth(20 * 1024 * 1024), 5400, 1800, 60)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["components"]["core"]["baseline_samples"], 30)
        self.assertEqual(report["components"]["core"]["final_samples"], 30)

    def test_rss_growth_over_limit_fails(self) -> None:
        report = resource_budget(samples_with_growth(21 * 1024 * 1024), 5400, 1800, 60)
        self.assertEqual(report["status"], "failed")

    def test_missing_samples_do_not_become_a_pass(self) -> None:
        report = resource_budget(samples_with_growth(0, points_per_window=1), 5400, 1800, 60)
        self.assertEqual(report["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
