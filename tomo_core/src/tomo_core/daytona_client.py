from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from daytona import CreateSandboxFromSnapshotParams, Daytona, VolumeMount


class DaytonaClientError(RuntimeError):
    """A Daytona operation failed without exposing its input or SDK details."""

    def __init__(self, operation: str) -> None:
        self.operation = operation
        super().__init__(f"Daytona {operation} failed")


@dataclass(frozen=True)
class SandboxHandle:
    id: str
    name: str
    _sandbox: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str


class DaytonaClient:
    def __init__(self, client: Daytona | None = None) -> None:
        self._client = client or Daytona()

    def get(self, sandbox_id_or_name: str) -> SandboxHandle:
        return self._handle("get", lambda: self._client.get(sandbox_id_or_name))

    def volume(self, volume_id: str, mount_path: str, *, subpath: str | None = None) -> VolumeMount:
        return VolumeMount(volume_id=volume_id, mount_path=mount_path, subpath=subpath)

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

    def _sandbox(self, handle: SandboxHandle) -> Any:
        return handle._sandbox or self._handle("get", lambda: self._client.get(handle.id))._sandbox

    def _handle(self, operation: str, action: Any) -> SandboxHandle:
        sandbox = self._run(operation, action)
        return SandboxHandle(id=sandbox.id, name=sandbox.name, _sandbox=sandbox)

    def _run(self, operation: str, action: Any) -> Any:
        try:
            return action()
        except Exception as error:
            raise DaytonaClientError(operation) from error
