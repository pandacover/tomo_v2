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
    def test_failed_peer_ask_cannot_be_followed_by_a_fabricated_answer(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("peer-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"bob is definitely free at 3pm."}\n'), ProviderStreamCompleted("stop")],
        ])
        peer_ask = BoundTool(
            ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False),
            lambda _: {"ok": False, "status": "failed"},
        )

        result = ConversationEngine(provider, tool_registry=registry(peer_ask)).respond(self.request())

        self.assertEqual(
            [frame.text for frame in result.frames],
            ["i couldn't get an answer from that tomo. try again in a moment."],
        )
        self.assertEqual(len(provider.calls), 1)

    def test_completed_peer_ask_emits_only_server_frames(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("peer-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"embellishment"}\n'), ProviderStreamCompleted("stop")],
        ])
        peer_ask = BoundTool(ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False), lambda _: {"ok": True, "status": "completed", "frames": ["bob is free after 3pm."]})

        result = ConversationEngine(provider, tool_registry=registry(peer_ask)).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["bob is free after 3pm."])
        self.assertEqual(len(provider.calls), 1)

    def test_confirmation_pending_peer_result_is_runtime_owned(self):
        provider = ScriptedProvider([[ProviderTextDelta(PLAN), ProviderToolCallReady("peer-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")]])
        peer_ask = BoundTool(ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False), lambda _: {"ok": True, "status": "confirmation_pending"})

        result = ConversationEngine(provider, tool_registry=registry(peer_ask)).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["that tomo needs their owner's approval before answering. check back after they approve it."])
        self.assertEqual(len(provider.calls), 1)

    def test_pending_peer_result_allows_peer_resume_then_fences_completed_result(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("ask-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderToolCallReady("resume-1", "peer_resume", "{}"), ProviderStreamCompleted("tool_calls")],
        ])
        peer_ask = BoundTool(ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False), lambda _: {"ok": True, "status": "pending", "thread_id": "thread"})
        peer_resume = BoundTool(ToolSpec("peer_resume", "resume peer", {"type": "object", "properties": {}}, read_only=True, parallel_safe=True), lambda _: {"ok": True, "status": "completed", "frames": ["safe result"]})

        result = ConversationEngine(provider, tool_registry=registry(peer_ask, peer_resume)).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["safe result"])
        self.assertEqual(len(provider.calls), 2)

    def test_pending_peer_result_cannot_be_replaced_by_a_guessed_answer(self):
        fabricated_memory = json.dumps({
            "type": "memory_control", "action": "add", "authority": "autonomous", "user_intent_excerpt": None,
            "memory_id": None, "kind": "fact", "subject_key": "bob", "topic": "peer.availability",
            "value": "bob is probably free now", "statement": "bob is probably free now", "confidence": 0.9, "salience": 0.5,
            "surface_scope": "contextual", "valid_from": None, "valid_until": None,
            "sources": [{"source_kind": "tool_observation", "source_id": "ask-1", "observed_at": "2026-01-01T00:00:00Z"}],
        })
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("ask-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta(fabricated_memory + '\n{"type":"frame","text":"bob is probably free now."}\n'), ProviderStreamCompleted("stop")],
        ])
        peer_ask = BoundTool(
            ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False),
            lambda _: {"ok": True, "status": "pending", "thread_id": "thread"},
        )
        peer_resume = BoundTool(
            ToolSpec("peer_resume", "resume peer", {"type": "object", "properties": {}}, read_only=True, parallel_safe=True),
            lambda _: {"ok": True, "status": "pending", "thread_id": "thread"},
        )

        events = tuple(ConversationEngine(provider, tool_registry=registry(peer_ask, peer_resume)).respond_iter(self.request()))
        result = next(event.result for event in events if isinstance(event, TurnRunCompleted))

        self.assertEqual(
            [frame.text for frame in result.frames],
            ["that tomo hasn't answered yet. i don't have an answer yet."],
        )
        self.assertFalse(any(isinstance(event, MemoryControlReady) for event in events))
        self.assertTrue(all(not segment.memory_controls for segment in result.segments))
        self.assertEqual(len(provider.calls), 2)

    def test_pending_peer_resume_never_releases_a_pretool_guessed_frame(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("ask-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"bob is free now."}\n'), ProviderToolCallReady("resume-1", "peer_resume", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"still guessing."}\n'), ProviderStreamCompleted("stop")],
        ])
        peer_ask = BoundTool(
            ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False),
            lambda _: {"ok": True, "status": "pending", "thread_id": "thread"},
        )
        peer_resume = BoundTool(
            ToolSpec("peer_resume", "resume peer", {"type": "object", "properties": {}}, read_only=True, parallel_safe=True),
            lambda _: {"ok": True, "status": "pending", "thread_id": "thread"},
        )

        events = tuple(ConversationEngine(provider, tool_registry=registry(peer_ask, peer_resume)).respond_iter(self.request()))
        result = next(event.result for event in events if isinstance(event, TurnRunCompleted))

        self.assertEqual(
            [event.frame.text for event in events if isinstance(event, FrameReady)],
            ["that tomo hasn't answered yet. i don't have an answer yet."],
        )
        self.assertEqual(
            [frame.text for frame in result.frames],
            ["that tomo hasn't answered yet. i don't have an answer yet."],
        )

    def test_pending_peer_with_exhausted_tool_budget_suppresses_guessed_frame(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("ask-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"guessed peer answer"}\n'), ProviderStreamCompleted("stop")],
        ])
        peer_ask = BoundTool(
            ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False),
            lambda _: {"ok": True, "status": "pending", "thread_id": "thread"},
        )

        result = ConversationEngine(
            provider,
            tool_registry=registry(peer_ask),
            budget=TurnBudget(3, 1, 1, 2, 3, 2, 800),
        ).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["that tomo hasn't answered yet. i don't have an answer yet."])
        self.assertEqual(provider.calls[1][1], ())

    def test_pending_peer_cannot_execute_a_tool_omitted_from_narrowed_schema(self):
        invoked = []
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("ask-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderToolCallReady("create-1", "cron_create", "{}"), ProviderStreamCompleted("tool_calls")],
        ])
        peer_ask = BoundTool(
            ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False),
            lambda _: {"ok": True, "status": "pending", "thread_id": "thread"},
        )
        peer_resume = BoundTool(ToolSpec("peer_resume", "resume peer", {"type": "object", "properties": {}}, read_only=True, parallel_safe=True), lambda _: {"ok": True, "status": "pending", "thread_id": "thread"})
        mutation = BoundTool(ToolSpec("cron_create", "create", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False), lambda _: invoked.append(True) or "created")

        result = ConversationEngine(provider, tool_registry=registry(peer_ask, peer_resume, mutation)).respond(self.request())

        self.assertEqual(invoked, [])
        self.assertEqual([frame.text for frame in result.frames], ["that tomo hasn't answered yet. i don't have an answer yet."])
        self.assertEqual([schema["function"]["name"] for schema in provider.calls[1][1]], ["peer_resume"])

    def test_peer_timeout_returns_pending_frame_when_elapsed_budget_expires_during_tool(self):
        now = [0.0]
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("ask-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")],
        ])
        peer_ask = BoundTool(
            ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False),
            lambda _: now.__setitem__(0, 1.0) or {"ok": True, "status": "pending", "thread_id": "thread"},
        )

        result = ConversationEngine(
            provider,
            tool_registry=registry(peer_ask),
            budget=TurnBudget(3, 2, 2, 2, 3, 2, 800, 1, 0.5),
            monotonic_clock=lambda: now[0],
        ).respond(self.request())

        self.assertEqual(
            [frame.text for frame in result.frames],
            ["that tomo hasn't answered yet. i don't have an answer yet."],
        )
        self.assertEqual(len(provider.calls), 1)

    def test_completed_peer_result_is_not_released_after_elapsed_budget_expires_during_tool(self):
        now = [0.0]
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("ask-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")],
        ])
        peer_ask = BoundTool(
            ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False),
            lambda _: now.__setitem__(0, 2.0) or {"ok": True, "status": "completed", "frames": ["late peer result"]},
        )

        with self.assertRaisesRegex(ConversationOutputError, "elapsed_budget_exhausted"):
            ConversationEngine(
                provider,
                tool_registry=registry(peer_ask),
                budget=TurnBudget(3, 2, 2, 2, 3, 2, 800, 1, 1.0),
                monotonic_clock=lambda: now[0],
            ).respond(self.request())

    def test_pending_peer_is_returned_when_budget_expires_at_next_segment_boundary(self):
        phase = {"after_tool": False, "clock_calls": 0}

        def clock():
            if not phase["after_tool"]:
                return 0.0
            phase["clock_calls"] += 1
            return 0.5 if phase["clock_calls"] <= 3 else 2.0

        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("ask-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")],
        ])
        peer_ask = BoundTool(
            ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False),
            lambda _: phase.__setitem__("after_tool", True) or {"ok": True, "status": "pending", "thread_id": "thread"},
        )

        result = ConversationEngine(
            provider,
            tool_registry=registry(peer_ask),
            budget=TurnBudget(3, 2, 2, 2, 3, 2, 800, 1, 1.0),
            monotonic_clock=clock,
        ).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["that tomo hasn't answered yet. i don't have an answer yet."])
        self.assertGreaterEqual(phase["clock_calls"], 4)

    def test_pending_peer_fallback_discards_pre_result_memory_controls(self):
        control = json.dumps({
            "type": "memory_control", "action": "add", "authority": "autonomous", "user_intent_excerpt": None,
            "memory_id": None, "kind": "fact", "subject_key": "bob", "topic": "peer.availability",
            "value": "bob is probably free now", "statement": "bob is probably free now", "confidence": 0.9, "salience": 0.5,
            "surface_scope": "contextual", "valid_from": None, "valid_until": None,
            "sources": [{"source_kind": "tool_observation", "source_id": "ask-1", "observed_at": "2026-01-01T00:00:00Z"}],
        })
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("ask-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta(control + '\n'), ProviderStreamCompleted("stop")],
        ])
        peer_ask = BoundTool(
            ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False),
            lambda _: {"ok": True, "status": "pending", "thread_id": "thread"},
        )
        peer_resume = BoundTool(
            ToolSpec("peer_resume", "resume peer", {"type": "object", "properties": {}}, read_only=True, parallel_safe=True),
            lambda _: {"ok": True, "status": "pending", "thread_id": "thread"},
        )

        result = ConversationEngine(provider, tool_registry=registry(peer_ask, peer_resume)).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["that tomo hasn't answered yet. i don't have an answer yet."])
        self.assertTrue(all(not segment.memory_controls for segment in result.segments))

    def test_malformed_peer_resume_after_pending_returns_only_pending_frame(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("ask-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderToolCallReady("resume-1", "peer_resume", "{"), ProviderStreamCompleted("tool_calls")],
        ])
        peer_ask = BoundTool(
            ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False),
            lambda _: {"ok": True, "status": "pending", "thread_id": "thread"},
        )
        peer_resume = BoundTool(
            ToolSpec(
                "peer_resume", "resume peer",
                {"type": "object", "properties": {"thread_id": {"type": "string"}}, "required": ["thread_id"], "additionalProperties": False},
                read_only=True, parallel_safe=True,
            ),
            lambda _: {"ok": True, "status": "completed", "frames": ["unsafe"]},
        )

        result = ConversationEngine(provider, tool_registry=registry(peer_ask, peer_resume)).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["that tomo hasn't answered yet. i don't have an answer yet."])
        self.assertEqual(len(provider.calls), 2)

    def test_completed_peer_result_discards_pre_result_memory_controls(self):
        control = json.dumps({
            "type": "memory_control", "action": "add", "authority": "autonomous", "user_intent_excerpt": None,
            "memory_id": None, "kind": "fact", "subject_key": "bob", "topic": "peer.availability",
            "value": "bob is free", "statement": "bob is free", "confidence": 0.9, "salience": 0.5,
            "surface_scope": "contextual", "valid_from": None, "valid_until": None,
            "sources": [{"source_kind": "tool_observation", "source_id": "ask-1", "observed_at": "2026-01-01T00:00:00Z"}],
        })
        provider = ScriptedProvider([[ProviderTextDelta(PLAN + control + '\n'), ProviderToolCallReady("ask-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")]])
        peer_ask = BoundTool(
            ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False),
            lambda _: {"ok": True, "status": "completed", "frames": ["bob is free"]},
        )

        events = tuple(ConversationEngine(provider, tool_registry=registry(peer_ask)).respond_iter(self.request()))
        result = next(event.result for event in events if isinstance(event, TurnRunCompleted))

        self.assertEqual([frame.text for frame in result.frames], ["bob is free"])
        self.assertFalse(any(isinstance(event, MemoryControlReady) for event in events))
        self.assertTrue(all(not segment.memory_controls for segment in result.segments))

    def test_failed_peer_resume_cannot_be_followed_by_a_fabricated_answer(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("peer-1", "peer_resume", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"invented answer"}\n'), ProviderStreamCompleted("stop")],
        ])
        peer_resume = BoundTool(ToolSpec("peer_resume", "resume peer", {"type": "object", "properties": {}}, read_only=True, parallel_safe=True), lambda _: {"ok": False, "status": "failed"})

        result = ConversationEngine(provider, tool_registry=registry(peer_resume)).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["i couldn't get an answer from that tomo. try again in a moment."])
        self.assertEqual(len(provider.calls), 1)

    def test_peer_execution_failure_reports_unavailability_not_permissions(self):
        provider = ScriptedProvider([[
            ProviderTextDelta(PLAN),
            ProviderToolCallReady("peer-1", "peer_resume", "{}"),
            ProviderStreamCompleted("tool_calls"),
        ]])
        peer_resume = BoundTool(
            ToolSpec("peer_resume", "resume peer", {"type": "object", "properties": {}}, read_only=True, parallel_safe=True),
            lambda _: {"ok": True, "status": "failed", "error_code": "peer_unavailable"},
        )

        result = ConversationEngine(provider, tool_registry=registry(peer_resume)).respond(self.request())

        self.assertEqual(
            [frame.text for frame in result.frames],
            ["that tomo is unavailable right now. try again in a moment."],
        )

    def test_denied_peer_request_reports_changed_connection(self):
        provider = ScriptedProvider([[
            ProviderTextDelta(PLAN),
            ProviderToolCallReady("peer-1", "peer_resume", "{}"),
            ProviderStreamCompleted("tool_calls"),
        ]])
        peer_resume = BoundTool(
            ToolSpec("peer_resume", "resume peer", {"type": "object", "properties": {}}, read_only=True, parallel_safe=True),
            lambda _: {"ok": True, "status": "denied"},
        )

        result = ConversationEngine(provider, tool_registry=registry(peer_resume)).respond(self.request())

        self.assertEqual(
            [frame.text for frame in result.frames],
            ["the connection or its permissions changed before that tomo could answer. check tomo connections, then try again."],
        )

    def test_completed_direct_peer_resume_emits_only_returned_safe_frames(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"bob is free now."}\n'), ProviderToolCallReady("peer-1", "peer_resume", "{}"), ProviderStreamCompleted("tool_calls")],
        ])
        peer_resume = BoundTool(
            ToolSpec("peer_resume", "resume peer", {"type": "object", "properties": {}}, read_only=True, parallel_safe=True),
            lambda _: {"ok": True, "status": "completed", "frames": ["safe result"]},
        )

        events = tuple(ConversationEngine(provider, tool_registry=registry(peer_resume)).respond_iter(self.request()))

        self.assertEqual([event.frame.text for event in events if isinstance(event, FrameReady)], ["safe result"])

    def test_terminal_peer_frames_fail_closed_instead_of_exceeding_remaining_turn_frames(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"Checking."}\n'), ProviderToolCallReady("lookup-1", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderToolCallReady("ask-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")],
        ])
        lookup = BoundTool(ToolSpec("lookup", "lookup", {"type": "object", "properties": {}}, read_only=True, parallel_safe=True), lambda _: "found")
        peer_ask = BoundTool(
            ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False),
            lambda _: {"ok": True, "status": "completed", "frames": ["one", "two", "three"]},
        )

        result = ConversationEngine(provider, tool_registry=registry(lookup, peer_ask)).respond(self.request())

        self.assertEqual(
            [frame.text for frame in result.frames],
            ["Checking.", "i couldn't get an answer from that tomo. try again in a moment."],
        )

    def test_terminal_peer_frames_respect_per_segment_frame_budget(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("ask-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")],
        ])
        peer_ask = BoundTool(
            ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False),
            lambda _: {"ok": True, "status": "completed", "frames": ["one", "two"]},
        )

        result = ConversationEngine(
            provider,
            tool_registry=registry(peer_ask),
            budget=TurnBudget(2, 1, 1, 1, 1, 2, 800),
        ).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["i couldn't get an answer from that tomo. try again in a moment."])

    def test_oversized_terminal_peer_frame_fails_closed(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("ask-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")],
        ])
        peer_ask = BoundTool(
            ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False),
            lambda _: {"ok": True, "status": "completed", "frames": ["x" * 801]},
        )

        result = ConversationEngine(provider, tool_registry=registry(peer_ask)).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["i couldn't get an answer from that tomo. try again in a moment."])

    def test_terminal_peer_frame_over_sentence_budget_fails_closed(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("ask-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")],
        ])
        peer_ask = BoundTool(
            ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False),
            lambda _: {"ok": True, "status": "completed", "frames": ["one. two. three. four."]},
        )

        result = ConversationEngine(provider, tool_registry=registry(peer_ask)).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["i couldn't get an answer from that tomo. try again in a moment."])

    def test_malformed_direct_peer_resume_emits_only_no_answer_frame(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"bob is free now."}\n'), ProviderToolCallReady("peer-1", "peer_resume", "{"), ProviderStreamCompleted("tool_calls")],
        ])
        peer_resume = BoundTool(
            ToolSpec(
                "peer_resume", "resume peer",
                {"type": "object", "properties": {"thread_id": {"type": "string"}}, "required": ["thread_id"], "additionalProperties": False},
                read_only=True, parallel_safe=True,
            ),
            lambda _: {"ok": True, "status": "completed", "frames": ["unsafe"]},
        )

        result = ConversationEngine(provider, tool_registry=registry(peer_resume)).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["i couldn't get an answer from that tomo. try again in a moment."])

    def test_validation_failure_peer_fallback_respects_tight_frame_budget(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("peer-1", "peer_resume", "{"), ProviderStreamCompleted("tool_calls")],
        ])
        peer_resume = BoundTool(
            ToolSpec("peer_resume", "resume peer", {"type": "object", "properties": {}}, read_only=True, parallel_safe=True),
            lambda _: {"ok": True, "status": "completed", "frames": ["unsafe"]},
        )

        result = ConversationEngine(
            provider,
            tool_registry=registry(peer_resume),
            budget=TurnBudget(2, 1, 1, 1, 1, 1, 10),
        ).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["no answer."])

    def test_ordinary_read_only_pretool_announcements_remain_progressive(self):
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"Checking."}\n'), ProviderToolCallReady("lookup-1", "lookup", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Found it."}\n'), ProviderStreamCompleted("stop")],
        ])

        events = tuple(ConversationEngine(provider, tool_registry=registry(BoundTool(ToolSpec("lookup", "lookup", {"type": "object", "properties": {}}, read_only=True, parallel_safe=True), lambda _: "found"))).respond_iter(self.request()))

        self.assertEqual([event.frame.text for event in events if isinstance(event, FrameReady)], ["Checking.", "Found it."])

    def test_malformed_completed_peer_observation_fails_closed(self):
        provider = ScriptedProvider([[ProviderTextDelta(PLAN), ProviderToolCallReady("peer-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")]])
        peer_ask = BoundTool(ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False), lambda _: {"ok": True, "status": "completed", "frames": [42]})

        result = ConversationEngine(provider, tool_registry=registry(peer_ask)).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["i couldn't get an answer from that tomo. try again in a moment."])

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

    def test_mutating_tool_suppresses_pre_observation_frame_and_persists_only_grounded_frame(self):
        invoked = []
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"Created."}\n'), ProviderToolCallReady("create-1", "cron_create", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"Created for 10 minutes from now."}\n'), ProviderStreamCompleted("stop")],
        ])
        mutation = BoundTool(ToolSpec("cron_create", "create", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False), lambda _: invoked.append(True) or "created")

        events = list(ConversationEngine(provider, tool_registry=registry(mutation)).respond_iter(self.request()))

        visible = [event.frame.text for event in events if isinstance(event, FrameReady)]
        result = events[-1].result
        self.assertEqual(visible, ["Created for 10 minutes from now."])
        self.assertEqual([frame.text for frame in result.frames], visible)
        self.assertEqual(invoked, [True])

    def test_invalid_mutating_tool_does_not_surface_false_pre_observation_confirmation(self):
        invoked = []
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"Created."}\n'), ProviderToolCallReady("create-1", "cron_create", "{"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta('{"type":"frame","text":"I couldn’t create that reminder."}\n'), ProviderStreamCompleted("stop")],
        ])
        mutation = BoundTool(ToolSpec("cron_create", "create", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False), lambda _: invoked.append(True))

        events = list(ConversationEngine(provider, tool_registry=registry(mutation)).respond_iter(self.request()))

        visible = [event.frame.text for event in events if isinstance(event, FrameReady)]
        result = events[-1].result
        self.assertEqual(visible, ["I couldn’t create that reminder."])
        self.assertEqual([frame.text for frame in result.frames], visible)
        self.assertEqual(invoked, [])

    def test_mutating_tool_with_stop_completion_never_surfaces_or_invokes_before_repair(self):
        invoked = []
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"Created."}\n'), ProviderToolCallReady("create-1", "cron_create", "{}"), ProviderStreamCompleted("stop")],
            [ProviderTextDelta('{"type":"frame","text":"I could not create that reminder."}\n'), ProviderStreamCompleted("stop")],
        ])
        mutation = BoundTool(ToolSpec("cron_create", "create", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False), lambda _: invoked.append(True))

        result = ConversationEngine(provider, tool_registry=registry(mutation)).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["I could not create that reminder."])
        self.assertEqual(invoked, [])

    def test_mutating_tool_without_completion_never_surfaces_or_invokes_before_repair(self):
        invoked = []
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN + '{"type":"frame","text":"Created."}\n'), ProviderToolCallReady("create-1", "cron_create", "{}")],
            [ProviderTextDelta('{"type":"frame","text":"I could not create that reminder."}\n'), ProviderStreamCompleted("stop")],
        ])
        mutation = BoundTool(ToolSpec("cron_create", "create", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False), lambda _: invoked.append(True))

        result = ConversationEngine(provider, tool_registry=registry(mutation)).respond(self.request())

        self.assertEqual([frame.text for frame in result.frames], ["I could not create that reminder."])
        self.assertEqual(invoked, [])

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

    def test_failed_peer_list_observation_cannot_ground_later_memory(self):
        control = json.dumps({
            "type": "memory_control", "action": "add", "authority": "autonomous", "user_intent_excerpt": None,
            "memory_id": None, "kind": "fact", "subject_key": "bob", "topic": "peer.relationship",
            "value": "connected", "statement": "bob is connected", "confidence": 0.8, "salience": 0.5,
            "surface_scope": "contextual", "valid_from": None, "valid_until": None,
            "sources": [{"source_kind": "tool_observation", "source_id": "list-1", "observed_at": "2026-01-01T00:00:00Z"}],
        })
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("list-1", "peer_list", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta(control + '\n{"type":"frame","text":"I could not check connections."}\n'), ProviderStreamCompleted("stop")],
        ])
        peer_list = BoundTool(
            ToolSpec("peer_list", "list peers", {"type": "object", "properties": {}}, read_only=True, parallel_safe=True),
            lambda _: {"ok": False, "status": "failed", "relationships": []},
        )

        events = tuple(ConversationEngine(provider, tool_registry=registry(peer_list)).respond_iter(self.request()))

        ready = next(event for event in events if isinstance(event, MemoryControlReady))
        self.assertEqual(ready.tool_observation_ids, ())

    def test_malformed_peer_list_observation_cannot_ground_later_memory(self):
        control = json.dumps({
            "type": "memory_control", "action": "add", "authority": "autonomous", "user_intent_excerpt": None,
            "memory_id": None, "kind": "fact", "subject_key": "bob", "topic": "peer.relationship",
            "value": "connected", "statement": "bob is connected", "confidence": 0.8, "salience": 0.5,
            "surface_scope": "contextual", "valid_from": None, "valid_until": None,
            "sources": [{"source_kind": "tool_observation", "source_id": "list-1", "observed_at": "2026-01-01T00:00:00Z"}],
        })
        provider = ScriptedProvider([
            [ProviderTextDelta(PLAN), ProviderToolCallReady("list-1", "peer_list", "{}"), ProviderStreamCompleted("tool_calls")],
            [ProviderTextDelta(control + '\n{"type":"frame","text":"I could not check connections."}\n'), ProviderStreamCompleted("stop")],
        ])
        peer_list = BoundTool(
            ToolSpec("peer_list", "list peers", {"type": "object", "properties": {}}, read_only=True, parallel_safe=True),
            lambda _: "not-json",
        )

        events = tuple(ConversationEngine(provider, tool_registry=registry(peer_list)).respond_iter(self.request()))

        ready = next(event for event in events if isinstance(event, MemoryControlReady))
        self.assertEqual(ready.tool_observation_ids, ())

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
        self.assertIn("ordinary completion requires 1 to 1 frame records", provider.calls[2][0][0]["content"])

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
