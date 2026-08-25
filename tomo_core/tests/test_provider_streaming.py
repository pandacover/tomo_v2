import json
import unittest
from unittest.mock import patch

import httpx

from tomo_core.providers import (
    GrokAuthProvider,
    OAuthBackedSuperGrokProvider,
    ProviderStreamCompleted,
    ProviderStreamError,
    ProviderTextDelta,
    ProviderToolCallReady,
    StaticProvider,
    XaiApiProvider,
    supergrok_oauth_provider_from_access_token,
)


STRUCTURED_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "vision_observation",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "visible_text", "relevant_details", "uncertainties"],
            "properties": {
                "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
                "visible_text": {"type": "array", "maxItems": 8, "items": {"type": "string", "minLength": 1, "maxLength": 1000}},
                "relevant_details": {"type": "array", "maxItems": 8, "items": {"type": "string", "minLength": 1, "maxLength": 1000}},
                "uncertainties": {"type": "array", "maxItems": 8, "items": {"type": "string", "minLength": 1, "maxLength": 1000}},
            },
        },
        "strict": True,
    },
}


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
    def test_structured_stream_serializes_response_format_only_for_xai_and_fixed_token_providers(self):
        terminal = json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        providers = (
            XaiApiProvider(api_key="test-key"),
            supergrok_oauth_provider_from_access_token("test-token"),
        )
        for provider in providers:
            with self.subTest(provider=provider.name), patch("tomo_core.providers.httpx.stream", return_value=FakeStreamResponse(sse_chunks(terminal, "[DONE]"))) as stream:
                request_format = json.loads(json.dumps(STRUCTURED_RESPONSE_FORMAT))
                events = provider.stream_structured([{"role": "user", "content": "describe"}], response_format=request_format)
                request_format["type"] = "text"
                list(events)
                self.assertEqual(stream.call_args.kwargs["json"]["response_format"], STRUCTURED_RESPONSE_FORMAT)
            with self.subTest(provider=f"ordinary-{provider.name}"), patch("tomo_core.providers.httpx.stream", return_value=FakeStreamResponse(sse_chunks(terminal, "[DONE]"))) as stream:
                list(provider.stream([{"role": "user", "content": "describe"}]))
                self.assertNotIn("response_format", stream.call_args.kwargs["json"])

    def test_openrouter_structured_stream_requires_schema_capable_endpoint(self):
        terminal = json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        provider = XaiApiProvider(
            api_key="or-key",
            model="deepseek/deepseek-v4-flash-0731",
            base_url="https://openrouter.ai/api/v1",
        )

        with patch("tomo_core.providers.httpx.stream", return_value=FakeStreamResponse(sse_chunks(terminal, "[DONE]"))) as stream:
            list(provider.stream_structured([{"role": "user", "content": "describe"}], response_format=STRUCTURED_RESPONSE_FORMAT))

        self.assertEqual(stream.call_args.kwargs["json"]["provider"], {"require_parameters": True})

    def test_oauth_backed_and_grok_auth_providers_forward_structured_streams(self):
        oauth = type("OAuth", (), {"token_path": lambda *_: type("Path", (), {"exists": lambda _: True, "read_text": lambda _, **__: '{"access_token":"unused"}'})()})()
        auth_store = type("AuthStore", (), {"access_token": lambda _: "unused"})()
        with patch("tomo_core.providers.SuperGrokOAuthProvider.stream_structured", return_value=iter(())) as supergrok_stream:
            list(OAuthBackedSuperGrokProvider(oauth=oauth).stream_structured([{"role": "user", "content": "describe"}], response_format=STRUCTURED_RESPONSE_FORMAT, actor_id="actor"))
            supergrok_stream.assert_called_once_with([{"role": "user", "content": "describe"}], response_format=STRUCTURED_RESPONSE_FORMAT, actor_id="actor")
        with patch("tomo_core.providers.XaiApiProvider.stream_structured", return_value=iter(())) as xai_stream:
            list(GrokAuthProvider(auth_store=auth_store).stream_structured([{"role": "user", "content": "describe"}], response_format=STRUCTURED_RESPONSE_FORMAT, actor_id="actor"))
            xai_stream.assert_called_once_with([{"role": "user", "content": "describe"}], response_format=STRUCTURED_RESPONSE_FORMAT, actor_id="actor")

    def test_vision_store_policy_and_multimodal_content_are_serialized(self):
        terminal = json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        messages = [{"role": "user", "content": [{"type": "text", "text": "describe"}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc"}}]}]
        provider = supergrok_oauth_provider_from_access_token("token", model="grok-4.3", reasoning_effort="low", store=False)

        with patch("tomo_core.providers.httpx.stream", return_value=FakeStreamResponse(sse_chunks(terminal, "[DONE]"))) as stream:
            list(provider.stream(messages))

        body = stream.call_args.kwargs["json"]
        self.assertEqual(body["model"], "grok-4.3")
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertIs(body["store"], False)
        self.assertEqual(body["messages"], messages)

    def test_ordinary_requests_omit_store_policy(self):
        terminal = json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        with patch("tomo_core.providers.httpx.stream", return_value=FakeStreamResponse(sse_chunks(terminal, "[DONE]"))) as stream:
            list(XaiApiProvider(api_key="test-key").stream([{"role": "user", "content": "hi"}]))
        self.assertNotIn("store", stream.call_args.kwargs["json"])

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

    def test_openrouter_usage_chunk_without_choices_is_ignored(self):
        events = (
            json.dumps({"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}),
            json.dumps({"usage": {"prompt_tokens": 3, "completion_tokens": 1}}),
            "[DONE]",
        )
        with patch("tomo_core.providers.httpx.stream", return_value=FakeStreamResponse(sse_chunks(*events))) as stream:
            result = list(
                XaiApiProvider(
                    api_key="or-key",
                    model="deepseek/deepseek-v4-flash-0731",
                    base_url="https://openrouter.ai/api/v1",
                ).stream([{"role": "user", "content": "hi"}])
            )
        self.assertEqual(result, [ProviderTextDelta("hi"), ProviderStreamCompleted("stop", input_tokens=3, output_tokens=1)])
        self.assertEqual(stream.call_args.kwargs["timeout"], 180)
        self.assertEqual(stream.call_args.kwargs["headers"]["HTTP-Referer"], "https://github.com/pandacover/tomo_v2")

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
                    "usage": {"prompt_tokens": 12, "completion_tokens": 7, "completion_tokens_details": {"reasoning_tokens": 4}},
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
                ProviderStreamCompleted("tool_calls", input_tokens=12, output_tokens=7, reasoning_tokens=4),
            ],
        )

    def test_stream_accepts_finish_without_a_done_sentinel(self):
        event = json.dumps({"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]})
        with patch("tomo_core.providers.httpx.stream", return_value=FakeStreamResponse(sse_chunks(event))):
            events = list(XaiApiProvider(api_key="test-key").stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(events, [ProviderTextDelta("hi"), ProviderStreamCompleted("stop")])

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

    def test_stream_failures_have_stable_privacy_safe_codes(self):
        cases = (
            (sse_chunks("not json"), "malformed_json"),
            (b"not-sse\n\n", "invalid_sse_event"),
            (sse_chunks(json.dumps({"choices": "wrong"})), "invalid_choices_type"),
        )
        for chunks, expected in cases:
            with self.subTest(expected=expected), patch(
                "tomo_core.providers.httpx.stream",
                return_value=FakeStreamResponse(chunks if isinstance(chunks, list) else [chunks]),
            ):
                with self.assertRaises(ProviderStreamError) as raised:
                    list(XaiApiProvider(api_key="test-key").stream([{"role": "user", "content": "hi"}]))
                self.assertEqual(raised.exception.code, expected)

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
        ):
            with self.subTest(chunks=chunks), patch("tomo_core.providers.httpx.stream", return_value=FakeStreamResponse(chunks)):
                with self.assertRaises(ValueError):
                    list(XaiApiProvider(api_key="test-key").stream([{"role": "user", "content": "hi"}]))
        with patch("tomo_core.providers.httpx.stream", return_value=FakeStreamResponse(sse_chunks(no_finish, "[DONE]"))):
            events = list(XaiApiProvider(api_key="test-key").stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(events, [ProviderTextDelta("hi"), ProviderStreamCompleted("stop")])

    def test_stream_keeps_last_reasoning_usage_and_rejects_invalid_usage(self):
        invalid = json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens_details": {"reasoning_tokens": -1}}})
        first = json.dumps({"choices": [{"delta": {}, "finish_reason": None}], "usage": {"completion_tokens_details": {"reasoning_tokens": 2}}})
        later = json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens_details": {"reasoning_tokens": 3}}})
        with patch("tomo_core.providers.httpx.stream", return_value=FakeStreamResponse(sse_chunks(invalid, "[DONE]"))):
            with self.assertRaises(ValueError):
                list(XaiApiProvider(api_key="test-key").stream([{"role": "user", "content": "hi"}]))
        with patch("tomo_core.providers.httpx.stream", return_value=FakeStreamResponse(sse_chunks(first, later, "[DONE]"))):
            events = list(XaiApiProvider(api_key="test-key").stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(events, [ProviderStreamCompleted("stop", reasoning_tokens=3)])

    def test_completed_rejects_invalid_reasoning_tokens(self):
        with self.assertRaises(ValueError):
            ProviderStreamCompleted("stop", reasoning_tokens=True)

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
        first_messages = [{"role": "system", "content": "SHOULD emit one turn_plan before controls or frames."}]
        later_messages = [{"role": "system", "content": "do not emit a turn_plan; reuse the fixed turn plan."}]

        first_events = list(provider.stream(first_messages))
        later_events = list(provider.stream(later_messages))

        self.assertEqual(first_events[-1], ProviderStreamCompleted("stop"))
        self.assertEqual(later_events[-1], ProviderStreamCompleted("stop"))
        first_records = [json.loads(line) for line in first_events[0].text.splitlines()]
        later_records = [json.loads(line) for line in later_events[0].text.splitlines()]
        self.assertEqual(
            first_records,
            [
                {"type": "turn_plan", "primary_move": "answer", "supporting_moves": [], "response_goal": "return the configured static smoke response", "confidence": "high", "reaction": None},
                {"type": "frame", "text": "hello"},
            ],
        )
        self.assertEqual(later_records, [{"type": "frame", "text": "hello"}])
        with self.assertRaisesRegex(ValueError, "does not support tool calls"):
            list(provider.stream(first_messages, tools=({"type": "function"},)))


if __name__ == "__main__":
    unittest.main()
