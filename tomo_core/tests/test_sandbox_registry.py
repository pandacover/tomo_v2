import sqlite3
import tempfile
import unittest
from pathlib import Path

from tomo_core.sandbox_registry import SandboxRegistry


class SandboxRegistryTests(unittest.TestCase):
    def test_migrates_registry_schema_in_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = SandboxRegistry(tmp)

            self.assertEqual(registry.db_path, Path(tmp) / "sandbox_registry.sqlite")
            db = sqlite3.connect(registry.db_path)
            try:
                columns = {row[1]: row for row in db.execute("pragma table_info(sandboxes)")}
            finally:
                db.close()

            self.assertEqual(columns["tomo_id"][5], 1)
            self.assertEqual(
                set(columns),
                {
                    "tomo_id",
                    "sandbox_id",
                    "sandbox_name",
                    "volume_name",
                    "snapshot",
                    "status",
                    "error_code",
                    "updated_at",
                },
            )

    def test_upsert_and_read_replaces_existing_tomo_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = SandboxRegistry(tmp)
            created = registry.upsert(
                tomo_id="tomo-a",
                sandbox_id="sbx-1",
                snapshot="base-v1",
                status="provisioning",
                error_code=None,
                updated_at=100,
            )
            updated = registry.upsert(
                tomo_id="tomo-a",
                sandbox_id="sbx-2",
                snapshot="base-v2",
                status="ready",
                error_code=None,
                updated_at=200,
            )

            self.assertEqual(created.sandbox_name, updated.sandbox_name)
            self.assertEqual(created.volume_name, updated.volume_name)
            self.assertEqual(registry.get("tomo-a"), updated)
            self.assertEqual(updated.sandbox_id, "sbx-2")
            self.assertEqual(updated.snapshot, "base-v2")
            self.assertEqual(updated.status, "ready")
            self.assertEqual(updated.updated_at, 200)

    def test_records_are_isolated_by_tomo_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = SandboxRegistry(tmp)
            first = registry.upsert("tomo-a", "sbx-a", "base", "ready", None, 100)
            second = registry.upsert("tomo-b", "sbx-b", "base", "error", "sandbox_create_failed", 200)

            self.assertEqual(registry.get("tomo-a"), first)
            self.assertEqual(registry.get("tomo-b"), second)
            self.assertIsNone(registry.get("missing"))
            self.assertNotEqual(first.sandbox_name, second.sandbox_name)
            self.assertNotEqual(first.volume_name, second.volume_name)

    def test_names_are_deterministic_hashes_without_raw_user_identifier(self):
        tomo_id = "tomo-alice@example.com-42"

        self.assertEqual(SandboxRegistry.sandbox_name_for(tomo_id), "tomo-sandbox-7af1f042458a784e")
        self.assertEqual(SandboxRegistry.volume_name_for(tomo_id), "tomo-volume-7af1f042458a784e")
        self.assertNotIn("alice", SandboxRegistry.sandbox_name_for(tomo_id))
        self.assertNotIn("example", SandboxRegistry.volume_name_for(tomo_id))
