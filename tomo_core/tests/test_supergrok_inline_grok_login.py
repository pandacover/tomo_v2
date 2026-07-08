import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tomo_core.oauth import OAuthManager
from tomo_core.telegram_bot import TelegramPollingBot


class FakeTelegramClient:
    def __init__(self, updates):
        self.updates = updates
        self.sent_messages = []
        self.answered_callbacks = []

    def get_updates(self, offset=None, timeout=30):
        return self.updates

    def answer_callback_query(self, callback_query_id, text=None):
        self.answered_callbacks.append({"id": callback_query_id, "text": text})

    def send_message(self, actor_id, text, reply_to_message_id=None, reply_markup=None):
        self.sent_messages.append(
            {"actor_id": actor_id, "text": text, "reply_to_message_id": reply_to_message_id, "reply_markup": reply_markup}
        )


class SuperGrokInlineGrokLoginTests(unittest.TestCase):
    def test_supergrok_connect_imports_existing_grok_login_auth_into_tomo_oauth_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            grok_auth = Path(tmp) / "grok-auth.json"
            grok_auth.write_text(json.dumps({"access_token": "grok-access", "refresh_token": "grok-refresh"}), encoding="utf-8")
            client = FakeTelegramClient(
                [
                    {
                        "update_id": 10,
                        "callback_query": {
                            "id": "cb-1",
                            "from": {"id": 99},
                            "message": {"message_id": 7, "chat": {"id": 99, "type": "private"}},
                            "data": "connect:supergrok",
                        },
                    }
                ]
            )
            oauth = OAuthManager(data_dir=tmp, providers={})

            with patch.dict("os.environ", {"GROK_AUTH_JSON": str(grok_auth)}):
                TelegramPollingBot(client=client, runtime=None, oauth=oauth).poll_once()

            token_path = Path(tmp) / "oauth" / "token_supergrok_99.json"
            saved = json.loads(token_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["access_token"], "grok-access")
            self.assertEqual(saved["refresh_token"], "grok-refresh")
            self.assertIn("supergrok oauth connected", client.sent_messages[0]["text"])

    def test_supergrok_connect_without_grok_login_or_oauth_config_sends_login_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeTelegramClient(
                [
                    {
                        "update_id": 11,
                        "callback_query": {
                            "id": "cb-2",
                            "from": {"id": 99},
                            "message": {"message_id": 7, "chat": {"id": 99, "type": "private"}},
                            "data": "connect:supergrok",
                        },
                    }
                ]
            )
            oauth = OAuthManager(data_dir=tmp, providers={})

            with patch.dict("os.environ", {"GROK_AUTH_JSON": str(Path(tmp) / "missing.json")}):
                TelegramPollingBot(client=client, runtime=None, oauth=oauth).poll_once()

            self.assertIn("grok login", client.sent_messages[0]["text"])
            self.assertFalse((Path(tmp) / "oauth" / "token_supergrok_99.json").exists())


if __name__ == "__main__":
    unittest.main()
