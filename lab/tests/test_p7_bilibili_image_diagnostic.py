from __future__ import annotations

import json
import socket
import ssl
from io import BytesIO
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener
from unittest.mock import patch


class P7BilibiliImageDiagnosticTests(unittest.TestCase):
    def test_selection_and_profile_bind_exact_entry_sources_and_three_command_cycles(self) -> None:
        from lab.automation.p7_bilibili_image_diagnostic import (
            BILIBILI_ENTRY_ID,
            build_selection_document,
            profile_binding,
            stimulus_for_command,
            validate_selection_document,
        )

        selection = build_selection_document()
        validate_selection_document(selection)
        self.assertEqual(
            {row["entry"]["registry_entry_id"] for row in selection["samples"]},
            {BILIBILI_ENTRY_ID},
        )
        self.assertEqual(selection["selection"]["counts"], {"koishi": 0, "nonebot": 1})
        for command_id in ("B站封面", "专栏下载", "动态下载"):
            stimulus = stimulus_for_command(command_id)
            self.assertEqual(len(stimulus["input_cycle"]), 2)
            self.assertEqual(stimulus["command_id"], command_id)
        candidate = {
            "ecosystem": "nonebot",
            "registry_entry_id": BILIBILI_ENTRY_ID,
            "package": "nonebot-plugin-bilibili-image",
            "version": "0.0.2.1",
            "artifact_sha256": "a9c165f99d4cf41a2364141050e4646a7223fab3c152aecc2f975739938fec20",
        }
        profile = profile_binding(candidate)
        self.assertEqual(profile["network_mode"], "none")
        self.assertEqual(profile["fixture_address"], "127.0.0.1")
        self.assertEqual(set(profile["host_aliases"]), {
            "api.bilibili.com", "api.vc.bilibili.com", "www.bilibili.com", "i0.hdslb.com",
        })

    def test_native_and_federated_requests_share_the_same_hash_bound_fixture_profile(self) -> None:
        from pathlib import Path

        from lab.automation.p7_bilibili_image_diagnostic import (
            build_selection_document,
            stimulus_for_command,
        )
        from lab.automation.p7_differential_campaign import _build_context, select_candidates

        candidates = select_candidates(
            build_selection_document(),
            allow_bilibili_image_diagnostic=True,
        )
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        stimulus = {"events": []}
        contexts = [
            _build_context(
                candidate,
                Path("/tmp/p7-bilibili-bundle"),
                {},
                stimulus,
                mode=mode,
                seed=20261112,
                discover_commands=(mode == "federated"),
            )
            for mode in ("native", "federated")
        ]
        profiles = [context["bilibili_https_fixture_profile"] for context in contexts]
        self.assertEqual(profiles[0], profiles[1])
        self.assertEqual(contexts[0]["execution"]["mode"], "native")
        self.assertEqual(contexts[1]["execution"]["mode"], "federated")
        self.assertEqual(stimulus_for_command("动态下载")["input_cycle"], ["7001", "7002"])

    def test_https_fixture_serves_bound_json_and_image_without_retaining_payloads(self) -> None:
        from lab.automation.p7_bilibili_image_diagnostic import (
            BILIBILI_ENTRY_ID,
            BILIBILI_TLS_CERT_PATH,
            BilibiliImageHttpsFixture,
            profile_binding,
        )

        profile = profile_binding({
            "ecosystem": "nonebot",
            "registry_entry_id": BILIBILI_ENTRY_ID,
            "package": "nonebot-plugin-bilibili-image",
            "version": "0.0.2.1",
            "artifact_sha256": "a9c165f99d4cf41a2364141050e4646a7223fab3c152aecc2f975739938fec20",
        })
        fixture = BilibiliImageHttpsFixture(profile, port_override=0)
        fixture.start()
        try:
            port = fixture.bound_port
            addr_info = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]
            with patch("socket.getaddrinfo", return_value=addr_info):
                context = ssl.create_default_context(cafile=str(BILIBILI_TLS_CERT_PATH))
                client = build_opener(ProxyHandler({}), HTTPSHandler(context=context))
                with client.open(Request(f"https://api.bilibili.com:{port}/x/web-interface/view?bvid=1TESTA")) as response:
                    api_status = response.status
                    api = response.read()
                with client.open(Request(f"https://i0.hdslb.com:{port}/fixture/cover-a.png")) as response:
                    image_status = response.status
                    image = response.read()
            self.assertEqual(api_status, 200)
            self.assertEqual(json.loads(api)["data"]["title"], "Fixture video A")
            self.assertEqual(image_status, 200)
            self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
            evidence = fixture.evidence()
            self.assertEqual(evidence["route_counts"], {"video-view-a": 1, "cover-image-a": 1})
            self.assertEqual(evidence["unmatched_request_count"], 0)
            self.assertNotIn("requests", evidence)
        finally:
            fixture.close()

    def test_native_recorder_rejects_inline_image_bytes_without_serializing_payload(self) -> None:
        import json
        from types import SimpleNamespace

        from lab.baselines.nonebot.runner import NativeOneBotApiRecorder

        operations: list[dict] = []
        api_inputs: list[dict] = []
        recorder = NativeOneBotApiRecorder(
            operations=operations,
            receipts=[],
            api_inputs=api_inputs,
            source_event_id=lambda: "event-inline-image",
            capture_source_event_id=True,
        )
        node = {
            "type": "node",
            "data": {
                "name": "Fixture Bot",
                "uin": "9000",
                "content": [SimpleNamespace(type="image", data={"file": BytesIO(b"private synthetic image bytes")})],
            },
        }

        async def run() -> None:
            with self.assertRaisesRegex(RuntimeError, "stable resource reference"):
                await recorder(SimpleNamespace(self_id="9000"), "send_group_forward_msg", group_id=42, messages=[node])

        import asyncio
        asyncio.run(run())
        self.assertEqual(operations, [])
        self.assertNotIn("private synthetic image bytes", json.dumps(api_inputs))
        self.assertIn("byte_length", json.dumps(api_inputs))

    def test_docker_side_maps_only_profile_hosts_and_sets_fixture_ca(self) -> None:
        from lab.automation.p7_bilibili_image_diagnostic import (
            BILIBILI_ENTRY_ID,
            profile_binding,
        )
        from lab.automation.phase4_docker_side import _docker_side

        sample = {
            "sample_id": f"p7-{BILIBILI_ENTRY_ID}",
            "ecosystem": "nonebot",
            "package": "nonebot-plugin-bilibili-image",
            "version": "0.0.2.1",
            "_artifact_hash": "a9c165f99d4cf41a2364141050e4646a7223fab3c152aecc2f975739938fec20",
        }
        profile = profile_binding({
            "ecosystem": sample["ecosystem"],
            "registry_entry_id": BILIBILI_ENTRY_ID,
            "package": sample["package"],
            "version": sample["version"],
            "artifact_sha256": sample["_artifact_hash"],
        })
        captured: dict[str, object] = {}

        class FakeDockerWorker:
            def __init__(self, **kwargs) -> None:
                pass

            def run(self, spec):
                captured["spec"] = spec
                return SimpleNamespace(
                    status="passed", stdout=b'{"status":"completed"}', cleanup_confirmed=True,
                    worker_id="worker", reason_code=None, exit_code=0, elapsed_seconds=1.0,
                    peak_memory_bytes=0, stdout_sha256="a" * 64, stderr_sha256="b" * 64,
                    output_truncated=False,
                )

        with TemporaryDirectory() as directory, patch("lab.automation.phase4_docker_side.DockerWorker", FakeDockerWorker):
            request = {
                "case_id": "bilibili-fixture-test",
                "stimulus": {"events": []},
                "context": {
                    "sample": sample,
                    "execution": {"mode": "native", "harness_timeout_seconds": 30},
                    "host_environment": {},
                    "test_context_profile": None,
                    "bilibili_https_fixture_profile": profile,
                    "runtime_artifacts": {},
                },
            }
            result = _docker_side(request, SimpleNamespace(
                image="python@sha256:" + "a" * 64,
                bundle=Path(directory),
                run_id="run",
                entry_id=BILIBILI_ENTRY_ID,
                attempt_id="attempt",
                generation=1,
                side="native",
            ))

        self.assertEqual(result["status"], "completed")
        spec = captured["spec"]
        self.assertEqual(set(spec.loopback_host_aliases), set(profile["host_aliases"]))
        worker_request = json.loads(spec.stdin_data)
        self.assertEqual(
            worker_request["context"]["host_environment"]["SSL_CERT_FILE"],
            "/input/project/" + profile["tls_certificate_path"],
        )


if __name__ == "__main__":
    unittest.main()
