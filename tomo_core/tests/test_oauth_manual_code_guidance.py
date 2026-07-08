import tempfile
import unittest

from tomo_core.oauth import OAuthManager
from tomo_core.telegram_bot import TelegramPollingBot


class NoCodeCallbackClient:
    def __init__(self, text):
        self.text = text
        self.sent_messages = []

    def get_updates(self, offset=None, timeout=30):
        return [
            {
                "update_id": 31,
                "message": {
                    "message_id": 12,
                    "from": {"id": 99},
                    "chat": {"id": 99, "type": "private"},
                    "text": self.text,
                },
            }
        ]

    def send_message(self, actor_id, text, reply_to_message_id=None, reply_markup=None):
        self.sent_messages.append({"actor_id": actor_id, "text": text, "reply_to_message_id": reply_to_message_id})


class OAuthManualCodeGuidanceTests(unittest.TestCase):
    def test_callback_url_without_code_asks_for_screen_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            oauth = OAuthManager(data_dir=tmp, providers=OAuthManager.default_providers())
            oauth.begin("99", "supergrok")
            client = NoCodeCallbackClient("http://127.0.0.1:56121/callback?state=abc")

            TelegramPollingBot(client=client, runtime=None, oauth=oauth).poll_once()

            self.assertIn("paste the code", client.sent_messages[0]["text"])
            self.assertIn("screen", client.sent_messages[0]["text"])


if __name__ == "__main__":
    unittest.main()
