import tempfile
import unittest

from tomo_core.onboarding_store import TelegramOnboardingStore


class InterruptibleTelegramTurnTests(unittest.TestCase):
    def test_visible_superseded_bubble_becomes_replacement_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(
                1,
                "123",
                '{"update_id":1,"message":{"message_id":1,"date":1,"from":{"id":999},"chat":{"id":123,"type":"private"},"text":"first"}}',
                now=0,
                update_kind="message",
                message_id="1",
                telegram_sent_at=1,
                tomo_id="tomo-1",
            )
            first = store.claim_next_work(now=1)
            self.assertTrue(store.reserve_delivery(first.generation_id, first.revision, 0, 0, 0, "visible.", "1", legacy_move="acknowledge", now=1.1))
            self.assertTrue(store.mark_delivery_sent(first.generation_id, 0, "telegram-10", now=1.2))

            supersede = store.enqueue_update(
                2,
                "123",
                '{"update_id":2,"message":{"message_id":2,"date":2,"from":{"id":999},"chat":{"id":123,"type":"private"},"text":"second"}}',
                now=2,
                update_kind="message",
                message_id="2",
                telegram_sent_at=2,
                tomo_id="tomo-1",
            )
            replacement = store.claim_next_work(now=3)

            self.assertEqual(supersede.superseded_generation_id, first.generation_id)
            self.assertEqual([item.update_id for item in replacement.inputs], [1, 2])
            self.assertEqual(replacement.visible_assistant_utterances, ("visible.",))
            self.assertNotIn(first.generation_id, replacement.accepted_generation_ids)


if __name__ == "__main__":
    unittest.main()
