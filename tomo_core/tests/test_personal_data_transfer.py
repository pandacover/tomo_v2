import io
import unittest
import tempfile

from tomo_core.personal_data_transfer import export_owner, import_owner
from tomo_core.peer_exchange import PeerExchange


class _TransferRepository:
    def __init__(self):
        self.records = [{"table": "memories", "row": {"id": "one"}}]
        self.imported = []

    def export_owner_records(self, _owner_id):
        return iter(self.records)

    def import_owner_records(self, _owner_id, records):
        self.imported = list(records)


class PersonalDataTransferTests(unittest.TestCase):
    def test_round_trip_uses_canonical_jsonl_records(self):
        source = _TransferRepository()
        output = io.StringIO()

        export_owner(source, "owner", output)
        target = _TransferRepository()
        target.records = []
        import_owner(target, "owner", output.getvalue().splitlines())

        self.assertEqual(target.imported, source.records)

    def test_rejects_non_object_records_before_adapter_reads_them(self):
        target = _TransferRepository()

        with self.assertRaisesRegex(ValueError, "malformed canonical stream"):
            import_owner(target, "owner", ['{"format":"tomo_personal_data","version":1,"owner_id":"owner"}', "[]"])

        self.assertEqual(target.imported, [])

    def test_exports_peer_records_only_by_keyword_and_rejects_cross_owner_peer_records(self):
        source = _TransferRepository()
        output = io.StringIO()

        export_owner(source, "owner", output, peer_records=({"table": "peer_records", "owner_id": "owner", "records": {}},))

        self.assertIn('"table":"peer_records"', output.getvalue())
        target = _TransferRepository()
        with self.assertRaisesRegex(ValueError, "cross-owner canonical record"):
            import_owner(target, "owner", [
                '{"format":"tomo_personal_data","version":1,"owner_id":"owner"}',
                '{"table":"peer_records","owner_id":"another-owner","records":{}}',
            ])
        self.assertEqual(target.imported, [])

    def test_peer_import_is_inert_archive_and_round_trips(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as target_dir:
            source_peer = PeerExchange(source_dir)
            source_peer.register_handle("owner", "owner")
            records = list(source_peer.export_owner_records(owner_id="owner"))
            target_peer = PeerExchange(target_dir)
            target_peer.register_handle("owner", "owner")
            target = _TransferRepository()
            stream = ['{"format":"tomo_personal_data","version":1,"owner_id":"owner"}', *(__import__('json').dumps(record) for record in records)]

            import_owner(target, "owner", stream, peer_exchange=target_peer)

            self.assertEqual(target_peer.list_relationships("owner"), ())
            self.assertEqual(list(target_peer.export_owner_records(owner_id="owner")), records)

            target_peer.register_handle("owner", "new_owner")
            reexported = list(target_peer.export_owner_records(owner_id="owner"))
            self.assertEqual(reexported[0]["records"]["handle"], "new_owner")

    def test_inert_relationship_archive_is_visible_and_selectively_purgeable(self):
        with tempfile.TemporaryDirectory() as directory:
            peer = PeerExchange(directory)
            record = {
                "table": "peer_relationship",
                "owner_id": "owner",
                "records": {
                    "relationship_id": "restored-relationship",
                    "peer_handle": "archived_peer",
                    "status": "active",
                    "relationship_revision": 4,
                },
            }
            peer.import_owner_records("owner", [record])

            restored = peer.list_relationships("owner")
            self.assertEqual(restored[0].relationship_id, "restored-relationship")
            self.assertEqual(restored[0].status.value, "revoked")

            peer.purge_history("owner", "restored-relationship")

            self.assertEqual(peer.list_relationships("owner"), ())
            self.assertNotIn(
                "restored-relationship",
                repr(list(peer.export_owner_records(owner_id="owner"))),
            )


if __name__ == "__main__":
    unittest.main()
