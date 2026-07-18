import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tomo_core import RuntimeConfig
from tomo_core.providers import StaticProvider
from tomo_core.runtime import PersonalAgentRuntime
from tomo_core.telegram import TelegramDeliverySink, TelegramSendReceipt
from tomo_core.telegram_bot import TelegramBotApiClient, TelegramBotApiError, TelegramDeliveryError, TelegramPollingBot, TelegramReactionError, envelope_from_update


class FakeBotApiClient:
    def __init__(self, updates):
        self.updates = updates
        self.sent_actions = []
        self.sent_messages = []
        self.get_updates_calls = []

    def get_updates(self, offset=None, timeout=30):
        self.get_updates_calls.append({"offset": offset, "timeout": timeout})
        return self.updates

    def send_typing(self, actor_id):
        self.sent_actions.append({"chat_id": actor_id, "action": "typing"})

    def send_message(self, actor_id, text, reply_to_message_id=None):
        self.sent_messages.append(
            {"chat_id": actor_id, "text": text, "reply_to_message_id": reply_to_message_id}
        )


class TelegramBotTests(unittest.TestCase):
    def test_envelope_from_private_text_update(self):
        envelope = envelope_from_update(
            {
                "update_id": 10,
                "message": {
                    "message_id": 77,
                    "from": {"id": 123},
                    "chat": {"id": 123, "type": "private"},
                    "text": "hi tomo",
                },
            }
        )
        self.assertIsNotNone(envelope)
        assert envelope is not None
        self.assertEqual(envelope.actor_id, "123")
        self.assertEqual(envelope.message_id, "77")
        self.assertEqual(envelope.text, "hi tomo")

    def test_envelope_captures_same_chat_reply_to_user_text(self):
        envelope = envelope_from_update({"update_id": 10, "message": {"message_id": 77, "from": {"id": 123}, "chat": {"id": 123, "type": "private"}, "text": "what about this?", "reply_to_message": {"message_id": 76, "from": {"id": 123}, "chat": {"id": 123, "type": "private"}, "date": 100, "text": "the earlier idea"}}})

        assert envelope is not None
        self.assertEqual(envelope.reply_context.message_id, "76")
        self.assertEqual(envelope.reply_context.author_role, "user")
        self.assertEqual(envelope.reply_context.text, "the earlier idea")

    def test_envelope_omits_cross_chat_reply_context(self):
        envelope = envelope_from_update({"update_id": 10, "message": {"message_id": 77, "from": {"id": 123}, "chat": {"id": 123, "type": "private"}, "text": "what about this?", "reply_to_message": {"message_id": 76, "chat": {"id": 999, "type": "private"}, "text": "wrong chat"}}})

        assert envelope is not None
        self.assertIsNone(envelope.reply_context)

    def test_envelope_captures_bot_caption_and_photo_reply_and_honest_unavailable_reply(self):
        base = {"update_id": 10, "message": {"message_id": 77, "from": {"id": 123}, "chat": {"id": 123, "type": "private"}, "text": "what about this?"}}
        base["message"]["reply_to_message"] = {"message_id": 76, "from": {"id": 1, "is_bot": True}, "chat": {"id": 123, "type": "private"}, "caption": "bot image", "photo": [{"file_id": "small", "width": 1}, {"file_id": "large", "width": 20, "height": 10}]}
        envelope = envelope_from_update(base)

        assert envelope is not None
        self.assertEqual((envelope.reply_context.author_role, envelope.reply_context.text, envelope.reply_context.attachments[0].file_id), ("assistant", "bot image", "large"))
        base["message"]["reply_to_message"] = {"message_id": 76, "chat": {"id": 123, "type": "private"}}
        self.assertEqual(envelope_from_update(base).reply_context.availability, "unavailable")
        base["message"]["reply_to_message"]["text"] = "x" * 5000
        reply = envelope_from_update(base).reply_context
        self.assertEqual((len(reply.text), reply.truncated), (4096, True))

    def test_group_updates_are_ignored_for_dm_only_slice(self):
        envelope = envelope_from_update(
            {
                "update_id": 10,
                "message": {
                    "message_id": 77,
                    "from": {"id": 123},
                    "chat": {"id": -100, "type": "group"},
                    "text": "hi tomo",
                },
            }
        )
        self.assertIsNone(envelope)

    def test_poll_once_routes_update_to_runtime_and_sends_telegram_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            update = {
                "update_id": 42,
                "message": {
                    "message_id": 5,
                    "from": {"id": 99},
                    "chat": {"id": 99, "type": "private"},
                    "text": "yo",
                },
            }
            client = FakeBotApiClient([update])
            runtime = PersonalAgentRuntime(
                provider=StaticProvider("yooo. bot wire is alive."),
                telegram=TelegramDeliverySink(client),
                config=RuntimeConfig(data_dir=tmp, soul_path=str(Path(tmp) / "SOUL.md")),
            )
            next_offset = TelegramPollingBot(client=client, runtime=runtime, poll_timeout=1).poll_once()

            self.assertEqual(next_offset, 43)
            self.assertEqual(client.sent_actions, [{"chat_id": "99", "action": "typing"}])
            self.assertEqual(client.sent_messages[0]["chat_id"], "99")
            self.assertEqual(client.sent_messages[0]["reply_to_message_id"], "5")

    def test_process_update_propagates_runtime_failures_for_durable_retry(self):
        class FailingRuntime:
            def handle_telegram_text(self, envelope):
                raise RuntimeError("temporary dispatch failure")

        reported = []
        with self.assertRaisesRegex(RuntimeError, "temporary dispatch failure"):
            TelegramPollingBot(client=FakeBotApiClient([]), runtime=FailingRuntime(), on_error=reported.append).process_update(
                {
                    "update_id": 42,
                    "message": {
                        "message_id": 5,
                        "from": {"id": 99},
                        "chat": {"id": 99, "type": "private"},
                        "text": "yo",
                    },
                }
            )
        self.assertEqual(len(reported), 1)

    def test_bot_api_send_message_returns_message_receipt(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"ok": True, "result": {"message_id": 123}}

        with patch("httpx.post", return_value=Response()) as post:
            receipt = TelegramBotApiClient("token").send_message("chat", "hello", reply_to_message_id="7")

        self.assertEqual(receipt, TelegramSendReceipt("123"))
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["reply_parameters"], {"message_id": 7})

    def test_bot_api_classifies_transport_failures_without_exposing_details(self):
        import httpx
        client = TelegramBotApiClient("secret-token")
        with patch("httpx.post", side_effect=httpx.ReadTimeout("payload secret")):
            with self.assertRaises(TelegramDeliveryError) as error:
                client.send_message("chat", "hello")
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(str(error.exception), "telegram_sendMessage_timeout")

    def test_bot_api_keeps_known_rejection_definite(self):
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return {"ok": False, "description": "private payload"}
        with patch("httpx.post", return_value=Response()):
            with self.assertRaises(TelegramDeliveryError) as error:
                TelegramBotApiClient("secret-token").send_message("chat", "hello")
        self.assertFalse(error.exception.uncertain)
        self.assertEqual(str(error.exception), "telegram_sendMessage_rejected")

    def test_bot_api_missing_message_receipt_is_uncertain(self):
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return {"ok": True, "result": {}}
        with patch("httpx.post", return_value=Response()):
            with self.assertRaises(TelegramDeliveryError) as error:
                TelegramBotApiClient("secret-token").send_message("chat", "hello")
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(str(error.exception), "telegram_sendMessage_missing_receipt")

    def test_bot_api_request_keeps_transport_errors_generic_for_polling(self):
        import httpx
        with patch("httpx.post", side_effect=httpx.ReadTimeout("upstream secret body")):
            with self.assertRaises(httpx.ReadTimeout):
                TelegramBotApiClient("secret-token").get_updates()

    def test_bot_api_reaction_timeout_remains_retryable(self):
        import httpx
        with patch("httpx.post", side_effect=httpx.ReadTimeout("upstream secret body")):
            with self.assertRaises(TelegramReactionError) as error:
                TelegramBotApiClient("secret-token").set_message_reaction("chat", "7", "👍")
        self.assertTrue(error.exception.retryable)

    def test_bot_api_typing_uses_a_short_best_effort_timeout(self):
        client = TelegramBotApiClient("token")

        with patch.object(client, "request") as request:
            client.send_typing("chat")

        request.assert_called_once_with("sendChatAction", {"chat_id": "chat", "action": "typing"}, timeout=2.0)

    def test_bot_api_reaction_uses_one_non_big_emoji_payload(self):
        client = TelegramBotApiClient("token")
        with patch.object(client, "request") as request:
            client.set_message_reaction("chat", "7", "👍")

        request.assert_called_once_with(
            "setMessageReaction",
            {"chat_id": "chat", "message_id": 7, "reaction": [{"type": "emoji", "emoji": "👍"}], "is_big": False},
        )

    def test_bot_api_reaction_maps_failures_to_safe_codes(self):
        client = TelegramBotApiClient("token")
        with self.assertRaises(TelegramReactionError) as invalid:
            client.set_message_reaction("chat", "not-an-id", "👍")
        self.assertEqual((invalid.exception.code, invalid.exception.retryable), ("telegram_reaction_invalid_target", False))
        with patch.object(client, "request", side_effect=RuntimeError("telegram rejected reaction")):
            with self.assertRaises(TelegramReactionError) as rejected:
                client.set_message_reaction("chat", "7", "👍")
        self.assertEqual((rejected.exception.code, rejected.exception.retryable), ("telegram_reaction_rejected", False))
        self.assertNotIn("telegram rejected reaction", str(rejected.exception))

        import httpx
        with patch.object(client, "request", side_effect=httpx.ReadTimeout("upstream secret body")):
            with self.assertRaises(TelegramReactionError) as retryable:
                client.set_message_reaction("chat", "7", "👍")
        self.assertEqual((retryable.exception.code, retryable.exception.retryable), ("telegram_reaction_retryable", True))
        self.assertNotIn("upstream secret body", str(retryable.exception))


if __name__ == "__main__":
    unittest.main()
