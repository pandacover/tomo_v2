import json
import unittest
from unittest.mock import patch

import httpx

from tomo_core.providers import (
    ProviderStreamCompleted,
    ProviderTextDelta,
    ProviderToolCallReady,
    StaticProvider,
    XaiApiProvider,
)


class FakeStreamResponse:
    def __init__(self, chunks, *, status_error=None):
        self.chunks = chunks
        self.status_error = status_error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error

    def iter_raw(self):
        yield from self.chunks


def sse_chunks(*events, splits=()):
    payload = "".join(f"data: {event}\n\n" for event in events).encode("utf-8")
    points = (0, *splits, len(payload))
    return [payload[start:end] for start, end in zip(points, points[1:])]


class ProviderStreamingTests(unittest.TestCase):
    def test_stream_decodes_utf8_text_across_arbitrary_sse_chunks(self):
        first = json.dumps({"choices": [{"delta": {"content": "hello "}, "finish_reason": None}]})
        second = json.dumps({"choices": [{"delta": {"content": "café"}, "finish_reason": "stop"}]}, ensure_ascii=False)
        chunks = sse_chunks(first, second, "[DONE]", splits=(7, 39, 96))

        with patch("tomo_core.providers.httpx.stream", return_value=FakeStreamResponse(chunks)) as stream:
            events = list(XaiApiProvider(api_key="test-key").stream([{"role": "user", "content": "hi"}]))

        self.assertEqual(
            events,
            [
                ProviderTextDelta("hello "),
                ProviderTextDelta("café"),
                ProviderStreamCompleted("stop"),
            ],
        )
        self.assertEqual(stream.call_args.args[:2], ("POST", "https://api.x.ai/v1/chat/completions"))
        self.assertTrue(stream.call_args.kwargs["json"]["stream"])

    def test_stream_assembles_multiple_native_tool_calls_before_terminal_event(self):
        events = (
            json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "id": "call_1", "function": {"name": "search", "arguments": '{"q":'}},
                                    {"index": 1, "id": "call_2", "function": {"name": "weather", "arguments": '{"city":'}},
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            ),
            json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": '"japan"}'}},
                                    {"index": 1, "function": {"arguments": '"tokyo"}'}},
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 7},
                }
            ),
            "[DONE]",
        )

        with patch("tomo_core.providers.httpx.stream", return_value=FakeStreamResponse(sse_chunks(*events, splits=(2, 71, 183)))):
            result = list(XaiApiProvider(api_key="test-key").stream([{"role": "user", "content": "hi"}]))

        self.assertEqual(
            result,
            [
                ProviderToolCallReady("call_1", "search", '{"q":"japan"}'),
                ProviderToolCallReady("call_2", "weather", '{"city":"tokyo"}'),
                ProviderStreamCompleted("tool_calls", input_tokens=12, output_tokens=7),
            ],
        )

    def test_stream_fails_closed_without_a_done_sentinel(self):
        event = json.dumps({"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]})
        with patch("tomo_core.providers.httpx.stream", return_value=FakeStreamResponse(sse_chunks(event))):
            with self.assertRaises(ValueError):
                list(XaiApiProvider(api_key="test-key").stream([{"role": "user", "content": "hi"}]))

    def test_stream_fails_closed_on_malformed_payload_http_error_and_incomplete_tool_call(self):
        cases = [
            FakeStreamResponse(sse_chunks("not json", "[DONE]")),
            FakeStreamResponse((), status_error=httpx.HTTPStatusError("bad", request=httpx.Request("POST", "https://example.test"), response=httpx.Response(500))),
            FakeStreamResponse(
                sse_chunks(
                    json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "search"}}]}, "finish_reason": "tool_calls"}]}),
                    "[DONE]",
                )
            ),
        ]
        for response in cases:
            with self.subTest(response=response), patch("tomo_core.providers.httpx.stream", return_value=response):
                with self.assertRaises((ValueError, httpx.HTTPStatusError)):
                    list(XaiApiProvider(api_key="test-key").stream([{"role": "user", "content": "hi"}]))

    def test_stream_fails_closed_on_conflicting_tool_identity_invalid_usage_or_missing_finish(self):
        conflicting_id = json.dumps(
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "search", "arguments": "{}"}}]}, "finish_reason": None}]}
        )
        conflicting_id_next = json.dumps(
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_2", "function": {}}]}, "finish_reason": "tool_calls"}]}
        )
        invalid_usage = json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": -1}})
        no_finish = json.dumps({"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]})
        for chunks in (
            sse_chunks(conflicting_id, conflicting_id_next, "[DONE]"),
            sse_chunks(invalid_usage, "[DONE]"),
            sse_chunks(no_finish, "[DONE]"),
        ):
            with self.subTest(chunks=chunks), patch("tomo_core.providers.httpx.stream", return_value=FakeStreamResponse(chunks)):
                with self.assertRaises(ValueError):
                    list(XaiApiProvider(api_key="test-key").stream([{"role": "user", "content": "hi"}]))

    def test_stream_rejects_duplicate_tool_call_id_across_indices_before_ready_event(self):
        duplicate_ids = json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call_1", "function": {"name": "search", "arguments": "{}"}},
                                {"index": 1, "id": "call_1", "function": {"name": "weather", "arguments": "{}"}},
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        ready_events = []
        with patch("tomo_core.providers.httpx.stream", return_value=FakeStreamResponse(sse_chunks(duplicate_ids, "[DONE]"))):
            stream = XaiApiProvider(api_key="test-key").stream([{"role": "user", "content": "hi"}])
            with self.assertRaises(ValueError):
                for event in stream:
                    if isinstance(event, ProviderToolCallReady):
                        ready_events.append(event)

        self.assertEqual(ready_events, [])

    def test_complete_collects_text_and_rejects_tool_calls(self):
        provider = XaiApiProvider(api_key="test-key")
        with patch.object(provider, "stream", return_value=iter([ProviderTextDelta("a"), ProviderTextDelta("b"), ProviderStreamCompleted("stop")])):
            self.assertEqual(provider.complete([{"role": "user", "content": "hi"}]), "ab")
        with patch.object(provider, "stream", return_value=iter([ProviderToolCallReady("call_1", "search", "{}")] )):
            with self.assertRaises(ValueError):
                provider.complete([{"role": "user", "content": "hi"}])

    def test_static_provider_emits_turnrun_jsonl_for_first_and_fixed_plan_segments(self):
        provider = StaticProvider("hello")
        first_messages = [{"role": "system", "content": "generate exactly one internal turn_plan JSONL record before any frame record."}]
        later_messages = [{"role": "system", "content": "do not emit a turn_plan record; reuse the fixed turn plan."}]

        first_events = list(provider.stream(first_messages))
        later_events = list(provider.stream(later_messages))

        self.assertEqual(first_events[-1], ProviderStreamCompleted("stop"))
        self.assertEqual(later_events[-1], ProviderStreamCompleted("stop"))
        first_records = [json.loads(line) for line in first_events[0].text.splitlines()]
        later_records = [json.loads(line) for line in later_events[0].text.splitlines()]
        self.assertEqual(
            first_records,
            [
                {"type": "turn_plan", "primary_move": "answer", "supporting_moves": [], "move_sequence": ["answer"], "response_goal": "return the configured static smoke response", "confidence": "high"},
                {"type": "frame", "text": "hello"},
            ],
        )
        self.assertEqual(later_records, [{"type": "frame", "text": "hello"}])
        with self.assertRaisesRegex(ValueError, "does not support tool calls"):
            list(provider.stream(first_messages, tools=({"type": "function"},)))


if __name__ == "__main__":
    unittest.main()
