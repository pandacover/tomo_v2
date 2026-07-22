import tempfile
import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from tomo_core.peer_store import PeerStore

class PeerStoreTests(unittest.TestCase):
    def test_handles_are_unique_and_relationship_pair_is_unordered(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PeerStore(tmp); now = datetime.now(timezone.utc)
            store.register_handle("a", "alice", now=now)
            with self.assertRaisesRegex(ValueError, "handle_taken"): store.register_handle("b", "alice", now=now)
            store.register_handle("b", "bob", now=now)
            relation = store.invite("a", "bob", now=now)
            self.assertEqual(store.invite("a", "bob", now=now).relationship_id, relation.relationship_id)

    def test_import_rejects_peer_records_for_another_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PeerStore(tmp)

            with self.assertRaisesRegex(ValueError, "cross-owner peer records"):
                store.import_owner_records("a", iter(({"table": "peer_records", "owner_id": "b", "records": []},)))

    def test_initialization_repairs_an_interrupted_schema_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(f"{tmp}/peer.sqlite3")
            db.execute("CREATE TABLE peer_schema(version INTEGER NOT NULL CHECK(version=1))")
            db.execute("INSERT INTO peer_schema VALUES(1)")
            db.commit()
            db.close()

            store = PeerStore(tmp)

            store.register_handle("a", "alice")

    def test_initialization_adds_safe_failure_code_to_existing_request_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(f"{tmp}/peer.sqlite3")
            db.execute("CREATE TABLE peer_schema(version INTEGER NOT NULL CHECK(version=1))")
            db.execute("INSERT INTO peer_schema VALUES(1)")
            db.execute(
                "CREATE TABLE peer_requests("
                "request_id TEXT PRIMARY KEY,relationship_id TEXT NOT NULL,sender TEXT NOT NULL,"
                "recipient TEXT NOT NULL,thread_id TEXT NOT NULL,generation_id TEXT NOT NULL,"
                "call_id TEXT NOT NULL,kind TEXT NOT NULL,action TEXT NOT NULL,text TEXT NOT NULL,"
                "sequence INTEGER NOT NULL,created_at TEXT NOT NULL,status TEXT NOT NULL,"
                "disclosure_scope TEXT NOT NULL DEFAULT 'none',lease_token TEXT,lease_until TEXT,"
                "execution_deadline TEXT,attempt_count INTEGER NOT NULL,available_at TEXT NOT NULL,"
                "UNIQUE(sender,generation_id,call_id))"
            )
            db.commit()
            db.close()

            PeerStore(tmp)

            db = sqlite3.connect(f"{tmp}/peer.sqlite3")
            try:
                columns = {row[1] for row in db.execute("PRAGMA table_info(peer_requests)")}
            finally:
                db.close()
            self.assertIn("error_code", columns)
