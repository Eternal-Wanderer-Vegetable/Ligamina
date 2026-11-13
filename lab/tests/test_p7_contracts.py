from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lab.automation.p7_contracts import (
    CONTRACT_IDS,
    FIXTURES_PATH,
    build_pilot_report,
    evaluate_contract,
    evaluate_synthetic_fixtures,
    write_report,
)


class P7ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixtures = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))

    def test_each_contract_oracle_detects_positive_and_negative_synthetic_fixtures(self) -> None:
        result = evaluate_synthetic_fixtures(self.fixtures)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["counts"], {"fixtures": 16, "passed": 16, "failed": 0, "positive": 8, "negative": 8})
        self.assertEqual(set(result["by_contract"]), set(CONTRACT_IDS))
        self.assertTrue(all(value == {"positive": 1, "negative": 1} for value in result["by_contract"].values()))

    def test_missing_evidence_is_unknown_and_never_passes(self) -> None:
        for contract_id in CONTRACT_IDS:
            with self.subTest(contract_id=contract_id):
                self.assertEqual(evaluate_contract(contract_id, {})["status"], "unknown")

    def test_pilot_adjudication_has_all_contracts_and_keeps_unobserved_checks_unknown(self) -> None:
        pilot = {
            "kind": "p7_p704_generated_differential_pilot",
            "status": "completed",
            "entries": [
                {
                    "registry_entry_id": "fixture-clean",
                    "ecosystem": "koishi",
                    "status": "completed",
                    "native_discovery": {"status": "completed", "outer_worker": {"cleanup_confirmed": True}},
                    "rounds": [
                        {
                            "cleanup_confirmed": True,
                            "federated_host_reaped": True,
                            "fresh_workers": {
                                name: {"status": "completed", "outer_worker": {"cleanup_confirmed": True}}
                                for name in ("native-1", "native-2", "federated")
                            },
                        }
                        for _ in range(3)
                    ],
                },
                {"registry_entry_id": "fixture-incomplete", "ecosystem": "nonebot", "status": "skipped", "rounds": []},
            ],
        }

        report = build_pilot_report(pilot, source_path=FIXTURES_PATH)

        first = report["entries"][0]["contracts"]
        self.assertEqual(first["C1"]["status"], "passed")
        self.assertTrue(all(first[contract_id]["status"] == "unknown" for contract_id in CONTRACT_IDS[1:]))
        second = report["entries"][1]["contracts"]
        self.assertEqual(second["C1"]["status"], "unknown")
        self.assertEqual(len(second), 8)
        self.assertEqual(report["counts"]["checks"], 16)

    def test_report_writes_sidecar_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            digest = write_report(output, {"status": "completed"})
            self.assertEqual(output.with_suffix(".json.sha256").read_text(encoding="ascii").split()[0], digest)
            with self.assertRaises(FileExistsError):
                write_report(output, {"status": "completed"})


if __name__ == "__main__":
    unittest.main()
