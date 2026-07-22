import tempfile
import unittest
from pathlib import Path

from tomo_core.personal_data import MemorySourceRef, MemoryWriteControl
from tomo_core.personal_search_tools import peer_personal_search_registry, personal_search_registry
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

    def test_peer_projection_has_one_fixed_tool_and_never_returns_raw_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqlitePersonalDataRepository(Path(tmp) / "data.sqlite3")
            control = MemoryWriteControl(
                "upsert", "autonomous", None, None, "fact", "self", "contact.email",
                "owner@example.com", "My email is owner@example.com and my private note is hidden.",
                1.0, 0.9, "always", None, None,
                (MemorySourceRef("current_message", "m1", "2026-01-01T00:00:00Z"),),
            )
            other_person = MemoryWriteControl(
                "add", "autonomous", None, None, "fact", "alice", "contact.email",
                "alice@example.com", "Alice's email is alice@example.com.",
                1.0, 0.9, "always", None, None,
                (MemorySourceRef("current_message", "m1", "2026-01-01T00:00:00Z"),),
            )
            session = ConversationSession("telegram:actor:a")
            session.append(StoredMessage("assistant", "answer", metadata={"generation_id": "gen-a"}))
            repository.save_session("owner-a", session)
            repository.stage_memory_controls(
                "owner-a", "telegram:actor:a", "gen-a", 0, 0,
                (control, other_person),
            )
            session = repository.load_session("owner-a", "telegram:actor:a")
            session.accept_generations(("gen-a",))
            repository.save_session("owner-a", session)

            ordinary = peer_personal_search_registry(repository, "owner-a", "none")
            scoped = peer_personal_search_registry(repository, "owner-a", "contact_email")
            result = scoped.resolve("personal_search").invoke({})

            self.assertEqual(ordinary.schemas(), ())
            self.assertEqual([item["function"]["name"] for item in scoped.schemas()], ["personal_search"])
            self.assertEqual(result, {"ok": True, "scope": "contact_email", "candidates": [{"email": "owner@example.com"}]})
            self.assertNotIn("private note", repr(result))
            self.assertNotIn("alice@example.com", repr(result))


if __name__ == "__main__":
    unittest.main()
