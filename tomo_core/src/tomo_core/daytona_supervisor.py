"""Idempotently reconcile the Daytona resources owned by one Tomo."""

from __future__ import annotations

from collections import defaultdict
from threading import Lock

from .daytona_client import DaytonaClient, DaytonaClientError, SandboxHandle
from .sandbox_registry import SandboxRecord, SandboxRegistry


DATA_DIR = "/home/daytona/.tomo"
SMOKE_COMMAND = "tomo-core sandbox inbound --once"


class SandboxSupervisorError(RuntimeError):
    """A safe, stable reconciliation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"sandbox reconciliation failed: {code}")


class DaytonaSupervisor:
    _locks: defaultdict[str, Lock] = defaultdict(Lock)

    def __init__(self, registry: SandboxRegistry, daytona: DaytonaClient, *, snapshot: str, data_dir: str = DATA_DIR) -> None:
        self.registry = registry
        self.daytona = daytona
        self.snapshot = snapshot
        self.data_dir = data_dir

    def reconcile(self, tomo_id: str) -> SandboxRecord:
        """Return the ready persistent sandbox for ``tomo_id``, creating it when absent."""
        with self._locks[tomo_id]:
            record = self.registry.get(tomo_id)
            if record is not None and record.status == "ready" and record.snapshot == self.snapshot and record.sandbox_id:
                try:
                    self.daytona.get(record.sandbox_id)
                    return record
                except DaytonaClientError:
                    pass
            return self._create(tomo_id, record)

    def _create(self, tomo_id: str, existing: SandboxRecord | None) -> SandboxRecord:
        record = self.registry.upsert(tomo_id, None, self.snapshot, "provisioning")
        try:
            volume = self.daytona.get_volume(record.volume_name)
        except DaytonaClientError:
            try:
                volume = self.daytona.create_volume(record.volume_name)
            except DaytonaClientError:
                return self._fail(tomo_id, "volume_create_failed")
        try:
            sandbox = self.daytona.create_snapshot(record.sandbox_name, self.snapshot, volume.id, self.data_dir)
            self.daytona.start(sandbox)
            self._smoke_test(sandbox)
        except DaytonaClientError:
            return self._fail(tomo_id, "sandbox_create_failed")
        except SandboxSupervisorError as error:
            return self._fail(tomo_id, error.code)
        return self.registry.upsert(tomo_id, sandbox.id, self.snapshot, "ready")

    def _smoke_test(self, sandbox: SandboxHandle) -> None:
        try:
            output = self.daytona.exec(sandbox, SMOKE_COMMAND, env={"TOMO_DATA_DIR": self.data_dir}).output
        except DaytonaClientError as error:
            raise SandboxSupervisorError("sandbox_smoke_failed") from error
        if not any(line.startswith("TOMO_SANDBOX_RESULT:") for line in output.splitlines()):
            raise SandboxSupervisorError("sandbox_smoke_failed")

    def _fail(self, tomo_id: str, code: str) -> SandboxRecord:
        self.registry.upsert(tomo_id, None, self.snapshot, "error", code)
        raise SandboxSupervisorError(code)
