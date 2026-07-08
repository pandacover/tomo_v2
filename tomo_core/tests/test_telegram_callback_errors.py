import tempfile
import unittest

from tomo_core.oauth import OAuthManager, OAuthProviderConfig
from tomo_core.telegram_bot import TelegramBotApiError, TelegramPollingBot


class CallbackAnswerFailureClient:
    def __init__(self):
        self.sent_messages = []

    def get_updates(self, offset=None, timeout=30):
        return [
            {
                "update_id": 5,
                "callback_query": {
                    "id": "stale-callback",
                    "from": {"id": 99},
                    "message": {"message_id": 7, "chat": {"id": 99, "type": "private"}},
                    "data": "connect:supergrok",
                },
            }
        ]

    def answer_callback_query(self, callback_query_id, text=None):
        raise TelegramBotApiError("telegram answerCallbackQuery failed: query is too old")

    def send_message(self, actor_id, text, reply_to_message_id=None, reply_markup=None):
        self.sent_messages.append({"actor_id": actor_id, "text": text, "reply_to_message_id": reply_to_message_id})


class TelegramCallbackErrorTests(unittest.TestCase):
    def test_stale_answer_callback_query_does_not_crash_connect_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = CallbackAnswerFailureClient()
            oauth = OAuthManager(
                data_dir=tmp,
                providers={
                    "supergrok": OAuthProviderConfig(
                        provider="supergrok",
                        client_id="sg-client",
                        auth_url="https://auth.example/authorize",
                        token_url="https://auth.example/token",
                        redirect_uri="http://127.0.0.1:56120/callback",
                        scopes=("openid",),
                    )
                },
            )
            next_offset = TelegramPollingBot(client=client, runtime=None, oauth=oauth).poll_once()

            self.assertEqual(next_offset, 6)
            self.assertEqual(len(client.sent_messages), 1)
            self.assertIn("https://auth.example/authorize?", client.sent_messages[0]["text"])


if __name__ == "__main__":
    unittest.main()
