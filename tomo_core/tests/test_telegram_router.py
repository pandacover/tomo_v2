import tempfile
import threading
import time
import unittest

from tomo_core.onboarding_store import TelegramOnboardingStore
from tomo_core.telegram_router import RetryableTelegramUpdateError, TelegramUpdateRouter


def private_update(update_id, chat_id="123", text="hello"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": int(chat_id)},
            "chat": {"id": int(chat_id), "type": "private"},
            "text": text,
        },
    }


class FakeTelegramClient:
    def __init__(self, updates):
        self.updates = updates
        self.offsets = []

    def get_updates(self, offset=None, timeout=30):
        self.offsets.append(offset)
        return self.updates


class TelegramUpdateRouterTests(unittest.TestCase):
    def test_poll_enqueues_compact_private_updates_before_advancing_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            client = FakeTelegramClient([private_update(7), {"update_id": 8, "message": {"chat": {"type": "group"}}}])
            router = TelegramUpdateRouter(client=client, store=store, process_update=lambda _: None)

            self.assertEqual(router.poll_once(), 9)
            claimed = store.claim_next_update()
            self.assertEqual(claimed.update_id, 7)
            self.assertEqual(claimed.chat_id, "123")
            self.assertEqual(claimed.payload, '{"message":{"chat":{"id":123,"type":"private"},"from":{"id":123},"message_id":7,"text":"hello"},"update_id":7}')

    def test_poll_enqueues_private_callback_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            callback = {
                "update_id": 7,
                "callback_query": {"message": {"chat": {"id": 123, "type": "private"}}},
            }
            router = TelegramUpdateRouter(client=FakeTelegramClient([callback]), store=store, process_update=lambda _: None)

            router.poll_once()

            self.assertEqual(store.claim_next_update().payload, '{"callback_query":{"message":{"chat":{"id":123,"type":"private"}}},"update_id":7}')

    def test_duplicate_delivery_does_not_create_a_second_inbox_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            update = private_update(7)
            router = TelegramUpdateRouter(client=FakeTelegramClient([update]), store=store, process_update=lambda _: None)

            self.assertEqual(router.poll_once(), 8)
            self.assertEqual(router.poll_once(7), 8)
            self.assertEqual(store.claim_next_update().update_id, 7)
            self.assertIsNone(store.claim_next_update())

    def test_startup_recovers_interrupted_work_and_worker_completes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "123", '{"update_id":1}', now=100)
            store.claim_next_update(now=100)
            processed = []

            router = TelegramUpdateRouter(client=FakeTelegramClient([]), store=store, process_update=processed.append)

            self.assertTrue(router.process_next())
            self.assertEqual(processed, [{"update_id": 1}])
            self.assertIsNone(store.claim_next_update())

    def test_worker_preserves_chat_order_across_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "123", '{"update_id":1}', now=0)
            store.enqueue_update(2, "123", '{"update_id":2}', now=0)
            calls = []

            def process(update):
                calls.append(update["update_id"])
                if update["update_id"] == 1 and len(calls) == 1:
                    raise RetryableTelegramUpdateError("upstream_unavailable")

            router = TelegramUpdateRouter(client=FakeTelegramClient([]), store=store, process_update=process)
            self.assertTrue(router.process_next(now=0))
            self.assertFalse(router.process_next(now=0))
            self.assertTrue(router.process_next(now=1))
            self.assertTrue(router.process_next(now=1))
            self.assertEqual(calls, [1, 1, 2])

    def test_worker_bounds_retry_and_persists_only_typed_safe_error_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "123", '{"update_id":1}', now=0)

            def process(_):
                raise RetryableTelegramUpdateError("upstream_unavailable")

            router = TelegramUpdateRouter(client=FakeTelegramClient([]), store=store, process_update=process, max_attempts=2)
            self.assertTrue(router.process_next(now=0))
            retry = store.claim_next_update(now=1)
            self.assertEqual(retry.error_code, "upstream_unavailable")
            store.retry_update(retry.update_id, retry.error_code, now=1)
            self.assertTrue(router.process_next(now=3))
            self.assertIsNone(store.claim_next_update(now=999))

    def test_stop_joins_idle_workers_within_the_shutdown_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            router = TelegramUpdateRouter(
                client=FakeTelegramClient([]),
                store=TelegramOnboardingStore(tmp),
                process_update=lambda _: None,
                idle_sleep_seconds=10,
                shutdown_timeout=0.2,
            )
            runner = threading.Thread(target=router.run_forever)
            runner.start()
            deadline = time.monotonic() + 1
            while not any(thread.name.startswith("telegram-update-worker") for thread in threading.enumerate()):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)

            router.stop()
            runner.join(timeout=0.5)

            self.assertFalse(runner.is_alive())
            self.assertFalse(any(thread.name.startswith("telegram-update-worker") for thread in threading.enumerate()))


if __name__ == "__main__":
    unittest.main()
