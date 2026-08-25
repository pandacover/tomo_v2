import unittest
from unittest.mock import patch

from tomo_core.conversation import ConversationEngine, ConversationRequest, FrameReady, MemoryControlReady, ReactionWindowReady, SegmentFinish, TurnBudget, TurnRunCompleted, TurnRunStarted, TurnRunStatus
from tomo_core.conversation.parsing import ConversationOutputError
from tomo_core.models import InboundEnvelope, PeerTurn, ResponseContract
from tomo_core.providers import ProviderSetupRequired, ProviderStreamCompleted, ProviderStreamError, ProviderTextDelta, ProviderToolCallReady


PLAN = '{"type":"turn_plan","primary_move":"answer","supporting_moves":["acknowledge"],"move_sequence":["acknowledge","answer"],"response_goal":"answer directly","confidence":"high"}\n'
CANONICAL_PLAN = '{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"response_goal":"answer directly","confidence":"high","reaction":null}\n'


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

    def test_provider_stream_error_keeps_its_stable_code(self):
        from tomo_core.conversation.engine import _provider_failure_code

        self.assertEqual(
            _provider_failure_code(ProviderStreamError("invalid_sse_event")),
            "provider_stream_invalid_sse_event",
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

    def test_peer_turn_streams_without_connector_actor_identity(self):
        provider = ScriptedProvider([[
            ProviderTextDelta('{"type":"frame","text":"Safe answer."}\n'),
            ProviderStreamCompleted("stop"),
        ]])
        turn = PeerTurn(
            "peer-generation",
            1,
            "relationship",
            "thread",
            "peer-request",
            "alice",
            "question",
            "ordinary_message",
            "hello",
            "2099-01-01T00:15:00+00:00",
        )

        result = ConversationEngine(provider).respond(ConversationRequest(turn, "SOUL SENTINEL", ()))

        self.assertEqual([frame.text for frame in result.frames], ["Safe answer."])
        self.assertEqual(provider.calls[0][2], None)

    def test_no_tools_three_frame_reply_completes_with_exactly_three_frames(self):
        provider = ScriptedProvider([[
            ProviderTextDelta(PLAN + '{"type":"frame","text":"First."}\n{"type":"frame","text":"Second."}\n{"type":"frame","text":"Third."}\n'),
            ProviderStreamCompleted("stop"),
        ]])

        result = ConversationEngine(provider).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["First.", "Second.", "Third."])
        self.assertEqual(result.status, TurnRunStatus.COMPLETED)

    def test_frame_only_first_attempt_synthesizes_plan_without_repair(self):
        provider = ScriptedProvider([[ProviderTextDelta('{"type":"frame","text":"Fast answer."}\n'), ProviderStreamCompleted("stop")]])

        events = list(ConversationEngine(provider).respond_iter(self.request()))

        self.assertEqual([type(event) for event in events], [TurnRunStarted, FrameReady, TurnRunCompleted])
        self.assertEqual(events[0].plan.primary.value, "answer")
        self.assertEqual(events[1].frame.text, "Fast answer.")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(events[-1].result.usage.contract_repairs, 0)

    def test_normalized_plan_preserves_frame_without_repair(self):
        provider = ScriptedProvider([[
            ProviderTextDelta('{"type":"turn_plan","primary_move":"invalid","supporting_moves":[],"response_goal":true,"confidence":"certain","reaction":null}\n{"type":"frame","text":"Still valid."}\n'),
            ProviderStreamCompleted("stop"),
        ]])

        events = list(ConversationEngine(provider).respond_iter(self.request()))

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(events[0].plan.primary.value, "answer")
        self.assertEqual(events[0].plan.confidence.value, "low")
        self.assertEqual(events[1].frame.text, "Still valid.")
        self.assertEqual(events[-1].result.usage.contract_repairs, 0)

    def test_newline_and_final_buffer_records_have_identical_turn_event_order_and_source(self):
        record = CANONICAL_PLAN + '{"type":"frame","text":"Buffered frame."}'
        for suffix in ("\n", ""):
            with self.subTest(trailing_newline=bool(suffix)):
                provider = ScriptedProvider([[
                    ProviderTextDelta(record + suffix),
                    ProviderStreamCompleted("stop"),
                ]])
                with patch("tomo_core.conversation.engine.latency_trace.emit_sandbox") as emit:
                    events = list(ConversationEngine(provider).respond_iter(self.request()))

                self.assertEqual(
                    [type(event) for event in events],
                    [TurnRunStarted, FrameReady, TurnRunCompleted],
                )
                self.assertEqual(events[1].frame.text, "Buffered frame.")
                resolved = [call for call in emit.call_args_list if call.args[0] == "sandbox_provider_move_plan_validated"]
                self.assertEqual(len(resolved), 1)
                self.assertEqual(resolved[0].kwargs["plan_model"], 1)

    def test_plan_resolution_emits_one_source_count(self):
        attempts = (
            (CANONICAL_PLAN, "plan_model"),
            ('{"type":"turn_plan","primary_move":"invalid","supporting_moves":[],"response_goal":"answer","confidence":"low","reaction":null}\n', "plan_normalized"),
            ('', "plan_synthesized"),
        )
        for plan_record, expected_count in attempts:
            with self.subTest(expected_count=expected_count):
                provider = ScriptedProvider([[
                    ProviderTextDelta(plan_record + '{"type":"frame","text":"Resolved."}\n'),
                    ProviderStreamCompleted("stop"),
                ]])
                with patch("tomo_core.conversation.engine.latency_trace.emit_sandbox") as emit:
                    ConversationEngine(provider).respond(self.request())

                resolved = [call for call in emit.call_args_list if call.args[0] == "sandbox_provider_move_plan_validated"]
                self.assertEqual(len(resolved), 1)
                self.assertEqual({name: value for name, value in resolved[0].kwargs.items() if name.startswith("plan_")}, {expected_count: 1})

    def test_memory_control_before_plan_starts_turn_before_control_and_frame(self):
        provider = ScriptedProvider([[
            ProviderTextDelta('{"type":"memory_control","action":"set_owner_setting","setting":"capture_enabled","enabled":true,"user_intent_excerpt":"remember this"}\n{"type":"frame","text":"Noted."}\n'),
            ProviderStreamCompleted("stop"),
        ]])

        events = list(ConversationEngine(provider).respond_iter(self.request()))

        self.assertEqual([type(event) for event in events], [TurnRunStarted, MemoryControlReady, FrameReady, TurnRunCompleted])

    def test_tool_only_first_segment_synthesizes_tool_plan_before_execution(self):
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        executed = []
        provider = ScriptedProvider([
            [ProviderToolCallReady("call-1", "search", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Grounded answer."}\n'), ProviderStreamCompleted("stop")],
        ])
        registry = ToolRegistry((BoundTool(ToolSpec("search", "search", {"type": "object", "properties": {}}), lambda arguments: executed.append(arguments) or "found"),))

        events = list(ConversationEngine(provider, tool_registry=registry).respond_iter(self.request()))

        self.assertEqual([type(event) for event in events], [TurnRunStarted, FrameReady, TurnRunCompleted])
        self.assertEqual(events[0].plan.primary.value, "act")
        self.assertEqual(tuple(move.value for move in events[0].plan.supporting), ("answer",))
        self.assertEqual(executed, [{}])
        self.assertEqual(events[1].frame.text, "Grounded answer.")
        self.assertEqual(events[-1].result.usage.contract_repairs, 0)

    def test_planless_tool_preflight_resolves_registry_once_before_synthesis(self):
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        class CountingRegistry(ToolRegistry):
            def __init__(self, tools):
                super().__init__(tools)
                self.resolve_calls = 0

            def resolve(self, name):
                self.resolve_calls += 1
                return super().resolve(name)

        provider = ScriptedProvider([
            [ProviderToolCallReady("call-1", "search", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Grounded answer."}\n'), ProviderStreamCompleted("stop")],
        ])
        registry = CountingRegistry((BoundTool(ToolSpec("search", "search", {"type": "object", "properties": {}}), lambda _: "found"),))

        result = ConversationEngine(provider, tool_registry=registry).respond(self.request())

        self.assertEqual(registry.resolve_calls, 1)
        self.assertEqual(result.frames[0].text, "Grounded answer.")

    def test_invalid_planless_tool_call_does_not_start_turn_or_synthesize_plan(self):
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        executed = []
        provider = ScriptedProvider([
            [ProviderToolCallReady("call-1", "search", "not-json"), ProviderStreamCompleted("tool_calls")],
            [ProviderToolCallReady("call-2", "search", "not-json"), ProviderStreamCompleted("tool_calls")],
        ])
        registry = ToolRegistry((BoundTool(ToolSpec("search", "search", {"type": "object", "properties": {}}), lambda _: executed.append(True)),))
        events = []

        with patch("tomo_core.conversation.engine.latency_trace.emit_sandbox") as emit:
            iterator = ConversationEngine(provider, tool_registry=registry).respond_iter(self.request())
            with self.assertRaises(ConversationOutputError):
                while True:
                    events.append(next(iterator))

        self.assertEqual(events, [])
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(executed, [])
        self.assertFalse(any(isinstance(event, TurnRunStarted) for event in events))
        self.assertEqual(
            [call for call in emit.call_args_list if call.args[0] == "sandbox_provider_move_plan_validated" and "plan_synthesized" in call.kwargs],
            [],
        )

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

    def test_latency_labels_provider_repairs_and_aggregate_tool_batches_without_content(self):
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        provider = ScriptedProvider([
            [ProviderTextDelta("not json\n")],
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"Done."}\n'), ProviderStreamCompleted("stop")],
        ])
        registry = ToolRegistry((BoundTool(ToolSpec("search", "search", {"type": "object", "properties": {}}), lambda _: "private result"),))
        with patch("tomo_core.conversation.engine.latency_trace.emit_sandbox") as emit:
            ConversationEngine(provider, tool_registry=registry).respond(self.request())
        provider_calls = [call for call in emit.call_args_list if call.args[0] == "sandbox_provider_attempt"]
        tool_calls = [call for call in emit.call_args_list if call.args[0] == "sandbox_tool_batch"]
        self.assertEqual([(call.kwargs["attempt"], call.kwargs["segment"], call.kwargs["repair"]) for call in provider_calls], [(1, 0, 0), (2, 0, 1)])
        self.assertEqual([call.kwargs["outcome"] for call in provider_calls], ["error", "ok"])
        self.assertEqual(tool_calls, [])
        self.assertNotIn("call-private", repr(emit.call_args_list))
        self.assertNotIn("private result", repr(emit.call_args_list))

    def test_provider_stages_are_once_chunk_accurate_and_measure_consumer_suspension(self):
        clock = [0.0]
        first_frame = '{"type":"frame","text":"private frame"}\n'
        second_frame = '{"type":"frame","text":"later private frame"}\n'
        payload = PLAN + first_frame + second_frame
        provider = ScriptedProvider([
            [ProviderTextDelta(payload), ProviderStreamCompleted("stop", 12, 7, 3)],
        ])
        from tomo_core.conversation.framing import SegmentFrameParser

        original_finish = SegmentFrameParser.finish

        def finish_after_validation(parser):
            records = original_finish(parser)
            clock[0] += 0.250
            return records

        with patch("tomo_core.conversation.engine.latency_trace.emit_sandbox") as emit:
            with patch.object(SegmentFrameParser, "finish", finish_after_validation):
                iterator = ConversationEngine(provider, monotonic_clock=lambda: clock[0]).respond_iter(self.request())
                self.assertIsInstance(next(iterator), TurnRunStarted)
                clock[0] += 0.250
                self.assertIsInstance(next(iterator), FrameReady)
                clock[0] += 0.500
                self.assertIsInstance(next(iterator), FrameReady)
                self.assertIsInstance(next(iterator), TurnRunCompleted)

        provider_calls = [call for call in emit.call_args_list if call.kwargs.get("attempt") == 1]
        self.assertEqual(
            [(call.args[0], call.kwargs["elapsed_ms"]) for call in provider_calls],
            [
                ("sandbox_provider_attempt_start", 0),
                ("sandbox_provider_first_text_delta", 0),
                ("sandbox_provider_move_plan_validated", 0),
                ("sandbox_provider_first_frame_validated", 250),
                ("sandbox_provider_stream_completed", 750),
                ("sandbox_provider_attempt", 1000),
            ],
        )
        self.assertNotEqual(provider_calls[-2].kwargs["elapsed_ms"], provider_calls[-1].kwargs["elapsed_ms"])
        self.assertEqual(
            {name: value for name, value in provider_calls[2].kwargs.items() if name.startswith("plan_")},
            {"plan_normalized": 1},
        )
        self.assertEqual(provider_calls[-1].kwargs["suspended_ms"], 750)
        self.assertEqual(provider_calls[-1].kwargs["active_ms"], 250)
        self.assertEqual(provider_calls[-1].kwargs["input_tokens"], 12)
        self.assertEqual(provider_calls[-1].kwargs["output_tokens"], 7)
        self.assertEqual(provider_calls[-1].kwargs["reasoning_tokens"], 3)
        expected_first_frame_output = len(PLAN + first_frame)
        for call in (provider_calls[3], provider_calls[4], provider_calls[5]):
            self.assertEqual(call.kwargs["output_chars_through_first_frame"], expected_first_frame_output)
            self.assertEqual(call.kwargs["first_frame_chars"], len("private frame"))
        self.assertNotEqual(provider_calls[-1].kwargs["output_chars_through_first_frame"], len(payload))
        self.assertEqual(
            provider_calls[-1].kwargs["elapsed_ms"],
            provider_calls[-1].kwargs["active_ms"] + provider_calls[-1].kwargs["suspended_ms"],
        )
        self.assertNotIn("private frame", repr(emit.call_args_list))

    def test_consumer_close_finishes_provider_attempt_with_suspension_time(self):
        clock = [0.0]
        provider = ScriptedProvider([[ProviderTextDelta(PLAN)]])
        with patch("tomo_core.conversation.engine.latency_trace.emit_sandbox") as emit:
            iterator = ConversationEngine(provider, monotonic_clock=lambda: clock[0]).respond_iter(self.request())
            self.assertIsInstance(next(iterator), TurnRunStarted)
            clock[0] = 1.0
            iterator.close()

        attempt = [call for call in emit.call_args_list if call.args[0] == "sandbox_provider_attempt"]
        self.assertEqual(len(attempt), 1)
        self.assertEqual(attempt[0].kwargs["outcome"], "error")
        self.assertEqual(attempt[0].kwargs["elapsed_ms"], 1000)
        self.assertEqual(attempt[0].kwargs["suspended_ms"], 1000)
        self.assertEqual(attempt[0].kwargs["active_ms"], 0)
        self.assertTrue(provider.iterators[0].closed)

    def test_latency_times_one_aggregate_tool_batch(self):
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("call-private", "search", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Done."}\n'), ProviderStreamCompleted("stop")],
        ])
        registry = ToolRegistry((BoundTool(ToolSpec("search", "search", {"type": "object", "properties": {}}), lambda _: "private result"),))
        with patch("tomo_core.conversation.engine.latency_trace.emit_sandbox") as emit:
            ConversationEngine(provider, tool_registry=registry).respond(self.request())
        tool_calls = [call for call in emit.call_args_list if call.args[0] == "sandbox_tool_batch"]
        provider_calls = [call for call in emit.call_args_list if call.args[0] == "sandbox_provider_attempt"]
        self.assertEqual([call.kwargs["outcome"] for call in provider_calls], ["ok", "ok"])
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].kwargs["segment"], 0)
        self.assertNotIn("call-private", repr(emit.call_args_list))
        self.assertNotIn("private result", repr(emit.call_args_list))

    def test_frameless_tool_reaction_window_measures_both_consumer_suspensions(self):
        from tomo_core.tool_execution import ToolExecutor
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        clock = [0.0]
        reaction_plan = PLAN.rstrip("\n")[:-1] + ',"reaction":"👍"}\n'
        provider = ScriptedProvider([
            [ProviderTextDelta(reaction_plan), ProviderToolCallReady("call-1", "search", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Done."}\n'), ProviderStreamCompleted("stop")],
        ])
        registry = ToolRegistry((BoundTool(ToolSpec("search", "search", {"type": "object", "properties": {}}), lambda _: "result"),))

        class AdvancingExecutor(ToolExecutor):
            def execute_prepared_batch(self, *args, **kwargs):
                clock[0] += 10
                return super().execute_prepared_batch(*args, **kwargs)

        with patch("tomo_core.conversation.engine.latency_trace.emit_sandbox") as emit:
            iterator = ConversationEngine(provider, tool_registry=registry, tool_executor=AdvancingExecutor(registry), monotonic_clock=lambda: clock[0]).respond_iter(self.request())
            self.assertIsInstance(next(iterator), TurnRunStarted)
            clock[0] += 0.250
            self.assertIsInstance(next(iterator), ReactionWindowReady)
            clock[0] += 0.500
            self.assertIsInstance(next(iterator), FrameReady)
            self.assertIsInstance(next(iterator), TurnRunCompleted)

        attempts = [call for call in emit.call_args_list if call.args[0] == "sandbox_provider_attempt"]
        first_attempt = attempts[0]
        self.assertEqual(first_attempt.kwargs["elapsed_ms"], 750)
        self.assertEqual(first_attempt.kwargs["suspended_ms"], 750)
        self.assertEqual(first_attempt.kwargs["elapsed_ms"], first_attempt.kwargs["active_ms"] + first_attempt.kwargs["suspended_ms"])
        self.assertEqual([call.kwargs["elapsed_ms"] for call in emit.call_args_list if call.args[0] == "sandbox_tool_batch"], [10000])
        self.assertNotIn("result", repr(emit.call_args_list))

    def test_cancellation_after_tool_plan_suppresses_reaction_window_and_execution(self):
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        active = [True]
        invoked = []

        class SupersedingRegistry(ToolRegistry):
            def is_mutating(self, name: str) -> bool:
                active[0] = False
                return super().is_mutating(name)

        reaction_plan = PLAN.rstrip("\n")[:-1] + ',"reaction":"👍"}\n'
        provider = ScriptedProvider([[
            ProviderTextDelta(reaction_plan + '{"type":"frame","text":"Searching."}\n'), ProviderToolCallReady("call-1", "search", "{}"), ProviderStreamCompleted("tool_calls"),
        ]])
        registry = SupersedingRegistry((BoundTool(ToolSpec("search", "search", {"type": "object", "properties": {}}), lambda _: invoked.append(True)),))
        iterator = ConversationEngine(provider, tool_registry=registry).respond_iter(self.request(), is_active=lambda: active[0])

        events = list(iterator)

        self.assertEqual([type(event) for event in events], [TurnRunStarted])
        self.assertEqual(invoked, [])

    def test_tool_executor_failure_keeps_provider_ok_and_marks_tool_error(self):
        from tomo_core.tool_execution import ToolExecutor
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        class ExplodingExecutor(ToolExecutor):
            def execute_prepared_batch(self, *args, **kwargs):
                raise RuntimeError("private executor failure")

        registry = ToolRegistry((BoundTool(ToolSpec("search", "search", {"type": "object", "properties": {}}), lambda _: "result"),))
        provider = ScriptedProvider([[ProviderTextDelta(PLAN + '{"type":"frame","text":"Checking."}\n'), ProviderToolCallReady("call-1", "search", "{}"), ProviderStreamCompleted("tool_calls")]])
        with patch("tomo_core.conversation.engine.latency_trace.emit_sandbox") as emit:
            events = list(ConversationEngine(provider, tool_registry=registry, tool_executor=ExplodingExecutor(registry)).respond_iter(self.request()))
        provider_calls = [call for call in emit.call_args_list if call.args[0] == "sandbox_provider_attempt"]
        tool_calls = [call for call in emit.call_args_list if call.args[0] == "sandbox_tool_batch"]
        self.assertEqual([call.kwargs["outcome"] for call in provider_calls], ["ok"])
        self.assertEqual([call.kwargs["outcome"] for call in tool_calls], ["error"])
        self.assertEqual(events[-1].result.status, TurnRunStatus.COMPLETED_PARTIAL)

    def test_provider_elapsed_excludes_tool_execution_time(self):
        from tomo_core.tool_execution import ToolExecutor
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        clock = [0.0]

        class AdvancingExecutor(ToolExecutor):
            def execute_prepared_batch(self, *args, **kwargs):
                clock[0] += 10
                return super().execute_prepared_batch(*args, **kwargs)

        registry = ToolRegistry((BoundTool(ToolSpec("search", "search", {"type": "object", "properties": {}}), lambda _: "result"),))
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("call-1", "search", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Done."}\n'), ProviderStreamCompleted("stop")],
        ])
        with patch("tomo_core.conversation.engine.latency_trace.emit_sandbox") as emit:
            ConversationEngine(provider, tool_registry=registry, tool_executor=AdvancingExecutor(registry), monotonic_clock=lambda: clock[0]).respond(self.request())
        provider_calls = [call for call in emit.call_args_list if call.args[0] == "sandbox_provider_attempt"]
        tool_calls = [call for call in emit.call_args_list if call.args[0] == "sandbox_tool_batch"]
        self.assertEqual(provider_calls[0].kwargs["elapsed_ms"], 0)
        self.assertEqual(tool_calls[0].kwargs["elapsed_ms"], 10000)

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

    def test_empty_stop_and_malformed_json_still_use_one_repair(self):
        for first_attempt, repair_code in (
            ([ProviderStreamCompleted("stop")], "missing_frame"),
            ([ProviderTextDelta("not json\n"), ProviderStreamCompleted("stop")], "invalid_json"),
        ):
            with self.subTest(repair_code=repair_code):
                provider = ScriptedProvider([
                    first_attempt,
                    [ProviderTextDelta(CANONICAL_PLAN + '{"type":"frame","text":"Recovered answer."}\n'), ProviderStreamCompleted("stop")],
                ])

                result = ConversationEngine(provider).respond(self.request())

                self.assertEqual(result.frames[0].text, "Recovered answer.")
                self.assertEqual(result.usage.contract_repairs, 1)
                self.assertEqual(len(provider.calls), 2)
                self.assertIn(repair_code, provider.calls[1][0][0]["content"])

    def test_invalid_first_response_repairs_with_one_mutating_tool_call(self):
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        executed = []
        provider = ScriptedProvider([
            [ProviderTextDelta("not json\n"), ProviderStreamCompleted("stop")],
            [ProviderToolCallReady("call-1", "schedule_reminder", '{"when":"tomorrow"}'), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Your reminder is scheduled for tomorrow."}\n'), ProviderStreamCompleted("stop")],
        ])
        registry = ToolRegistry((BoundTool(
            ToolSpec("schedule_reminder", "Schedule a reminder", {"type": "object", "properties": {"when": {"type": "string"}}, "required": ["when"]}, read_only=False),
            lambda arguments: executed.append(arguments) or "scheduled",
        ),))

        result = ConversationEngine(provider, tool_registry=registry).respond(self.request())

        self.assertEqual(executed, [{"when": "tomorrow"}])
        self.assertNotEqual(provider.calls[1][1], ())
        self.assertEqual(provider.calls[1][1][0]["function"]["name"], "schedule_reminder")
        self.assertEqual([segment.finish for segment in result.segments], [SegmentFinish.TOOL_BATCH, SegmentFinish.COMPLETE])
        self.assertEqual([frame.text for frame in result.frames], ["Your reminder is scheduled for tomorrow."])
        self.assertEqual(result.usage.contract_repairs, 1)

    def test_mutating_callback_failure_fences_tools_for_remaining_turn(self):
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        mutations = []

        class RetryWhileToolsProvider(ScriptedProvider):
            def __init__(self):
                super().__init__(())

            def stream(self, messages, *, tools=(), actor_id=None):
                self.calls.append((messages, tools, actor_id))
                attempt = len(self.calls)
                scripted = (
                    [ProviderToolCallReady(f"call-{attempt}", "schedule_reminder", '{"when":"tomorrow"}'), ProviderStreamCompleted("tool_calls")]
                    if attempt <= 2 and tools
                    else [ProviderTextDelta('{"type":"frame","text":"I could not confirm the reminder status."}\n'), ProviderStreamCompleted("stop")]
                )
                iterator = ClosingIterator(scripted)
                self.iterators.append(iterator)
                return iterator

        def mutate(arguments):
            mutations.append(arguments)
            raise RuntimeError("committed mutation failed to report")

        provider = RetryWhileToolsProvider()
        registry = ToolRegistry((BoundTool(
            ToolSpec("schedule_reminder", "Schedule a reminder", {"type": "object", "properties": {"when": {"type": "string"}}, "required": ["when"]}, read_only=False),
            mutate,
        ),))

        result = ConversationEngine(provider, tool_registry=registry).respond(self.request())

        self.assertEqual(mutations, [{"when": "tomorrow"}])
        self.assertNotEqual(provider.calls[0][1], ())
        self.assertEqual(provider.calls[1][1], ())
        self.assertEqual([frame.text for frame in result.frames], ["I could not confirm the reminder status."])

    def test_fixed_plan_repair_can_execute_mutating_tool_before_fenced_continuation(self):
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        mutations = []
        provider = ScriptedProvider([
            [ProviderTextDelta(CANONICAL_PLAN + "not json\n"), ProviderStreamCompleted("stop")],
            [ProviderToolCallReady("call-1", "schedule_reminder", '{"when":"tomorrow"}'), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Your reminder is scheduled for tomorrow."}\n'), ProviderStreamCompleted("stop")],
        ])
        registry = ToolRegistry((BoundTool(
            ToolSpec("schedule_reminder", "Schedule a reminder", {"type": "object", "properties": {"when": {"type": "string"}}, "required": ["when"]}, read_only=False),
            lambda arguments: mutations.append(arguments) or "scheduled",
        ),))

        events = list(ConversationEngine(provider, tool_registry=registry).respond_iter(self.request()))

        self.assertEqual(events[0].plan.response_goal, "answer directly")
        self.assertEqual(sum(isinstance(event, TurnRunStarted) for event in events), 1)
        self.assertEqual(events[-1].result.plan, events[0].plan)
        self.assertNotEqual(provider.calls[1][1], ())
        self.assertEqual(provider.calls[2][1], ())
        self.assertEqual(mutations, [{"when": "tomorrow"}])
        self.assertEqual([frame.frame.text for frame in events if isinstance(frame, FrameReady)], ["Your reminder is scheduled for tomorrow."])

    def test_failed_mutating_execution_repair_is_tool_free(self):
        from tomo_core.tool_execution import ToolExecutor
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        class FailingExecutor(ToolExecutor):
            def execute_prepared_batch(self, *args, **kwargs):
                raise RuntimeError("executor failure")

        registry = ToolRegistry((BoundTool(
            ToolSpec("schedule_reminder", "Schedule a reminder", {"type": "object", "properties": {}}, read_only=False),
            lambda _: self.fail("the failed batch must not invoke the tool"),
        ),))
        provider = ScriptedProvider([
            [ProviderToolCallReady("call-1", "schedule_reminder", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"I could not schedule that."}\n'), ProviderStreamCompleted("stop")],
        ])

        result = ConversationEngine(provider, tool_registry=registry, tool_executor=FailingExecutor(registry)).respond(self.request())

        self.assertEqual(provider.calls[1][1], ())
        self.assertEqual([frame.text for frame in result.frames], ["I could not schedule that."])
        self.assertEqual(result.usage.contract_repairs, 1)

    def test_cancelled_mutating_execution_does_not_repair_or_retry(self):
        from tomo_core.tool_execution import ToolBatchCancelled, ToolExecutor
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        attempts = []

        class CancellingExecutor(ToolExecutor):
            def execute_prepared_batch(self, *args, **kwargs):
                attempts.append(True)
                raise ToolBatchCancelled

        registry = ToolRegistry((BoundTool(
            ToolSpec("schedule_reminder", "Schedule a reminder", {"type": "object", "properties": {}}, read_only=False),
            lambda _: self.fail("the cancelled batch must not invoke the bound tool"),
        ),))
        provider = ScriptedProvider([
            [ProviderToolCallReady("call-1", "schedule_reminder", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderToolCallReady("call-2", "schedule_reminder", "{}"), ProviderStreamCompleted("tool_calls")],
        ])

        events = list(ConversationEngine(
            provider,
            tool_registry=registry,
            tool_executor=CancellingExecutor(registry),
        ).respond_iter(self.request()))

        self.assertEqual(attempts, [True])
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual([type(event) for event in events], [TurnRunStarted])

    def test_planless_native_tool_attempt_synthesizes_only_when_available(self):
        from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec

        executed = []
        provider = ScriptedProvider([
            [ProviderToolCallReady("call-1", "search", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Recovered answer."}\n'), ProviderStreamCompleted("stop")],
        ])
        registry = ToolRegistry((BoundTool(ToolSpec("search", "search", {"type": "object", "properties": {}}), lambda _: executed.append(True) or "found"),))

        result = ConversationEngine(provider, tool_registry=registry).respond(self.request())

        self.assertEqual(result.frames[0].text, "Recovered answer.")
        self.assertNotEqual(provider.calls[0][1], ())
        self.assertNotEqual(provider.calls[1][1], ())
        self.assertEqual(executed, [True])

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

            def execute_prepared_batch(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return super().execute_prepared_batch(*args, **kwargs)
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
