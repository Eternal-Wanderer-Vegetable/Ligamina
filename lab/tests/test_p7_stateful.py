from __future__ import annotations

import unittest
from pathlib import Path

from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from lab.automation.p7_stateful import (
    Action,
    MAX_SEQUENCE_LENGTH,
    SyntheticCounterPlugin,
    SyntheticTeamupPlugin,
    build_pilot_report,
    evaluate_metamorphic,
    fixed_contexts,
    generate_sequences,
    infer_counter_dependencies,
    infer_teamup_dependencies,
    shrink_failing_sequence,
)


class P7StatefulTests(unittest.TestCase):
    def test_actor_session_grid_is_fixed_and_sequences_are_bounded_and_repeatable(self) -> None:
        contexts = fixed_contexts()
        self.assertEqual(len(contexts), 4)
        self.assertEqual({row["actor_id"] for row in contexts}, {"actor-a", "actor-b"})
        self.assertEqual({row["conversation_kind"] for row in contexts}, {"private", "group"})

        edges = infer_counter_dependencies()
        first = generate_sequences(edges, seed=7)
        second = generate_sequences(edges, seed=7)
        self.assertEqual(first, second)
        self.assertTrue(first)
        self.assertTrue(all(1 <= len(sequence) <= MAX_SEQUENCE_LENGTH for sequence in first))
        self.assertGreater(max(map(len, first)), 2)
        self.assertEqual({action.actor_id for sequence in first for action in sequence}, {"actor-a", "actor-b"})

    def test_pairwise_probes_infer_only_effectful_command_dependencies(self) -> None:
        edges = infer_counter_dependencies()

        self.assertEqual(edges, [
            {"before": "increment", "after": "read"},
            {"before": "clear", "after": "increment"},
            {"before": "clear", "after": "read"},
        ])

    def test_teamup_plugin_infers_membership_dependencies_and_replays_them(self) -> None:
        edges = infer_teamup_dependencies()
        group = next(row for row in fixed_contexts() if row["conversation_kind"] == "group")
        actions = [Action("join", **group), Action("members", **{**group, "actor_id": "actor-b"})]
        outputs = SyntheticTeamupPlugin().run(actions)

        self.assertEqual(edges, [
            {"before": "join", "after": "members"},
            {"before": "leave", "after": "members"},
        ])
        self.assertEqual(outputs[-1]["reply"], ["actor-a"])

    def test_metamorphic_relation_is_gated_by_native_observation(self) -> None:
        skipped = evaluate_metamorphic("session-isolation", 0, 1, 0, 2)
        passed = evaluate_metamorphic("session-isolation", 0, 0, 7, 7)
        failed = evaluate_metamorphic("session-isolation", 0, 0, 7, 8)

        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(skipped["reason_code"], "native_relation_not_satisfied")
        self.assertEqual(passed["status"], "passed")
        self.assertEqual(failed["status"], "failed")

    def test_native_first_metamorphic_oracle_detects_cross_conversation_state(self) -> None:
        context_a, context_b = fixed_contexts()[1], fixed_contexts()[3]
        increment = Action("increment", **context_a)
        read_other = Action("read", **context_b)

        native_before = SyntheticCounterPlugin().run([read_other])[0]["reply"]
        native_after = SyntheticCounterPlugin().run([increment, read_other])[1]["reply"]
        federated_before = SyntheticCounterPlugin(global_state=True).run([read_other])[0]["reply"]
        federated_after = SyntheticCounterPlugin(global_state=True).run([increment, read_other])[1]["reply"]

        self.assertEqual(evaluate_metamorphic("session-isolation", native_before, native_after, federated_before, federated_after)["status"], "failed")

    def test_pilot_adjudication_marks_unrun_and_skipped_entries_without_inference(self) -> None:
        pilot = {
            "kind": "p7_p704_generated_differential_pilot",
            "status": "completed",
            "entries": [
                {"registry_entry_id": "ready", "ecosystem": "koishi", "status": "completed"},
                {"registry_entry_id": "skip", "ecosystem": "nonebot", "status": "skipped"},
            ],
        }

        report = build_pilot_report(pilot, source_path=Path(__file__))

        self.assertEqual(report["entries"][0]["relations"]["session_isolation"]["status"], "unknown")
        self.assertEqual(report["entries"][1]["relations"]["session_isolation"]["status"], "skipped")
        self.assertEqual(report["counts"]["relation_positions"], 4)

    def test_hypothesis_shrinks_a_failing_sequence_to_a_failing_subsequence(self) -> None:
        context = fixed_contexts()[0]
        sequence = [
            Action("increment", **context),
            Action("read", **context),
            Action("clear", **context),
            Action("read", **context),
        ]
        shrunk = shrink_failing_sequence(sequence, lambda candidate: [item.command_id for item in candidate] == ["clear", "read"])

        self.assertEqual([item.command_id for item in shrunk], ["clear", "read"])


class _BoundedSequenceMachine(RuleBasedStateMachine):
    @initialize()
    def begin(self) -> None:
        self.sequence: list[str] = []

    @precondition(lambda self: len(self.sequence) < MAX_SEQUENCE_LENGTH)
    @rule(command=st.sampled_from(("increment", "clear", "read")))
    def append_command(self, command: str) -> None:
        self.sequence.append(command)

    @invariant()
    def sequence_never_exceeds_six(self) -> None:
        assert len(self.sequence) <= MAX_SEQUENCE_LENGTH


TestBoundedSequenceMachine = _BoundedSequenceMachine.TestCase
TestBoundedSequenceMachine.settings = settings(max_examples=30, stateful_step_count=MAX_SEQUENCE_LENGTH, derandomize=True)


if __name__ == "__main__":
    unittest.main()
