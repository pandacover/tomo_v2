import unittest
import json
from io import BytesIO

import httpx
from PIL import Image

from tomo_core.attachment_reader import AttachmentReadError
from tomo_core.models import InboundEnvelope, InboundMessage, MessageAttachment
from tomo_core.providers import ProviderStreamCompleted, ProviderTextDelta, ProviderToolCallReady
from tomo_core.sessions import ConversationSession
from tomo_core.vision import DownloadedAttachment, ProviderVisionInterpreter, VisionObservation


class VisionObservationTests(unittest.TestCase):
    def test_ok_observation_exposes_only_safe_prompt_payload(self):
        observation = VisionObservation(
            message_id="m1",
            attachment_index=0,
            status="ok",
            summary="a terminal showing a failed test",
            visible_text=("AssertionError",),
            relevant_details=("the failure is in test_login",),
            uncertainties=("the final path segment is blurred",),
        )

        self.assertEqual(observation.prompt_payload()["status"], "ok")
        self.assertNotIn("model", observation.prompt_payload())

    def test_observation_requires_safe_bounded_content(self):
        with self.assertRaises(ValueError):
            VisionObservation("m1", 0, "ok", "", (), (), ())
        with self.assertRaises(ValueError):
            VisionObservation("m1", 0, "unavailable", "", (), (), (), "provider said nope")

    def test_unavailable_observation_has_only_safe_code_and_empty_content(self):
        for values in (("detail", (), (), ()), ("", ("detail",), (), ()), ("", (), ("detail",), ()), ("", (), (), ("detail",))):
            with self.subTest(values=values), self.assertRaises(ValueError):
                VisionObservation("m1", 0, "unavailable", *values, "unsupported_image")

    def test_attachment_and_observation_reject_invalid_types_and_bounds(self):
        for args in ((b"", "image/jpeg"), ("data", "image/jpeg"), (b"data", "text/plain")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                DownloadedAttachment(*args)
        for args in (("", 0, "ok", "ok", (), (), ()), ("m", True, "ok", "ok", (), (), ()), ("m", 0, "ok", "x" * 2001, (), (), ()), ("m", 0, "ok", "ok", tuple("x" for _ in range(9)), (), ())):
            with self.subTest(args=args), self.assertRaises(ValueError):
                VisionObservation(*args)

    def test_attachment_metadata_rejects_booleans(self):
        with self.assertRaises(ValueError):
            MessageAttachment("image", "id", metadata={"width": True})

    def test_session_validates_observations_before_mutating_and_orders_them(self):
        session = ConversationSession("telegram:actor:u")
        inbound = InboundMessage(1, 1, InboundEnvelope("telegram", "u", "m", "", attachments=(MessageAttachment("image", "a"), MessageAttachment("image", "b"))))
        session.append_inbound_once(inbound, "burst")
        good = VisionObservation("m", 1, "ok", "second", (), (), ())
        for invalid in ((object(),), (VisionObservation("other", 0, "ok", "wrong", (), (), ()),), (good, good)):
            before = list(session.messages)
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                session.record_vision_observations("burst", invalid)
            self.assertEqual(session.messages, before)
        first = VisionObservation("m", 0, "ok", "first", (), (), ())
        session.record_vision_observations("burst", (good, first))
        self.assertEqual([item["summary"] for item in session.messages[0].metadata["vision_observations"]], ["first", "second"])
        history = session.model_history_for_burst("other")[0]["content"]
        self.assertNotIn('"file_id"', history)
        self.assertNotIn("data:image", history)


class ProviderVisionInterpreterTests(unittest.TestCase):
    def _interpreter(self, image_data, events):
        class Reader:
            def read(self, attachment):
                return DownloadedAttachment(image_data, "image/jpeg")

        class Provider:
            name = "fake"
            supports_images_in = True
            supports_images_out = False
            supports_tool_calls = False
            def __init__(self): self.calls = []
            def stream(self, messages, *, tools=(), actor_id=None):
                self.calls.append((messages, tools, actor_id))
                return iter(events)

        provider = Provider()
        return ProviderVisionInterpreter(provider, Reader()), provider

    @staticmethod
    def _png(mode="RGB", size=(20, 10)):
        output = BytesIO()
        Image.new(mode, size, "red").save(output, format="PNG")
        return output.getvalue()

    def test_normalizes_image_and_uses_one_untrusted_multimodal_request(self):
        image = Image.new("RGBA", (3000, 1000), "red")
        source = BytesIO()
        image.save(source, format="PNG")

        class Reader:
            def read(self, attachment):
                return DownloadedAttachment(source.getvalue(), "image/not-real")

        class Provider:
            name = "fake"
            supports_images_in = True
            supports_images_out = False
            supports_tool_calls = False
            def __init__(self): self.calls = []
            def stream(self, messages, *, tools=(), actor_id=None):
                self.calls.append((messages, tools, actor_id))
                payload = {"summary": "red rectangle", "visible_text": [], "relevant_details": ["wide image"], "uncertainties": []}
                return iter((ProviderTextDelta(json.dumps(payload)), ProviderStreamCompleted("stop")))

        provider = Provider()
        interpreter = ProviderVisionInterpreter(provider, Reader())
        observation = interpreter.observe(MessageAttachment("image", "id"), "what is this?", message_id="m1", attachment_index=0, actor_id="telegram-actor")

        self.assertEqual(observation.summary, "red rectangle")
        self.assertEqual(len(provider.calls), 1)
        messages, tools, actor_id = provider.calls[0]
        self.assertEqual(tools, ())
        self.assertEqual(actor_id, "telegram-actor")
        self.assertIn("untrusted evidence", messages[0]["content"])
        self.assertEqual(messages[1]["content"][0]["text"], "what is this?")
        self.assertTrue(messages[1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_accepts_bounded_json_fence_with_harmless_outer_whitespace(self):
        payload = '{"summary":"red rectangle","visible_text":[],"relevant_details":[],"uncertainties":[]}'
        for document in (" \n" + payload + "\n ", " \n```json\n" + payload + "\n```\n ", "\n```\n" + payload + "\n```\n"):
            with self.subTest(document=document[:10]):
                interpreter, provider = self._interpreter(
                    self._png(),
                    (ProviderTextDelta(document), ProviderStreamCompleted("stop")),
                )

                observation = interpreter.observe(MessageAttachment("image", "id"), "q", message_id="m", attachment_index=0, actor_id="actor")

                self.assertEqual(observation.status, "ok")
                self.assertEqual(observation.summary, "red rectangle")
                prompt = provider.calls[0][0][0]["content"]
                self.assertIn('summary (a nonblank string)', prompt)
                self.assertIn('visible_text (an array of strings)', prompt)
                self.assertIn('Do not use Markdown, fences, or prose', prompt)

    def test_rejects_all_malformed_specialist_json_shapes(self):
        image = self._png()
        valid = {"summary": "seen", "visible_text": [], "relevant_details": [], "uncertainties": []}
        invalid = [
            {**valid, "summary": value} for value in (None, True, 3, " ", "x" * 2001)
        ] + [
            {**valid, field: value}
            for field in ("visible_text", "relevant_details", "uncertainties")
            for value in ("characters", {}, None, True, 3, [" "], ["x" * 1001], ["x"] * 9)
        ] + [
            {"summary": "seen", "visible_text": [], "relevant_details": []},
            {**valid, "extra": "nope"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                interpreter, provider = self._interpreter(image, (ProviderTextDelta(json.dumps(payload)), ProviderStreamCompleted("stop")))
                result = interpreter.observe(MessageAttachment("image", "id"), "q", message_id="m", attachment_index=0, actor_id="actor")
                self.assertEqual((result.status, result.error_code), ("unavailable", "vision_invalid_response"))
                self.assertEqual(len(provider.calls), 1)

    def test_rejects_invalid_stream_lifecycle_and_response_text(self):
        image = self._png()
        valid = json.dumps({"summary": "seen", "visible_text": [], "relevant_details": [], "uncertainties": []})
        cases = (
            ((), "missing completion"),
            ((ProviderTextDelta(valid), ProviderStreamCompleted("tool_calls")), "tool finish"),
            ((ProviderTextDelta(valid), ProviderStreamCompleted("stop"), ProviderStreamCompleted("stop")), "two completions"),
            ((ProviderToolCallReady("call", "tool", "{}"), ProviderStreamCompleted("stop")), "tool event"),
            ((ProviderTextDelta(valid), ProviderStreamCompleted("stop"), ProviderTextDelta("x")), "trailing delta"),
            ((ProviderTextDelta(" ```json"), ProviderStreamCompleted("stop")), "whitespace"),
            ((ProviderTextDelta("before\n" + valid), ProviderStreamCompleted("stop")), "leading prose"),
            ((ProviderTextDelta(valid + "\nafter"), ProviderStreamCompleted("stop")), "trailing prose"),
            ((ProviderTextDelta("```json\n```json\n" + valid + "\n```\n```"), ProviderStreamCompleted("stop")), "nested fence"),
            ((ProviderTextDelta("```json\n" + valid + "\n```\n```json\n" + valid + "\n```"), ProviderStreamCompleted("stop")), "multiple fences"),
            ((ProviderTextDelta("not json"), ProviderStreamCompleted("stop")), "malformed"),
            ((ProviderTextDelta("x" * 12001), ProviderStreamCompleted("stop")), "oversize"),
        )
        for events, name in cases:
            with self.subTest(name=name):
                interpreter, provider = self._interpreter(image, events)
                result = interpreter.observe(MessageAttachment("image", "id"), "q", message_id="m", attachment_index=0, actor_id="actor")
                self.assertEqual(result.error_code, "vision_invalid_response")
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(provider.calls[0][1], ())

    def test_normalizes_supported_images_without_source_metadata(self):
        for format in ("JPEG", "PNG", "WEBP"):
            with self.subTest(format=format):
                source = BytesIO()
                image = Image.new("RGBA" if format != "JPEG" else "RGB", (3000, 1000), "red")
                image.save(source, format=format)
                events = (ProviderTextDelta('{"summary":"seen","visible_text":[],"relevant_details":[],"uncertainties":[]}'), ProviderStreamCompleted("stop"))
                interpreter, provider = self._interpreter(source.getvalue(), events)
                self.assertEqual(interpreter.observe(MessageAttachment("image", "id"), "q", message_id="m", attachment_index=0, actor_id="actor").status, "ok")
                url = provider.calls[0][0][1]["content"][1]["image_url"]["url"]
                import base64
                with Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1]))) as normalized:
                    self.assertEqual(normalized.format, "JPEG")
                    self.assertLessEqual(max(normalized.size), 2048)
                    self.assertEqual(normalized.mode, "RGB")
                    self.assertNotIn("exif", normalized.info)

    def test_reader_and_image_failures_have_safe_distinct_results(self):
        class Reader:
            def __init__(self, result): self.result = result
            def read(self, attachment):
                if isinstance(self.result, Exception): raise self.result
                return self.result
        class Provider:
            name = "fake"; supports_images_in = True; supports_images_out = False; supports_tool_calls = False
            def stream(self, messages, *, tools=(), actor_id=None): return iter(())
        for result, code in ((AttachmentReadError("private detail"), "vision_unavailable"), (OSError("private detail"), "vision_unavailable"), (DownloadedAttachment(b"bad", "image/jpeg"), "unsupported_image")):
            with self.subTest(result=result):
                observation = ProviderVisionInterpreter(Provider(), Reader(result)).observe(MessageAttachment("image", "id"), "q", message_id="m", attachment_index=0, actor_id="actor")
                self.assertEqual(observation.error_code, code)
                self.assertNotIn("private", str(observation))

    def test_rejects_corrupt_animated_unsupported_and_oversized_images(self):
        animated = BytesIO()
        frames = (Image.new("RGB", (10, 10), "red"), Image.new("RGB", (10, 10), "blue"))
        frames[0].save(animated, format="GIF", save_all=True, append_images=frames[1:])
        oversized = self._png(size=(5000, 5000))
        for data in (b"corrupt", animated.getvalue(), oversized, b"x" * (10 * 1024 * 1024 + 1)):
            with self.subTest(length=len(data)):
                interpreter, provider = self._interpreter(data, ())
                result = interpreter.observe(MessageAttachment("image", "id", mime_type="image/jpeg"), "q", message_id="m", attachment_index=0, actor_id="actor")
                self.assertEqual(result.error_code, "unsupported_image")
                self.assertEqual(provider.calls, [])

    def test_401_propagates_and_other_provider_failures_are_safe(self):
        request = httpx.Request("POST", "https://provider.example")
        unauthorized = httpx.HTTPStatusError("secret body", request=request, response=httpx.Response(401, request=request, text="secret body"))
        unavailable = httpx.HTTPStatusError("secret body", request=request, response=httpx.Response(500, request=request, text="secret body"))
        for error, raises in ((unauthorized, True), (unavailable, False), (OSError("secret body"), False)):
            class Reader:
                def read(self, attachment): return DownloadedAttachment(self_image, "image/jpeg")
            class Provider:
                name = "fake"; supports_images_in = True; supports_images_out = False; supports_tool_calls = False
                def stream(self, messages, *, tools=(), actor_id=None): raise error
            self_image = self._png()
            interpreter = ProviderVisionInterpreter(Provider(), Reader())
            if raises:
                with self.assertRaises(httpx.HTTPStatusError) as caught:
                    interpreter.observe(MessageAttachment("image", "id"), "q", message_id="m", attachment_index=0, actor_id="actor")
                self.assertIs(caught.exception, error)
            else:
                result = interpreter.observe(MessageAttachment("image", "id"), "q", message_id="m", attachment_index=0, actor_id="actor")
                self.assertEqual(result.error_code, "vision_unavailable")
                self.assertNotIn("secret", str(result))
