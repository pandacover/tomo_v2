import tempfile
import unittest
from unittest.mock import patch

from tomo_core.oauth import OAuthManager
from tomo_core.telegram_bot import TelegramPollingBot


class RawCodeClient:
    def __init__(self):
        self.sent_messages = []

    def get_updates(self, offset=None, timeout=30):
        return [
            {
                "update_id": 41,
                "message": {
                    "message_id": 14,
                    "from": {"id": 99},
                    "chat": {"id": 99, "type": "private"},
                    "text": "raw-code-from-xai-screen",
                },
            }
        ]

    def send_message(self, actor_id, text, reply_to_message_id=None, reply_markup=None):
        self.sent_messages.append({"actor_id": actor_id, "text": text, "reply_to_message_id": reply_to_message_id})


class RuntimeShouldNotHandleRawOauthCode:
    def handle_telegram_text(self, envelope):
        raise AssertionError("raw oauth code must be consumed by oauth handler while a pending flow exists")


class RawOAuthCodePasteTests(unittest.TestCase):
    def test_raw_code_completes_pending_supergrok_oauth_instead_of_agent_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            oauth = OAuthManager(data_dir=tmp, providers=OAuthManager.default_providers())
            oauth.begin("99", "supergrok")
            client = RawCodeClient()

            class FakeResponse:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}

            with patch("tomo_core.oauth.httpx.post", return_value=FakeResponse()) as post:
                TelegramPollingBot(client=client, runtime=RuntimeShouldNotHandleRawOauthCode(), oauth=oauth).poll_once()

            self.assertEqual(post.call_args.kwargs["data"]["code"], "raw-code-from-xai-screen")
            self.assertEqual(client.sent_messages[0]["text"], "supergrok connected.")


if __name__ == "__main__":
    unittest.main()
