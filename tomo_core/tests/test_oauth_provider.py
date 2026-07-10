import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tomo_core.oauth import OAuthManager, OAuthProviderConfig
from tomo_core.providers import OAuthBackedSuperGrokProvider, ProviderSetupRequired


class OAuthBackedProviderTests(unittest.TestCase):
    def test_connected_supergrok_token_is_used_for_actor_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            oauth = OAuthManager(
                data_dir=tmp,
                providers={
                    "supergrok": OAuthProviderConfig(
                        provider="supergrok",
                        client_id="supergrok-client",
                        auth_url="https://auth.example/authorize",
                        token_url="https://auth.example/token",
                        redirect_uri="http://127.0.0.1:56120/callback",
                        scopes=("openid",),
                    )
                },
            )
            token_path = Path(tmp) / "oauth" / "token_supergrok_99.json"
            token_path.write_text(json.dumps({"access_token": "actor-access"}), encoding="utf-8")

            class FakeResponse:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"choices": [{"message": {"content": "real supergrok reply"}}]}

            with patch("tomo_core.providers.httpx.post", return_value=FakeResponse()) as post:
                reply = OAuthBackedSuperGrokProvider(oauth=oauth).complete([{"role": "user", "content": "hi"}], actor_id="99")

            self.assertEqual(reply, "real supergrok reply")
            self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer actor-access")
            request_body = post.call_args.kwargs["json"]
            self.assertEqual(request_body["reasoning_effort"], "high")
            self.assertNotIn("reasoning", request_body)

    def test_missing_supergrok_token_returns_connect_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            oauth = OAuthManager(data_dir=tmp, providers={})

            with self.assertRaises(ProviderSetupRequired) as raised:
                OAuthBackedSuperGrokProvider(oauth=oauth).complete([{"role": "user", "content": "hi"}], actor_id="99")

            self.assertIn("/connect", raised.exception.user_message)
            self.assertIn("supergrok", raised.exception.user_message)


if __name__ == "__main__":
    unittest.main()
