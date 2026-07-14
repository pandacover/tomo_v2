import sqlite3
import tempfile
import unittest
from pathlib import Path

from tomo_core.onboarding_store import TelegramOnboardingStore


class TelegramOnboardingStoreTests(unittest.TestCase):
    def test_fresh_delivery_schema_supports_coordinate_rows_without_legacy_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            db = sqlite3.connect(Path(tmp) / "onboarding.sqlite")
            try:
                columns = {row[1]: row for row in db.execute("pragma table_info(telegram_delivery_events)")}
                self.assertEqual(
                    list(columns),
                    [
                        "generation_id",
                        "sequence",
                        "segment_index",
                        "frame_index",
                        "move",
                        "text",
                        "reply_to_message_id",
                        "status",
                        "telegram_message_id",
                        "created_at",
                        "updated_at",
                    ],
                )
                self.assertEqual(columns["segment_index"][4], "0")
                self.assertEqual(columns["frame_index"][4], "0")
                self.assertEqual(columns["move"][3], 0)
                db.execute(
                    """
                    insert into telegram_delivery_events(
                      generation_id, sequence, segment_index, frame_index, move, text,
                      reply_to_message_id, status, telegram_message_id, created_at, updated_at
                    ) values ('g1', 0, 2, 3, null, 'frame', null, 'reserved', null, 1, 1)
                    """
                )
                db.commit()
            finally:
                db.close()

    def test_delivery_schema_migrates_legacy_rows_without_losing_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "onboarding.sqlite"
            db = sqlite3.connect(db_path)
            db.execute(
                """
                create table telegram_delivery_events(
                  generation_id text not null,
                  sequence integer not null,
                  move text not null,
                  text text not null,
                  reply_to_message_id text,
                  status text not null check(status in ('reserved','sent','unknown','suppressed')),
                  telegram_message_id text,
                  created_at real not null,
                  updated_at real not null,
                  primary key(generation_id, sequence)
                )
                """
            )
            db.execute(
                """
                insert into telegram_delivery_events values
                ('old-generation', 4, 'answer', 'preserved text', 'reply-1', 'sent', 'telegram-1', 1.25, 2.5)
                """
            )
            db.commit()
            db.close()

            TelegramOnboardingStore(tmp)
            TelegramOnboardingStore(tmp)

            db = sqlite3.connect(db_path)
            try:
                row = db.execute("select * from telegram_delivery_events where generation_id = 'old-generation'").fetchone()
                self.assertEqual(row, ("old-generation", 4, 0, 4, "answer", "preserved text", "reply-1", "sent", "telegram-1", 1.25, 2.5))
                db.execute(
                    """
                    insert into telegram_delivery_events(
                      generation_id, sequence, segment_index, frame_index, move, text,
                      reply_to_message_id, status, telegram_message_id, created_at, updated_at
                    ) values ('v3-generation', 0, 0, 0, null, 'new frame', null, 'reserved', null, 3, 3)
                    """
                )
                db.commit()
            finally:
                db.close()

    def test_delivery_schema_migration_discards_stale_internal_replacement_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "onboarding.sqlite"
            db = sqlite3.connect(db_path)
            db.execute(
                """
                create table telegram_delivery_events(
                  generation_id text not null,
                  sequence integer not null,
                  move text not null,
                  text text not null,
                  reply_to_message_id text,
                  status text not null check(status in ('reserved','sent','unknown','suppressed')),
                  telegram_message_id text,
                  created_at real not null,
                  updated_at real not null,
                  primary key(generation_id, sequence)
                )
                """
            )
            db.execute("create table telegram_delivery_events_v3(stale text)")
            db.commit()
            db.close()

            TelegramOnboardingStore(tmp)

            db = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    db.execute(
                        "select name from sqlite_master where type = 'table' and name = 'telegram_delivery_events_v3'"
                    ).fetchone(),
                    None,
                )
            finally:
                db.close()

    def test_failed_delivery_schema_migration_preserves_legacy_table_and_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "onboarding.sqlite"
            db = sqlite3.connect(db_path)
            db.execute(
                """
                create table telegram_delivery_events(
                  generation_id text not null,
                  sequence integer not null,
                  move text not null,
                  text text not null,
                  reply_to_message_id text,
                  status text not null check(status in ('reserved','sent','unknown','suppressed')),
                  telegram_message_id text,
                  created_at real not null,
                  updated_at real not null,
                  primary key(generation_id, sequence)
                )
                """
            )
            db.execute(
                """
                insert into telegram_delivery_events values
                ('old-generation', -1, 'answer', 'preserved text', 'reply-1', 'sent', 'telegram-1', 1.25, 2.5)
                """
            )
            before_schema = db.execute(
                "select sql from sqlite_master where type = 'table' and name = 'telegram_delivery_events'"
            ).fetchone()[0]
            before_row = db.execute("select * from telegram_delivery_events").fetchone()
            db.commit()
            db.close()

            with self.assertRaises(sqlite3.IntegrityError):
                TelegramOnboardingStore(tmp)

            db = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    db.execute(
                        "select sql from sqlite_master where type = 'table' and name = 'telegram_delivery_events'"
                    ).fetchone()[0],
                    before_schema,
                )
                self.assertEqual(db.execute("select * from telegram_delivery_events").fetchone(), before_row)
                self.assertIsNone(
                    db.execute(
                        "select name from sqlite_master where type = 'table' and name = 'telegram_delivery_events_v3'"
                    ).fetchone()
                )
            finally:
                db.close()

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

    def test_normal_messages_debounce_into_one_generation_and_delivery_is_fenced(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)

            first = store.enqueue_update(101, "chat-a", '{"text":"first"}', now=10.0, update_kind="message", message_id="m1", telegram_sent_at=9.0, tomo_id="tomo-1")
            second = store.enqueue_update(102, "chat-a", '{"text":"second"}', now=10.4, update_kind="message", message_id="m2", telegram_sent_at=9.0, tomo_id="tomo-1")

            self.assertTrue(first)
            self.assertEqual(first.revision, 1)
            self.assertEqual(second.revision, 2)
            self.assertIsNone(store.claim_next_work(now=11.0))
            work = store.claim_next_work(now=11.2)
            self.assertEqual(work.chat_id, "chat-a")
            self.assertEqual(work.revision, 2)
            self.assertAlmostEqual(work.eligible_at, 11.1)
            self.assertAlmostEqual(work.claimed_at, 11.2)
            self.assertEqual([item.update_id for item in work.inputs], [101, 102])
            self.assertEqual([item.ordinal for item in work.inputs], [1, 2])
            self.assertTrue(store.reserve_delivery(work.generation_id, work.revision, 0, 0, 0, "hi", "m2", legacy_move="answer", now=11.3))
            db = sqlite3.connect(store.db_path)
            try:
                self.assertEqual(
                    db.execute(
                        "select segment_index, frame_index, move, text from telegram_delivery_events"
                    ).fetchone(),
                    (0, 0, "answer", "hi"),
                )
            finally:
                db.close()
            self.assertTrue(store.mark_delivery_sent(work.generation_id, 0, "tg-1", now=11.4))
            self.assertTrue(store.complete_generation(work.generation_id, work.revision, now=11.5))
            self.assertFalse(store.reserve_delivery(work.generation_id, work.revision, 1, 0, 1, "late", None, legacy_move="answer", now=11.6))

    def test_reserve_delivery_rejects_blank_legacy_move_but_allows_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "chat-a", "first", now=0, update_kind="message", message_id="m1", tomo_id="tomo-1")
            work = store.claim_next_work(now=1)

            with self.assertRaises(ValueError):
                store.reserve_delivery(work.generation_id, work.revision, 0, 0, 0, "frame", "m1", legacy_move="  ", now=1.1)

            self.assertTrue(store.reserve_delivery(work.generation_id, work.revision, 0, 0, 0, "frame", "m1", now=1.1))

    def test_later_message_supersedes_active_generation_and_recovery_reports_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(101, "chat-a", "first", now=1.0, update_kind="message", message_id="m1", tomo_id="tomo-1")
            work = store.claim_next_work(now=2.0)

            result = store.enqueue_update(102, "chat-a", "second", now=2.1, update_kind="message", message_id="m2", tomo_id="tomo-1")

            self.assertEqual(result.superseded_generation_id, work.generation_id)
            self.assertEqual(result.superseded_session_id, work.session_id)
            self.assertFalse(store.is_generation_active(work.generation_id, work.revision))
            self.assertFalse(store.reserve_delivery(work.generation_id, work.revision, 0, 0, 0, "stale", "m1", legacy_move="answer", now=2.2))
            replacement = store.claim_next_work(now=3.0)
            self.assertEqual(replacement.revision, 2)
            self.assertEqual([item.update_id for item in replacement.inputs], [101, 102])
            self.assertEqual([item.session_id for item in store.recover_interrupted_generations()], [replacement.session_id])

    def test_normal_message_cannot_escape_debounce_through_legacy_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(
                1,
                "chat-a",
                '{"update_id":1}',
                now=10.0,
                update_kind="message",
                message_id="m1",
                tomo_id="tomo-1",
            )

            self.assertIsNone(store.claim_next_work(now=10.1))
            self.assertIsNone(store.claim_next_update(now=10.1))

    def test_failed_generation_requeues_burst_with_a_new_attempt_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "chat-a", "first", now=0, update_kind="message", message_id="m1", tomo_id="tomo-1")
            first = store.claim_next_work(now=1)

            self.assertTrue(store.fail_generation(first.generation_id, "provider_failed", now=2))
            replacement = store.claim_next_work(now=3)

            self.assertIsNotNone(replacement)
            self.assertEqual(replacement.revision, first.revision + 1)
            self.assertNotEqual(replacement.generation_id, first.generation_id)
            self.assertEqual([item.update_id for item in replacement.inputs], [1])

    def test_stale_failure_rebases_to_the_sandbox_revision_floor_without_lowering_newer_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "chat-a", "first", now=0, update_kind="message", message_id="m1", tomo_id="tomo-1")
            first = store.claim_next_work(now=1)
            self.assertTrue(store.fail_generation(first.generation_id, "stale_session_revision", now=2, minimum_next_revision=13))
            self.assertEqual(store.claim_next_work(now=3).revision, 13)

            store.enqueue_update(2, "chat-a", "second", now=4, update_kind="message", message_id="m2", tomo_id="tomo-1")
            second = store.claim_next_work(now=5)
            self.assertTrue(store.fail_generation(second.generation_id, "stale_session_revision", now=6, minimum_next_revision=13))
            self.assertEqual(store.claim_next_work(now=7).revision, 15)

    def test_completed_generation_is_accepted_by_the_next_burst(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "chat-a", "first", now=0, update_kind="message", message_id="m1", tomo_id="tomo-1")
            first = store.claim_next_work(now=1)
            self.assertTrue(store.complete_generation(first.generation_id, first.revision, now=1.1))
            store.enqueue_update(2, "chat-a", "second", now=2, update_kind="message", message_id="m2", tomo_id="tomo-1")

            second = store.claim_next_work(now=3)

            self.assertIn(first.generation_id, second.accepted_generation_ids)
            self.assertEqual(second.revision, first.revision + 1)

    def test_terminal_failure_preserves_monotonic_revision_for_the_next_burst(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "chat-a", "first", now=0, update_kind="message", message_id="m1", tomo_id="tomo-1")
            first = store.claim_next_work(now=1)
            self.assertTrue(store.fail_generation(first.generation_id, "invalid_result", now=2, max_attempts=1))

            store.enqueue_update(2, "chat-a", "second", now=3, update_kind="message", message_id="m2", tomo_id="tomo-1")
            second = store.claim_next_work(now=4)

            self.assertEqual(second.revision, first.revision + 1)

    def test_recovery_atomically_requeues_active_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "chat-a", "first", now=0, update_kind="message", message_id="m1", tomo_id="tomo-1")
            first = store.claim_next_work(now=1)

            interrupted = store.recover_interrupted_generations(now=2)
            replacement = store.claim_next_work(now=2)

            self.assertEqual(interrupted[0].generation_id, first.generation_id)
            self.assertEqual(interrupted[0].tomo_id, "tomo-1")
            self.assertIsNotNone(replacement)
            self.assertNotEqual(replacement.generation_id, first.generation_id)
            self.assertEqual(replacement.revision, first.revision + 1)

    def test_recovery_converts_abandoned_reservation_to_unknown_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "chat-a", "first", now=0, update_kind="message", message_id="m1", tomo_id="tomo-1")
            first = store.claim_next_work(now=1)
            self.assertTrue(store.reserve_delivery(first.generation_id, first.revision, 0, 0, 0, "possibly visible", "m1", legacy_move="answer", now=1.1))

            store.recover_interrupted_generations(now=2)
            replacement = store.claim_next_work(now=2)

            self.assertEqual(replacement.visible_assistant_utterances, ("possibly visible",))
            self.assertEqual(replacement.accepted_generation_ids, ())

    def test_superseded_visible_partial_is_context_not_accepted_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "chat-a", "first", now=0, update_kind="message", message_id="m1", tomo_id="tomo-1")
            first = store.claim_next_work(now=1)
            self.assertTrue(store.reserve_delivery(first.generation_id, first.revision, 0, 0, 0, "visible only", "m1", legacy_move="answer", now=1.1))
            self.assertTrue(store.mark_delivery_sent(first.generation_id, 0, "tm1", now=1.2))
            store.enqueue_update(2, "chat-a", "second", now=1.3, update_kind="message", message_id="m2", tomo_id="tomo-1")

            replacement = store.claim_next_work(now=3)

            self.assertEqual(replacement.visible_assistant_utterances, ("visible only",))
            self.assertNotIn(first.generation_id, replacement.accepted_generation_ids)

    def test_reserved_stale_delivery_can_be_marked_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            store.enqueue_update(1, "chat-a", "first", now=0, update_kind="message", message_id="m1", tomo_id="tomo-1")
            first = store.claim_next_work(now=1)
            self.assertTrue(store.reserve_delivery(first.generation_id, first.revision, 0, 0, 0, "stale", "m1", legacy_move="answer", now=1.1))

            self.assertTrue(store.fail_generation(first.generation_id, "superseded", now=1.2))
            self.assertTrue(store.mark_delivery_suppressed(first.generation_id, 0, now=1.3))
            replacement = store.claim_next_work(now=2)

            self.assertEqual(replacement.visible_assistant_utterances, ())
