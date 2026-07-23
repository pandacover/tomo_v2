import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tomo_core.personal_data import MemoryContextQuery, MemorySearchQuery, MemorySourceRef, MemoryWriteControl, SessionSearchQuery
from tomo_core.sessions import ConversationSession, StoredMessage
from tomo_core.sqlite_personal_data import SqlitePersonalDataRepository


class SqlitePersonalDataTests(unittest.TestCase):
    def test_current_session_revision_is_read_only_and_owner_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            self.assertIsNone(repository.current_session_revision("owner-a", "telegram:actor:user-1"))
            session = ConversationSession("telegram:actor:user-1")
            self.assertTrue(repository.save_session("owner-a", session, generation_id="generation-1", revision=1))
            self.assertEqual(repository.current_session_revision("owner-a", session.session_key), 1)
            self.assertIsNone(repository.current_session_revision("owner-b", session.session_key))
    @staticmethod
    def _control(action="upsert", value="tea", *, statement=None, memory_id=None, sources=None, surface_scope="always"):
        return MemoryWriteControl(
            action, "autonomous", None, memory_id, "fact", "self", "drink", {"value": value},
            statement or f"Owner likes {value}", 0.9, 0.8, surface_scope, None, None,
            sources or (MemorySourceRef("current_message", "message-1", "2026-01-01T00:00:00Z"),),
        )

    def test_archived_contextual_memory_requires_explicit_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:one")
            for generation in ("add", "archive"):
                session.append(StoredMessage("assistant", generation, metadata={"generation_id": generation}))
            repository.save_session("owner-a", session)

            repository.stage_memory_controls(
                "owner-a",
                session.session_key,
                "add",
                0,
                0,
                (self._control("add", "tea", surface_scope="contextual"),),
            )
            repository.accept_generations("owner-a", session.session_key, ("add",))
            active = repository.memory_context(MemoryContextQuery("owner-a", "tea"))[0]

            repository.stage_memory_controls(
                "owner-a",
                session.session_key,
                "archive",
                0,
                0,
                (self._control("archive", "tea", memory_id=active.id, surface_scope="contextual"),),
            )
            repository.accept_generations("owner-a", session.session_key, ("archive",))

            self.assertEqual(repository.memory_context(MemoryContextQuery("owner-a", "tea")), ())
            self.assertEqual(repository.search_memories(MemorySearchQuery("owner-a", "tea"))[0].memory.status, "archived")

    def test_write_actions_have_distinct_accepted_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:one")
            for generation in ("add", "archive", "disable", "remove"):
                session.append(StoredMessage("assistant", generation, metadata={"generation_id": generation}))
            repository.save_session("owner-a", session)

            repository.stage_memory_controls("owner-a", session.session_key, "add", 0, 0, (self._control("add", "tea"),))
            repository.accept_generations("owner-a", session.session_key, ("add",))
            active = repository.search_memories(MemorySearchQuery("owner-a", "tea"))[0].memory

            repository.stage_memory_controls("owner-a", session.session_key, "archive", 0, 0, (self._control("archive", "tea", memory_id=active.id),))
            repository.accept_generations("owner-a", session.session_key, ("archive",))
            self.assertEqual(repository.search_memories(MemorySearchQuery("owner-a", "tea"))[0].memory.status, "archived")

            repository.stage_memory_controls("owner-a", session.session_key, "disable", 0, 0, (self._control("disable_by_agent", "tea", memory_id=active.id),))
            repository.accept_generations("owner-a", session.session_key, ("disable",))
            self.assertEqual(repository.search_memories(MemorySearchQuery("owner-a", "tea")), ())

            repository.stage_memory_controls("owner-a", session.session_key, "remove", 0, 0, (self._control("remove", "tea", memory_id=active.id),))
            repository.accept_generations("owner-a", session.session_key, ("remove",))
            self.assertEqual(repository.search_memories(MemorySearchQuery("owner-a", None)), ())

    def test_upsert_correction_supersedes_old_value_and_keeps_new_statement(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:one")
            for generation in ("first", "correction"):
                session.append(StoredMessage("assistant", generation, metadata={"generation_id": generation}))
            repository.save_session("owner-a", session)
            repository.stage_memory_controls("owner-a", session.session_key, "first", 0, 0, (self._control(value="tea"),))
            repository.accept_generations("owner-a", session.session_key, ("first",))
            repository.stage_memory_controls("owner-a", session.session_key, "correction", 0, 0, (self._control(value="coffee", statement="Owner now likes coffee"),))
            repository.accept_generations("owner-a", session.session_key, ("correction",))
            hits = repository.search_memories(MemorySearchQuery("owner-a", None))
            self.assertEqual([(hit.memory.statement, hit.memory.status) for hit in hits], [("Owner now likes coffee", "active")])

    def test_upsert_mutates_changed_statement_without_silently_ignoring_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:one")
            for generation in ("first", "rewrite"):
                session.append(StoredMessage("assistant", generation, metadata={"generation_id": generation}))
            repository.save_session("owner-a", session)
            repository.stage_memory_controls("owner-a", session.session_key, "first", 0, 0, (self._control(statement="Owner likes tea"),))
            repository.accept_generations("owner-a", session.session_key, ("first",))
            repository.stage_memory_controls("owner-a", session.session_key, "rewrite", 0, 0, (self._control(statement="Owner strongly prefers tea"),))
            repository.accept_generations("owner-a", session.session_key, ("rewrite",))
            self.assertEqual(repository.search_memories(MemorySearchQuery("owner-a", None))[0].memory.statement, "Owner strongly prefers tea")

    def test_activation_rejects_stale_revision_and_foreign_session_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            first = ConversationSession("telegram:actor:one")
            second = ConversationSession("telegram:actor:two")
            first.append(StoredMessage("assistant", "answer", metadata={"generation_id": "shared"}))
            second.append(StoredMessage("assistant", "answer", metadata={"generation_id": "shared"}))
            repository.save_session("owner-a", first)
            repository.save_session("owner-a", second)
            repository.stage_memory_controls("owner-a", first.session_key, "shared", 0, 0, (self._control(),))
            repository.update_memory_setting("owner-a", "capture_enabled", True)
            repository.accept_generations("owner-a", first.session_key, ("shared",))
            self.assertEqual(repository.search_memories(MemorySearchQuery("owner-a", None)), ())
            repository.stage_memory_controls("owner-a", first.session_key, "shared", 0, 1, (self._control(value="coffee"),))
            repository.accept_generations("owner-a", second.session_key, ("shared",))
            self.assertEqual(repository.search_memories(MemorySearchQuery("owner-a", None)), ())
            repository.stage_memory_controls("owner-a", first.session_key, "fabricated", 0, 1, (self._control(value="water"),))
            repository.accept_generations("owner-a", first.session_key, ("fabricated",))
            self.assertEqual(repository.search_memories(MemorySearchQuery("owner-a", None)), ())

    def test_user_stated_evidence_is_stronger_than_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:one")
            session.append(StoredMessage("assistant", "answer", metadata={"generation_id": "g1"}))
            repository.save_session("owner-a", session)
            control = self._control(sources=(
                MemorySourceRef("inference", "turn-1", "2026-01-01T00:00:00Z"),
                MemorySourceRef("current_message", "message-1", "2026-01-01T00:00:00Z"),
            ))
            repository.stage_memory_controls("owner-a", session.session_key, "g1", 0, 0, (control,))
            repository.accept_generations("owner-a", session.session_key, ("g1",))
            self.assertEqual(repository.search_memories(MemorySearchQuery("owner-a", "tea"))[0].memory.epistemic_kind, "inferred")

    def test_unmatched_remove_and_duplicate_fingerprint_do_not_create_visible_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:one")
            for generation in ("first", "duplicate", "remove"):
                session.append(StoredMessage("assistant", generation, metadata={"generation_id": generation}))
            repository.save_session("owner-a", session)
            repository.stage_memory_controls("owner-a", session.session_key, "first", 0, 0, (self._control("add", "tea"),))
            repository.accept_generations("owner-a", session.session_key, ("first",))
            repository.stage_memory_controls("owner-a", session.session_key, "duplicate", 0, 0, (self._control("add", "tea"),))
            repository.accept_generations("owner-a", session.session_key, ("duplicate",))
            repository.stage_memory_controls("owner-a", session.session_key, "remove", 0, 0, (self._control("remove", "coffee"),))
            repository.accept_generations("owner-a", session.session_key, ("remove",))
            self.assertEqual([hit.memory.statement for hit in repository.search_memories(MemorySearchQuery("owner-a", None))], ["Owner likes tea"])

    def test_sessions_are_owner_scoped_and_only_accepted_assistant_rows_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:one")
            session.append(StoredMessage("user", "unicode cafe planning", metadata={"burst_id": "b1", "update_id": 1}))
            session.append(StoredMessage("assistant", "provisional private answer", metadata={"generation_id": "g1", "generation_status": "provisional"}))
            repository.save_session("owner-a", session)
            self.assertEqual([hit.matched_text for hit in repository.search_sessions(SessionSearchQuery("owner-a", "cafe"))], ["unicode cafe planning"])
            self.assertEqual(repository.search_sessions(SessionSearchQuery("owner-b", "cafe")), ())
            repository.accept_generations("owner-a", session.session_key, ("g1",))
            self.assertEqual([hit.matched_text for hit in repository.search_sessions(SessionSearchQuery("owner-a", "private"))], ["provisional private answer"])

    def test_session_search_context_excludes_neighboring_peer_exchange_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:one")
            session.append(StoredMessage("user", "earlier context", metadata={"burst_id": "b1", "update_id": 1}))
            session.append(StoredMessage("assistant", "peer moon claim", metadata={"generation_id": "peer", "generation_status": "provisional", "peer_exchange": True}))
            session.append(StoredMessage("user", "needle question", metadata={"burst_id": "b2", "update_id": 2}))
            repository.save_session("owner-a", session)
            repository.accept_generations("owner-a", session.session_key, ("peer",))

            with closing(sqlite3.connect(repository.path)) as connection:
                self.assertEqual(connection.execute("SELECT record_id FROM messages_fts WHERE messages_fts MATCH 'moon'").fetchall(), [])
            repository.rebuild_index("owner-a")
            with closing(sqlite3.connect(repository.path)) as connection:
                self.assertEqual(connection.execute("SELECT record_id FROM messages_fts WHERE messages_fts MATCH 'moon'").fetchall(), [])

            hit = repository.search_sessions(SessionSearchQuery("owner-a", "needle"))[0]

            self.assertEqual(hit.matched_text, "needle question")
            self.assertNotIn("peer moon claim", [message.content for message in hit.context])

    def test_session_search_excludes_peer_rows_before_candidate_ranking_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:one")
            generations = []
            for index in range(8):
                generation = f"peer-{index}"
                generations.append(generation)
                session.append(StoredMessage(
                    "assistant",
                    ("needle " * 20) + str(index),
                    metadata={"generation_id": generation, "peer_exchange": True},
                ))
            session.append(StoredMessage("user", "legitimate needle", metadata={"burst_id": "valid", "update_id": 1}))
            repository.save_session("owner-a", session)
            repository.accept_generations("owner-a", session.session_key, tuple(generations))

            hits = repository.search_sessions(SessionSearchQuery("owner-a", "needle", limit=1))

            self.assertEqual([hit.matched_text for hit in hits], ["legitimate needle"])

    def test_purge_owner_window_removes_affected_chat_and_memory_generations_and_restores_predecessor(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:one")
            session.append(StoredMessage("user", "legitimate before", "2025-01-01T10:00:00+00:00", {"burst_id": "before", "update_id": 1}))
            session.append(StoredMessage("assistant", "before answer", "2025-01-01T10:01:00+00:00", {"generation_id": "before"}))
            session.append(StoredMessage("user", "polluted question", "2025-01-02T10:00:00+00:00", {"burst_id": "affected", "update_id": 2}))
            session.append(StoredMessage("assistant", "polluted answer", "2025-01-02T10:01:00+00:00", {"generation_id": "affected"}))
            session.append(StoredMessage("user", "legitimate after", "2025-01-04T10:00:00+00:00", {"burst_id": "after", "update_id": 3}))
            repository.save_session("owner-a", session, generation_id="affected", revision=3)
            other = ConversationSession("telegram:actor:other")
            other.append(StoredMessage("user", "other owner polluted text", "2025-01-02T10:00:00+00:00", {"burst_id": "other", "update_id": 1}))
            repository.save_session("owner-b", other)
            collision = ConversationSession("telegram:actor:collision")
            collision.append(StoredMessage("assistant", "same generation, different session", "2025-01-04T11:00:00+00:00", {"generation_id": "affected"}))
            repository.save_session("owner-a", collision, generation_id="affected", revision=7)
            collision_control = MemoryWriteControl(
                "upsert", "autonomous", None, None, "fact", "self", "unrelated", {"value": "safe"},
                "Unrelated retained memory", 0.9, 0.8, "always", None, None,
                (MemorySourceRef("assistant_conclusion", "affected", "2025-01-04T11:00:00Z"),),
            )
            repository.stage_memory_controls("owner-a", collision.session_key, "affected", 0, 0, (collision_control,), revision=7)
            repository.accept_generations("owner-a", collision.session_key, ("affected",))
            repository.stage_memory_controls("owner-a", session.session_key, "before", 0, 0, (self._control(value="tea"),))
            repository.accept_generations("owner-a", session.session_key, ("before",))
            original = repository.search_memories(MemorySearchQuery("owner-a", "tea"))[0].memory
            repository.stage_memory_controls("owner-a", session.session_key, "affected", 0, 0, (self._control(value="coffee", memory_id=original.id),))
            repository.accept_generations("owner-a", session.session_key, ("affected",))

            result = repository.purge_owner_window(
                "owner-a", "2025-01-02T00:00:00+00:00", "2025-01-03T00:00:00+00:00"
            )

            self.assertEqual(result, {"messages": 2, "memories": 1, "pending_actions": 0})
            self.assertEqual(repository.current_session_revision("owner-a", session.session_key), 4)
            self.assertFalse(repository.save_session("owner-a", session, generation_id="affected", revision=3))
            self.assertFalse(repository.save_session("owner-a", session))
            self.assertEqual(
                {hit.memory.statement for hit in repository.search_memories(MemorySearchQuery("owner-a", None))},
                {"Owner likes tea", "Unrelated retained memory"},
            )
            self.assertEqual(repository.search_sessions(SessionSearchQuery("owner-a", "polluted")), ())
            self.assertEqual(repository.search_sessions(SessionSearchQuery("owner-b", "polluted"))[0].matched_text, "other owner polluted text")
            persisted = repository.load_session("owner-a", session.session_key)
            self.assertEqual(persisted.accepted_generation_ids, ("before",))
            collision_persisted = repository.load_session("owner-a", collision.session_key)
            self.assertEqual(collision_persisted.accepted_generation_ids, ("affected",))
            self.assertEqual(repository.current_session_revision("owner-a", collision.session_key), 7)
            self.assertTrue(repository.integrity_check())

    def test_purge_owner_window_rejects_an_empty_or_reversed_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            for start, end in (
                ("2026-07-23T00:00:00Z", "2026-07-23T00:00:00Z"),
                ("2026-07-24T00:00:00Z", "2026-07-23T00:00:00Z"),
            ):
                with self.subTest(start=start, end=end), self.assertRaisesRegex(ValueError, "purge window"):
                    repository.purge_owner_window("owner", start, end)

    def test_purge_owner_window_preserves_full_microsecond_half_open_precision(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:one")
            for index, (content, timestamp) in enumerate((
                ("before boundary", "2026-07-23T00:00:00.000000Z"),
                ("at start", "2026-07-23T00:00:00.000001Z"),
                ("inside", "2026-07-23T00:00:00.000002Z"),
                ("at end", "2026-07-23T00:00:00.000003Z"),
            )):
                session.append(StoredMessage("user", content, timestamp, {"burst_id": f"b{index}", "update_id": index}))
            repository.save_session("owner", session)

            result = repository.purge_owner_window(
                "owner", "2026-07-23T00:00:00.000001Z", "2026-07-23T00:00:00.000003Z"
            )

            self.assertEqual(result["messages"], 2)
            remaining = repository.load_session("owner", session.session_key)
            self.assertEqual([message.content for message in remaining.messages], ["before boundary", "at end"])

    def test_purge_owner_window_removes_the_complete_generation_crossing_the_end_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:crossing")
            session.append(StoredMessage("user", "trigger inside", "2026-07-21T23:59:59+00:00", {"burst_id": "crossing"}))
            session.append(StoredMessage("assistant", "response outside", "2026-07-22T00:00:01+00:00", {"burst_id": "crossing", "generation_id": "crossing", "generation_status": "accepted"}))
            session.append(StoredMessage("automation", "generation peer outside", "2026-07-22T00:00:02+00:00", {"source": "automation", "burst_id": "chained", "generation_id": "crossing"}))
            session.append(StoredMessage("user", "burst peer outside", "2026-07-22T00:00:03+00:00", {"burst_id": "chained"}))
            session.accept_generations(("crossing",))
            repository.save_session("owner-a", session, generation_id="crossing", revision=1)

            result = repository.purge_owner_window("owner-a", "2026-07-21T00:00:00+00:00", "2026-07-22T00:00:00+00:00")

            self.assertEqual(result["messages"], 4)
            retained = repository.load_session("owner-a", session.session_key)
            self.assertEqual(retained.messages, [])
            self.assertEqual(retained.accepted_generation_ids, ())

    def test_list_owners_includes_every_personal_data_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session_a = ConversationSession("telegram:actor:a")
            session_a.append(StoredMessage("user", "a", metadata={"burst_id": "a", "update_id": 1}))
            repository.save_session("owner-a", session_a)
            repository.update_memory_setting("owner-b", "capture_enabled", True)
            self.assertEqual(repository.list_owners(), ("owner-a", "owner-b"))

    def test_memory_search_respects_owner_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            self.assertEqual(repository.search_memories(MemorySearchQuery("owner-a", "anything")), ())
            repository.update_memory_setting("owner-a", "retrieval_enabled", False)
            self.assertEqual(repository.search_memories(MemorySearchQuery("owner-a", None)), ())

    def test_accepted_generation_activates_staged_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:one")
            session.append(StoredMessage("assistant", "answer", metadata={"generation_id": "g1"}))
            repository.save_session("owner-a", session)
            control = MemoryWriteControl(
                "upsert", "autonomous", None, None, "fact", "self", "favorite", {"value": "tea"},
                "The owner likes tea", 0.9, 0.8, "always", None, None,
                (MemorySourceRef("assistant_conclusion", "g1", "2026-01-01T00:00:00Z"),),
            )
            repository.stage_memory_controls("owner-a", session.session_key, "g1", 0, 0, (control,))
            self.assertEqual(repository.search_memories(MemorySearchQuery("owner-a", "tea")), ())
            repository.accept_generations("owner-a", session.session_key, ("g1",))
            self.assertEqual(
                [hit.memory.statement for hit in repository.search_memories(MemorySearchQuery("owner-a", "tea"))],
                ["The owner likes tea"],
            )

    def test_stale_revision_cannot_overwrite_provisional_output_or_stage_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session_key = "telegram:actor:one"
            newer = ConversationSession(session_key)
            newer.append(StoredMessage("user", "new input", metadata={"burst_id": "new", "update_id": 2}))
            newer.append(StoredMessage("assistant", "new output", metadata={"generation_id": "new", "generation_status": "provisional"}))
            self.assertTrue(repository.save_session("owner-a", newer, generation_id="new", revision=2))

            stale = ConversationSession(session_key)
            stale.append(StoredMessage("user", "old input", metadata={"burst_id": "old", "update_id": 1}))
            stale.append(StoredMessage("assistant", "stale output", metadata={"generation_id": "old", "generation_status": "provisional"}))
            self.assertFalse(repository.save_session("owner-a", stale, generation_id="old", revision=1))
            self.assertFalse(repository.stage_memory_controls("owner-a", session_key, "old", 0, 0, (self._control(),), revision=1))

            saved = repository.load_session("owner-a", session_key)
            self.assertEqual([message.content for message in saved.messages], ["new input", "new output"])
            self.assertEqual(repository.search_memories(MemorySearchQuery("owner-a", None)), ())


if __name__ == "__main__":
    unittest.main()
