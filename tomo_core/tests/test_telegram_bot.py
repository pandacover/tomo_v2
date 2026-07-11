import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tomo_core import RuntimeConfig
from tomo_core.providers import StaticProvider
from tomo_core.runtime import PersonalAgentRuntime
from tomo_core.telegram import TelegramDeliverySink, TelegramSendReceipt
from tomo_core.telegram_bot import TelegramBotApiClient, TelegramPollingBot, envelope_from_update


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


if __name__ == "__main__":
    unittest.main()
