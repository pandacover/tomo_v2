"""Idempotently reconcile the Daytona resources owned by one Tomo."""

from __future__ import annotations

import os
from collections import defaultdict
from threading import Lock
from typing import Protocol

from .daytona_client import DaytonaClient, DaytonaClientError, DaytonaNotFoundClientError, SandboxHandle
from .sandbox_registry import SandboxRecord, SandboxRegistry
from .models import InboundEnvelope, OutboundBubble
from .sandbox_protocol import encode_inbound, parse_result_marker


SMOKE_COMMAND = "/opt/tomo/.venv/bin/tomo-core sandbox-inbound --health"


class SandboxAuthBroker(Protocol):
    def access_token(self, force_refresh: bool = False) -> str:
        ...


class SandboxSupervisorError(RuntimeError):
    """A safe, stable reconciliation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"sandbox reconciliation failed: {code}")


class DaytonaSupervisor:
    _locks: defaultdict[str, Lock] = defaultdict(Lock)

    def __init__(
        self,
        registry: SandboxRegistry,
        daytona: DaytonaClient,
        auth_broker: SandboxAuthBroker,
        *,
        snapshot: str,
        data_dir: str,
        xai_model: str,
        xai_reasoning_effort: str,
    ) -> None:
        self.registry = registry
        self.daytona = daytona
        self.auth_broker = auth_broker
        self.snapshot = snapshot
        self.data_dir = data_dir
        self.xai_model = xai_model
        self.xai_reasoning_effort = xai_reasoning_effort

    def reconcile(self, tomo_id: str) -> SandboxRecord:
        """Return the ready persistent sandbox for ``tomo_id``, creating it when absent."""
        with self._locks[tomo_id]:
            record = self.registry.get(tomo_id)
            if record is not None and record.snapshot != self.snapshot:
                if not self._delete_recorded_sandbox(record):
                    return self._fail(tomo_id, "sandbox_delete_failed")
                return self._create(tomo_id)
            if record is not None and record.status == "ready" and record.sandbox_id:
                try:
                    sandbox = self._recorded_sandbox(record)
                except DaytonaClientError:
                    return self._fail(tomo_id, "sandbox_lookup_failed")
                if sandbox is None:
                    return self._create(tomo_id)
                if sandbox.snapshot is not None and sandbox.snapshot != self.snapshot:
                    if not self._delete(sandbox):
                        return self._fail(tomo_id, "sandbox_delete_failed")
                    return self._create(tomo_id)
                if sandbox.state == "stopped":
                    try:
                        self.daytona.start(sandbox)
                        self._smoke_test(sandbox, tomo_id)
                    except SandboxSupervisorError as error:
                        self._delete(sandbox)
                        return self._fail(tomo_id, error.code)
                    except DaytonaClientError:
                        return self._fail(tomo_id, "sandbox_create_failed")
                elif sandbox.state not in (None, "started"):
                    if not self._delete(sandbox):
                        return self._fail(tomo_id, "sandbox_delete_failed")
                    return self._create(tomo_id)
                return self.registry.upsert(tomo_id, sandbox.id, self.snapshot, "ready")
            return self._create(tomo_id)

    def _create(self, tomo_id: str) -> SandboxRecord:
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
        except DaytonaClientError:
            sandbox = self._sandbox_named(record.sandbox_name)
            if sandbox is None:
                return self._fail(tomo_id, "sandbox_create_failed")
        try:
            if sandbox.state == "stopped" or sandbox.snapshot is None:
                self.daytona.start(sandbox)
            self._smoke_test(sandbox, tomo_id)
        except DaytonaClientError:
            return self._fail(tomo_id, "sandbox_create_failed")
        except SandboxSupervisorError as error:
            self._delete(sandbox)
            return self._fail(tomo_id, error.code)
        return self.registry.upsert(tomo_id, sandbox.id, self.snapshot, "ready")

    def _recorded_sandbox(self, record: SandboxRecord) -> SandboxHandle | None:
        try:
            return self.daytona.get(record.sandbox_id)
        except DaytonaNotFoundClientError:
            return self._sandbox_named(record.sandbox_name)

    def _sandbox_named(self, name: str) -> SandboxHandle | None:
        try:
            return self.daytona.get(name)
        except DaytonaNotFoundClientError:
            return None

    def _delete_recorded_sandbox(self, record: SandboxRecord) -> bool:
        try:
            sandbox = self._recorded_sandbox(record) if record.sandbox_id else self._sandbox_named(record.sandbox_name)
        except DaytonaClientError:
            return False
        if sandbox is not None:
            return self._delete(sandbox)
        return True

    def _delete(self, sandbox: SandboxHandle) -> bool:
        try:
            self.daytona.delete(sandbox)
        except DaytonaClientError:
            return False
        return True

    def _smoke_test(self, sandbox: SandboxHandle, tomo_id: str) -> None:
        request_id = "health-1"
        health_inbound = InboundEnvelope(connector="telegram", actor_id="health", message_id="health", text="health")
        try:
            output = self.daytona.exec(
                sandbox,
                SMOKE_COMMAND,
                env={
                    "TOMO_INBOUND_JSON": encode_inbound(request_id, health_inbound),
                    "TOMO_CORE_DATA_DIR": self.data_dir,
                    "TOMO_INSTANCE_ID": tomo_id,
                    "TOMO_SUPERGROK_ACCESS_TOKEN": self.auth_broker.access_token(),
                    "TOMO_CORE_SOUL": "/opt/tomo/SOUL.md",
                    "TOMO_XAI_MODEL": os.getenv("TOMO_XAI_MODEL", self.xai_model),
                    "TOMO_XAI_REASONING_EFFORT": os.getenv("TOMO_XAI_REASONING_EFFORT", self.xai_reasoning_effort),
                },
            ).output
        except DaytonaClientError as error:
            raise SandboxSupervisorError("sandbox_smoke_failed") from error
        try:
            parse_result_marker(output, request_id)
        except ValueError:
            raise SandboxSupervisorError("sandbox_smoke_failed")

    def _fail(self, tomo_id: str, code: str) -> SandboxRecord:
        self.registry.upsert(tomo_id, None, self.snapshot, "error", code)
        raise SandboxSupervisorError(code)
