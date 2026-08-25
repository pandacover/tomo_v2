import unittest

from tomo_core.conversation.framing import SegmentFrameParser
from tomo_core.conversation.contract import PlanSource
from tomo_core.conversation.models import ConversationMove, Frame, MovePlan, TurnBudget
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

    def test_memory_control_without_plan_emits_plan_before_control(self):
        parser = SegmentFrameParser(segment_index=0, first_segment=True, budget=budget())

        records = parser.feed(
            '{"type":"memory_control","action":"set_owner_setting","setting":"capture_enabled","enabled":true,"user_intent_excerpt":"remember this"}\n'
        )

        self.assertEqual(records[0], MovePlan.direct_answer())
        self.assertEqual(records[1].action, "set_owner_setting")
        self.assertIs(parser.plan_source, PlanSource.SYNTHESIZED)

    def test_advisory_plan_drift_is_normalized_before_a_valid_frame(self):
        parser = SegmentFrameParser(segment_index=0, first_segment=True, budget=budget())

        records = parser.feed(
            '{"type":"turn_plan","primary_move":"invalid","supporting_moves":["reassure","invalid","reassure"],"response_goal":true,"confidence":"certain","reaction":"unsupported","extra":"ignored"}\n'
            '{"type":"frame","text":"Still valid."}\n'
        )

        self.assertIs(records[0].primary, ConversationMove.ANSWER)
        self.assertEqual(records[0].supporting, (ConversationMove.REASSURE,))
        self.assertIsNone(records[0].reaction)
        self.assertEqual(records[1], Frame(0, 0, "Still valid."))
        self.assertIs(parser.plan_source, PlanSource.NORMALIZED)

    def test_first_frame_without_plan_emits_synthesized_plan_then_frame(self):
        parser = SegmentFrameParser(segment_index=0, first_segment=True, budget=budget())

        records = parser.feed('{"type":"frame","text":"Direct answer."}\n')

        self.assertEqual(records, [MovePlan.direct_answer(), Frame(0, 0, "Direct answer.")])
        self.assertIs(parser.plan_source, PlanSource.SYNTHESIZED)
        self.assertEqual(parser.finish(), [])

    def test_accepts_standalone_markdown_fences_around_valid_jsonl(self):
        parser = SegmentFrameParser(segment_index=0, first_segment=True, budget=budget())

        records = parser.feed(
            "```jsonl\n"
            '{"type":"frame","text":"Direct answer."}\n'
            "```\n"
        )

        self.assertEqual(records, [MovePlan.direct_answer(), Frame(0, 0, "Direct answer.")])
        self.assertEqual(parser.finish(), [])

    def test_rejects_invalid_record_shapes_and_plan_ordering_with_safe_codes(self):
        cases = [
            ('{"type":"frame","text":"ok","extra":"x"}\n', "invalid_frame"),
            ('{"type":"unknown"}\n', "invalid_record"),
            ('{"type":"frame","text":"line\\nbreak"}\n', "invalid_frame"),
        ]
        for raw, code in cases:
            with self.subTest(code=code):
                parser = SegmentFrameParser(segment_index=1, first_segment=False, budget=budget())
                with self.assertRaises(ConversationOutputError) as raised:
                    parser.feed(raw)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn(raw, str(raised.exception))

        parser = SegmentFrameParser(segment_index=0, first_segment=True, budget=budget())
        parser.feed('{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"response_goal":"goal","confidence":"low"}\n')
        with self.assertRaises(ConversationOutputError) as raised:
            parser.feed('{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"response_goal":"goal","confidence":"low"}\n')
        self.assertEqual(raised.exception.code, "duplicate_plan")

        parser = SegmentFrameParser(segment_index=0, first_segment=True, budget=budget())
        parser.feed('{"type":"frame","text":"First."}\n')
        with self.assertRaises(ConversationOutputError) as raised:
            parser.feed('{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"response_goal":"goal","confidence":"low"}\n')
        self.assertEqual(raised.exception.code, "late_plan")

    def test_finish_rejects_incomplete_or_garbage_json_and_allows_empty_first_segment(self):
        cases = [
            (False, '{"type":"frame","text":"unfinished', "invalid_json_object"),
            (False, '{"type":"frame","text":"valid"} garbage', "invalid_json_object"),
            (False, "Here is the response:", "invalid_json_non_record"),
        ]
        for first_segment, raw, code in cases:
            with self.subTest(code=code):
                parser = SegmentFrameParser(segment_index=0, first_segment=first_segment, budget=budget())
                parser.feed(raw)
                with self.assertRaises(ConversationOutputError) as raised:
                    parser.finish()
                self.assertEqual(raised.exception.code, code)

        parser = SegmentFrameParser(segment_index=0, first_segment=True, budget=budget())
        parser.feed("\n\n")
        self.assertEqual(parser.finish(), [])

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

    def test_preserves_ascii_ellipses_at_terminal_standalone_and_mid_sentence_positions(self):
        examples = (
            "i was going to say this plan is doomed, but...",
            "...",
            "the approval process was... optimistic.",
        )
        for text in examples:
            with self.subTest(text=text):
                parser = SegmentFrameParser(segment_index=1, first_segment=False, budget=budget())
                self.assertEqual(parser.feed('{"type":"frame","text":' + repr(text).replace("'", '"') + '}\n'), [Frame(1, 0, text)])
                self.assertEqual(parser.finish(), [])


if __name__ == "__main__":
    unittest.main()
