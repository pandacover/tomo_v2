import unittest

import json

from tomo_core.conversation import ConversationEngine, ConversationRequest, FrameReady, MemoryControlReady, SegmentFinish, TurnBudget, TurnRunCompleted, TurnRunStatus
from tomo_core.conversation.parsing import ConversationOutputError
from tomo_core.models import InboundEnvelope
from tomo_core.providers import ProviderStreamCompleted, ProviderTextDelta, ProviderToolCallReady
from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec


PLAN = '{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"move_sequence":["answer"],"response_goal":"answer directly","confidence":"high"}\n'


class ScriptedProvider:
    name = "scripted"
    supports_images_in = False
    supports_images_out = False
    supports_tool_calls = True

    def __init__(self, streams):
        self.streams = list(streams)
        self.calls = []

    def stream(self, messages, *, tools=(), actor_id=None):
        self.calls.append((messages, tools, actor_id))
        return iter(self.streams.pop(0))


def registry(*tools):
    return ToolRegistry(tuple(tools))


def tool(name, invoke):
    return BoundTool(ToolSpec(name, name, {"type": "object", "properties": {}}), invoke)


class TurnRunToolTests(unittest.TestCase):
    def request(self):
        return ConversationRequest.from_history(
            envelope=InboundEnvelope("telegram", "user", "message", "look it up"),
            soul="SOUL",
            history=(),
        )

    def test_first_segment_tool_only_then_final_segment_frame(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("lookup-1", "lookup", '{"q":"x"}'), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"I found it."}\n'), ProviderStreamCompleted("stop")],
        ])

        result = ConversationEngine(provider, tool_registry=registry(tool("lookup", lambda _: "found"))).respond(self.request())

        self.assertEqual([segment.finish for segment in result.segments], [SegmentFinish.TOOL_BATCH, SegmentFinish.COMPLETE])
        self.assertEqual(result.frames[-1].text, "I found it.")
        self.assertEqual(result.segments[0].tool_calls[0].name, "lookup")
        self.assertEqual(result.usage.tool_rounds, 1)
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0][1][0]["function"]["name"], "lookup")
        self.assertEqual(provider.calls[1][0][-2]["role"], "assistant")
        message = provider.calls[1][0][-1]
        self.assertEqual(set(message), {"role", "tool_call_id", "content"})
        self.assertEqual(json.loads(message["content"]), {"name": "lookup", "ok": True, "content": "found"})

    def test_memory_control_after_tool_batch_receives_only_that_turns_observation_id(self):
        control = json.dumps({
            "type": "memory_control", "action": "add", "authority": "autonomous", "user_intent_excerpt": None,
            "memory_id": None, "kind": "fact", "subject_key": "self", "topic": "lookup.result",
            "value": "found", "statement": "Lookup found a result", "confidence": 0.8, "salience": 0.5,
            "surface_scope": "contextual", "valid_from": None, "valid_until": None,
            "sources": [{"source_kind": "tool_observation", "source_id": "lookup-1", "observed_at": "2026-01-01T00:00:00Z"}],
        })
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("lookup-1", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta(control + '\n{"type":"frame","text":"I found it."}\n'), ProviderStreamCompleted("stop")],
        ])

        events = list(ConversationEngine(provider, tool_registry=registry(tool("lookup", lambda _: "found"))).respond_iter(self.request()))

        ready = next(event for event in events if isinstance(event, MemoryControlReady))
        self.assertEqual(ready.tool_observation_ids, ("lookup-1",))

    def test_malformed_continuation_after_tool_only_segment_does_not_repair(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("lookup-1", "lookup", '{"q":"x"}'), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta("not json\n")],
            [ProviderTextDelta('{"type":"frame","text":"must not run."}\n'), ProviderStreamCompleted("stop")],
        ])
        events = []
        iterator = ConversationEngine(provider, tool_registry=registry(tool("lookup", lambda _: "found"))).respond_iter(self.request())

        with self.assertRaisesRegex(ConversationOutputError, "invalid conversation output"):
            events.extend(iterator)

        self.assertEqual(len(provider.calls), 2)
        self.assertFalse(any(isinstance(event, (FrameReady, TurnRunCompleted)) for event in events))

    def test_stop_without_frame_after_tool_only_segment_does_not_repair(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("lookup-1", "lookup", '{"q":"x"}'), ProviderStreamCompleted("tool_calls")],
            [ProviderStreamCompleted("stop")],
            [ProviderTextDelta('{"type":"frame","text":"must not run."}\n'), ProviderStreamCompleted("stop")],
        ])
        events = []
        iterator = ConversationEngine(provider, tool_registry=registry(tool("lookup", lambda _: "found"))).respond_iter(self.request())

        with self.assertRaisesRegex(ConversationOutputError, "invalid conversation output"):
            events.extend(iterator)

        self.assertEqual(len(provider.calls), 2)
        self.assertFalse(any(isinstance(event, (FrameReady, TurnRunCompleted)) for event in events))

    def test_parallel_calls_and_safe_failures_are_ordered_in_the_next_segment(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"Checking."}\n'), ProviderToolCallReady("one", "good", "{}"), ProviderToolCallReady("two", "bad", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Verified answer."}\n'), ProviderStreamCompleted("stop")],
        ])
        result = ConversationEngine(provider, tool_registry=registry(tool("good", lambda _: "yes"), tool("bad", lambda _: (_ for _ in ()).throw(RuntimeError("secret"))))).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["Checking.", "Verified answer."])
        self.assertEqual([call.call_id for call in result.segments[0].tool_calls], ["one", "two"])
        continuation = provider.calls[1][0][-3:]
        self.assertEqual(json.loads(continuation[1]["content"]), {"name": "good", "ok": True, "content": "yes"})
        self.assertEqual(json.loads(continuation[2]["content"])["error_code"], "tool_execution_failed")
        self.assertNotIn("secret", continuation[2]["content"])

    def test_dependent_tools_use_separate_rounds(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("one", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderToolCallReady("two", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Final answer."}\n'), ProviderStreamCompleted("stop")],
        ])
        result = ConversationEngine(provider, tool_registry=registry(tool("lookup", lambda arguments: "ok"))).respond(self.request())

        self.assertEqual(result.usage.tool_rounds, 2)
        self.assertEqual(result.usage.tool_calls, 2)
        self.assertEqual([segment.index for segment in result.segments], [0, 1, 2])
        self.assertEqual(provider.calls[2][0][-1]["tool_call_id"], "two")

    def test_cross_round_duplicate_call_after_visible_frame_is_not_executed(self):
        invoked = []
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"Checking."}\n'), ProviderToolCallReady("same", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderToolCallReady("same", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
        ])

        result = ConversationEngine(provider, tool_registry=registry(tool("lookup", lambda _: invoked.append(True)))).respond(self.request())

        self.assertEqual(len(invoked), 1)
        self.assertEqual(result.status, TurnRunStatus.COMPLETED_PARTIAL)
        self.assertEqual([segment.finish for segment in result.segments], [SegmentFinish.TOOL_BATCH, SegmentFinish.FAILED])

    def test_cross_round_duplicate_call_after_tool_only_round_fails_without_repair(self):
        invoked = []
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("same", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderToolCallReady("same", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"must not run."}\n'), ProviderStreamCompleted("stop")],
        ])

        with self.assertRaisesRegex(ConversationOutputError, "invalid conversation output"):
            ConversationEngine(provider, tool_registry=registry(tool("lookup", lambda _: invoked.append(True)))).respond(self.request())

        self.assertEqual(len(invoked), 1)
        self.assertEqual(len(provider.calls), 2)

    def test_elapsed_at_next_segment_after_visible_tool_batch_completes_partial(self):
        class Clock:
            def __init__(self):
                self.after_tool = False
                self.checks_after_tool = 0

            def __call__(self):
                if not self.after_tool:
                    return 0
                self.checks_after_tool += 1
                return 0 if self.checks_after_tool == 1 else 1

        clock = Clock()
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"Checking."}\n'), ProviderToolCallReady("one", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
        ])

        result = ConversationEngine(
            provider,
            tool_registry=registry(tool("lookup", lambda _: setattr(clock, "after_tool", True))),
            budget=TurnBudget(6, 5, 5, 3, 3, 2, 800, 1, 0.5),
            monotonic_clock=clock,
        ).respond(self.request())

        self.assertEqual(result.status, TurnRunStatus.COMPLETED_PARTIAL)
        self.assertEqual([segment.finish for segment in result.segments], [SegmentFinish.TOOL_BATCH])
        self.assertEqual(len(provider.calls), 1)

    def test_cancellation_after_tool_observations_prevents_next_segment_and_completion(self):
        active = [True]
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("one", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"must not run."}\n'), ProviderStreamCompleted("stop")],
        ])

        events = list(
            ConversationEngine(
                provider,
                tool_registry=registry(tool("lookup", lambda _: active.__setitem__(0, False) or "found")),
            ).respond_iter(self.request(), is_active=lambda: active[0])
        )

        self.assertEqual(len(provider.calls), 1)
        self.assertFalse(any(isinstance(event, TurnRunCompleted) for event in events))

    def test_call_budget_removes_schemas_and_finalizes_from_observations(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("1", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderToolCallReady("2", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderToolCallReady("3", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderToolCallReady("4", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderToolCallReady("5", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Five checked results."}\n'), ProviderStreamCompleted("stop")],
        ])
        result = ConversationEngine(provider, tool_registry=registry(tool("lookup", lambda _: "ok"))).respond(self.request())

        self.assertEqual(result.usage.model_segments, 6)
        self.assertEqual(result.usage.tool_calls, 5)
        self.assertEqual(provider.calls[-1][1], ())

    def test_visible_tool_segments_reserve_the_final_segment_from_tools(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"First."}\n'), ProviderToolCallReady("1", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Second."}\n'), ProviderToolCallReady("2", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Final."}\n'), ProviderStreamCompleted("stop")],
        ])
        result = ConversationEngine(provider, tool_registry=registry(tool("lookup", lambda _: "ok")), budget=TurnBudget(6, 5, 5, 3, 3, 2, 800)).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["First.", "Second.", "Final."])
        self.assertEqual(provider.calls[2][1], ())

    def test_two_pre_tool_frames_leave_one_frame_for_the_final_segment(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"First."}\n'), ProviderToolCallReady("1", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Second."}\n'), ProviderToolCallReady("2", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Final."}\n'), ProviderStreamCompleted("stop")],
        ])

        result = ConversationEngine(provider, tool_registry=registry(tool("lookup", lambda _: "ok"))).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["First.", "Second.", "Final."])
        self.assertIn("at most 1 frames per segment", provider.calls[2][0][0]["content"])

    def test_fourth_frame_completes_partial_without_executing_later_tools(self):
        invoked = []
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"One."}\n{"type":"frame","text":"Two."}\n{"type":"frame","text":"Three."}\n{"type":"frame","text":"Four."}\n'), ProviderToolCallReady("1", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
        ])

        result = ConversationEngine(provider, tool_registry=registry(tool("lookup", lambda _: invoked.append(True)))).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["One.", "Two.", "Three."])
        self.assertEqual(result.status, TurnRunStatus.COMPLETED_PARTIAL)
        self.assertEqual(invoked, [])


if __name__ == "__main__":
    unittest.main()
