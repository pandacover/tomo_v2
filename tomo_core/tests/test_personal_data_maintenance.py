import tempfile
import unittest
from pathlib import Path

from tomo_core.personal_data import MemorySourceRef, MemoryWriteControl
from tomo_core.sessions import ConversationSession, StoredMessage
from tomo_core.sqlite_personal_data import SqlitePersonalDataRepository


class PersonalDataMaintenanceTests(unittest.TestCase):
    def test_rebuild_integrity_prune_and_owner_deletion_are_owner_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            for owner in ("one", "two"):
                session = ConversationSession(f"telegram:actor:{owner}")
                session.append(StoredMessage("user", f"message {owner}", metadata={"burst_id": owner, "update_id": 1}))
                repo.save_session(owner, session)
            repo.rebuild_index("one")
            self.assertTrue(repo.integrity_check())
            stale = MemoryWriteControl("add", "autonomous", None, None, "fact", "self", "stale", "value", "stale", 1, 1, "always", None, None, (MemorySourceRef("inference", "x", "2020-01-01T00:00:00+00:00"),))
            repo.stage_memory_controls("one", "telegram:actor:one", "g", 0, 0, (stale,))
            self.assertEqual(repo.prune_provisional_artifacts("9999-01-01T00:00:00+00:00"), 1)
            repo.delete_owner("one")
            self.assertEqual(repo.load_session("one", "telegram:actor:one").messages, [])
            self.assertEqual(len(repo.load_session("two", "telegram:actor:two").messages), 1)
            self.assertTrue(repo.integrity_check())


if __name__ == "__main__":
    unittest.main()
