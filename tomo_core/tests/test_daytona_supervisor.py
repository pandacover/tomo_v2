import tempfile
import unittest
from unittest.mock import Mock

from tomo_core.daytona_client import DaytonaClientError, SandboxHandle, VolumeHandle
from tomo_core.daytona_supervisor import DaytonaSupervisor, SandboxSupervisorError
from tomo_core.sandbox_registry import SandboxRegistry
from tomo_core.sandbox_protocol import RESULT_MARKER, encode_result
from tomo_core.models import InboundEnvelope, OutboundBubble


class DaytonaSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = SandboxRegistry(self.tmp.name)
        self.daytona = Mock()
        self.daytona.get_volume.return_value = VolumeHandle("vol-1", "tomo-volume-7af1f042458a784e")
        self.daytona.create_snapshot.return_value = SandboxHandle("sbx-1", "tomo-sandbox-7af1f042458a784e")
        self.daytona.exec.return_value.output = f"{RESULT_MARKER}{encode_result('health-1', [OutboundBubble('healthy')])}\n"
        self.supervisor = DaytonaSupervisor(self.registry, self.daytona, snapshot="base-v1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_reconcile_creates_one_volume_and_sandbox_then_smoke_tests_it(self):
        record = self.supervisor.reconcile("tomo-alice@example.com-42")

        self.assertEqual(record.sandbox_id, "sbx-1")
        self.assertEqual(record.status, "ready")
        self.daytona.get_volume.assert_called_once_with(record.volume_name)
        self.daytona.create_snapshot.assert_called_once_with(record.sandbox_name, "base-v1", "vol-1", "/home/daytona/.tomo")
        self.daytona.start.assert_called_once_with(SandboxHandle("sbx-1", record.sandbox_name))
        self.assertEqual(self.daytona.exec.call_args.args[1], "/opt/tomo/.venv/bin/tomo-core sandbox-inbound --health")
        env = self.daytona.exec.call_args.kwargs["env"]
        self.assertEqual(env["TOMO_CORE_DATA_DIR"], "/home/daytona/.tomo")
        self.assertEqual(env["TOMO_INSTANCE_ID"], "tomo-alice@example.com-42")
        self.assertIn("TOMO_INBOUND_JSON", env)
        self.assertNotIn("TOMO_SUPERGROK_ACCESS_TOKEN", env)

    def test_reconcile_creates_the_deterministic_volume_when_it_is_missing(self):
        self.daytona.get_volume.side_effect = DaytonaClientError("get_volume")
        self.daytona.create_volume.return_value = VolumeHandle("vol-new", "tomo-volume-7af1f042458a784e")

        self.supervisor.reconcile("tomo-alice@example.com-42")

        self.daytona.create_volume.assert_called_once_with("tomo-volume-7af1f042458a784e")
        self.assertEqual(self.daytona.create_snapshot.call_args.args[2], "vol-new")

    def test_reconcile_reuses_a_valid_ready_record_without_creating_again(self):
        self.registry.upsert("tomo-a", "sbx-1", "base-v1", "ready")
        self.daytona.get.return_value = SandboxHandle("sbx-1", self.registry.get("tomo-a").sandbox_name)

        record = self.supervisor.reconcile("tomo-a")

        self.assertEqual(record.status, "ready")
        self.daytona.create_snapshot.assert_not_called()
        self.daytona.start.assert_not_called()

    def test_reconcile_recreates_a_record_when_its_sandbox_is_missing(self):
        self.registry.upsert("tomo-a", "old", "base-v1", "ready")
        self.daytona.get.side_effect = DaytonaClientError("get")

        record = self.supervisor.reconcile("tomo-a")

        self.assertEqual(record.sandbox_id, "sbx-1")
        self.daytona.create_snapshot.assert_called_once()

    def test_reconcile_does_not_adopt_a_deterministic_name_conflict(self):
        self.daytona.create_snapshot.side_effect = DaytonaClientError("create_snapshot")

        with self.assertRaises(SandboxSupervisorError) as raised:
            self.supervisor.reconcile("tomo-a")

        self.assertEqual(raised.exception.code, "sandbox_create_failed")
        self.assertEqual(self.registry.get("tomo-a").error_code, "sandbox_create_failed")


if __name__ == "__main__":
    unittest.main()
