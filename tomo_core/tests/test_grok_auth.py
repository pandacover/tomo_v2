import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tomo_core.grok_auth import GrokAuthStore
from tomo_core.providers import GrokAuthProvider, ProviderSetupRequired


class GrokAuthTests(unittest.TestCase):
    def test_reads_access_token_from_grok_auth_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_path = Path(tmp) / "auth.json"
            auth_path.write_text(json.dumps({"access_token": "grok-access"}), encoding="utf-8")

            self.assertEqual(GrokAuthStore(auth_path=auth_path).access_token(), "grok-access")

    def test_grok_auth_provider_uses_cached_grok_login_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_path = Path(tmp) / "auth.json"
            auth_path.write_text(json.dumps({"access_token": "grok-access"}), encoding="utf-8")

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return None

                def raise_for_status(self):
                    return None

                def iter_raw(self):
                    yield b'data: {"choices":[{"delta":{"content":"cached grok reply"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

            with patch("tomo_core.providers.httpx.stream", return_value=FakeResponse()) as stream:
                reply = GrokAuthProvider(auth_store=GrokAuthStore(auth_path=auth_path)).complete(
                    [{"role": "user", "content": "hi"}], actor_id="99"
                )

            self.assertEqual(reply, "cached grok reply")
            self.assertEqual(stream.call_args.kwargs["headers"]["Authorization"], "Bearer grok-access")

    def test_missing_grok_auth_returns_login_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ProviderSetupRequired) as raised:
                GrokAuthProvider(auth_store=GrokAuthStore(auth_path=Path(tmp) / "missing.json")).complete(
                    [{"role": "user", "content": "hi"}], actor_id="99"
                )

            self.assertIn("grok login", raised.exception.user_message)


if __name__ == "__main__":
    unittest.main()
