import unittest

from tomo_core.conversation.contract import (
    PlanSource,
    render_first_segment_contract,
    render_later_segment_contract,
    resolve_advisory_plan,
    synthesized_direct_plan,
    synthesized_tool_plan,
)
from tomo_core.conversation.models import (
    ConversationMove,
    MoveConfidence,
    MovePlan,
    REACTION_EMOJI_OPTIONS,
)


class AdvisoryPlanContractTests(unittest.TestCase):
    def test_complete_canonical_plan_is_model_owned(self):
        resolution = resolve_advisory_plan({
            "primary_move": "challenge",
            "supporting_moves": ["acknowledge", "explore"],
            "response_goal": "challenge the assumption",
            "confidence": "high",
            "reaction": None,
        })

        self.assertIs(resolution.source, PlanSource.MODEL)
        self.assertIs(resolution.plan.primary, ConversationMove.CHALLENGE)
        self.assertEqual(
            resolution.plan.supporting,
            (ConversationMove.ACKNOWLEDGE, ConversationMove.EXPLORE),
        )
        self.assertEqual(
            resolution.plan.sequence,
            (ConversationMove.CHALLENGE, ConversationMove.ACKNOWLEDGE, ConversationMove.EXPLORE),
        )

    def test_model_authored_sequence_is_ignored_and_normalized(self):
        resolution = resolve_advisory_plan({
            "primary_move": "answer",
            "supporting_moves": ["reassure"],
            "move_sequence": ["reassure", "answer"],
            "response_goal": "answer calmly",
            "confidence": "high",
            "reaction": None,
        })

        self.assertIs(resolution.source, PlanSource.NORMALIZED)
        self.assertEqual(
            resolution.plan.sequence,
            (ConversationMove.ANSWER, ConversationMove.REASSURE),
        )

    def test_trimmed_response_goal_is_classified_as_normalized(self):
        resolution = resolve_advisory_plan({
            "primary_move": "answer",
            "supporting_moves": [],
            "response_goal": "  answer calmly  ",
            "confidence": "high",
            "reaction": None,
        })

        self.assertIs(resolution.source, PlanSource.NORMALIZED)
        self.assertEqual(resolution.plan.response_goal, "answer calmly")

    def test_invalid_advisory_values_normalize_to_safe_plan(self):
        payload = {
            "primary_move": "unknown",
            "supporting_moves": ["reassure", "unknown", "reassure"],
            "response_goal": True,
            "confidence": "certain",
            "reaction": "unsupported",
            "extra": "ignored",
        }

        resolution = resolve_advisory_plan(payload)

        self.assertIs(resolution.source, PlanSource.NORMALIZED)
        self.assertIs(resolution.plan.primary, ConversationMove.ANSWER)
        self.assertEqual(resolution.plan.supporting, (ConversationMove.REASSURE,))
        self.assertEqual(resolution.plan.response_goal, MovePlan.direct_answer().response_goal)
        self.assertIs(resolution.plan.confidence, MoveConfidence.LOW)
        self.assertIsNone(resolution.plan.reaction)
        self.assertNotIn("ignored", repr(resolution.plan))
        self.assertEqual(payload["extra"], "ignored")

    def test_supporting_moves_are_capped_and_compact_goal_is_preserved(self):
        resolution = resolve_advisory_plan({
            "primary_move": "answer",
            "supporting_moves": ["answer", "reassure", "reassure", "explore", "challenge"],
            "response_goal": "answer calmly",
            "confidence": "medium",
            "reaction": "👍",
        })

        self.assertIs(resolution.source, PlanSource.NORMALIZED)
        self.assertEqual(
            resolution.plan.supporting,
            (ConversationMove.REASSURE, ConversationMove.EXPLORE),
        )
        self.assertEqual(resolution.plan.response_goal, "answer calmly")
        self.assertEqual(resolution.plan.reaction.emoji, "👍")

    def test_synthesized_plans_are_safe_and_preserve_tool_intent(self):
        direct = synthesized_direct_plan()
        tool = synthesized_tool_plan()

        self.assertIs(direct.source, PlanSource.SYNTHESIZED)
        self.assertEqual(direct.plan, MovePlan.direct_answer())
        self.assertIsNone(direct.plan.reaction)
        self.assertIs(tool.source, PlanSource.SYNTHESIZED)
        self.assertIs(tool.plan.primary, ConversationMove.ACT)
        self.assertEqual(tool.plan.supporting, (ConversationMove.ANSWER,))
        self.assertEqual(tool.plan.sequence, (ConversationMove.ACT, ConversationMove.ANSWER))
        self.assertIs(tool.plan.confidence, MoveConfidence.LOW)
        self.assertIsNone(tool.plan.reaction)

    def test_first_segment_renderer_has_one_canonical_plan_and_live_vocabulary(self):
        contract = render_first_segment_contract(
            max_frames=3,
            max_sentences=2,
            max_chars=800,
            native_tools_available=False,
        )

        example = ('{"type":"turn_plan","primary_move":"answer","supporting_moves":[],'
                   '"response_goal":"answer the user","confidence":"low","reaction":null}')
        self.assertEqual(contract.count(example), 1)
        self.assertNotIn("move_sequence", contract)
        self.assertIn("SHOULD emit one turn_plan before controls or frames", contract)
        self.assertIn(", ".join(move.value for move in ConversationMove), contract)
        self.assertIn(", ".join(REACTION_EMOJI_OPTIONS), contract)
        self.assertIn("1 to 3 frame records", contract)
        self.assertIn("2 sentences per frame", contract)
        self.assertIn("800 characters per frame", contract)
        self.assertIn("native tools are unavailable", contract)
        self.assertIn("one JSON object per physical line", contract)
        self.assertIn("no prose, markdown, fences, or backticks outside records", contract.lower())

    def test_first_segment_renderer_distinguishes_native_tool_completion(self):
        contract = render_first_segment_contract(
            max_frames=1,
            max_sentences=3,
            max_chars=400,
            native_tools_available=True,
        )

        self.assertIn("ordinary completion requires 1 to 1 frame records", contract)
        self.assertIn("native tool completion permits zero or one pre-tool frame", contract)
        self.assertIn("native tool calls are provider-native, never JSONL records", contract)
        self.assertIn("memory_control records are optional, strictly validated, and must precede frame records", contract)

    def test_later_segment_renderer_excludes_plan_and_keeps_bounds(self):
        contract = render_later_segment_contract(
            max_frames=2,
            max_sentences=1,
            max_chars=120,
            native_tools_available=False,
        )

        self.assertIn("do not emit a turn_plan", contract)
        self.assertNotIn('"type":"turn_plan"', contract)
        self.assertIn("only optional memory_control records before frame records", contract)
        self.assertIn("ordinary completion requires 1 to 2 frame records", contract)
        self.assertIn("1 sentences per frame", contract)
        self.assertIn("120 characters per frame", contract)


if __name__ == "__main__":
    unittest.main()
