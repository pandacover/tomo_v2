import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tomo_core.oauth import OAuthManager, OAuthProviderConfig
from tomo_core.telegram_bot import TelegramPollingBot


class FakeTelegramConnectClient:
    def __init__(self, updates):
        self.updates = updates
        self.sent_messages = []
        self.answered_callbacks = []

    def get_updates(self, offset=None, timeout=30):
        return self.updates

    def send_typing(self, actor_id):
        raise AssertionError("/connect should not start the agent turn")

    def send_message(self, actor_id, text, reply_to_message_id=None, reply_markup=None):
        self.sent_messages.append(
            {
                "chat_id": actor_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
                "reply_markup": reply_markup,
            }
        )

    def answer_callback_query(self, callback_query_id, text=None):
        self.answered_callbacks.append({"id": callback_query_id, "text": text})


class ConnectOAuthTests(unittest.TestCase):
    def test_oauth_begin_generates_provider_url_and_persists_pending_challenge(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = OAuthManager(
                data_dir=tmp,
                providers={
                    "supergrok": OAuthProviderConfig(
                        provider="supergrok",
                        client_id="xai-client",
                        auth_url="https://auth.example/authorize",
                        token_url="https://auth.example/token",
                        redirect_uri="http://127.0.0.1:56120/callback",
                        scopes=("openid", "profile", "offline.access"),
                    )
                },
            )

            auth_url = manager.begin("actor-1", "supergrok")

            self.assertIn("https://auth.example/authorize?", auth_url)
            self.assertIn("client_id=xai-client", auth_url)
            self.assertIn("code_challenge=", auth_url)
            self.assertIn("state=", auth_url)
            pending = json.loads((Path(tmp) / "oauth" / "pending_supergrok_actor-1.json").read_text())
            self.assertEqual(pending["provider"], "supergrok")
            self.assertEqual(pending["actor_id"], "actor-1")

    def test_google_calendar_provider_uses_calendar_scopes(self):
        providers = OAuthManager.default_providers(
            google_client_id="google-client",
            google_client_secret="google-secret",
            xai_client_id="xai-client",
        )
        google = providers["google_calendar"]

        self.assertEqual(google.auth_url, "https://accounts.google.com/o/oauth2/v2/auth")
        self.assertIn("https://www.googleapis.com/auth/calendar.events", google.scopes)
        self.assertEqual(google.client_id, "google-client")

    def test_connect_command_sends_inline_menu_without_agent_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeTelegramConnectClient(
                [
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 2,
                            "from": {"id": 99},
                            "chat": {"id": 99, "type": "private"},
                            "text": "/connect",
                        },
                    }
                ]
            )
            bot = TelegramPollingBot(client=client, runtime=None, oauth=OAuthManager(data_dir=tmp, providers={}))

            bot.poll_once()

            self.assertEqual(len(client.sent_messages), 1)
            message = client.sent_messages[0]
            self.assertEqual(message["chat_id"], "99")
            self.assertIn("connect", message["text"].lower())
            buttons = message["reply_markup"]["inline_keyboard"]
            self.assertEqual(buttons[0][0]["callback_data"], "connect:supergrok")
            self.assertEqual(buttons[1][0]["callback_data"], "connect:google_calendar")

    def test_connect_callback_sends_provider_auth_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            oauth = OAuthManager(
                data_dir=tmp,
                providers={
                    "supergrok": OAuthProviderConfig(
                        provider="supergrok",
                        client_id="xai-client",
                        auth_url="https://auth.example/authorize",
                        token_url="https://auth.example/token",
                        redirect_uri="http://127.0.0.1:56120/callback",
                        scopes=("openid",),
                    )
                },
            )
            client = FakeTelegramConnectClient(
                [
                    {
                        "update_id": 2,
                        "callback_query": {
                            "id": "cb-1",
                            "from": {"id": 99},
                            "message": {"message_id": 7, "chat": {"id": 99, "type": "private"}},
                            "data": "connect:supergrok",
                        },
                    }
                ]
            )
            bot = TelegramPollingBot(client=client, runtime=None, oauth=oauth)

            bot.poll_once()

            self.assertEqual(client.answered_callbacks, [{"id": "cb-1", "text": "opening supergrok connect"}])
            self.assertIn("https://auth.example/authorize?", client.sent_messages[0]["text"])
            self.assertEqual(client.sent_messages[0]["reply_to_message_id"], "7")

    def test_complete_callback_exchanges_code_and_saves_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            oauth = OAuthManager(
                data_dir=tmp,
                providers={
                    "google_calendar": OAuthProviderConfig(
                        provider="google_calendar",
                        client_id="google-client",
                        client_secret="google-secret",
                        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
                        token_url="https://oauth2.googleapis.com/token",
                        redirect_uri="http://127.0.0.1:56121/callback",
                        scopes=("https://www.googleapis.com/auth/calendar.events",),
                    )
                },
            )
            auth_url = oauth.begin("99", "google_calendar")
            state = auth_url.split("state=")[1].split("&")[0]

            class FakeResponse:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}

            with patch("tomo_core.oauth.httpx.post", return_value=FakeResponse()) as post:
                token = oauth.complete("99", "google_calendar", f"http://127.0.0.1:56121/callback?code=abc&state={state}")

            self.assertEqual(token["access_token"], "access")
            saved = json.loads((Path(tmp) / "oauth" / "token_google_calendar_99.json").read_text())
            self.assertEqual(saved["refresh_token"], "refresh")
            self.assertEqual(post.call_args.kwargs["data"]["code"], "abc")


if __name__ == "__main__":
    unittest.main()
