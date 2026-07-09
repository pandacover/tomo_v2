import unittest
from unittest.mock import Mock

from tomo_core.daytona_client import DaytonaClient, DaytonaClientError, SandboxHandle, VolumeHandle


class DaytonaClientTests(unittest.TestCase):
    def setUp(self):
        self.sdk = Mock()
        self.sandbox = Mock()
        self.sandbox.id = "sandbox-123"
        self.sandbox.name = "agent"
        self.sdk.get.return_value = self.sandbox
        self.sdk.create.return_value = self.sandbox
        self.client = DaytonaClient(self.sdk)

    def test_creates_snapshot_sandbox_with_volume_and_disabled_auto_stop(self):
        handle = self.client.create_snapshot(
            name="agent",
            snapshot="python-base",
            volume_id="volume-123",
            mount_path="/workspace",
        )

        self.assertEqual(handle, SandboxHandle(id="sandbox-123", name="agent"))
        params = self.sdk.create.call_args.args[0]
        self.assertEqual(params.name, "agent")
        self.assertEqual(params.snapshot, "python-base")
        self.assertEqual(params.auto_stop_interval, 0)
        self.assertEqual(params.volumes[0].volume_id, "volume-123")
        self.assertEqual(params.volumes[0].mount_path, "/workspace")

    def test_get_start_delete_and_exec_delegate_to_the_sandbox(self):
        handle = self.client.get("agent")
        self.client.start(handle)
        self.client.delete(handle)
        self.sandbox.process.exec.return_value.exit_code = 7
        self.sandbox.process.exec.return_value.result = "out"

        result = self.client.exec(handle, "exit 7", cwd="/workspace", timeout=10)

        self.assertEqual(handle, SandboxHandle(id="sandbox-123", name="agent"))
        self.assertEqual((result.exit_code, result.output), (7, "out"))
        self.sdk.start.assert_called_once_with(self.sandbox)
        self.sdk.delete.assert_called_once_with(self.sandbox)
        self.sandbox.process.exec.assert_called_once_with("exit 7", cwd="/workspace", env=None, timeout=10)

    def test_volume_returns_a_sdk_volume_mount(self):
        mount = self.client.volume("volume-123", "/workspace", subpath="project")

        self.assertEqual(mount.volume_id, "volume-123")
        self.assertEqual(mount.mount_path, "/workspace")
        self.assertEqual(mount.subpath, "project")

    def test_get_and_create_volume_return_safe_handles(self):
        self.sdk.volume.get.return_value.id = "volume-123"
        self.sdk.volume.get.return_value.name = "agent-data"
        self.sdk.volume.create.return_value.id = "volume-456"
        self.sdk.volume.create.return_value.name = "other-data"

        existing = self.client.get_volume("agent-data")
        created = self.client.create_volume("other-data")

        self.assertEqual(existing, VolumeHandle("volume-123", "agent-data"))
        self.assertEqual(created, VolumeHandle("volume-456", "other-data"))
        self.sdk.volume.create.assert_called_once_with(name="other-data")

    def test_errors_are_typed_and_do_not_expose_command_or_secrets(self):
        self.sandbox.process.exec.side_effect = RuntimeError("token=sensitive-value")

        with self.assertRaises(DaytonaClientError) as raised:
            self.client.exec(SandboxHandle(id="sandbox-123", name="agent"), "echo sensitive-value")

        self.assertEqual(raised.exception.operation, "exec")
        self.assertNotIn("sensitive-value", str(raised.exception))
