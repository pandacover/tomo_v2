import tempfile
import unittest

import httpx

from tomo_core.control_api import create_app


class ControlApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_install_link_requires_api_key_and_returns_no_raw_secret_fields_besides_deeplink_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(data_dir=tmp, api_key="secret", bot_username="tmnvm_bot")
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                denied = await client.post("/v1/onboarding/telegram/install-link", json={"userId": "u1"})
                self.assertEqual(denied.status_code, 401)

                ok = await client.post(
                    "/v1/onboarding/telegram/install-link",
                    headers={"x-api-key": "secret"},
                    json={"userId": "u1"},
                )

            self.assertEqual(ok.status_code, 200)
            payload = ok.json()
            self.assertTrue(payload["dmUrl"].startswith("tg://resolve?domain=tmnvm_bot&start="))
            self.assertTrue(payload["browserUrl"].startswith("https://t.me/tmnvm_bot?start="))
            self.assertIn("expiresAt", payload)
            self.assertNotIn("botToken", payload)
