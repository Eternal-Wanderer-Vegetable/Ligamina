from __future__ import annotations

import unittest

from lab.automation.phase4_p402_campaign import _classify_pair, _failure_domain_counts, _slot_unit, _validate_runtime_image_reference


class Phase4P402CampaignTests(unittest.TestCase):
    def test_attempt_slot_ids_are_stable_and_distinct(self) -> None:
        slots = [_slot_unit("nb-help-positive", attempt) for attempt in range(1, 4)]
        self.assertEqual(slots, ["nb-help-positive--attempt-01", "nb-help-positive--attempt-02", "nb-help-positive--attempt-03"])
        self.assertEqual(len(set(slots)), 3)

    def test_cleanup_uncertainty_is_unknown_and_not_a_behavior_failure(self) -> None:
        pair = {
            "native": {"outer_worker": {"worker_id": "native-1", "cleanup_confirmed": True}},
            "federated": {"outer_worker": {"worker_id": "federated-1", "cleanup_confirmed": False}},
            "comparison": {"status": "failed", "issues": [{"code": "cleanup_unconfirmed", "side": "federated"}]},
        }
        self.assertEqual(_classify_pair(pair), ("unknown", "worker_cleanup_unconfirmed", False, ["native-1", "federated-1"]))

    def test_pair_oracle_mismatch_with_confirmed_cleanup_is_failed(self) -> None:
        pair = {
            "native": {"outer_worker": {"worker_id": "native-1", "cleanup_confirmed": True}},
            "federated": {"outer_worker": {"worker_id": "federated-1", "cleanup_confirmed": True}},
            "comparison": {"status": "failed", "issues": [{"code": "behavior_mismatch", "side": "pair"}]},
        }
        self.assertEqual(_classify_pair(pair), ("failed", "behavior_oracle_or_pair_mismatch", True, ["native-1", "federated-1"]))

    def test_plugin_load_failure_is_attributed_to_plugin_compatibility(self) -> None:
        pair = {
            "native": {
                "status": "failed",
                "stderr_summary": "RuntimeError: NoneBot could not load plugin 'nonebot_plugin_help'",
                "outer_worker": {"worker_id": "native-1", "cleanup_confirmed": True},
            },
            "federated": {
                "status": "failed",
                "stderr_summary": "RuntimeError: NoneBot plugin could not be loaded: nonebot_plugin_help",
                "outer_worker": {"worker_id": "federated-1", "cleanup_confirmed": True},
            },
            "comparison": {"status": "failed", "issues": [{"code": "run_not_completed", "side": "native"}]},
        }
        self.assertEqual(_classify_pair(pair), ("failed", "plugin_load_failed", True, ["native-1", "federated-1"]))

    def test_failure_domain_rollup_keeps_plugin_compatibility_distinct(self) -> None:
        jobs = {
            "load": {"result": {"failure_domain": "plugin_compatibility"}},
            "behavior": {"result": {"failure_domain": "plugin_behavior"}},
            "harness": {"result": {"failure_domain": "host_or_harness"}},
            "pass": {"result": {"failure_domain": None}},
        }
        self.assertEqual(
            _failure_domain_counts(jobs),
            {"plugin_compatibility": 1, "plugin_behavior": 1, "host_or_harness": 1},
        )

    def test_behavior_runtime_image_is_digest_pinned_separately_from_worker_admission(self) -> None:
        admission = {"facts": {"image": {"locked_reference": "python@sha256:" + "a" * 64}}}
        runtime_image = "ligamina-p401-runtime-offline-profile@sha256:" + "b" * 64
        self.assertNotEqual(runtime_image, admission["facts"]["image"]["locked_reference"])
        self.assertIsNone(_validate_runtime_image_reference(runtime_image, admission))

    def test_behavior_runtime_image_rejects_mutable_tags(self) -> None:
        admission = {"facts": {"image": {"locked_reference": "python@sha256:" + "a" * 64}}}
        with self.assertRaisesRegex(ValueError, "pinned by a sha256 digest"):
            _validate_runtime_image_reference("node:24.20.0", admission)


if __name__ == "__main__":
    unittest.main()
