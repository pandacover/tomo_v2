import tempfile
import threading
import time
import unittest

from tomo_core.onboarding_store import TelegramOnboardingStore
from tomo_core.onboarding_store import TelegramGenerationWork
from tomo_core.telegram_router import CompactTelegramUpdate, RetryableTelegramUpdateError, TelegramUpdateRouter, compact_private_update


def private_update(update_id, chat_id="123", text="hello"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": int(chat_id)},
            "chat": {"id": int(chat_id), "type": "private"},
            "text": text,
            "date": update_id,
        },
    }


class FakeTelegramClient:
    def __init__(self, updates):
        self.updates = updates
        self.offsets = []

    def get_updates(self, offset=None, timeout=30):
        self.offsets.append(offset)
        return self.updates


def install_chat(store, chat_id="123"):
    link = store.create_install_link(f"user-{chat_id}", "tmnvm_bot")
    return store.consume_start_token(link.token, chat_id=chat_id, actor_id=chat_id)


class TelegramUpdateRouterTests(unittest.TestCase):
    def test_poll_enqueues_normal_messages_as_debounced_generation_work_before_advancing_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            install_chat(store)
            client = FakeTelegramClient([private_update(7), {"update_id": 8, "message": {"chat": {"type": "group"}}}])
            router = TelegramUpdateRouter(client=client, store=store, process_update=lambda _: None)

            self.assertEqual(router.poll_once(), 9)
            self.assertIsNone(store.claim_next_work(now=time.time()))
            claimed = store.claim_next_work(now=time.time() + 1)
            self.assertEqual(claimed.inputs[0].update_id, 7)
            self.assertEqual(claimed.inputs[0].message_id, "7")

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
            install_chat(store)
            update = private_update(7)
            router = TelegramUpdateRouter(client=FakeTelegramClient([update]), store=store, process_update=lambda _: None)

            self.assertEqual(router.poll_once(), 8)
            self.assertEqual(router.poll_once(7), 8)
            self.assertEqual(store.claim_next_work(now=time.time() + 1).inputs[0].update_id, 7)
            self.assertIsNone(store.claim_next_work(now=time.time() + 1))

    def test_compact_private_update_classifies_control_and_message_updates(self):
        normal = compact_private_update(private_update(7, text="hello"))
        start = compact_private_update(private_update(8, text="/start token"))

        self.assertEqual(normal.kind, "message")
        self.assertEqual(normal.message_id, "7")
        self.assertEqual(normal.telegram_sent_at, 7)
        self.assertEqual(start.kind, "control")

    def test_callback_query_with_message_text_is_always_control_work(self):
        callback = {
            "update_id": 9,
            "callback_query": {
                "message": {
                    "message_id": 3,
                    "date": 9,
                    "chat": {"id": 123, "type": "private"},
                    "text": "button label",
                }
            },
        }

        self.assertEqual(compact_private_update(callback).kind, "control")

    def test_private_photo_without_caption_is_generation_message_work(self):
        update = {
            "update_id": 10,
            "message": {
                "message_id": 4,
                "date": 10,
                "from": {"id": 123},
                "chat": {"id": 123, "type": "private"},
                "photo": [{"file_id": "small", "width": 90, "height": 90}, {"file_id": "large", "width": 900, "height": 900}],
            },
        }

        compact = compact_private_update(update)

        self.assertEqual(compact.kind, "message")

    def test_poll_attaches_installation_identity_to_generation_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            link = store.create_install_link("user-1", "tmnvm_bot")
            installation = store.consume_start_token(link.token, chat_id="123", actor_id="123")
            router = TelegramUpdateRouter(
                client=FakeTelegramClient([private_update(7)]),
                store=store,
                process_update=lambda _: None,
            )

            router.poll_once()
            work = store.claim_next_work(now=time.time() + 1)

            self.assertEqual(work.tomo_id, installation.tomo_id)

    def test_uninstalled_plain_text_uses_control_path_for_setup_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            processed = []
            router = TelegramUpdateRouter(
                client=FakeTelegramClient([private_update(7)]),
                store=store,
                process_update=processed.append,
            )

            router.poll_once()

            self.assertTrue(router.process_next())
            self.assertIsInstance(processed[0], dict)
            self.assertIsNone(store.claim_next_work(now=time.time() + 1))

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

    def test_process_next_passes_generation_work_for_debounced_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "123", '{"update_id":1}', now=0, update_kind="message", message_id="1")
            processed = []
            router = TelegramUpdateRouter(client=FakeTelegramClient([]), store=store, process_update=processed.append)

            self.assertTrue(router.process_next(now=1))
            self.assertIsInstance(processed[0], TelegramGenerationWork)

    def test_poll_schedules_superseded_generation_cancellation_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            installation = install_chat(store)
            store.enqueue_update(
                1,
                "123",
                '{"update_id":1}',
                now=0,
                update_kind="message",
                message_id="1",
                tomo_id=installation.tomo_id,
            )
            active = store.claim_next_work(now=1)
            cancelled = []
            router = TelegramUpdateRouter(
                client=FakeTelegramClient([private_update(2)]),
                store=store,
                process_update=lambda _: None,
                cancel_generation=cancelled.append,
            )

            self.assertEqual(router.poll_once(), 3)
            self.assertEqual(cancelled, [])

            router.drain_cancellations()

            self.assertEqual(cancelled[0].generation_id, active.generation_id)

    def test_startup_schedules_recovered_generation_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "123", '{"update_id":1}', now=0, update_kind="message", message_id="1")
            active = store.claim_next_work(now=1)
            cancelled = []
            router = TelegramUpdateRouter(
                client=FakeTelegramClient([]),
                store=store,
                process_update=lambda _: None,
                cancel_generation=cancelled.append,
            )

            router.drain_cancellations()

            self.assertEqual(cancelled[0].generation_id, active.generation_id)

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

    def test_generation_retry_uses_backoff_and_stops_at_max_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "123", '{"update_id":1}', now=0, update_kind="message", message_id="1")

            def process(_):
                raise RetryableTelegramUpdateError("provider_failed")

            router = TelegramUpdateRouter(client=FakeTelegramClient([]), store=store, process_update=process, max_attempts=2)
            self.assertTrue(router.process_next(now=1))
            self.assertFalse(router.process_next(now=1))
            self.assertTrue(router.process_next(now=2))
            self.assertFalse(router.process_next(now=100))

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
