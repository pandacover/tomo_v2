import unittest

from tomo_core.snapshot_builder import ensure_snapshot


class FakeSnapshotGateway:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot
        self.created = []
        self.waited = []

    def get(self, name):
        return self.snapshot if self.snapshot and self.snapshot["name"] == name else None

    def create(self, name, image):
        self.created.append((name, image))
        self.snapshot = {"name": name}
        return self.snapshot

    def wait(self, snapshot):
        self.waited.append(snapshot)
        return snapshot


class SnapshotBuilderTests(unittest.TestCase):
    def test_build_reuses_existing_snapshot_and_creates_only_when_absent(self):
        existing = {"name": "tomo-core-sandbox"}
        gateway = FakeSnapshotGateway(existing)

        self.assertIs(
            ensure_snapshot(gateway, "tomo-core-sandbox", "registry.example/tomo:1", build=True, wait=True),
            existing,
        )
        self.assertEqual(gateway.created, [])
        self.assertEqual(gateway.waited, [existing])

        created = ensure_snapshot(gateway := FakeSnapshotGateway(), "tomo-core-sandbox", "registry.example/tomo:1", build=True, wait=True)

        self.assertEqual(gateway.created, [("tomo-core-sandbox", "registry.example/tomo:1")])
        self.assertEqual(gateway.waited, [created])
