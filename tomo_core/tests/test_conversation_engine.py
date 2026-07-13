import unittest

from tomo_core.conversation import ConversationEngine, ConversationRequest, FrameReady, SegmentFinish, TurnBudget, TurnRunCompleted, TurnRunStarted, TurnRunStatus
from tomo_core.conversation.parsing import ConversationOutputError
from tomo_core.models import InboundEnvelope, ResponseContract
from tomo_core.providers import ProviderSetupRequired, ProviderStreamCompleted, ProviderTextDelta, ProviderToolCallReady


PLAN = '{"type":"turn_plan","primary_move":"answer","supporting_moves":["acknowledge"],"move_sequence":["acknowledge","answer"],"response_goal":"answer directly","confidence":"high"}\n'


class ClosingIterator:
    def __init__(self, events):
        self.events = iter(events)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.events)

    def close(self):
        self.closed = True


class ScriptedProvider:
    name = "scripted"
    supports_images_in = False
    supports_images_out = False
    supports_tool_calls = True

    def __init__(self, streams):
        self.streams = list(streams)
        self.calls = []
        self.iterators = []

    def stream(self, messages, *, tools=(), actor_id=None):
        self.calls.append((messages, tools, actor_id))
        scripted = self.streams.pop(0)
        if isinstance(scripted, Exception):
            raise scripted
        iterator = ClosingIterator(scripted)
        self.iterators.append(iterator)
        return iterator

    def complete(self, *args, **kwargs):
        raise AssertionError("ordinary turns must not call complete")


