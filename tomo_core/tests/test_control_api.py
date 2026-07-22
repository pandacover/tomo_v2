import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import httpx

from tomo_core.attachment_capability import AttachmentCapability, hash_file_id, issue_attachment_capability
from tomo_core.control_api import create_app
from tomo_core.telegram_bot import TelegramBotApiError
from tomo_core.vision import DownloadedAttachment


class ControlApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_attachment_resolution_lazily_constructs_telegram_source_without_leaking_secrets(self):
        class Files:
            def fetch(self, file_id):
                self.file_id = file_id
                return DownloadedAttachment(b"jpeg-bytes", "image/jpeg")

        files = Files()
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"TOMO_TELEGRAM_GLOBAL_BOT_TOKEN": "secret-token"}), patch("tomo_core.control_api.TelegramBotApiClient", return_value=files) as client_class:
            token = issue_attachment_capability(b"k" * 32, AttachmentCapability("owner", "generation", (hash_file_id("photo-id"),), 100, 400))
            app = create_app(data_dir=tmp, attachment_key=b"k" * 32, now=lambda: datetime.fromtimestamp(200, timezone.utc))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/v1/attachments/resolve", headers={"Authorization": f"Bearer {token}", "X-Tomo-Owner-Id": "owner", "X-Tomo-Generation-Id": "generation"}, json={"fileId": "photo-id"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"jpeg-bytes")
        client_class.assert_called_once_with("secret-token")
        self.assertEqual(files.file_id, "photo-id")
        self.assertNotIn("secret-token", response.text)
        self.assertNotIn("photo-id", response.text)

    async def test_attachment_resolution_requires_exact_capability_and_returns_binary(self):
        class Files:
            def fetch(self, file_id):
                self.called = file_id
                return DownloadedAttachment(b"jpeg-bytes", "image/jpeg")
        with tempfile.TemporaryDirectory() as tmp:
            files = Files()
            token = issue_attachment_capability(b"k" * 32, AttachmentCapability("owner", "generation", (hash_file_id("photo-id"),), 100, 400))
            app = create_app(data_dir=tmp, telegram_files=files, attachment_key=b"k" * 32, now=lambda: datetime.fromtimestamp(200, timezone.utc))
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/v1/attachments/resolve", headers={"Authorization": f"Bearer {token}", "X-Tomo-Owner-Id": "owner", "X-Tomo-Generation-Id": "generation"}, json={"fileId": "photo-id"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"jpeg-bytes")
        self.assertEqual(response.headers["content-type"], "image/jpeg")
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

    async def test_peer_router_is_mounted_and_applies_the_control_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(data_dir=tmp, api_key="secret")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                denied = await client.get("/v1/peers/relationships", params={"userId": "u1"})
                accepted = await client.get(
                    "/v1/peers/relationships",
                    headers={"x-api-key": "secret"},
                    params={"userId": "u1"},
                )

            self.assertEqual(denied.status_code, 401)
            self.assertEqual(accepted.status_code, 200)
            self.assertEqual(accepted.json(), {"relationships": []})

    async def test_attachment_resolution_uses_safe_statuses_and_never_calls_unauthorized_source(self):
        class Files:
            called = False
            def fetch(self, file_id):
                self.called = True
                raise TelegramBotApiError("telegram_file_too_large")
        with tempfile.TemporaryDirectory() as tmp:
            files = Files()
            app = create_app(data_dir=tmp, telegram_files=files, attachment_key=b"k" * 32, now=lambda: datetime.fromtimestamp(200, timezone.utc))
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                denied = await client.post("/v1/attachments/resolve", headers={"Authorization": "Bearer forged", "X-Tomo-Owner-Id": "owner", "X-Tomo-Generation-Id": "generation"}, json={"fileId": "secret-file"})
                missing = await client.post("/v1/attachments/resolve", json={"fileId": "secret-file"})
            self.assertEqual(denied.status_code, 401)
            self.assertEqual(missing.status_code, 401)
            self.assertFalse(files.called)

    async def test_attachment_resolution_classifies_source_results_and_disables_caching(self):
        class Files:
            def __init__(self, result): self.result = result
            def fetch(self, file_id):
                if isinstance(self.result, BaseException): raise self.result
                return self.result
        token = issue_attachment_capability(b"k" * 32, AttachmentCapability("owner", "generation", (hash_file_id("photo-id"),), 100, 400))
        headers = {"Authorization": f"Bearer {token}", "X-Tomo-Owner-Id": "owner", "X-Tomo-Generation-Id": "generation"}
        for result, status in ((TelegramBotApiError("telegram_file_too_large"), 413), (TelegramBotApiError("secret upstream body"), 503), (object(), 503), (DownloadedAttachment(b"x" * (10 * 1024 * 1024 + 1), "image/jpeg"), 413), (DownloadedAttachment(b"jpeg", "image/jpeg"), 200)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                app = create_app(data_dir=tmp, telegram_files=Files(result), attachment_key=b"k" * 32, now=lambda: datetime.fromtimestamp(200, timezone.utc))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    response = await client.post("/v1/attachments/resolve", headers=headers, json={"fileId": "photo-id"})
                self.assertEqual(response.status_code, status)
                self.assertNotIn("secret upstream body", response.text)
                if status == 200:
                    self.assertEqual(response.headers["cache-control"], "no-store")
