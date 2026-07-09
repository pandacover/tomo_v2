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

    def test_enqueue_persists_a_unique_pending_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)

            self.assertTrue(store.enqueue_update(101, "chat-a", '{"message":"hello"}', now=100))
            self.assertFalse(store.enqueue_update(101, "chat-a", "duplicate", now=101))

            update = store.claim_next_update(now=100)
            self.assertEqual(update.update_id, 101)
            self.assertEqual(update.chat_id, "chat-a")
            self.assertEqual(update.payload, '{"message":"hello"}')
            self.assertEqual(update.status, "processing")
            self.assertEqual(update.attempts, 1)
            self.assertEqual(update.available_at, 100)
            self.assertIsNone(update.error_code)
            self.assertEqual(update.created_at, 100)
            self.assertEqual(update.updated_at, 100)

    def test_claim_blocks_same_chat_but_allows_other_chats(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "chat-a", "first", now=100)
            store.enqueue_update(2, "chat-a", "second", now=100)
            store.enqueue_update(3, "chat-b", "other", now=100)

            self.assertEqual(store.claim_next_update(now=100).update_id, 1)
            self.assertEqual(store.claim_next_update(now=100).update_id, 3)
            self.assertIsNone(store.claim_next_update(now=100))

            store.complete_update(1, now=101)
            self.assertEqual(store.claim_next_update(now=101).update_id, 2)

    def test_complete_retry_and_reset_interrupted_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "chat-a", "retry", now=100)
            store.enqueue_update(2, "chat-b", "complete", now=100)

            retrying = store.claim_next_update(now=100)
            completing = store.claim_next_update(now=100)
            store.retry_update(retrying.update_id, "HTTP 503: upstream unavailable", now=101)
            store.complete_update(completing.update_id, now=101)

            self.assertIsNone(store.claim_next_update(now=101))
            retried = store.claim_next_update(now=102)
            self.assertEqual(retried.update_id, 1)
            self.assertEqual(retried.attempts, 2)
            self.assertEqual(retried.error_code, "http_503_upstream_unavailable")

            self.assertEqual(store.reset_interrupted_updates(now=103), 1)
            recovered = store.claim_next_update(now=103)
            self.assertEqual(recovered.update_id, 1)
            self.assertEqual(recovered.attempts, 3)

    def test_retry_backoff_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "chat-a", "retry", now=0)
            update = store.claim_next_update(now=0)

            for now in range(1, 12):
                store.retry_update(update.update_id, "temporary_failure", now=now)
                update = store.claim_next_update(now=now + 300)
                self.assertIsNotNone(update)
