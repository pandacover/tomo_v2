import json
import tempfile
import unittest
from pathlib import Path

from tomo_core.legacy_session_import import import_legacy_sessions
from tomo_core.sqlite_personal_data import SqlitePersonalDataRepository


class LegacySessionImportTests(unittest.TestCase):
    def test_imports_recovery_copy_when_primary_is_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions"
            sessions.mkdir()
            primary = sessions / "telegram_actor_one.json"
            primary.write_text("{", encoding="utf-8")
            recovery = primary.with_suffix(".json.recovery")
            recovery.write_text(json.dumps({"session_key": "telegram:actor:one", "messages": []}), encoding="utf-8")
            repository = SqlitePersonalDataRepository(root / "tomo.sqlite3")

            self.assertEqual(import_legacy_sessions(repository, "owner-a", root), 1)
            self.assertEqual(repository.load_session("owner-a", "telegram:actor:one").session_key, "telegram:actor:one")


if __name__ == "__main__":
    unittest.main()
