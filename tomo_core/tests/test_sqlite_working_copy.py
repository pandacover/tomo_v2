import tempfile
import unittest
import struct
from pathlib import Path
from unittest.mock import Mock, patch

from tomo_core.models import RuntimeConfig
from tomo_core.personal_data import (MemoryGovernanceControl, MemorySearchQuery,
                                     MemorySourceRef, MemoryWriteControl,
                                     PendingMemoryActionControl)
from tomo_core.sandbox_inbound import build_runtime
from tomo_core.sessions import ConversationSession, StoredMessage
from tomo_core.sqlite_personal_data import SqlitePersonalDataRepository


class SqliteWorkingCopyTests(unittest.TestCase):
    _HEADER = struct.Struct("!8sBQQ32s")

    def _slots(self, durable):
        return sorted(durable.parent.glob(f"{durable.name}.checkpoint.*"))

    def _generation(self, durable):
        return max(self._HEADER.unpack(slot.read_bytes()[:self._HEADER.size])[2] for slot in self._slots(durable))

    @staticmethod
    def _remove_work_dir(work):
        for path in work.iterdir():
            path.unlink()
        work.rmdir()

    @staticmethod
    def _memory_control():
        return MemoryWriteControl("add", "autonomous", None, None, "fact", "self", "secret", {"value": "secret"}, "private secret", 1, 1, "always", None, None, (MemorySourceRef("current_message", "g1", "2026-01-01T00:00:00+00:00"),))

    def test_restores_session_after_local_work_directory_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            durable = root / "durable" / "tomo.sqlite3"
            work = root / "work"
            repository = SqlitePersonalDataRepository(durable, local_work_dir=work)
            session = ConversationSession("telegram:actor:one")
            session.append(StoredMessage("user", "remember this", metadata={"burst_id": "b1", "update_id": 1}))
            repository.save_session("owner", session)

            for path in work.iterdir():
                path.unlink()
            work.rmdir()

            restored = SqlitePersonalDataRepository(durable, local_work_dir=work)
            self.assertEqual([message.content for message in restored.load_session("owner", session.session_key).messages], ["remember this"])

    def test_truncated_newest_checkpoint_falls_back_to_prior_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            durable, work = root / "durable" / "tomo.sqlite3", root / "work"
            repository = SqlitePersonalDataRepository(durable, local_work_dir=work)
            first = ConversationSession("telegram:actor:one")
            first.append(StoredMessage("user", "first", metadata={"burst_id": "b1", "update_id": 1}))
            repository.save_session("owner", first)
            second = ConversationSession("telegram:actor:one")
            second.append(StoredMessage("user", "second", metadata={"burst_id": "b2", "update_id": 2}))
            repository.save_session("owner", second)
            newest = max(self._slots(durable), key=lambda slot: self._HEADER.unpack(slot.read_bytes()[:self._HEADER.size])[2])
            newest.write_bytes(b"truncated")
            for path in work.iterdir():
                path.unlink()
            work.rmdir()

            restored = SqlitePersonalDataRepository(durable, local_work_dir=work)
            self.assertEqual([message.content for message in restored.load_session("owner", first.session_key).messages], ["first"])
            self.assertTrue(restored.integrity_check())

    def test_failed_mutation_and_reads_do_not_advance_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            durable, work = root / "durable" / "tomo.sqlite3", root / "work"
            repository = SqlitePersonalDataRepository(durable, local_work_dir=work)
            self.assertEqual(self._slots(durable), [])
            repository.load_session("owner", "telegram:actor:one")
            self.assertEqual(self._slots(durable), [])
            current = ConversationSession("telegram:actor:one")
            current.append(StoredMessage("user", "current", metadata={"burst_id": "b2", "update_id": 2}))
            self.assertTrue(repository.save_session("owner", current, generation_id="new", revision=2))
            generation = self._generation(durable)
            stale = ConversationSession("telegram:actor:one")
            stale.append(StoredMessage("user", "stale", metadata={"burst_id": "b1", "update_id": 1}))
            self.assertFalse(repository.save_session("owner", stale, generation_id="old", revision=1))
            repository.load_session("owner", current.session_key)
            self.assertEqual(self._generation(durable), generation)

    def test_durable_slots_are_framed_and_have_no_sqlite_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            durable, work = root / "durable" / "tomo.sqlite3", root / "work"
            repository = SqlitePersonalDataRepository(durable, local_work_dir=work)
            session = ConversationSession("telegram:actor:one")
            session.append(StoredMessage("user", "stored", metadata={"burst_id": "b1", "update_id": 1}))
            repository.save_session("owner", session)
            slots = self._slots(durable)
            self.assertEqual(len(slots), 1)
            self.assertEqual(slots[0].read_bytes()[:8], b"TOMOCP01")
            self.assertFalse(durable.exists())
            self.assertEqual(list(durable.parent.glob("*.checkpoint.*-wal")), [])
            self.assertEqual(list(durable.parent.glob("*.checkpoint.*-shm")), [])

    def test_direct_path_mode_keeps_using_the_configured_sqlite_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            durable = Path(tmp) / "tomo.sqlite3"
            repository = SqlitePersonalDataRepository(durable)
            session = ConversationSession("telegram:actor:one")
            session.append(StoredMessage("user", "local", metadata={"burst_id": "b1", "update_id": 1}))
            repository.save_session("owner", session)
            self.assertTrue(durable.is_file())
            self.assertEqual(self._slots(durable), [])

    def test_working_copy_ignores_existing_durable_sqlite_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            durable = root / "durable" / "tomo.sqlite3"
            legacy = SqlitePersonalDataRepository(durable)
            session = ConversationSession("telegram:actor:one")
            session.append(StoredMessage("user", "legacy", metadata={"burst_id": "b1", "update_id": 1}))
            legacy.save_session("owner", session)

            repository = SqlitePersonalDataRepository(durable, local_work_dir=root / "work")
            self.assertEqual(repository.load_session("owner", session.session_key).messages, [])

    def test_confirmed_memory_deletion_cannot_recover_from_either_checkpoint_slot(self):
        for corrupted_slot in (0, 1):
            with self.subTest(corrupted_slot=corrupted_slot), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                durable, work = root / "durable" / "tomo.sqlite3", root / "work"
                repository = SqlitePersonalDataRepository(durable, local_work_dir=work)
                session = ConversationSession("telegram:actor:one")
                session.append(StoredMessage("assistant", "answer", metadata={"generation_id": "g1"}))
                repository.save_session("owner", session)
                repository.stage_memory_controls("owner", session.session_key, "g1", 0, 0, (self._memory_control(),))
                repository.accept_generations("owner", session.session_key, ("g1",))
                memory_id = repository.search_memories(MemorySearchQuery("owner", "secret"))[0].memory.id
                pending = repository.apply_user_memory_control("owner", session.session_key, MemoryGovernanceControl("request_delete", (memory_id,), "delete secret"))
                repository.apply_user_memory_control("owner", session.session_key, PendingMemoryActionControl("confirm_delete", pending.pending_action_id, "confirm delete secret"))

                self.assertEqual(len(self._slots(durable)), 2)
                self._slots(durable)[corrupted_slot].write_bytes(b"corrupted")
                self._remove_work_dir(work)

                restored = SqlitePersonalDataRepository(durable, local_work_dir=work)
                self.assertEqual(restored.search_memories(MemorySearchQuery("owner", "secret")), ())

    def test_owner_deletion_cannot_recover_from_either_checkpoint_slot(self):
        for corrupted_slot in (0, 1):
            with self.subTest(corrupted_slot=corrupted_slot), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                durable, work = root / "durable" / "tomo.sqlite3", root / "work"
                repository = SqlitePersonalDataRepository(durable, local_work_dir=work)
                session = ConversationSession("telegram:actor:one")
                session.append(StoredMessage("user", "private owner content", metadata={"burst_id": "b1", "update_id": 1}))
                repository.save_session("owner", session)
                session.append(StoredMessage("user", "more private owner content", metadata={"burst_id": "b2", "update_id": 2}))
                repository.save_session("owner", session)
                repository.delete_owner("owner")

                self.assertEqual(len(self._slots(durable)), 2)
                self._slots(durable)[corrupted_slot].write_bytes(b"corrupted")
                self._remove_work_dir(work)

                restored = SqlitePersonalDataRepository(durable, local_work_dir=work)
                self.assertEqual(restored.load_session("owner", session.session_key).messages, [])

    def test_sandbox_selects_local_working_copy_but_direct_runtime_does_not(self):
        config = RuntimeConfig(data_dir="/var/lib/tomo", owner_id="owner")
        with patch("tomo_core.sandbox_inbound.PersonalAgentRuntime") as runtime:
            build_runtime(Mock(), config)
        sandbox_config = runtime.call_args.kwargs["config"]
        self.assertEqual(sandbox_config.local_work_dir, "/tmp/tomo-core-sqlite")
        self.assertIsNone(config.local_work_dir)


if __name__ == "__main__":
    unittest.main()
