from __future__ import annotations

import unittest

from lab.inventory_samples import _selection_rank


class InventorySampleSelectionTests(unittest.TestCase):
    def test_hash_ranked_selection_is_order_independent_and_seed_bound(self) -> None:
        identities = [f"ko-{index:04d}" for index in range(40)]

        original = sorted(identities, key=lambda item: _selection_rank("pilot-v1", item))
        reversed_input = sorted(reversed(identities), key=lambda item: _selection_rank("pilot-v1", item))
        other_seed = sorted(identities, key=lambda item: _selection_rank("pilot-v2", item))

        self.assertEqual(original, reversed_input)
        self.assertNotEqual(original[:10], other_seed[:10])
        self.assertEqual(len(set(original[:10])), 10)

    def test_hash_rank_is_stable_for_the_same_registry_identity(self) -> None:
        self.assertEqual(
            _selection_rank("p7-p704-independent-v1", "nb-123"),
            _selection_rank("p7-p704-independent-v1", "nb-123"),
        )


if __name__ == "__main__":
    unittest.main()
