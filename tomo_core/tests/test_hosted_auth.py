import base64
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import call, patch

from tomo_core.hosted_auth import HostedAuthError, HostedSuperGrokTokenBroker


class HostedSuperGrokTokenBrokerTests(unittest.TestCase):
    def test_accepts_current_grok_cli_key_and_iso_expiry_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            encoded = base64.b64encode(
                json.dumps(
                    {
                        "https://auth.x.ai::client": {
                            "key": "access",
                            "refresh_token": "refresh",
                            "expires_at": "2099-01-01T00:00:00Z",
                            "oidc_issuer": "https://auth.x.ai",
                        }
                    }
                ).encode()
            ).decode()

            with patch("tomo_core.hosted_auth.httpx.post") as post:
                token = HostedSuperGrokTokenBroker(tmp, encoded).access_token()

            self.assertEqual(token, "access")
            post.assert_not_called()

    def test_access_token_strictly_decodes_normalizes_and_writes_private_auth_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            encoded = base64.b64encode(
                json.dumps({"session": {"accessToken": "access", "refreshToken": "refresh", "expiresAt": 2_000_000_000}}).encode()
            ).decode()

            with patch("tomo_core.hosted_auth.os.chmod", wraps=os.chmod) as chmod:
                token = HostedSuperGrokTokenBroker(tmp, encoded).access_token()

            auth_path = Path(tmp) / "hosted-auth" / "supergrok.json"
            saved = json.loads(auth_path.read_text(encoding="utf-8"))
            self.assertEqual(token, "access")
            self.assertEqual(saved["session"]["access_token"], "access")
            self.assertEqual(saved["session"]["refresh_token"], "refresh")
            self.assertEqual(saved["session"]["expires_at"], 2_000_000_000)
            self.assertIn(call(auth_path, 0o600), chmod.call_args_list)
            self.assertEqual(
                (auth_path.parent / ".supergrok.json.bootstrap").read_text(encoding="ascii"),
                hashlib.sha256(encoded.encode("ascii")).hexdigest(),
            )

    def test_bootstrap_keeps_refreshed_token_until_the_broker_value_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_path = Path(tmp) / "hosted-auth" / "supergrok.json"
            auth_path.parent.mkdir()
            original = {"access_token": "bootstrap", "refresh_token": "refresh", "expires_at": 2_000_000_000}
            encoded = base64.b64encode(json.dumps(original).encode()).decode()
            auth_path.write_text(json.dumps({**original, "access_token": "refreshed"}), encoding="utf-8")
            (auth_path.parent / ".supergrok.json.bootstrap").write_text(
                hashlib.sha256(encoded.encode("ascii")).hexdigest(), encoding="ascii"
            )

            HostedSuperGrokTokenBroker(tmp, encoded).access_token()

            self.assertEqual(json.loads(auth_path.read_text(encoding="utf-8"))["access_token"], "refreshed")

            replacement = base64.b64encode(json.dumps({**original, "access_token": "replacement"}).encode()).decode()
            HostedSuperGrokTokenBroker(tmp, replacement).access_token()

            self.assertEqual(json.loads(auth_path.read_text(encoding="utf-8"))["access_token"], "replacement")

    def test_bootstrap_refreshes_near_expiry_and_keeps_omitted_refresh_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_path = Path(tmp) / "hosted-auth" / "supergrok.json"
            encoded = base64.b64encode(
                json.dumps({"access_token": "old", "refresh_token": "refresh", "expires_at": int(time.time()) + 10}).encode()
            ).decode()

            class Response:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"access_token": "new", "expires_in": 3600}

            with patch("tomo_core.hosted_auth.httpx.post", return_value=Response()) as post:
                HostedSuperGrokTokenBroker(tmp, encoded).access_token()

            saved = json.loads(auth_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["access_token"], "new")
            self.assertEqual(saved["refresh_token"], "refresh")
            self.assertGreater(saved["expires_at"], int(time.time()))
            self.assertEqual(post.call_args.kwargs["data"]["client_id"], "b1a00492-073a-47ea-816f-4c329264a828")

    def test_access_token_force_refreshes_even_when_unexpired(self):
        with tempfile.TemporaryDirectory() as tmp:
            encoded = base64.b64encode(
                json.dumps({"access_token": "old", "refresh_token": "refresh", "expires_at": int(time.time()) + 3600}).encode()
            ).decode()

            class Response:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"access_token": "new", "expires_in": 3600}

            with patch("tomo_core.hosted_auth.httpx.post", return_value=Response()) as post:
                token = HostedSuperGrokTokenBroker(tmp, encoded).access_token(force_refresh=True)

            self.assertEqual(token, "new")
            post.assert_called_once()

    def test_access_token_raises_a_safe_refresh_unavailable_error_without_refresh_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = "secret-access"
            encoded = base64.b64encode(json.dumps({"access_token": secret, "expires_at": 0}).encode()).decode()

            with self.assertRaises(HostedAuthError) as raised:
                HostedSuperGrokTokenBroker(tmp, encoded).access_token()

            self.assertEqual(raised.exception.code, "refresh_unavailable")
            self.assertNotIn(secret, str(raised.exception))

    def test_access_token_rejects_invalid_base64_with_a_safe_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HostedAuthError) as raised:
                HostedSuperGrokTokenBroker(tmp, "not base64").access_token()

            self.assertEqual(raised.exception.code, "invalid_bootstrap")


if __name__ == "__main__":
    unittest.main()
