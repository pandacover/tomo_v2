import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tomo_core.cli import build_oauth_manager, build_provider
from tomo_core.oauth import OAuthManager, OAuthProviderConfig
from tomo_core.providers import OAuthBackedSuperGrokProvider, SuperGrokOAuthProvider, SuperGrokTokenStore, XaiApiProvider
from tomo_core.telegram_bot import TelegramPollingBot


class NamingMigrationTests(unittest.TestCase):
    def test_default_providers_use_supergrok_oauth_not_xai_oauth(self):
        providers = OAuthManager.default_providers(supergrok_client_id="sg-client")

        self.assertIn("supergrok", providers)
        self.assertNotIn("xai", providers)
        self.assertEqual(providers["supergrok"].provider, "supergrok")

    def test_connect_menu_uses_supergrok_oauth_and_google_calendar(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeTelegramClient(
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

            buttons = client.sent_messages[0]["reply_markup"]["inline_keyboard"]
            self.assertEqual(buttons[0][0]["text"], "supergrok oauth")
            self.assertEqual(buttons[0][0]["callback_data"], "connect:supergrok")
            self.assertEqual(buttons[1][0]["callback_data"], "connect:google_calendar")

    def test_xai_api_key_builds_xai_api_provider(self):
        args = argparse.Namespace(
            static_response=None,
            xai_api_key="api-key",
            xai_access_token=None,
            model="grok-4.5",
        )

        provider = build_provider(args, OAuthManager(data_dir=tempfile.mkdtemp(), providers={}))

        self.assertIsInstance(provider, XaiApiProvider)
        self.assertEqual(provider.api_key, "api-key")

    def test_without_api_key_provider_uses_supergrok_oauth_tokens(self):
        args = argparse.Namespace(
            static_response=None,
            xai_api_key=None,
            xai_access_token=None,
            model="grok-4.5",
        )

        provider = build_provider(args, OAuthManager(data_dir=tempfile.mkdtemp(), providers={}))

        self.assertIsInstance(provider, OAuthBackedSuperGrokProvider)

    def test_supergrok_token_is_used_for_actor_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            oauth = OAuthManager(data_dir=tmp, providers={})
            token_path = Path(tmp) / "oauth" / "token_supergrok_99.json"
            token_path.write_text(json.dumps({"access_token": "supergrok-access"}), encoding="utf-8")

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return None

                def raise_for_status(self):
                    return None

                def iter_raw(self):
                    yield b'data: {"choices":[{"delta":{"content":"supergrok reply"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

            with patch("tomo_core.providers.httpx.stream", return_value=FakeResponse()) as stream:
                reply = OAuthBackedSuperGrokProvider(oauth=oauth).complete([{"role": "user", "content": "hi"}], actor_id="99")

            self.assertEqual(reply, "supergrok reply")
            self.assertEqual(stream.call_args.kwargs["headers"]["Authorization"], "Bearer supergrok-access")
            request_body = stream.call_args.kwargs["json"]
            self.assertEqual(request_body["reasoning_effort"], "high")
            self.assertNotIn("reasoning", request_body)

    def test_supergrok_fixed_token_provider_does_not_delegate_to_xai_api_provider(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def raise_for_status(self):
                return None

            def iter_raw(self):
                yield b'data: {"choices":[{"delta":{"content":"supergrok reply"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

        provider = SuperGrokOAuthProvider(token_store=SuperGrokTokenStore("supergrok-access"))
        with patch("tomo_core.providers.XaiApiProvider.complete", side_effect=AssertionError("must not use API-key provider semantics")), patch(
            "tomo_core.providers.httpx.stream", return_value=FakeResponse()
        ) as stream:
            reply = provider.complete([{"role": "user", "content": "hi"}])

        self.assertEqual(reply, "supergrok reply")
        self.assertEqual(stream.call_args.kwargs["headers"]["Authorization"], "Bearer supergrok-access")


class FakeTelegramClient:
    def __init__(self, updates):
        self.updates = updates
        self.sent_messages = []
        self.answered_callbacks = []

    def get_updates(self, offset=None, timeout=30):
        return self.updates

    def send_typing(self, actor_id):
        raise AssertionError("/connect should not start the agent turn")

    def send_message(self, actor_id, text, reply_to_message_id=None, reply_markup=None):
        self.sent_messages.append({"chat_id": actor_id, "text": text, "reply_markup": reply_markup})

    def answer_callback_query(self, callback_query_id, text=None):
        self.answered_callbacks.append({"id": callback_query_id, "text": text})


if __name__ == "__main__":
    unittest.main()
