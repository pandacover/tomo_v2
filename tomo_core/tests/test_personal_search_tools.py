import tempfile
import unittest
from pathlib import Path

from tomo_core.personal_data import MemorySourceRef, MemoryWriteControl
from tomo_core.personal_search_tools import personal_search_registry
from tomo_core.sessions import ConversationSession, StoredMessage
from tomo_core.sqlite_personal_data import SqlitePersonalDataRepository


class PersonalSearchToolTests(unittest.TestCase):
    def test_tools_are_owner_bound_and_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "data.sqlite3")
            control = MemoryWriteControl(
                "upsert", "autonomous", None, None, "fact", "self", "favorite.color",
                "blue", "The favorite color is blue.", 1.0, 0.9, "always", None, None,
                (MemorySourceRef("current_message", "m1", "2026-01-01T00:00:00Z"),),
            )
            session = ConversationSession("telegram:actor:a")
            session.append(StoredMessage("assistant", "answer", metadata={"generation_id": "gen-a"}))
            repository.save_session("owner-a", session)
            repository.stage_memory_controls("owner-a", "telegram:actor:a", "gen-a", 0, 0, (control,))
            session = repository.load_session("owner-a", "telegram:actor:a")
            session.accept_generations(("gen-a",))
            repository.save_session("owner-a", session)
            registry = personal_search_registry(repository, "owner-a")

            schemas = registry.schemas()
            self.assertEqual([item["function"]["name"] for item in schemas], ["search_memories", "search_sessions"])
            self.assertEqual(registry.resolve("search_memories").spec.read_only, True)
            result = registry.resolve("search_memories").invoke({"query": "color"})
            self.assertEqual(result["memories"][0]["statement"], "The favorite color is blue.")
            self.assertEqual(result["memories"][0]["sources"], [{"kind": "current_message", "available": True}])
            self.assertEqual(personal_search_registry(repository, "owner-b").resolve("search_memories").invoke({"query": "color"})["memories"], [])


if __name__ == "__main__":
    unittest.main()
