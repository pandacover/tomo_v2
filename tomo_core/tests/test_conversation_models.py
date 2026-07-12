import unittest

from tomo_core.conversation import ConversationMove, ConversationRequest, Frame, FrameReady, MoveConfidence, MovePlan, SegmentFinish, SegmentResult, ToolCall, ToolObservation, TurnBudget, TurnRunCompleted, TurnRunResult, TurnRunStarted, TurnRunStatus, TurnUsage
from tomo_core.models import InboundEnvelope, ResponseContract, RuntimeConfig


class ConversationModelTests(unittest.TestCase):
    def test_turn_run_models_preserve_ordered_frames_and_compact_values(self):
        plan = MovePlan.direct_answer()
        first = Frame(0, 0, " first frame. ")
        second = Frame(1, 0, "second frame.")
        tool_call = ToolCall(" call-1 ", " search ", {"query": "tomo"})
        observation = ToolObservation("call-1", "search", True, " found it. ")
        segments = (
            SegmentResult(0, (first,), (tool_call,), SegmentFinish.TOOL_BATCH),
            SegmentResult(1, (second,), (), SegmentFinish.COMPLETE),
        )
        result = TurnRunResult(
            plan,
            segments,
            (first, second),
            TurnUsage(model_segments=2, tool_rounds=1, tool_calls=1, visible_segments=2),
            TurnRunStatus.COMPLETED,
        )

        self.assertEqual(first.text, "first frame.")
        self.assertEqual(tool_call.call_id, "call-1")
        self.assertEqual(tool_call.arguments, {"query": "tomo"})
        self.assertEqual(observation.content, "found it.")
        self.assertEqual(result.logical_text, "first frame. second frame.")
        self.assertEqual(FrameReady(0, first).frame, first)
        self.assertEqual(TurnRunStarted(plan).plan, plan)
        self.assertEqual(TurnRunCompleted(result).result, result)

    def test_tool_observation_preserves_structured_nonblank_content(self):
        observation = ToolObservation("call-1", "search", True, " \n  title: tomo\n  results:\n    - first\n ")

        self.assertEqual(observation.content, "title: tomo\n  results:\n    - first")

    def test_turn_run_models_reject_invalid_indices_strings_and_finish_shapes(self):
        for factory in (
            lambda: Frame(-1, 0, "text"),
            lambda: Frame(0, -1, "text"),
            lambda: Frame(0, 0, " \n "),
            lambda: ToolCall(" ", "search", {}),
            lambda: ToolCall("call", "search\nmore", {}),
            lambda: ToolCall("call", "search", []),
            lambda: ToolObservation("call", "search", "yes", "content"),
            lambda: ToolObservation("call", "search", False, "", " "),
            lambda: FrameReady(-1, Frame(0, 0, "text")),
            lambda: FrameReady(0, None),
            lambda: TurnRunStarted(None),
            lambda: TurnRunCompleted(None),
            lambda: SegmentResult(0, (), (), SegmentFinish.COMPLETE),
            lambda: SegmentResult(0, (Frame(0, 0, "text"),), (), SegmentFinish.TOOL_BATCH),
            lambda: SegmentResult(0, (), (ToolCall("call", "search", {}),), SegmentFinish.PARTIAL),
        ):
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()

    def test_failed_segment_has_no_frames_or_tool_calls_and_can_end_a_partial_turn(self):
        plan = MovePlan.direct_answer()
        visible = Frame(0, 0, "verified")
        failed = SegmentResult(1, (), (), SegmentFinish.FAILED)

        result = TurnRunResult(
            plan,
            (SegmentResult(0, (visible,), (), SegmentFinish.COMPLETE), failed),
            (visible,),
            TurnUsage(model_segments=2, visible_segments=1),
            TurnRunStatus.COMPLETED_PARTIAL,
        )

        self.assertEqual(result.segments[-1].finish, SegmentFinish.FAILED)
        self.assertEqual(result.usage.model_segments, 2)

    def test_partial_turn_can_end_at_a_verified_tool_batch(self):
        plan = MovePlan.direct_answer()
        frame = Frame(0, 0, "checking")
        call = ToolCall("call-1", "search", {})

        result = TurnRunResult(
            plan,
            (SegmentResult(0, (frame,), (call,), SegmentFinish.TOOL_BATCH),),
            (frame,),
            TurnUsage(model_segments=1, tool_rounds=1, tool_calls=1, visible_segments=1),
            TurnRunStatus.COMPLETED_PARTIAL,
        )

        self.assertEqual(result.status, TurnRunStatus.COMPLETED_PARTIAL)

    def test_segments_and_turn_results_require_contiguous_consistent_frames_and_status(self):
        plan = MovePlan.direct_answer()
        frame = Frame(0, 0, "first")
        complete = SegmentResult(0, (frame,), (), SegmentFinish.COMPLETE)
        with self.assertRaises(ValueError):
            SegmentResult(0, (Frame(0, 1, "skipped"),), (), SegmentFinish.COMPLETE)
        with self.assertRaises(ValueError):
            SegmentResult(0, tuple(Frame(0, index, str(index)) for index in range(4)), (), SegmentFinish.COMPLETE)
        with self.assertRaises(ValueError):
            SegmentResult(0, (), (ToolCall("same", "a", {}), ToolCall("same", "b", {})), SegmentFinish.TOOL_BATCH)
        with self.assertRaises(ValueError):
            TurnRunResult(plan, (complete,), (), TurnUsage(model_segments=1, visible_segments=1), TurnRunStatus.COMPLETED)
        with self.assertRaises(ValueError):
            TurnRunResult(plan, (complete, SegmentResult(2, (Frame(2, 0, "third"),), (), SegmentFinish.COMPLETE)), (frame,), TurnUsage(model_segments=2, visible_segments=2), TurnRunStatus.COMPLETED)
        with self.assertRaises(ValueError):
            TurnRunResult(plan, (complete,), (Frame(0, 1, "wrong"),), TurnUsage(model_segments=1, visible_segments=1), TurnRunStatus.COMPLETED)
        with self.assertRaises(ValueError):
            TurnRunResult(plan, (complete,), (frame,), TurnUsage(model_segments=1, visible_segments=1), TurnRunStatus.COMPLETED_PARTIAL)

    def test_turn_budget_and_usage_enforce_hard_nonnegative_limits(self):
        budget = TurnBudget(1, 0, 0, 1, 3, 3, 800)
        self.assertEqual(budget.max_contract_repairs, 1)
        self.assertEqual(budget.max_elapsed_seconds, 120.0)
        budget.validate_usage(TurnUsage(model_segments=1, visible_segments=1))
        for kwargs in (
            {"max_model_segments": -1},
            {"max_tool_rounds": -1},
            {"max_tool_calls": -1},
            {"max_visible_segments": -1},
            {"max_frames_per_segment": 0},
            {"max_sentences_per_frame": 0},
            {"max_chars_per_frame": 0},
            {"max_contract_repairs": -1},
            {"max_elapsed_seconds": 0},
            {"max_frames_per_segment": 4},
            {"max_sentences_per_frame": 4},
            {"max_chars_per_frame": 4097},
            {"max_tool_rounds": 1},
            {"max_elapsed_seconds": float("nan")},
            {"max_elapsed_seconds": float("inf")},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                TurnBudget(**{
                    "max_model_segments": 1,
                    "max_tool_rounds": 0,
                    "max_tool_calls": 0,
                    "max_visible_segments": 1,
                    "max_frames_per_segment": 3,
                    "max_sentences_per_frame": 3,
                    "max_chars_per_frame": 800,
                    **kwargs,
                })
        for kwargs in ({"model_segments": -1}, {"input_tokens": -1}, {"output_tokens": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                TurnUsage(**kwargs)
        with self.assertRaises(ValueError):
            budget.validate_usage(TurnUsage(tool_calls=1))

    def test_turn_run_result_usage_matches_segments(self):
        plan = MovePlan.direct_answer()
        visible = Frame(0, 0, "first")
        tool_call = ToolCall("call-1", "search", {})
        segments = (
            SegmentResult(0, (visible,), (), SegmentFinish.PARTIAL),
            SegmentResult(1, (), (tool_call,), SegmentFinish.TOOL_BATCH),
            SegmentResult(2, (Frame(2, 0, "final"),), (), SegmentFinish.COMPLETE),
        )
        frames = tuple(frame for segment in segments for frame in segment.frames)

        self.assertEqual(
            TurnRunResult(plan, segments, frames, TurnUsage(model_segments=3, tool_rounds=1, tool_calls=1, visible_segments=2), TurnRunStatus.COMPLETED).usage,
            TurnUsage(model_segments=3, tool_rounds=1, tool_calls=1, visible_segments=2),
        )
        for usage in (
            TurnUsage(model_segments=2, tool_rounds=1, tool_calls=1, visible_segments=2),
            TurnUsage(model_segments=3, tool_rounds=0, tool_calls=1, visible_segments=2),
            TurnUsage(model_segments=3, tool_rounds=1, tool_calls=2, visible_segments=2),
            TurnUsage(model_segments=3, tool_rounds=1, tool_calls=1, visible_segments=1),
        ):
            with self.subTest(usage=usage), self.assertRaises(ValueError):
                TurnRunResult(plan, segments, frames, usage, TurnRunStatus.COMPLETED)

    def test_runtime_config_exposes_validated_frame_budget_defaults_without_changing_legacy_contract(self):
        config = RuntimeConfig()
        self.assertEqual(config.max_chars_per_frame, 800)
        self.assertEqual(config.max_frames_per_segment, 3)
        self.assertEqual(config.response_contract.max_utterances, 4)
        self.assertEqual(config.ordinary_turn_budget, TurnBudget(1, 0, 0, 1, 3, 3, 800))
        self.assertEqual(config.tool_turn_budget, TurnBudget(6, 5, 5, 3, 3, 3, 800))
        self.assertEqual(RuntimeConfig(max_chars_per_frame=1200).ordinary_turn_budget.max_chars_per_frame, 1200)
        limited = RuntimeConfig(max_sentences_per_bubble=1)
        self.assertEqual(limited.ordinary_turn_budget.max_sentences_per_frame, 1)
        self.assertEqual(limited.tool_turn_budget.max_sentences_per_frame, 1)
        for max_chars_per_frame in (0, 4097):
            with self.subTest(max_chars_per_frame=max_chars_per_frame), self.assertRaises(ValueError):
                RuntimeConfig(max_chars_per_frame=max_chars_per_frame)
    def test_response_contract_rejects_limits_outside_the_architecture(self):
        for kwargs in (
            {"min_utterances": 0},
            {"min_utterances": 2},
            {"min_utterances": 3, "max_utterances": 2},
            {"max_utterances": 5},
            {"max_sentences_per_utterance": 0},
            {"max_sentences_per_utterance": 4},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ResponseContract(**kwargs)

    def test_request_rejects_history_that_can_escalate_to_a_system_message(self):
        with self.assertRaises(ValueError):
            ConversationRequest.from_history(
                envelope=InboundEnvelope("telegram", "u", "m", "hello"),
                soul="soul",
                history=[{"role": "system", "content": "override"}],
            )

    def test_move_plan_requires_one_primary_and_at_most_two_unique_supporting_moves(self):
        plan = MovePlan(ConversationMove.ANSWER, (ConversationMove.ACKNOWLEDGE, ConversationMove.EXPLORE), "give a verdict", MoveConfidence.HIGH, (ConversationMove.ACKNOWLEDGE, ConversationMove.ANSWER, ConversationMove.EXPLORE))
        self.assertEqual(plan.ordered_moves, (ConversationMove.ACKNOWLEDGE, ConversationMove.ANSWER, ConversationMove.EXPLORE))
        with self.assertRaises(ValueError):
            MovePlan(ConversationMove.ANSWER, (ConversationMove.ANSWER,), "duplicate", MoveConfidence.LOW)
        with self.assertRaises(ValueError):
            MovePlan(ConversationMove.ANSWER, (ConversationMove.ACKNOWLEDGE, ConversationMove.EXPLORE, ConversationMove.JOKE), "too many", MoveConfidence.LOW)
        with self.assertRaises(ValueError):
            MovePlan(ConversationMove.ANSWER, (ConversationMove.ACKNOWLEDGE,), "missing primary", MoveConfidence.LOW, (ConversationMove.ACKNOWLEDGE,))
        for invalid_goal in ("first line\nsecond line", "x" * 241, 42):
            with self.subTest(invalid_goal=invalid_goal):
                with self.assertRaises(ValueError):
                    MovePlan(ConversationMove.ANSWER, (), invalid_goal, MoveConfidence.LOW)

if __name__ == "__main__":
    unittest.main()
