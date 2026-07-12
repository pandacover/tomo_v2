import json
import unittest

from tomo_core.conversation import (
    ConversationMove,
    Frame,
    FrameReady,
    MoveConfidence,
    MovePlan,
    SegmentFinish,
    SegmentResult,
    ToolCall,
    TurnBudget,
    TurnRunCompleted,
    TurnRunResult,
    TurnRunStatus,
    TurnUsage,
)
from tomo_core.models import InboundEnvelope, InboundMessage, InputBurst, OutboundBubble, ResponseContract
from tomo_core.runtime import RuntimeCompleted, RuntimeFrameReady
from tomo_core.sandbox_protocol import (
    EVENT_MARKER,
    INBOUND_PROTOCOL_VERSION,
    LEGACY_EVENT_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    RESULT_MARKER,
    SandboxErrorEvent,
    SandboxFrameEvent,
    SandboxTracebackFrame,
    decode_inbound,
    encode_error,
    encode_event,
    encode_inbound,
    encode_result,
    iter_event_markers,
    parse_result_marker,
)


class SandboxProtocolTests(unittest.TestCase):
    def test_v2_inbound_and_v3_frame_completion_round_trip(self):
        burst = self._burst()
        inbound_payload = encode_inbound("request-7", burst)
        self.assertEqual(json.loads(inbound_payload)["version"], 2)
        self.assertEqual(INBOUND_PROTOCOL_VERSION, 2)
        request_id, decoded = decode_inbound(inbound_payload)
        self.assertEqual((request_id, decoded), ("request-7", burst))

        frame, completed = self._events()
        frame_payload = encode_event("request-7", "gen-1", 0, frame)
        completed_payload = encode_event("request-7", "gen-1", 1, completed)
        self.assertEqual(json.loads(frame_payload)["version"], 3)
        self.assertEqual(json.loads(frame_payload)["type"], "frame")
        self.assertEqual(json.loads(completed_payload)["result"]["segments"][0]["tool_call_count"], 1)
        self.assertNotIn("call_id", completed_payload)
        self.assertNotIn("arguments", completed_payload)

        events = list(iter_event_markers([
            "log\n" + EVENT_MARKER + frame_payload[:20],
            frame_payload[20:] + "\n",
            "\x1b[?2004h" + EVENT_MARKER + completed_payload + "\n",
        ], "request-7", "gen-1"))
        self.assertEqual(events[0], SandboxFrameEvent(0, 0, 0, "hello back."))
        self.assertEqual(events[1].result["logical_text"], "hello back.")

    def test_v2_and_v3_inbound_and_v2_event_fixtures_remain_readable(self):
        burst = self._burst()
        inbound = json.loads(encode_inbound("request-7", burst))
        for version in (LEGACY_EVENT_PROTOCOL_VERSION, PROTOCOL_VERSION):
            with self.subTest(version=version):
                inbound["version"] = version
                self.assertEqual(decode_inbound(json.dumps(inbound)), ("request-7", burst))
        events = [
            {"version": 2, "request_id": "request-7", "generation_id": "gen-1", "sequence": 0, "type": "utterance", "move": "answer", "text": "hello back."},
            {"version": 2, "request_id": "request-7", "generation_id": "gen-1", "sequence": 1, "type": "completed", "result": {"logical_text": "hello back.", "utterances": ["hello back."], "plan": {"primary_move": "answer", "supporting_moves": [], "move_sequence": ["answer"], "response_goal": "answer", "confidence": "high"}}},
        ]
        parsed = list(iter_event_markers([EVENT_MARKER + json.dumps(event) + "\n" for event in events], "request-7", "gen-1"))
        self.assertEqual(parsed[0], SandboxFrameEvent(0, 0, 0, "hello back.", ConversationMove.ANSWER))

    def test_v3_encoder_rejects_unsupported_objects(self):
        for event in (object(), {"type": "frame"}):
            with self.subTest(event=event), self.assertRaises(TypeError):
                encode_event("request-7", "gen-1", 0, event)

    def test_v2_events_retain_response_contract_validation(self):
        contract = ResponseContract(max_utterances=2, max_sentences_per_utterance=1)
        plan = {"primary_move": "answer", "supporting_moves": [], "move_sequence": ["answer"], "response_goal": "answer", "confidence": "high"}
        cases = (
            [{"version": 2, "request_id": "request-7", "generation_id": "gen-1", "sequence": 0, "type": "utterance", "move": "answer", "text": "One. Two."}],
            [
                {"version": 2, "request_id": "request-7", "generation_id": "gen-1", "sequence": 0, "type": "utterance", "move": "answer", "text": "One."},
                {"version": 2, "request_id": "request-7", "generation_id": "gen-1", "sequence": 1, "type": "completed", "result": {"logical_text": "One. Two. Three.", "utterances": ["One.", "Two.", "Three."], "plan": plan}},
            ],
        )

        for messages in cases:
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                list(iter_event_markers([EVENT_MARKER + json.dumps(message) + "\n" for message in messages], "request-7", "gen-1", contract))

    def test_rejects_mixed_versions_coordinates_sizes_and_terminal_violations(self):
        frame, completed = self._events()
        valid = json.loads(encode_event("request-7", "gen-1", 0, frame))
        terminal = encode_event("request-7", "gen-1", 1, completed)
        cases = []
        mixed = [valid, json.loads(terminal)]
        mixed[1]["version"] = 2
        cases.append(mixed)
        for key, value in (("sequence", -1), ("segment_index", -1), ("frame_index", 1), ("text", "x" * 801), ("text", "one. two. three. four.")):
            malformed = dict(valid)
            malformed[key] = value
            cases.append([malformed])
        gap = dict(valid)
        gap["sequence"] = 1
        cases.append([gap])
        conflict = dict(valid)
        conflict["text"] = "other."
        cases.append([valid, conflict])
        post_terminal = [valid, json.loads(terminal), dict(valid, sequence=2)]
        cases.append(post_terminal)
        for messages in cases:
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                list(iter_event_markers([EVENT_MARKER + json.dumps(message) + "\n" for message in messages], "request-7", "gen-1"))

    def test_rejects_noncontiguous_frames_and_smuggled_completion_fields(self):
        frame, completed = self._events()
        first = json.loads(encode_event("request-7", "gen-1", 0, frame))
        second = dict(first, sequence=1, frame_index=2)
        result = json.loads(encode_event("request-7", "gen-1", 1, completed))
        result["result"]["segments"][0]["tool_calls"] = [{"arguments": {"secret": "no"}}]
        for messages in ([first, second], [first, result]):
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                list(iter_event_markers([EVENT_MARKER + json.dumps(message) + "\n" for message in messages], "request-7", "gen-1"))

    def test_requires_exact_completion_reconciliation_and_safe_error_diagnostics(self):
        frame, completed = self._events()
        streamed = json.loads(encode_event("request-7", "gen-1", 0, frame))
        terminal = json.loads(encode_event("request-7", "gen-1", 1, completed))
        terminal["result"]["frames"][0]["text"] = "changed."
        terminal["result"]["logical_text"] = "changed."
        with self.assertRaises(ValueError):
            list(iter_event_markers([EVENT_MARKER + json.dumps(streamed), EVENT_MARKER + json.dumps(terminal)], "request-7", "gen-1"))

        encoded = encode_event("request-7", "gen-1", 0, SandboxErrorEvent(0, "runtime_failed", "RuntimeError", (SandboxTracebackFrame("run.py", "run", 1),)))
        self.assertEqual(list(iter_event_markers([EVENT_MARKER + encoded], "request-7", "gen-1"))[0].exception_class, "RuntimeError")
        unsafe = json.loads(encoded)
        unsafe["error"]["message"] = "secret exception text"
        with self.assertRaises(ValueError):
            list(iter_event_markers([EVENT_MARKER + json.dumps(unsafe)], "request-7", "gen-1"))

    def test_v3_reuses_strict_frame_validation_and_rejects_unsafe_error_codes(self):
        frame, _ = self._events()
        payload = json.loads(encode_event("request-7", "gen-1", 0, frame))
        for text in ("- list item", "Primary move: answer.", "first line\nsecond line", "has an em — dash"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                list(iter_event_markers([EVENT_MARKER + json.dumps(dict(payload, text=text))], "request-7", "gen-1"))
        for code in ("RuntimeError: secret", "safe-code", "UPPERCASE"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                encode_event("request-7", "gen-1", 0, SandboxErrorEvent(0, code))

    def test_v3_rejects_more_than_three_reconciled_frames(self):
        frame, completed = self._events()
        stream = [json.loads(encode_event("request-7", "gen-1", index, RuntimeFrameReady(FrameReady(index, Frame(0, index, f"Frame {index}.")), OutboundBubble(f"Frame {index}.")))) for index in range(3)]
        fourth = dict(stream[-1], sequence=3, frame_index=3, text="Fourth.")

        with self.assertRaises(ValueError):
            list(iter_event_markers([EVENT_MARKER + json.dumps(message) + "\n" for message in [*stream, fourth]], "request-7", "gen-1"))

    def test_v3_validates_segment_and_visible_budgets_before_yielding_frames(self):
        frame, _ = self._events()
        payload = json.loads(encode_event("request-7", "gen-1", 0, frame))
        oversized = dict(payload, segment_index=99)
        events = iter_event_markers([EVENT_MARKER + json.dumps(oversized) + "\n"], "request-7", "gen-1")
        with self.assertRaises(ValueError):
            next(events)

        at_segment_two = dict(payload, segment_index=2)
        terminal = {"version": 3, "request_id": "request-7", "generation_id": "gen-1", "sequence": 1, "type": "error", "error": {"code": "runtime_failed"}}
        parsed = list(iter_event_markers([EVENT_MARKER + json.dumps(at_segment_two) + "\n", EVENT_MARKER + json.dumps(terminal) + "\n"], "request-7", "gen-1"))
        self.assertEqual(parsed[0], SandboxFrameEvent(0, 2, 0, "hello back."))

    def test_accepts_partial_completion_and_safe_errors(self):
        frame = Frame(0, 0, "partial.")
        plan = MovePlan(ConversationMove.ANSWER, (), "answer", MoveConfidence.HIGH, (ConversationMove.ANSWER,))
        result = TurnRunResult(plan, (SegmentResult(0, (frame,), (), SegmentFinish.PARTIAL),), (frame,), TurnUsage(1, 0, 0, 1), TurnRunStatus.COMPLETED_PARTIAL)
        events = list(iter_event_markers([
            EVENT_MARKER + encode_event("request-7", "gen-1", 0, RuntimeFrameReady(FrameReady(0, frame), OutboundBubble("partial."))) + "\n",
            EVENT_MARKER + encode_event("request-7", "gen-1", 1, RuntimeCompleted(TurnRunCompleted(result))) + "\n",
        ], "request-7", "gen-1"))
        self.assertEqual(events[-1].result["status"], "completed_partial")
        error = encode_event("request-7", "gen-1", 0, SandboxErrorEvent(0, "runtime_failed"))
        self.assertEqual(list(iter_event_markers([EVENT_MARKER + error], "request-7", "gen-1"))[0].code, "runtime_failed")

    def test_v1_result_helpers_remain(self):
        self.assertEqual(PROTOCOL_VERSION, 3)
        result = encode_result("request-7", [OutboundBubble("ok")])
        self.assertEqual(parse_result_marker(RESULT_MARKER + result, "request-7"), [OutboundBubble("ok")])
        with self.assertRaises(Exception):
            parse_result_marker(RESULT_MARKER + encode_error("request-7", "failed"), "request-7")

    def _burst(self):
        return InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "message-1", "hello")),))

    def _events(self):
        frame = Frame(0, 0, "hello back.")
        plan = MovePlan(ConversationMove.ANSWER, (), "answer", MoveConfidence.HIGH, (ConversationMove.ANSWER,))
        result = TurnRunResult(plan, (SegmentResult(0, (frame,), (ToolCall("call-1", "lookup", {}),), SegmentFinish.TOOL_BATCH), SegmentResult(1, (), (), SegmentFinish.FAILED)), (frame,), TurnUsage(2, 1, 1, 1), TurnRunStatus.COMPLETED_PARTIAL)
        return RuntimeFrameReady(FrameReady(0, frame), OutboundBubble("hello back.")), RuntimeCompleted(TurnRunCompleted(result))


if __name__ == "__main__":
    unittest.main()
