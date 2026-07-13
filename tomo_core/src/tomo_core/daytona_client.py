from __future__ import annotations

import queue
from collections import deque
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from daytona import CreateSandboxFromSnapshotParams, Daytona, DaytonaNotFoundError, PtySize, VolumeMount


# Progressive sandbox turns emit long single-line protocol markers. Daytona's
# default PTY is ~80 columns and soft-wraps those lines, which corrupts the
# event stream into invalid_result failures on the host parser. Daytona also
# rejects cols >= 1000, so use the widest legal size.
_PTY_SIZE = PtySize(rows=50, cols=999)


class DaytonaClientError(RuntimeError):
    """A Daytona operation failed without exposing its input or SDK details."""

    def __init__(self, operation: str) -> None:
        self.operation = operation
        super().__init__(f"Daytona {operation} failed")


class DaytonaNotFoundClientError(DaytonaClientError):
    """A Daytona resource is known to be absent."""


@dataclass(frozen=True)
class SandboxHandle:
    id: str
    name: str
    state: str | None = None
    snapshot: str | None = None
    _sandbox: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class VolumeHandle:
    id: str
    name: str


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str


@dataclass(frozen=True)
class SessionCommandHandle:
    session_id: str
    command_id: str
    timeout: int | None = field(default=None, compare=False, repr=False)
    _pty: Any = field(default=None, compare=False, repr=False)


