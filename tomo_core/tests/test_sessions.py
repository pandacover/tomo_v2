import errno
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tomo_core.sessions import JsonSessionStore, StoredMessage
import tomo_core.sessions as sessions


class JsonSessionStoreTests(unittest.TestCase):
    def test_save_uses_existing_replace_path_when_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonSessionStore(tmp)
            session = store.load("telegram:actor:user")
            session.append(StoredMessage("user", "hello"))
            replaced = []
            original_replace = Path.replace

            def replace(source, destination):
                replaced.append((source, destination))
                return original_replace(source, destination)

            with patch.object(Path, "replace", autospec=True, side_effect=replace):
                store.save_atomic(session)

            path = store._path(session.session_key)
            self.assertEqual(len(replaced), 1)
            self.assertEqual(replaced[0][1], path)
            self.assertEqual(JsonSessionStore(tmp).load(session.session_key).messages, session.messages)
            self.assertFalse(store._recovery_path(path).exists())

    def test_enosys_replace_fallback_persists_and_reloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonSessionStore(tmp)
            session = store.load("telegram:actor:user")
            session.append(StoredMessage("user", "hello"))

            with patch.object(Path, "replace", autospec=True, side_effect=OSError(errno.ENOSYS, "unsupported")):
                store.save_atomic(session)

            path = store._path(session.session_key)
            self.assertEqual([message.content for message in JsonSessionStore(tmp).load(session.session_key).messages], ["hello"])
            self.assertEqual(store._recovery_path(path).read_text(encoding="utf-8"), path.read_text(encoding="utf-8"))
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_unrelated_replace_error_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonSessionStore(tmp)
            session = store.load("telegram:actor:user")
            session.append(StoredMessage("user", "hello"))

            with patch.object(Path, "replace", autospec=True, side_effect=OSError(errno.EIO, "disk error")):
                with self.assertRaisesRegex(OSError, "disk error"):
                    store.save_atomic(session)

            self.assertEqual(list(store.sessions_dir.glob("*.tmp")), [])

    def test_corrupt_primary_recovers_from_valid_recovery_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonSessionStore(tmp)
            session = store.load("telegram:actor:user")
            session.append(StoredMessage("user", "hello"))

            with patch.object(Path, "replace", autospec=True, side_effect=OSError(errno.ENOSYS, "unsupported")):
                store.save_atomic(session)

            path = store._path(session.session_key)
            path.write_text('{"messages": [', encoding="utf-8")

            recovered = JsonSessionStore(tmp).load(session.session_key)
            self.assertEqual([message.content for message in recovered.messages], ["hello"])

    def test_readers_wait_for_in_place_fallback_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonSessionStore(tmp)
            session = store.load("telegram:actor:user")
            session.append(StoredMessage("user", "before"))
            store.save_atomic(session)
            session.append(StoredMessage("assistant", "after"))
            path = store._path(session.session_key)
            primary_write_started = threading.Event()
            allow_primary_write = threading.Event()
            reader_started = threading.Event()
            reader_finished = threading.Event()
            reader_result = []
            writer_errors = []
            original_write = sessions._write_and_fsync

            def write_and_pause(write_path, content):
                if write_path == path:
                    primary_write_started.set()
                    self.assertTrue(allow_primary_write.wait(timeout=2))
                original_write(write_path, content)

            def save():
                try:
                    store.save_atomic(session)
                except BaseException as error:
                    writer_errors.append(error)

            def load():
                reader_started.set()
                reader_result.append(JsonSessionStore(tmp).load(session.session_key))
                reader_finished.set()

            with patch.object(Path, "replace", autospec=True, side_effect=OSError(errno.ENOSYS, "unsupported")), patch.object(
                sessions, "_write_and_fsync", side_effect=write_and_pause
            ):
                writer = threading.Thread(target=save)
                writer.start()
                self.assertTrue(primary_write_started.wait(timeout=2))
                reader = threading.Thread(target=load)
                reader.start()
                self.assertTrue(reader_started.wait(timeout=2))
                self.assertFalse(reader_finished.wait(timeout=0.1))
                allow_primary_write.set()
                writer.join(timeout=2)
                reader.join(timeout=2)

            self.assertFalse(writer.is_alive())
            self.assertFalse(reader.is_alive())
            self.assertEqual(writer_errors, [])
            self.assertEqual([message.content for message in reader_result[0].messages], ["before", "after"])


if __name__ == "__main__":
    unittest.main()
