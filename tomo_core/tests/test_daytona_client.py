import unittest
import threading
from unittest.mock import Mock

from daytona import PtySize, SessionExecuteRequest

from tomo_core.daytona_client import DaytonaClient, DaytonaClientError, SandboxHandle, SessionCommandHandle, VolumeHandle


class DaytonaClientTests(unittest.TestCase):
    def setUp(self):
        self.sdk = Mock()
        self.sandbox = Mock()
        self.sandbox.id = "sandbox-123"
        self.sandbox.name = "agent"
        self.sandbox.state = "started"
        self.sandbox.snapshot = "python-base"
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

        self.assertEqual(handle, SandboxHandle(id="sandbox-123", name="agent", state="started", snapshot="python-base"))
        self.assertEqual(handle.state, "started")
        self.assertEqual(handle.snapshot, "python-base")
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

        self.assertEqual(handle, SandboxHandle(id="sandbox-123", name="agent", state="started", snapshot="python-base"))
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

    def test_async_session_command_streams_logs_and_deletes_session_idempotently(self):
        handle = self.client.get("agent")
        pty = _FakePty([b"first", b"second"], exit_code=0)
        self.sandbox.process.create_pty_session.return_value = pty

        command = self.client.start_session_command(
            handle,
            "telegram-gen-1",
            "/opt/tomo/.venv/bin/tomo-core sandbox-inbound",
            env={"TOKEN": "safe"},
            timeout=120,
        )
        chunks = list(self.client.iter_session_logs(handle, command))
        exit_code = self.client.session_command_exit_code(handle, command)
        self.client.delete_session(handle, "telegram-gen-1")
        self.client.delete_session(handle, "telegram-gen-1")

        self.assertEqual(command, SessionCommandHandle("telegram-gen-1", "telegram-gen-1"))
        self.sandbox.process.create_pty_session.assert_called_once_with(
            "telegram-gen-1",
            envs={"TOKEN": "safe"},
            pty_size=PtySize(rows=50, cols=4096),
        )
        pty_size = self.sandbox.process.create_pty_session.call_args.kwargs["pty_size"]
        self.assertGreaterEqual(pty_size.cols, 1024)
        self.assertEqual(pty.sent, ["/opt/tomo/.venv/bin/tomo-core sandbox-inbound\nexit\n"])
        self.assertEqual(chunks, ["first", "second"])
        self.assertEqual(exit_code, 0)
        self.sandbox.process.kill_pty_session.assert_called_once_with("telegram-gen-1")

    def test_installed_sdk_session_request_silently_discards_environment(self):
        request = SessionExecuteRequest(command="true", run_async=True, env={"TOKEN": "secret"})

        self.assertFalse(hasattr(request, "env"))
        self.assertNotIn("env", request.model_dump())

    def test_session_adapter_errors_are_safe(self):
        self.sandbox.process.create_pty_session.side_effect = RuntimeError("secret-token")

        with self.assertRaises(DaytonaClientError) as raised:
            self.client.start_session_command(
                SandboxHandle(id="sandbox-123", name="agent"),
                "telegram-gen-1",
                "echo secret-token",
                env={"TOKEN": "secret-token"},
                timeout=120,
            )

        self.assertEqual(raised.exception.operation, "session_command")
        self.assertNotIn("secret-token", str(raised.exception))

    def test_session_connection_wait_is_bounded_and_cleans_up_session(self):
        handle = self.client.get("agent")
        pty = _HangingPty()
        self.sandbox.process.create_pty_session.return_value = pty

        with self.assertRaises(DaytonaClientError) as raised:
            self.client.start_session_command(handle, "telegram-gen-1", "true", timeout=0.01)

        self.assertEqual(raised.exception.operation, "session_command")
        self.assertEqual(pty.sent, [])
        self.assertTrue(pty.disconnected)
        self.sandbox.process.kill_pty_session.assert_called_once_with("telegram-gen-1")

    def test_get_returns_safe_state_and_snapshot_without_exposing_the_sdk_sandbox(self):
        handle = self.client.get("agent")

        self.assertEqual(handle.state, "started")
        self.assertEqual(handle.snapshot, "python-base")
        self.assertNotIn("_sandbox", repr(handle))


class _FakePty:
    def __init__(self, chunks, *, exit_code):
        self._chunks = chunks
        self.exit_code = exit_code
        self.sent = []
        self.disconnected = False

    def wait_for_connection(self):
        return None

    def send_input(self, data):
        self.sent.append(data)

    def __iter__(self):
        return iter(self._chunks)

    def disconnect(self):
        self.disconnected = True


class _HangingPty(_FakePty):
    def __init__(self):
        super().__init__([], exit_code=None)
        self._release = threading.Event()

    def wait_for_connection(self):
        self._release.wait(timeout=5)
