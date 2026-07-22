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
