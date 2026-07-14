import unittest

from tomo_core.conversation.framing import SegmentFrameParser
from tomo_core.conversation.models import ConversationMove, Frame, TurnBudget
from tomo_core.conversation.parsing import ConversationOutputError


def budget() -> TurnBudget:
    return TurnBudget(1, 0, 0, 1, 3, 3, 80)


class SegmentFrameParserTests(unittest.TestCase):
    def test_first_segment_buffers_split_jsonl_and_emits_plan_then_frames(self):
        parser = SegmentFrameParser(segment_index=0, first_segment=True, budget=budget())

        self.assertEqual(parser.feed('{"type":"turn_plan","primary_move":"answer",'), [])
        records = parser.feed(
            '"supporting_moves":[],"response_goal":"answer directly","confidence":"high"}\n'
            '{"type":"frame","text":"First."}\n{"type":"frame","text":"Second!"}\n'
        )

        self.assertEqual(records[0].primary, ConversationMove.ANSWER)
        self.assertEqual(records[0].confidence.value, "high")
        self.assertEqual(records[1:], [Frame(0, 0, "First."), Frame(0, 1, "Second!")])
        self.assertEqual(parser.finish(), [])

    def test_later_segment_accepts_an_unterminated_final_frame(self):
        parser = SegmentFrameParser(segment_index=2, first_segment=False, budget=budget())

        self.assertEqual(parser.feed('{"type":"frame","text":"Continued."}'), [])
        self.assertEqual(parser.finish(), [Frame(2, 0, "Continued.")])

    def test_memory_controls_precede_frames_and_are_bounded(self):
        parser = SegmentFrameParser(segment_index=0, first_segment=True, budget=budget())
        records = parser.feed(
            '{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"response_goal":"answer","confidence":"low"}\n'
            '{"type":"memory_control","action":"upsert","authority":"autonomous","user_intent_excerpt":null,"memory_id":null,"kind":"preference","subject_key":"self","topic":"food","value":"tea","statement":"The user likes tea","confidence":0.9,"salience":0.8,"surface_scope":"contextual","valid_from":null,"valid_until":null,"sources":[{"source_kind":"current_message","source_id":"m1","observed_at":"2026-07-13T00:00:00Z"}]}\n'
            '{"type":"frame","text":"Noted."}\n'
        )
        self.assertEqual(records[1].statement, "The user likes tea")
        self.assertEqual(records[2], Frame(0, 0, "Noted."))
        with self.assertRaises(ConversationOutputError) as raised:
            parser.feed('{"type":"memory_control","action":"set_owner_setting","setting":"capture_enabled","enabled":true,"user_intent_excerpt":"remember this"}\n')
        self.assertEqual(raised.exception.code, "late_memory_control")

    def test_plan_reaction_is_nullable_and_allowlisted(self):
        parser = SegmentFrameParser(segment_index=0, first_segment=True, budget=budget())
        records = parser.feed(
            '{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"response_goal":"answer","confidence":"low","reaction":"👏"}\n'
        )
        self.assertEqual(records[0].reaction.emoji, "👏")

        for reaction in ("custom_emoji_id", ["👍"], "👍👍"):
            with self.subTest(reaction=reaction):
                parser = SegmentFrameParser(segment_index=0, first_segment=True, budget=budget())
                records = parser.feed('{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"response_goal":"answer","confidence":"low","reaction":' + repr(reaction).replace("'", '"') + '}\n')
                self.assertIsNone(records[0].reaction)

    def test_first_segment_requires_one_plan_before_frames_but_allows_zero_frames(self):
        parser = SegmentFrameParser(segment_index=0, first_segment=True, budget=budget())
        with self.assertRaises(ConversationOutputError) as raised:
            parser.feed('{"type":"frame","text":"Too early."}\n')
        self.assertEqual(raised.exception.code, "missing_plan")

        parser = SegmentFrameParser(segment_index=0, first_segment=True, budget=budget())
        records = parser.feed('{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"response_goal":"answer directly","confidence":"low"}\n')
        self.assertEqual(records[0].response_goal, "answer directly")
        self.assertEqual(parser.finish(), [])

    def test_rejects_invalid_record_shapes_and_plan_ordering_with_safe_codes(self):
        cases = [
            ('{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"response_goal":"goal","confidence":"low","extra":"x"}\n', "invalid_plan"),
            ('{"type":"frame","text":"ok","extra":"x"}\n', "invalid_frame"),
            ('{"type":"unknown"}\n', "invalid_record"),
            ('{"type":"frame","text":"line\\nbreak"}\n', "invalid_frame"),
        ]
        for raw, code in cases:
            with self.subTest(code=code):
                parser = SegmentFrameParser(segment_index=1, first_segment=code == "invalid_plan", budget=budget())
                with self.assertRaises(ConversationOutputError) as raised:
                    parser.feed(raw)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn(raw, str(raised.exception))

        parser = SegmentFrameParser(segment_index=0, first_segment=True, budget=budget())
        parser.feed('{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"response_goal":"goal","confidence":"low"}\n')
        with self.assertRaises(ConversationOutputError) as raised:
            parser.feed('{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"response_goal":"goal","confidence":"low"}\n')
        self.assertEqual(raised.exception.code, "duplicate_plan")

    def test_finish_rejects_incomplete_or_garbage_json_and_missing_first_plan(self):
        cases = [
            (False, '{"type":"frame","text":"unfinished', "invalid_json"),
            (False, '{"type":"frame","text":"valid"} garbage', "invalid_json"),
            (True, "\n\n", "missing_plan"),
        ]
        for first_segment, raw, code in cases:
            with self.subTest(code=code):
                parser = SegmentFrameParser(segment_index=0, first_segment=first_segment, budget=budget())
                parser.feed(raw)
                with self.assertRaises(ConversationOutputError) as raised:
                    parser.finish()
                self.assertEqual(raised.exception.code, code)

    def test_ignores_blank_lines_and_rejects_plan_records_after_the_first_segment(self):
        parser = SegmentFrameParser(segment_index=3, first_segment=False, budget=budget())
        self.assertEqual(parser.feed(" \n\n\t\n"), [])
        with self.assertRaises(ConversationOutputError) as raised:
            parser.feed('{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"response_goal":"goal","confidence":"low"}\n')
        self.assertEqual(raised.exception.code, "unexpected_plan")

    def test_enforces_frame_contract_limits_and_style_rules(self):
        tight_budget = TurnBudget(1, 0, 0, 1, 1, 1, 12)
        style_budget = TurnBudget(1, 0, 0, 1, 1, 3, 80)
        cases = [
            ('{"type":"frame","text":"this is too long"}\n', "frame_too_long"),
            ('{"type":"frame","text":"one. two."}\n', "frame_sentence_limit"),
            ('{"type":"frame","text":"one — two"}\n', "banned_dash"),
            ('{"type":"frame","text":"## heading"}\n', "markdown"),
            ('{"type":"frame","text":"primary move: answer"}\n', "internal_label"),
        ]
        for raw, code in cases:
            with self.subTest(code=code):
                parser = SegmentFrameParser(
                    segment_index=1,
                    first_segment=False,
                    budget=tight_budget if code in {"frame_too_long", "frame_sentence_limit"} else style_budget,
                )
                with self.assertRaises(ConversationOutputError) as raised:
                    parser.feed(raw)
                self.assertEqual(raised.exception.code, code)

        parser = SegmentFrameParser(segment_index=1, first_segment=False, budget=tight_budget)
        parser.feed('{"type":"frame","text":"ok"}\n')
        with self.assertRaises(ConversationOutputError) as raised:
            parser.feed('{"type":"frame","text":"next"}\n')
        self.assertEqual(raised.exception.code, "frame_limit")


if __name__ == "__main__":
    unittest.main()
