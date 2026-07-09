import base64
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import call, patch

from tomo_core.hosted_auth import HostedGrokAuth


class HostedGrokAuthTests(unittest.TestCase):
    def test_bootstrap_decodes_normalizes_and_writes_private_auth_file_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_path = Path(tmp) / "auth.json"
            encoded = base64.b64encode(
                json.dumps({"session": {"accessToken": "access", "refreshToken": "refresh", "expiresAt": 2_000_000_000}}).encode()
            ).decode()

            with patch("tomo_core.hosted_auth.os.chmod", wraps=os.chmod) as chmod:
                with patch.dict(os.environ, {"TOMO_GROK_AUTH_B64": encoded, "GROK_AUTH_JSON": str(auth_path)}, clear=True):
                    HostedGrokAuth.from_environment().bootstrap()

            saved = json.loads(auth_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["session"]["access_token"], "access")
            self.assertEqual(saved["session"]["refresh_token"], "refresh")
            self.assertEqual(saved["session"]["expires_at"], 2_000_000_000)
            self.assertIn(call(auth_path, 0o600), chmod.call_args_list)
            self.assertTrue((Path(tmp) / ".auth.json.bootstrap").exists())

    def test_bootstrap_keeps_refreshed_token_until_the_broker_value_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_path = Path(tmp) / "auth.json"
            original = {"access_token": "bootstrap", "refresh_token": "refresh", "expires_at": 2_000_000_000}
            encoded = base64.b64encode(json.dumps(original).encode()).decode()
            auth_path.write_text(json.dumps({**original, "access_token": "refreshed"}), encoding="utf-8")
            (Path(tmp) / ".auth.json.bootstrap").write_text(
                hashlib.sha256(encoded.encode("ascii")).hexdigest(), encoding="ascii"
            )

            with patch.dict(os.environ, {"TOMO_GROK_AUTH_B64": encoded, "GROK_AUTH_JSON": str(auth_path)}, clear=True):
                HostedGrokAuth.from_environment().bootstrap()

            self.assertEqual(json.loads(auth_path.read_text(encoding="utf-8"))["access_token"], "refreshed")

            replacement = base64.b64encode(json.dumps({**original, "access_token": "replacement"}).encode()).decode()
            with patch.dict(os.environ, {"TOMO_GROK_AUTH_B64": replacement, "GROK_AUTH_JSON": str(auth_path)}, clear=True):
                HostedGrokAuth.from_environment().bootstrap()

            self.assertEqual(json.loads(auth_path.read_text(encoding="utf-8"))["access_token"], "replacement")

    def test_bootstrap_refreshes_near_expiry_and_keeps_omitted_refresh_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_path = Path(tmp) / "auth.json"
            encoded = base64.b64encode(
                json.dumps({"access_token": "old", "refresh_token": "refresh", "expires_at": int(time.time()) + 10}).encode()
            ).decode()

            class Response:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"access_token": "new", "expires_in": 3600}

            with patch.dict(os.environ, {"TOMO_GROK_AUTH_B64": encoded, "GROK_AUTH_JSON": str(auth_path)}, clear=True):
                with patch("tomo_core.hosted_auth.httpx.post", return_value=Response()) as post:
                    HostedGrokAuth.from_environment().bootstrap()

            saved = json.loads(auth_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["access_token"], "new")
            self.assertEqual(saved["refresh_token"], "refresh")
            self.assertGreater(saved["expires_at"], int(time.time()))
            self.assertEqual(post.call_args.kwargs["data"]["client_id"], "b1a00492-073a-47ea-816f-4c329264a828")


if __name__ == "__main__":
    unittest.main()
