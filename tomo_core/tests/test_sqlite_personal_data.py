import tempfile
import unittest
from pathlib import Path

from tomo_core.personal_data import MemoryContextQuery, MemorySearchQuery, MemorySourceRef, MemoryWriteControl, SessionSearchQuery
from tomo_core.sessions import ConversationSession, StoredMessage
from tomo_core.sqlite_personal_data import SqlitePersonalDataRepository


class SqlitePersonalDataTests(unittest.TestCase):
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