class DaytonaClient:
    _MAX_DELETED_SESSIONS = 1024

    def __init__(self, client: Daytona | None = None) -> None:
        self._client = client or Daytona()
        self._deleted_sessions: set[tuple[str, str]] = set()
        self._deleted_session_order: deque[tuple[str, str]] = deque()
        self._deleted_sessions_lock = threading.Lock()

    def get(self, sandbox_id_or_name: str) -> SandboxHandle:
        return self._handle("get", lambda: self._client.get(sandbox_id_or_name))

    def volume(self, volume_id: str, mount_path: str, *, subpath: str | None = None) -> VolumeMount:
        return VolumeMount(volume_id=volume_id, mount_path=mount_path, subpath=subpath)

    def get_volume(self, name: str) -> VolumeHandle:
        return self._volume_handle("get_volume", lambda: self._client.volume.get(name))

    def create_volume(self, name: str) -> VolumeHandle:
        return self._volume_handle("create_volume", lambda: self._client.volume.create(name=name))

    def create_snapshot(self, name: str, snapshot: str, volume_id: str, mount_path: str) -> SandboxHandle:
        params = CreateSandboxFromSnapshotParams(
            name=name,
            snapshot=snapshot,
            auto_stop_interval=0,
            volumes=[self.volume(volume_id, mount_path)],
        )
        return self._handle("create_snapshot", lambda: self._client.create(params))

    def start(self, handle: SandboxHandle) -> None:
        self._run("start", lambda: self._client.start(self._sandbox(handle)))

    def delete(self, handle: SandboxHandle) -> None:
        self._run("delete", lambda: self._client.delete(self._sandbox(handle)))

    def exec(self, handle: SandboxHandle, command: str, *, cwd: str | None = None, env: dict[str, str] | None = None, timeout: int | None = None) -> ExecResult:
        response = self._run("exec", lambda: self._sandbox(handle).process.exec(command, cwd=cwd, env=env, timeout=timeout))
        return ExecResult(exit_code=response.exit_code, output=response.result)

    def start_session_command(
        self,
        handle: SandboxHandle,
        session_id: str,
        command: str,
        *,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> SessionCommandHandle:
        if "\n" in command or "\r" in command:
            raise DaytonaClientError("session_command")

        def start() -> Any:
            process = self._sandbox(handle).process
            pty = process.create_pty_session(session_id, envs=env or {}, pty_size=_PTY_SIZE)
            try:
                self._wait_for_pty_connection(pty, timeout)
                pty.send_input(f"{command}\nexit\n")
                return pty
            except Exception:
                self._cleanup_pty(process, pty, session_id)
                raise

        pty = self._run("session_command", start)
        with self._deleted_sessions_lock:
            self._deleted_sessions.discard((handle.id, session_id))
        return SessionCommandHandle(
            session_id=session_id,
            command_id=session_id,
            timeout=timeout,
            _pty=pty,
        )

    def iter_session_logs(self, handle: SandboxHandle, command: SessionCommandHandle) -> Any:
        if command._pty is None:
            raise DaytonaClientError("session_logs")

        def stream() -> Any:
            messages: queue.Queue[tuple[str, Any]] = queue.Queue()

            def read() -> None:
                try:
                    for chunk in command._pty:
                        messages.put(("chunk", chunk))
                except Exception as error:
                    messages.put(("error", error))
                finally:
                    messages.put(("done", None))

            threading.Thread(target=read, name=f"daytona-pty-{command.session_id}", daemon=True).start()
            deadline = time.monotonic() + command.timeout if command.timeout is not None else None
            while True:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                try:
                    kind, value = messages.get(timeout=remaining)
                except queue.Empty as error:
                    raise TimeoutError("Daytona session timed out") from error
                if kind == "done":
                    return
                if kind == "error":
                    raise DaytonaClientError("session_logs") from value
                yield value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)

        return stream()

    def session_command_exit_code(self, handle: SandboxHandle, command: SessionCommandHandle) -> int:
        if command._pty is None:
            raise DaytonaClientError("session_command_status")
        exit_code = getattr(command._pty, "exit_code", None)
        return int(exit_code) if exit_code is not None else 1

    def delete_session(self, handle: SandboxHandle, session_id: str) -> None:
        key = (handle.id, session_id)
        with self._deleted_sessions_lock:
            if key in self._deleted_sessions:
                return
            self._deleted_sessions.add(key)
            self._deleted_session_order.append(key)
            while len(self._deleted_session_order) > self._MAX_DELETED_SESSIONS:
                self._deleted_sessions.discard(self._deleted_session_order.popleft())
        try:
            self._run("delete_session", lambda: self._sandbox(handle).process.kill_pty_session(session_id))
        except DaytonaClientError:
            with self._deleted_sessions_lock:
                self._deleted_sessions.discard(key)
            raise

    def _wait_for_pty_connection(self, pty: Any, timeout: int | float | None) -> None:
        result: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def wait() -> None:
            try:
                pty.wait_for_connection()
            except Exception as error:
                result.put(("error", error))
            else:
                result.put(("ok", None))

        threading.Thread(target=wait, name="daytona-pty-connect", daemon=True).start()
        try:
            kind, value = result.get(timeout=timeout if timeout is not None else 10.0)
        except queue.Empty as error:
            raise TimeoutError("Daytona session connection timed out") from error
        if kind == "error":
            raise value

    def _cleanup_pty(self, process: Any, pty: Any, session_id: str) -> None:
        disconnect = getattr(pty, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect()
            except Exception:
                pass
        try:
            process.kill_pty_session(session_id)
        except Exception:
            pass

    def _sandbox(self, handle: SandboxHandle) -> Any:
        return handle._sandbox or self._handle("get", lambda: self._client.get(handle.id))._sandbox

    def _handle(self, operation: str, action: Any) -> SandboxHandle:
        sandbox = self._run(operation, action)
        return SandboxHandle(
            id=sandbox.id,
            name=sandbox.name,
            state=getattr(sandbox, "state", None),
            snapshot=getattr(sandbox, "snapshot", None),
            _sandbox=sandbox,
        )

    def _volume_handle(self, operation: str, action: Any) -> VolumeHandle:
        volume = self._run(operation, action)
        return VolumeHandle(id=volume.id, name=volume.name)

    def _run(self, operation: str, action: Any) -> Any:
        try:
            return action()
        except DaytonaNotFoundError as error:
            raise DaytonaNotFoundClientError(operation) from error
        except Exception as error:
            raise DaytonaClientError(operation) from error
