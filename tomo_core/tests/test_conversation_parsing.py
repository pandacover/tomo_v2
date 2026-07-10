import unittest

from tomo_core.conversation.models import ConversationMove, MoveConfidence
from tomo_core.conversation.parsing import ConversationOutputError, parse_move_plan, parse_utterances
from tomo_core.models import ResponseContract


class ConversationParsingTests(unittest.TestCase):
    def test_parse_move_plan_accepts_only_compact_decision_fields(self):
        plan = parse_move_plan('{"primary_move":"challenge","supporting_moves":["acknowledge","explore"],"response_goal":"challenge the assumption","confidence":"high"}')
        self.assertEqual(plan.primary, ConversationMove.CHALLENGE)
        self.assertEqual(plan.supporting, (ConversationMove.ACKNOWLEDGE, ConversationMove.EXPLORE))
        self.assertEqual(plan.confidence, MoveConfidence.HIGH)

    def test_parse_move_plan_rejects_unknown_fields_and_falls_back(self):
        plan = parse_move_plan('{"primary_move":"answer","chain_of_thought":"hidden"}')
        self.assertEqual(plan.primary, ConversationMove.ANSWER)
        self.assertEqual(plan.confidence, MoveConfidence.LOW)

    def test_parse_move_plan_falls_back_for_markdown_or_invalid_json(self):
        for payload in (
            "```json\n{}\n```",
            "not json",
            "{}",
            '{"primary_move":"answer","supporting_moves":[],"response_goal":true,"confidence":"high"}',
            '{"primary_move":"answer","supporting_moves":[],"response_goal":"first\\nsecond","confidence":"high"}',
        ):
            self.assertEqual(parse_move_plan(payload).primary, ConversationMove.ANSWER)

    def test_parse_utterances_accepts_valid_contract(self):
        self.assertEqual(parse_utterances('{"utterances":["one.","two? three!"]}'), ("one.", "two? three!"))
        self.assertEqual(parse_utterances('{"utterances":["use a * b for multiplication."]}'), ("use a * b for multiplication.",))
        self.assertEqual(
            parse_utterances('{"utterances":["use https://example.com/a with v2.1 for 3.14 values."]}'),
            ("use https://example.com/a with v2.1 for 3.14 values.",),
        )

    def test_parse_utterances_uses_the_supplied_runtime_contract(self):
        contract = ResponseContract(max_utterances=2, max_sentences_per_utterance=1)
        self.assertEqual(parse_utterances('{"utterances":["one.","two."]}', contract), ("one.", "two."))
        for raw in ('{"utterances":["one.","two.","three."]}', '{"utterances":["one. two.","three."]}'):
            with self.subTest(raw=raw), self.assertRaises(ConversationOutputError):
                parse_utterances(raw, contract)

    def test_parse_utterances_returns_safe_validation_failures(self):
        cases = [
            ("{}", "invalid_shape"),
            ('{"utterances":[]}', "invalid_utterance_count"),
            ('{"utterances":["1","2","3","4","5"]}', "invalid_utterance_count"),
            ('{"utterances":[1]}', "invalid_utterance"),
            ("```json\n{}\n```", "invalid_json"),
            ('{"utterances":["one"],"extra":"x"}', "invalid_shape"),
            ('{"utterances":["one. two. three. four."]}', "sentence_limit"),
            ('{"utterances":["one — two"]}', "banned_dash"),
            ('{"utterances":["## hidden heading"]}', "markdown"),
            ('{"utterances":["primary move: answer"]}', "internal_label"),
        ]
        for raw, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ConversationOutputError) as raised:
                    parse_utterances(raw)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn(raw, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
