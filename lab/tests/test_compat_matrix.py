"""P6-04: the compatibility matrix is built from committed evidence and stays traceable."""

from __future__ import annotations

import copy
import unittest

from lab import compat_matrix


class CompatibilityMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = compat_matrix.build()
        cls.by_id = {entry["registry_entry_id"]: entry for entry in cls.matrix["entries"]}

    def test_build_is_deterministic_and_valid(self) -> None:
        again = compat_matrix.build()
        self.assertEqual(compat_matrix.render_json(again), compat_matrix.render_json(self.matrix))
        self.assertEqual(compat_matrix.validate(self.matrix), [])

    def test_every_catalog_entry_is_present_once(self) -> None:
        ids = [entry["registry_entry_id"] for entry in self.matrix["entries"]]
        self.assertEqual(len(ids), len(set(ids)))
        counts = {eco: data["entries"] for eco, data in self.matrix["summary"].items()}
        self.assertEqual(counts, {"astrbot": 3271, "koishi": 4747, "nonebot": 943})

    def test_load_has_exactly_one_result_per_platform_for_censused_ecosystems(self) -> None:
        for eco in ("koishi", "nonebot"):
            load = self.matrix["summary"][eco]["load"]
            for platform in ("linux", "windows"):
                total = sum(n for key, n in load.items() if key.startswith(f"{platform}:"))
                self.assertEqual(total, self.matrix["summary"][eco]["entries"], (eco, platform))

    def test_known_claims_match_their_acceptance_reports(self) -> None:
        echo = self.by_id["ko-163c3bd4cf87c72c24210251"]["capabilities"]
        self.assertEqual(echo["mcp_tool"]["linux"]["value"], "verified")
        self.assertEqual(echo["federated_event"]["linux"]["value"], "verified")
        delivery = echo["platform_delivery"]["qq"]
        self.assertEqual(delivery["value"], "verified")
        self.assertIn("positive cases only", delivery["scope"])
        self.assertTrue(delivery["history"], "earlier P4-05 canaries are kept as history")

        help_plugin = self.by_id["nb-0b37b4bb782b82c00e95a87f"]["capabilities"]
        self.assertEqual(help_plugin["federated_event"]["linux"]["value"], "failed")
        self.assertEqual(help_plugin["federated_event"]["linux"]["reason"], "plugin_load_failed")

        gallery = self.by_id["ab-00d908510026dba4d2cc6acd"]["capabilities"]["mcp_tool"]["linux"]
        self.assertEqual(gallery["value"], "verified")
        self.assertIn("no positive case", gallery["scope"])

        teamup = self.by_id["ab-01803ad6c618f2321ed12e48"]["capabilities"]
        self.assertEqual(teamup["load"]["linux"]["value"], "verified")
        self.assertEqual(teamup["platform_delivery"]["qq"]["value"], "verified")

    def test_p403_recoveries_annotate_without_changing_the_p3_verdict(self) -> None:
        annotated = [
            entry for entry in self.matrix["entries"]
            if (entry["capabilities"].get("load", {}).get("linux") or {}).get("annotations")
        ]
        self.assertEqual(len(annotated), 39)
        recovered = [
            entry for entry in annotated
            if any(a["recovery_status"] == "recovered" for a in entry["capabilities"]["load"]["linux"]["annotations"])
        ]
        self.assertEqual(len(recovered), 3)
        self.assertTrue(all(e["capabilities"]["load"]["linux"]["value"] == "unknown" for e in recovered))

    def test_every_decided_claim_cites_hashed_evidence(self) -> None:
        for entry in self.matrix["entries"]:
            for name, platforms in entry["capabilities"].items():
                for platform, claim in platforms.items():
                    if claim["value"] in ("verified", "failed"):
                        self.assertTrue(claim["evidence"], (entry["registry_entry_id"], name, platform))
                    for ref in claim["evidence"]:
                        self.assertEqual(self.matrix["sources"][ref["path"]], ref["sha256"])

    def test_tampered_evidence_hash_and_invalid_values_are_rejected(self) -> None:
        tampered = copy.deepcopy(self.matrix)
        claim = tampered["entries"][0]
        first = next(iter(next(iter(claim["capabilities"].values())).values()))
        first["evidence"][0]["sha256"] = "0" * 64
        self.assertTrue(any("evidence hash" in e for e in compat_matrix.validate(tampered)))

        invalid = copy.deepcopy(self.matrix)
        target = self.by_id["ko-163c3bd4cf87c72c24210251"]["registry_entry_id"]
        entry = next(e for e in invalid["entries"] if e["registry_entry_id"] == target)
        entry["capabilities"]["mcp_tool"]["linux"]["value"] = "supported"
        self.assertTrue(compat_matrix.validate(invalid))

    def test_committed_matrix_is_current(self) -> None:
        self.assertEqual(compat_matrix.main(["check"]), 0)


if __name__ == "__main__":
    unittest.main()
