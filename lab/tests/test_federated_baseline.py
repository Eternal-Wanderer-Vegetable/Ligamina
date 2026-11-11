from __future__ import annotations

import unittest

from lab.baselines.federated import _observed_message


class FederatedOperationObservationTests(unittest.TestCase):
    def test_source_bound_observation_preserves_forward_nodes_and_nested_segments(self) -> None:
        operation_message = [{
            "type": "forward_node",
            "user_id": "100000",
            "nickname": "这里是WFBOT",
            "content": [
                {"type": "text", "text": "本次查询："},
                {"type": "mention", "user_id": "200000"},
            ],
        }]

        observed = _observed_message(operation_message, preserve_segments=True)

        self.assertEqual(observed, operation_message)

    def test_legacy_observation_keeps_its_text_projection(self) -> None:
        operation_message = [{
            "type": "forward_node",
            "user_id": "100000",
            "nickname": "这里是WFBOT",
            "content": "本次查询：",
        }]

        observed = _observed_message(operation_message, preserve_segments=False)

        self.assertEqual(observed, [{"type": "text", "text": ""}])
