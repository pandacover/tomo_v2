import tempfile
import unittest
from pathlib import Path

from tomo_core.memory_governance import MemoryGovernanceService
from tomo_core.personal_data import (MemoryGovernanceControl, MemorySearchQuery,
                                     MemorySourceRef, MemoryWriteControl,
                                     PendingMemoryActionControl)
from tomo_core.sessions import ConversationSession, StoredMessage
from tomo_core.sqlite_personal_data import SqlitePersonalDataRepository


def control(value, generation):
    return MemoryWriteControl("upsert", "autonomous", None, None, "fact", "self", "drink", {"value": value}, f"Owner likes {value}", 1, 1, "always", None, None, (MemorySourceRef("current_message", generation, "2026-01-01T00:00:00+00:00"),))


class MemoryGovernanceTests(unittest.TestCase):
    def test_disable_is_immediate_and_blocks_duplicate_autonomous_relearning(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:one")
            session.append(StoredMessage("assistant", "answer", metadata={"generation_id": "g1"}))
            repo.save_session("owner", session)
            repo.stage_memory_controls("owner", session.session_key, "g1", 0, 0, (control("tea", "m1"),))
            repo.accept_generations("owner", session.session_key, ("g1",))
            memory_id = repo.search_memories(MemorySearchQuery("owner", "tea"))[0].memory.id
            service = MemoryGovernanceService(repo)
            result = service.apply("owner", session.session_key, "Please forget tea", MemoryGovernanceControl("disable_by_user", (memory_id,), "forget tea"))
            self.assertEqual(result.outcome, "applied")
            self.assertEqual(repo.search_memories(MemorySearchQuery("owner", "tea")), ())
            repo.stage_memory_controls("owner", session.session_key, "g2", 0, 1, (control("tea", "m2"),))
            self.assertEqual(repo.search_memories(MemorySearchQuery("owner", None)), ())

    def test_delete_requires_exact_current_burst_confirmation_and_allows_later_relearning(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:one")
            session.append(StoredMessage("assistant", "answer", metadata={"generation_id": "g1"}))
            repo.save_session("owner", session)
            repo.stage_memory_controls("owner", session.session_key, "g1", 0, 0, (control("tea", "m1"),))
            repo.accept_generations("owner", session.session_key, ("g1",))
            memory_id = repo.search_memories(MemorySearchQuery("owner", "tea"))[0].memory.id
            service = MemoryGovernanceService(repo)
            requested = service.apply("owner", session.session_key, "Delete tea permanently", MemoryGovernanceControl("request_delete", (memory_id,), "delete tea permanently"))
            self.assertEqual(requested.outcome, "pending_confirmation")
            pending = repo.pending_memory_actions("owner", session.session_key)
            self.assertEqual(pending[0].id, requested.pending_action_id)
            self.assertEqual(pending[0].target_statements, ("Owner likes tea",))
            self.assertEqual(service.apply("owner", session.session_key, "yes", PendingMemoryActionControl("confirm_delete", requested.pending_action_id, "delete it")).outcome, "rejected")
            self.assertEqual(service.apply("owner", session.session_key, "I confirm delete tea", PendingMemoryActionControl("confirm_delete", requested.pending_action_id, "confirm delete tea")).outcome, "applied")
            self.assertEqual(repo.search_memories(MemorySearchQuery("owner", "tea")), ())
            repo.stage_memory_controls("owner", session.session_key, "g2", 0, repo.memory_settings("owner").governance_revision, (control("tea", "m2"),))
            session.append(StoredMessage("assistant", "answer", metadata={"generation_id": "g2"}))
            repo.save_session("owner", session)
            repo.accept_generations("owner", session.session_key, ("g2",))
            self.assertEqual(len(repo.search_memories(MemorySearchQuery("owner", "tea"))), 1)

    def test_stale_generation_cannot_apply_direct_memory_governance(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:one")
            session.append(StoredMessage("assistant", "answer", metadata={"generation_id": "g1"}))
            repo.save_session("owner", session, generation_id="g1", revision=1)
            repo.stage_memory_controls("owner", session.session_key, "g1", 0, 0, (control("tea", "m1"),), revision=1)
            repo.accept_generations("owner", session.session_key, ("g1",))
            memory_id = repo.search_memories(MemorySearchQuery("owner", "tea"))[0].memory.id
            repo.save_session("owner", repo.load_session("owner", session.session_key), generation_id="g2", revision=2)

            result = MemoryGovernanceService(repo).apply(
                "owner",
                session.session_key,
                "Please forget tea",
                MemoryGovernanceControl("disable_by_user", (memory_id,), "forget tea"),
                expected_generation_id="g1",
                expected_revision=1,
            )

            self.assertEqual(result.outcome, "rejected")
            self.assertEqual(len(repo.search_memories(MemorySearchQuery("owner", "tea"))), 1)
            cancelled = MemoryGovernanceService(repo).apply(
                "owner",
                session.session_key,
                "Please forget tea",
                MemoryGovernanceControl("disable_by_user", (memory_id,), "forget tea"),
                expected_generation_id="g2",
                expected_revision=2,
                is_active=lambda: False,
            )
            self.assertEqual(cancelled.outcome, "rejected")
            self.assertEqual(len(repo.search_memories(MemorySearchQuery("owner", "tea"))), 1)


if __name__ == "__main__":
    unittest.main()
