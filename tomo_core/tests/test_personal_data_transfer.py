import io
import unittest

from tomo_core.personal_data_transfer import export_owner, import_owner


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


if __name__ == "__main__":
    unittest.main()