class ConversationEngineTests(unittest.TestCase):
    def request(self):
        return ConversationRequest.from_history(
            envelope=InboundEnvelope("telegram", "u1", "m1", "my interview is tomorrow"),
            soul="SOUL SENTINEL",
            history=[{"role": "user", "content": "i need this job"}],
        )

    def test_one_stream_progressively_yields_plan_and_frames_independent_of_moves(self):
        provider = ScriptedProvider([
            [
                ProviderTextDelta(PLAN + '{"type":"frame","text":"First frame."}\n'),
                ProviderTextDelta('{"type":"frame","text":"Second frame."}\n'),
                ProviderStreamCompleted("stop", 12, 7),
            ]
        ])
        iterator = ConversationEngine(provider).respond_iter(self.request())

        started = next(iterator)
        self.assertIsInstance(started, TurnRunStarted)
        first = next(iterator)
        self.assertEqual(first, FrameReady(0, first.frame))
        self.assertEqual(first.frame.text, "First frame.")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0][1:], ((), "u1"))
        second = next(iterator)
        self.assertEqual(second.frame.text, "Second frame.")
        completed = next(iterator)
        self.assertIsInstance(completed, TurnRunCompleted)
        self.assertEqual(completed.result.status, TurnRunStatus.COMPLETED)
        self.assertEqual(completed.result.usage.input_tokens, 12)
        self.assertEqual(completed.result.usage.output_tokens, 7)
        self.assertEqual(len(completed.result.frames), 2)
        self.assertEqual(completed.result.segments[0].finish, SegmentFinish.COMPLETE)
        self.assertTrue(provider.iterators[0].closed)

    def test_invalid_output_before_a_frame_uses_one_replacement_stream_and_keeps_plan(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + "not json\n")],
            [ProviderTextDelta('{"type":"frame","text":"Fixed frame."}\n'), ProviderStreamCompleted("stop")],
        ])

        result = ConversationEngine(provider).respond(self.request())

        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(result.plan.response_goal, "answer directly")
        self.assertEqual(result.frames[0].text, "Fixed frame.")
        self.assertIn("fixed turn plan", provider.calls[1][0][0]["content"])

    def test_second_invalid_replacement_raises_safe_output_error(self):
        provider = ScriptedProvider([
            [ProviderTextDelta("not json\n")],
            [ProviderTextDelta("still not json\n")],
        ])

        with self.assertRaisesRegex(ConversationOutputError, "invalid conversation output"):
            ConversationEngine(provider).respond(self.request())

    def test_malformed_remainder_after_visible_frame_completes_partial(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"Visible."}\nnot json\n')],
        ])

        events = list(ConversationEngine(provider).respond_iter(self.request()))

        self.assertEqual([type(event) for event in events], [TurnRunStarted, FrameReady, TurnRunCompleted])
        self.assertEqual(events[-1].result.status, TurnRunStatus.COMPLETED_PARTIAL)
        self.assertEqual(events[-1].result.segments[0].finish, SegmentFinish.PARTIAL)

    def test_prior_visible_frame_then_frameless_failure_records_failed_without_repair(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"Checking."}\n'), ProviderToolCallReady("call-1", "search", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta("not json\n")],
        ])
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        result = ConversationEngine(
            provider,
            tool_registry=ToolRegistry((BoundTool(ToolSpec("search", "search", {"type": "object", "properties": {}}), lambda _: "found"),)),
        ).respond(self.request())

        self.assertEqual(result.status, TurnRunStatus.COMPLETED_PARTIAL)
        self.assertEqual([segment.finish for segment in result.segments], [SegmentFinish.TOOL_BATCH, SegmentFinish.FAILED])
        self.assertEqual(len(provider.calls), 2)

    def test_native_tool_call_is_repaired_before_visible_output_and_never_executed(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("call-1", "search", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Direct answer."}\n'), ProviderStreamCompleted("stop")],
        ])

        result = ConversationEngine(provider).respond(self.request())

        self.assertEqual(result.frames[0].text, "Direct answer.")
        self.assertEqual(len(provider.calls), 2)
        self.assertTrue(all(call[1] == () for call in provider.calls))

    def test_planless_native_tool_attempt_gets_explicit_tool_free_plan_repair(self):
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        provider = ScriptedProvider([
            [ProviderToolCallReady("call-1", "search", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"Recovered answer."}\n'), ProviderStreamCompleted("stop")],
        ])
        registry = ToolRegistry((BoundTool(ToolSpec("search", "search", {"type": "object", "properties": {}}), lambda _: "found"),))

        result = ConversationEngine(provider, tool_registry=registry).respond(self.request())

        self.assertEqual(result.frames[0].text, "Recovered answer.")
        self.assertNotEqual(provider.calls[0][1], ())
        self.assertEqual(provider.calls[1][1], ())
        repair_instruction = provider.calls[1][0][-1]["content"]
        self.assertIn("missing_plan", repair_instruction)
        self.assertIn("begin with exactly one turn_plan", repair_instruction)

    def test_setup_guidance_bypasses_structured_parsing(self):
        provider = ScriptedProvider([ProviderSetupRequired("use /connect first.")])

        events = list(ConversationEngine(provider).respond_iter(self.request()))

        self.assertEqual([type(event) for event in events], [TurnRunStarted, FrameReady, TurnRunCompleted])
        self.assertEqual(events[1].frame.text, "use /connect first.")

    def test_later_setup_requirement_completes_partial_without_duplicate_start(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"Checking."}\n'), ProviderToolCallReady("call-1", "search", "{}"), ProviderStreamCompleted("tool_calls")],
            ProviderSetupRequired("connect required"),
        ])
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        events = list(ConversationEngine(
            provider,
            tool_registry=ToolRegistry((BoundTool(ToolSpec("search", "search", {"type": "object", "properties": {}}), lambda _: "found"),)),
        ).respond_iter(self.request()))

        self.assertEqual(sum(isinstance(event, TurnRunStarted) for event in events), 1)
        self.assertEqual(events[-1].result.status, TurnRunStatus.COMPLETED_PARTIAL)
        self.assertEqual(events[-1].result.segments[-1].finish, SegmentFinish.FAILED)

    def test_later_setup_synthetic_frame_respects_elapsed_budget(self):
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        class Clock:
            def __init__(self):
                self.after_tool = False
                self.checks_after_tool = 0

            def __call__(self):
                if not self.after_tool:
                    return 0
                self.checks_after_tool += 1
                return 1 if self.checks_after_tool == 3 else 0

        clock = Clock()
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("call-1", "search", "{}"), ProviderStreamCompleted("tool_calls")],
            ProviderSetupRequired("connect required"),
        ])

        with self.assertRaisesRegex(ConversationOutputError, "invalid conversation output"):
            ConversationEngine(
                provider,
                tool_registry=ToolRegistry((BoundTool(ToolSpec("search", "search", {"type": "object", "properties": {}}), lambda _: setattr(clock, "after_tool", True)),)),
                budget=TurnBudget(6, 5, 5, 3, 3, 2, 800, 1, 0.5),
                monotonic_clock=clock,
            ).respond(self.request())

    def test_executor_infrastructure_failure_after_visible_frame_completes_partial_without_leaking(self):
        from tomo_core.tool_execution import ToolExecutor
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        class ExplodingExecutor(ToolExecutor):
            def __init__(self, tool_registry):
                super().__init__(tool_registry)
                self.calls = 0

            def execute_batch(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return super().execute_batch(*args, **kwargs)
                raise RuntimeError("executor infrastructure secret")

        tool_registry = ToolRegistry((BoundTool(ToolSpec("search", "search", {"type": "object", "properties": {}}), lambda _: "found"),))
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"Checking."}\n'), ProviderToolCallReady("call-1", "search", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderToolCallReady("call-2", "search", "{}"), ProviderStreamCompleted("tool_calls")],
        ])

        events = list(ConversationEngine(provider, tool_registry=tool_registry, tool_executor=ExplodingExecutor(tool_registry)).respond_iter(self.request()))

        self.assertEqual(events[-1].result.status, TurnRunStatus.COMPLETED_PARTIAL)
        self.assertEqual(events[-1].result.segments[-1].finish, SegmentFinish.FAILED)
        self.assertNotIn("secret", repr(events))

    def test_inactive_turn_closes_stream_and_suppresses_later_events(self):
        active = [True]
        provider = ScriptedProvider([
            [
                ProviderTextDelta(PLAN + '{"type":"frame","text":"First."}\n'),
                ProviderTextDelta('{"type":"frame","text":"Second."}\n'),
                ProviderStreamCompleted("stop"),
            ]
        ])
        iterator = ConversationEngine(provider).respond_iter(self.request(), is_active=lambda: active[0])

        self.assertIsInstance(next(iterator), TurnRunStarted)
        self.assertIsInstance(next(iterator), FrameReady)
        active[0] = False
        with self.assertRaises(StopIteration):
            next(iterator)
        self.assertTrue(provider.iterators[0].closed)

    def test_cancellation_after_partial_sse_bytes_closes_stream_without_frame_or_completion(self):
        active = [True]

        def partial_stream():
            yield ProviderTextDelta(PLAN[:40])
            active[0] = False
            yield ProviderTextDelta(PLAN[40:])

        provider = ScriptedProvider([partial_stream()])

        events = list(ConversationEngine(provider).respond_iter(self.request(), is_active=lambda: active[0]))

        self.assertEqual(events, [])
        self.assertTrue(provider.iterators[0].closed)

    def test_legacy_contract_derives_sentence_and_frame_limits_when_budget_is_omitted(self):
        sentence_limited = ConversationEngine(
            ScriptedProvider([
                [ProviderTextDelta(PLAN + '{"type":"frame","text":"One. Two."}\n')],
                [ProviderTextDelta('{"type":"frame","text":"One. Two."}\n')],
            ]),
            contract=ResponseContract(max_sentences_per_utterance=1),
        )
        frame_limited = ConversationEngine(
            ScriptedProvider([
                [ProviderTextDelta(PLAN + '{"type":"frame","text":"First."}\n{"type":"frame","text":"Second."}\n')],
            ]),
            contract=ResponseContract(max_utterances=1),
        )

        with self.assertRaisesRegex(ConversationOutputError, "invalid conversation output"):
            sentence_limited.respond(self.request())
        frame_limited_result = frame_limited.respond(self.request())
        self.assertEqual([frame.text for frame in frame_limited_result.frames], ["First."])
        self.assertEqual(frame_limited_result.status, TurnRunStatus.COMPLETED_PARTIAL)

    def test_explicit_budget_overrides_legacy_contract(self):
        budget = TurnBudget(1, 0, 0, 1, 2, 2, 800)

        engine = ConversationEngine(ScriptedProvider([]), ResponseContract(max_utterances=1, max_sentences_per_utterance=1), budget)

        self.assertIs(engine.budget, budget)

    def test_executor_registry_must_match_the_prompt_registry_by_identity(self):
        from tomo_core.tool_execution import ToolExecutor
        from tomo_core.tools import ToolRegistry

        with self.assertRaises(ValueError):
            ConversationEngine(ScriptedProvider([]), tool_registry=ToolRegistry(), tool_executor=ToolExecutor(ToolRegistry()))

    def test_non_stop_completion_repairs_before_visible_output_and_partials_after_visible_output(self):
        for reason in ("length", "content_filter", "tool_calls", "unexpected"):
            with self.subTest(reason=reason):
                before_visible = ScriptedProvider([
                    [ProviderTextDelta(PLAN), ProviderStreamCompleted(reason)],
                    [ProviderTextDelta('{"type":"frame","text":"Replacement."}\n'), ProviderStreamCompleted("stop")],
                ])
                after_visible = ScriptedProvider([
                    [ProviderTextDelta(PLAN + '{"type":"frame","text":"Visible."}\n'), ProviderStreamCompleted(reason)],
                ])

                repaired = ConversationEngine(before_visible).respond(self.request())
                partial = ConversationEngine(after_visible).respond(self.request())

                self.assertEqual(len(before_visible.calls), 2)
                self.assertEqual(repaired.status, TurnRunStatus.COMPLETED)
                self.assertEqual(partial.status, TurnRunStatus.COMPLETED_PARTIAL)

    def test_provider_events_after_completion_repair_before_visible_output_and_partial_afterward(self):
        before_visible = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderStreamCompleted("stop"), ProviderTextDelta("ignored")],
            [ProviderTextDelta('{"type":"frame","text":"Replacement."}\n'), ProviderStreamCompleted("stop")],
        ])
        after_visible = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"Visible."}\n'), ProviderStreamCompleted("stop"), ProviderTextDelta("ignored")],
        ])

        repaired = ConversationEngine(before_visible).respond(self.request())
        partial = ConversationEngine(after_visible).respond(self.request())

        self.assertEqual(len(before_visible.calls), 2)
        self.assertEqual(repaired.status, TurnRunStatus.COMPLETED)
        self.assertEqual(partial.status, TurnRunStatus.COMPLETED_PARTIAL)

    def test_terminal_usage_is_aggregated_across_replacement_attempts(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderStreamCompleted("length", 3, None)],
            [ProviderTextDelta('{"type":"frame","text":"Replacement."}\n'), ProviderStreamCompleted("stop", 7, 11)],
        ])

        result = ConversationEngine(provider).respond(self.request())

        self.assertEqual(result.usage.input_tokens, 10)
        self.assertEqual(result.usage.output_tokens, 11)
        self.assertEqual(result.usage.model_segments, 1)
        self.assertEqual(result.usage.contract_repairs, 1)


if __name__ == "__main__":
    unittest.main()
