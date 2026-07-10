import json
import unittest

from tomo_core.models import InboundEnvelope, OutboundBubble
from tomo_core.sandbox_protocol import (
    PROTOCOL_VERSION,
    RESULT_MARKER,
    SandboxProtocolError,
    decode_inbound,
    encode_error,
    encode_inbound,
    encode_result,
    parse_result_marker,
)


class SandboxProtocolTests(unittest.TestCase):
    def test_versioned_messages_round_trip_and_enforce_result_contract(self):
        inbound = InboundEnvelope(
            connector="telegram",
            actor_id="user-1",
            message_id="message-9",
            text="hello",
        )

        request_id, decoded = decode_inbound(encode_inbound("request-7", inbound))

        self.assertEqual(request_id, "request-7")
        self.assertEqual(decoded, inbound)

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
        invalid_version["version"] = PROTOCOL_VERSION + 1
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
