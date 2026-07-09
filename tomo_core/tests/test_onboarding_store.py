import tempfile
import unittest

from tomo_core.onboarding_store import TelegramOnboardingStore


class TelegramOnboardingStoreTests(unittest.TestCase):
    def test_create_and_consume_single_use_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            link = store.create_install_link(user_id="user-1", bot_username="tmnvm_bot")

            self.assertTrue(link.dm_url.startswith("tg://resolve?domain=tmnvm_bot&start="))
            installation = store.consume_start_token(link.token, chat_id="123", actor_id="123")

            self.assertEqual(installation.user_id, "user-1")
            self.assertEqual(installation.chat_id, "123")
            self.assertTrue(installation.tomo_id.startswith("tomo-user-1-"))
            self.assertIsNone(store.consume_start_token(link.token, chat_id="123", actor_id="123"))

    def test_lookup_installation_by_chat_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            link = store.create_install_link(user_id="user-2", bot_username="tmnvm_bot")
            installation = store.consume_start_token(link.token, chat_id="999", actor_id="888")

            self.assertEqual(store.installation_for_chat("999"), installation)
