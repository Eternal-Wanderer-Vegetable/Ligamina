from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from lab.inventory_quality import build_report


def sample(registry_entry_id: str, ecosystem: str, *, commands: bool, handlers: bool) -> dict:
    def collection(present: bool) -> dict:
        return {
            "status": "observed",
            "items": [{"kind": "fixture", "name": "fixture", "evidence": [{"reference": "fixture.py:1"}]}]
            if present else [],
        }

    digest = (registry_entry_id.encode().hex() + "0" * 64)[:64]
    return {
        "ecosystem": ecosystem,
        "entry": {"registry_entry_id": registry_entry_id, "package": registry_entry_id, "artifact_sha256": digest},
        "static_inventory": {
            "schema_version": 1,
            "ecosystem": ecosystem,
            "plugin": {
                "registry_entry_id": registry_entry_id,
                "package": registry_entry_id,
                "version": "1.0",
                "artifact_sha256": digest,
            },
            "source": {
                "kind": "static",
                "collector": "fixture/1",
                "confidence": "heuristic",
                "evidence": [{"reference": f"sha256:{digest}"}],
            },
            "capabilities": {
                "commands": collection(commands),
                "handlers": collection(handlers),
                "services": collection(False),
                "schedulers": collection(False),
                "configuration": collection(False),
            },
            "limitations": [],
        },
    }


class InventoryQualityTests(unittest.TestCase):
    def test_report_binds_annotations_and_computes_plugin_presence_metrics(self):
        first = sample("ko-1", "koishi", commands=True, handlers=False)
        second = sample("nb-1", "nonebot", commands=False, handlers=True)
        samples = {"samples": [first, second], "selection": {"per_ecosystem_target": 1}}
        annotations = {
            "review_scope": "fixture scope",
            "reviewer": "fixture reviewer",
            "annotations": [
                {
                    "ecosystem": record["ecosystem"],
                    "registry_entry_id": record["entry"]["registry_entry_id"],
                    "artifact_sha256": record["entry"]["artifact_sha256"],
                    "ground_truth": {
                        "commands": index == 0,
                        "handlers": index == 1,
                    },
                    "review": {"review_status": "reviewed"},
                }
                for index, record in enumerate([first, second])
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            samples_path = root / "samples.json"
            annotations_path = root / "annotations.json"
            sample_text = json.dumps(samples)
            samples_path.write_text(sample_text, encoding="utf-8")
            annotations["source_static_sample_sha256"] = hashlib.sha256(sample_text.encode()).hexdigest()
            annotations_path.write_text(json.dumps(annotations), encoding="utf-8")

            report = build_report(samples_path, annotations_path)

        self.assertEqual(report["counts"]["samples"], 2)
        self.assertEqual(report["metrics"]["overall"]["commands"]["true_positive"], 1)
        self.assertEqual(report["metrics"]["overall"]["handlers"]["true_positive"], 1)
        self.assertTrue(report["decision"]["command_presence_recall_met"])
        self.assertTrue(report["decision"]["continue_to_p7_04"])

    def test_missing_sample_annotation_fails_closed(self):
        record = sample("ko-1", "koishi", commands=False, handlers=False)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            samples_path = root / "samples.json"
            annotations_path = root / "annotations.json"
            sample_text = json.dumps({"samples": [record], "selection": {}})
            samples_path.write_text(sample_text, encoding="utf-8")
            annotations_path.write_text(json.dumps({
                "source_static_sample_sha256": hashlib.sha256(sample_text.encode()).hexdigest(),
                "annotations": [],
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "exactly the selected sample IDs"):
                build_report(samples_path, annotations_path)


if __name__ == "__main__":
    unittest.main()
