from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from lab.automation import phase4_worker_side


class Phase4WorkerRequestInputTests(unittest.TestCase):
    def _run_main(self, argv: list[str], stdin: str) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["phase4-worker-side", *argv]),
            patch.object(sys, "stdin", io.StringIO(stdin)),
            patch.object(phase4_worker_side, "run_side", return_value={"status": "completed"}) as run_side,
            redirect_stdout(output),
        ):
            status = phase4_worker_side.main()
        run_side.assert_called_once()
        return status, json.loads(output.getvalue())

    def test_worker_request_can_be_read_from_stdin(self) -> None:
        request = {"case_id": "large-input", "stimulus": {"events": [{"text": "x" * 140_000}]}}
        status, report = self._run_main(["--request-stdin"], json.dumps(request))
        self.assertEqual(status, 0)
        self.assertEqual(report, {"status": "completed"})

    def test_legacy_request_json_argument_remains_supported(self) -> None:
        request = {"case_id": "legacy", "stimulus": {"events": []}}
        status, report = self._run_main(["--request-json", json.dumps(request)], "")
        self.assertEqual(status, 0)
        self.assertEqual(report, {"status": "completed"})


if __name__ == "__main__":
    unittest.main()
