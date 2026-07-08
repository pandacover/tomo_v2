import tempfile
import unittest

from tomo_core.oauth import OAuthManager
from tomo_core.telegram_bot import TelegramPollingBot


class CallbackPasteClient:
    def __init__(self):
        self.sent_messages = []

    def get_updates(self, offset=None, timeout=30):
        return [
            {
                "update_id": 22,
                "message": {
                    "message_id": 9,
                    "from": {"id": 99},
                    "chat": {"id": 99, "type": "private"},
                    "text": "http://127.0.0.1:56121/callback?code=abc&state=missing",
                },
            }
        ]

    def send_message(self, actor_id, text, reply_to_message_id=None, reply_markup=None):
        self.sent_messages.append({"actor_id": actor_id, "text": text, "reply_to_message_id": reply_to_message_id})


class RuntimeShouldNotHandleCallback:
    def handle_telegram_text(self, envelope):
        raise AssertionError("oauth callback paste must not fall through to the agent runtime")


class OAuthCallbackPasteErrorTests(unittest.TestCase):
    def test_callback_paste_without_pending_flow_reports_oauth_error_not_agent_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = CallbackPasteClient()
            oauth = OAuthManager(data_dir=tmp, providers=OAuthManager.default_providers())

            TelegramPollingBot(client=client, runtime=RuntimeShouldNotHandleCallback(), oauth=oauth).poll_once()

            self.assertEqual(len(client.sent_messages), 1)
            self.assertIn("no pending oauth flow", client.sent_messages[0]["text"])
            self.assertEqual(client.sent_messages[0]["reply_to_message_id"], "9")


if __name__ == "__main__":
    unittest.main()
