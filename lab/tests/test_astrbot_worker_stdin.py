from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class AstrBotWorkerStdinTests(unittest.TestCase):
    def test_federated_tool_side_reads_fenced_request_from_worker_stdin(self) -> None:
        self._assert_request_round_trip("lab.astrbot_tool_side")

    def test_native_tool_side_reads_fenced_request_from_worker_stdin(self) -> None:
        self._assert_request_round_trip("lab.astrbot_native_tool_side")

    def _assert_request_round_trip(self, module: str) -> None:
        request = {"case_id": "safe-command", "tool_calls": [[{"command_id": "safe-command"}]]}
        payload = json.dumps(request, separators=(",", ":")).encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="ligamina-astrbot-worker-stdin-") as temporary:
            code = (
                "import json,sys; from pathlib import Path; "
                f"import {module} as side; side.INPUT=Path(sys.argv[1]); "
                "print(json.dumps(side.worker_request(), separators=(',', ':')))"
            )
            completed = subprocess.run(
                [sys.executable, "-c", code, temporary], cwd=ROOT, input=payload,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(json.loads(completed.stderr.decode("utf-8")), request)


if __name__ == "__main__":
    unittest.main()
