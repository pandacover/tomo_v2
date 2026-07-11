import json
import unittest

from tomo_core.models import InboundEnvelope, OutboundBubble, ResponseContract
from tomo_core.conversation import ConversationCompleted, ConversationMove, ConversationResult, MoveConfidence, MovePlan, UtteranceReady
from tomo_core.models import InboundMessage, InputBurst
from tomo_core.sandbox_protocol import (
    EVENT_MARKER,
    PROTOCOL_VERSION,
    RESULT_MARKER,
    SandboxCompletedEvent,
    SandboxErrorEvent,
    SandboxProtocolError,
    SandboxUtteranceEvent,
    decode_inbound,
    encode_event,
    encode_error,
    encode_inbound,
    encode_result,
    iter_event_markers,
    parse_result_marker,
)


class SandboxProtocolTests(unittest.TestCase):
    def test_v2_inbound_burst_and_events_round_trip(self):
        burst = InputBurst(
            burst_id="burst-1",
            generation_id="gen-1",
            revision=2,
            visible_assistant_utterances=("already visible.",),
            accepted_generation_ids=("gen-0",),
            messages=(
                InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "message-9", "hello")),
                InboundMessage(2, 42, InboundEnvelope("telegram", "user-1", "message-10", "again")),
            ),
        )

        request_id, decoded = decode_inbound(encode_inbound("request-7", burst))

        self.assertEqual(request_id, "request-7")
        self.assertEqual(decoded, burst)

        plan = MovePlan(ConversationMove.ANSWER, (), "answer", MoveConfidence.HIGH, (ConversationMove.ANSWER,))
        chunks = [
            "log line\n" + EVENT_MARKER + encode_event("request-7", "gen-1", 0, UtteranceReady(0, ConversationMove.ANSWER, "hello back."))[:25],
            encode_event("request-7", "gen-1", 0, UtteranceReady(0, ConversationMove.ANSWER, "hello back."))[25:] + "\n",
            EVENT_MARKER + encode_event("request-7", "gen-1", 1, ConversationCompleted(ConversationResult(plan, ("hello back.",)))) + "\n",
        ]
        events = list(iter_event_markers(chunks, "request-7", "gen-1"))
        self.assertEqual(events[0], SandboxUtteranceEvent(0, ConversationMove.ANSWER, "hello back."))
        self.assertIsInstance(events[1], SandboxCompletedEvent)
        self.assertEqual(events[1].result["logical_text"], "hello back.")

    def test_v2_event_stream_rejects_protocol_violations(self):
        first = encode_event("request-7", "gen-1", 0, UtteranceReady(0, ConversationMove.ANSWER, "ok."))
        conflicting = json.loads(first)
        conflicting["text"] = "different."

        cases = [
            [EVENT_MARKER + encode_event("request-7", "gen-1", 1, UtteranceReady(1, ConversationMove.ANSWER, "gap."))],
            [EVENT_MARKER + first + "\n" + EVENT_MARKER + json.dumps(conflicting, separators=(",", ":"))],
            [EVENT_MARKER + first.replace('"request-7"', '"other"')],
            [EVENT_MARKER + first.replace('"gen-1"', '"gen-2"')],
            [EVENT_MARKER + first.replace('"answer"', '"fake"')],
            [EVENT_MARKER + first.replace('"ok."', '"' + ("x" * 4097) + '"')],
        ]
        for chunks in cases:
            with self.subTest(chunks=chunks), self.assertRaises(ValueError):
                list(iter_event_markers(chunks, "request-7", "gen-1"))

    def test_v2_event_stream_enforces_response_contract_and_completion_reconciliation(self):
        plan = MovePlan(
            ConversationMove.ANSWER,
            (ConversationMove.EXPLORE,),
            "answer then explore",
            MoveConfidence.HIGH,
            (ConversationMove.ANSWER, ConversationMove.EXPLORE),
        )
        contract = ResponseContract(max_utterances=2, max_sentences_per_utterance=1)
        valid = [
            EVENT_MARKER + encode_event("request-7", "gen-1", 0, UtteranceReady(0, ConversationMove.ANSWER, "one.")) + "\n",
            EVENT_MARKER + encode_event("request-7", "gen-1", 1, UtteranceReady(1, ConversationMove.EXPLORE, "two?")) + "\n",
            EVENT_MARKER + encode_event("request-7", "gen-1", 2, ConversationCompleted(ConversationResult(plan, ("one.", "two?")))) + "\n",
        ]

        events = list(iter_event_markers(valid, "request-7", "gen-1", contract))

        self.assertEqual([event.sequence for event in events], [0, 1, 2])

        too_many = valid[:2] + [
            EVENT_MARKER + encode_event("request-7", "gen-1", 2, UtteranceReady(2, ConversationMove.ANSWER, "three.")) + "\n"
        ]
        sentence_limit = [
            EVENT_MARKER + encode_event("request-7", "gen-1", 0, UtteranceReady(0, ConversationMove.ANSWER, "one. two.")) + "\n"
        ]
        mismatched_text = valid[:2] + [
            EVENT_MARKER + encode_event("request-7", "gen-1", 2, ConversationCompleted(ConversationResult(plan, ("one.", "changed.")))) + "\n"
        ]
        mismatched_moves = valid[:2] + [
            EVENT_MARKER
            + encode_event(
                "request-7",
                "gen-1",
                2,
                ConversationCompleted(ConversationResult(MovePlan(ConversationMove.EXPLORE, (ConversationMove.ANSWER,), "wrong", MoveConfidence.HIGH, (ConversationMove.EXPLORE, ConversationMove.ANSWER)), ("one.", "two?"))),
            )
            + "\n"
        ]
        smuggled_without_stream = [
            EVENT_MARKER + encode_event("request-7", "gen-1", 0, ConversationCompleted(ConversationResult(plan, ("one.",)))) + "\n"
        ]
        for chunks in (too_many, sentence_limit, mismatched_text, mismatched_moves, smuggled_without_stream):
            with self.subTest(chunks=chunks), self.assertRaises(ValueError):
                list(iter_event_markers(chunks, "request-7", "gen-1", contract))

    def test_v2_event_stream_requires_a_terminal_event(self):
        utterance = encode_event(
            "request-7",
            "gen-1",
            0,
            UtteranceReady(0, ConversationMove.ANSWER, "unfinished."),
        )

        with self.assertRaises(ValueError):
            list(iter_event_markers([EVENT_MARKER + utterance], "request-7", "gen-1"))

        first = encode_event("request-7", "gen-1", 0, UtteranceReady(0, ConversationMove.ANSWER, "ok."))
        error = encode_event("request-7", "gen-1", 0, SandboxErrorEvent(0, "auth_expired"))
        self.assertEqual(list(iter_event_markers([EVENT_MARKER + error], "request-7", "gen-1")), [SandboxErrorEvent(0, "auth_expired")])
        with self.assertRaises(ValueError):
            list(iter_event_markers([EVENT_MARKER + error + "\n" + EVENT_MARKER + first], "request-7", "gen-1"))

    def test_legacy_result_helpers_remain_available_until_dispatch_migrates(self):
        inbound = InboundEnvelope(connector="telegram", actor_id="user-1", message_id="message-9", text="hello")

        self.assertEqual(PROTOCOL_VERSION, 2)

        result = encode_result(
            "request-7",
            [OutboundBubble("first", reply_to_message_id="message-9"), OutboundBubble("second")],
        )
        self.assertEqual(
            parse_result_marker(f"sandbox log\n{RESULT_MARKER}{result}\n", "request-7"),
            [OutboundBubble("first", reply_to_message_id="message-9"), OutboundBubble("second")],
        )

        with self.assertRaises(ValueError):
            parse_result_marker(f"{RESULT_MARKER}{result}", "other-request")

        invalid_version = json.loads(result)
        invalid_version["version"] = PROTOCOL_VERSION
        with self.assertRaises(ValueError):
            parse_result_marker(f"{RESULT_MARKER}{json.dumps(invalid_version)}", "request-7")

        with self.assertRaises(ValueError):
            encode_result("request-7", [])
        with self.assertRaises(ValueError):
            encode_result("request-7", [OutboundBubble("ok")] * 5)
        with self.assertRaises(ValueError):
            encode_result("request-7", [OutboundBubble("x" * 4097)])

    def test_result_marker_requires_exactly_one_versioned_success_or_typed_error(self):
        result = encode_result("request-7", [OutboundBubble("ok")])
        self.assertEqual(RESULT_MARKER, "TOMO_SANDBOX_RESULT=")
        self.assertIn('"version":1', result)
        self.assertIn('"ok":true', result)
        self.assertEqual(parse_result_marker(f"{RESULT_MARKER}{result}\n", "request-7"), [OutboundBubble("ok")])

        error = encode_error("request-7", "runtime_failed")
        with self.assertRaises(SandboxProtocolError) as raised:
            parse_result_marker(f"{RESULT_MARKER}{error}\n", "request-7")
        self.assertEqual(raised.exception.code, "runtime_failed")

        with self.assertRaises(ValueError):
            parse_result_marker(f"{RESULT_MARKER}{result}\n{RESULT_MARKER}{result}\n", "request-7")


if __name__ == "__main__":
    unittest.main()
