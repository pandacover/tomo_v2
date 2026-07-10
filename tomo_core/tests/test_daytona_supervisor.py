import tempfile
import threading
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

    def test_reconcile_starts_and_smoke_tests_a_stopped_ready_sandbox(self):
        self.registry.upsert("tomo-a", "sbx-1", "base-v1", "ready")
        stopped = SandboxHandle("sbx-1", self.registry.get("tomo-a").sandbox_name, state="stopped", snapshot="base-v1")
        self.daytona.get.return_value = stopped

        record = self.supervisor.reconcile("tomo-a")

        self.assertEqual(record.status, "ready")
        self.daytona.start.assert_called_once_with(stopped)
        self.daytona.exec.assert_called_once()
        self.daytona.create_snapshot.assert_not_called()

    def test_reconcile_recreates_a_record_when_its_sandbox_is_missing(self):
        self.registry.upsert("tomo-a", "old", "base-v1", "ready")
        self.daytona.get.side_effect = DaytonaClientError("get")

        record = self.supervisor.reconcile("tomo-a")

        self.assertEqual(record.sandbox_id, "sbx-1")
        self.daytona.create_snapshot.assert_called_once()

    def test_reconcile_recovers_a_stale_id_from_its_deterministic_remote_name(self):
        existing = self.registry.upsert("tomo-a", "stale-id", "base-v1", "ready")
        recovered = SandboxHandle("sbx-remote", existing.sandbox_name, state="started", snapshot="base-v1")
        self.daytona.get.side_effect = [DaytonaClientError("get"), recovered]

        record = self.supervisor.reconcile("tomo-a")

        self.assertEqual(record.sandbox_id, "sbx-remote")
        self.daytona.get.assert_any_call(existing.sandbox_name)
        self.daytona.create_snapshot.assert_not_called()

    def test_reconcile_fetches_the_deterministic_name_after_a_create_conflict(self):
        existing = self.registry.upsert("tomo-a", None, "base-v1", "provisioning")
        conflicted = SandboxHandle("sbx-conflict", existing.sandbox_name, state="started", snapshot="base-v1")
        self.daytona.create_snapshot.side_effect = DaytonaClientError("create_snapshot")
        self.daytona.get.return_value = conflicted

        record = self.supervisor.reconcile("tomo-a")

        self.assertEqual(record.sandbox_id, "sbx-conflict")
        self.daytona.get.assert_called_once_with(existing.sandbox_name)

    def test_reconcile_marks_safe_error_and_deletes_sandbox_after_smoke_failure(self):
        sandbox = SandboxHandle("sbx-1", "tomo-sandbox-7af1f042458a784e", state="started", snapshot="base-v1")
        self.daytona.create_snapshot.return_value = sandbox
        self.daytona.exec.return_value.output = "not a protocol result"

        with self.assertRaises(SandboxSupervisorError) as raised:
            self.supervisor.reconcile("tomo-a")

        self.assertEqual(raised.exception.code, "sandbox_smoke_failed")
        self.assertEqual(self.registry.get("tomo-a").error_code, "sandbox_smoke_failed")
        self.daytona.delete.assert_called_once_with(sandbox)
        self.daytona.create_volume.assert_not_called()

    def test_reconcile_replaces_a_snapshot_changed_sandbox_without_replacing_its_volume(self):
        existing = self.registry.upsert("tomo-a", "old-id", "base-v0", "ready")
        old = SandboxHandle("old-id", existing.sandbox_name, state="started", snapshot="base-v0")
        self.daytona.get.return_value = old

        record = self.supervisor.reconcile("tomo-a")

        self.assertEqual(record.snapshot, "base-v1")
        self.daytona.delete.assert_called_once_with(old)
        self.daytona.create_snapshot.assert_called_once_with(existing.sandbox_name, "base-v1", "vol-1", "/home/daytona/.tomo")
        self.daytona.create_volume.assert_not_called()

    def test_reconcile_keeps_users_on_distinct_deterministic_resources(self):
        alice = self.supervisor.reconcile("tomo-alice")
        self.daytona.reset_mock()
        bob = self.supervisor.reconcile("tomo-bob")

        self.assertNotEqual(alice.sandbox_name, bob.sandbox_name)
        self.assertNotEqual(alice.volume_name, bob.volume_name)
        self.daytona.get_volume.assert_called_once_with(bob.volume_name)

    def test_concurrent_reconcile_for_one_tomo_creates_one_sandbox(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []
        self.daytona.get.return_value = SandboxHandle("sbx-1", "tomo-sandbox-c63d053c29e894db", state="started", snapshot="base-v1")

        def reconcile():
            try:
                barrier.wait()
                results.append(self.supervisor.reconcile("tomo-a"))
            except Exception as error:
                errors.append(error)

        first = threading.Thread(target=reconcile)
        second = threading.Thread(target=reconcile)
        first.start()
        second.start()
        first.join()
        second.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.daytona.create_snapshot.assert_called_once()


if __name__ == "__main__":
    unittest.main()
