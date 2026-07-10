"""Execution-only Telegram dispatch for a Daytona sandbox."""

from __future__ import annotations

from collections import defaultdict
from threading import Lock
from typing import Protocol

from .daytona_client import DaytonaClient, DaytonaClientError
from .daytona_supervisor import DaytonaSupervisor
from .models import InboundEnvelope, OutboundBubble
from .onboarding_store import TelegramInstallation
from .sandbox_protocol import SandboxProtocolError, encode_inbound, parse_result_marker


DATA_DIR = "/home/daytona/.tomo"
_COMMAND = "/opt/tomo/.venv/bin/tomo-core sandbox-inbound"
_EXEC_TIMEOUT_SECONDS = 120


class RailwayAuthBroker(Protocol):
    def access_token(self, force_refresh: bool = False) -> str:
        ...


class TelegramRuntimeDispatchError(RuntimeError):
    """A safe failure while dispatching a trusted Telegram update."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"telegram runtime dispatch failed: {code}")


class SandboxDispatchError(TelegramRuntimeDispatchError):
    pass


class SandboxDispatch:
    """Runs a trusted Telegram turn in Daytona without performing Telegram I/O."""

    _locks: defaultdict[str, Lock] = defaultdict(Lock)

    def __init__(
        self,
        supervisor: DaytonaSupervisor,
        client: DaytonaClient,
        auth_broker: RailwayAuthBroker,
        *,
        data_dir: str = DATA_DIR,
    ) -> None:
        self.supervisor = supervisor
        self.client = client
        self.auth_broker = auth_broker
        self.data_dir = data_dir

    def ensure_worker(self, installation: TelegramInstallation) -> None:
        self.supervisor.reconcile(installation.tomo_id)

    def deliver_telegram(
        self, installation: TelegramInstallation, update_id: int, envelope: InboundEnvelope
    ) -> list[OutboundBubble]:
        request_id = f"telegram:update:{update_id}"
        with self._locks[installation.tomo_id]:
            record = self.supervisor.reconcile(installation.tomo_id)
            if not record.sandbox_id:
                raise SandboxDispatchError("sandbox_not_ready")
            try:
                bubbles = self._execute(record.sandbox_id, installation.tomo_id, request_id, envelope)
            except SandboxProtocolError as error:
                if error.code != "auth_expired":
                    raise SandboxDispatchError(error.code) from error
                try:
                    bubbles = self._execute(record.sandbox_id, installation.tomo_id, request_id, envelope, force_refresh=True)
                except SandboxProtocolError as retry_error:
                    raise SandboxDispatchError(retry_error.code) from retry_error
            return bubbles

    def _execute(
        self, sandbox_id: str, tomo_id: str, request_id: str, envelope: InboundEnvelope, *, force_refresh: bool = False
    ) -> list[OutboundBubble]:
        try:
            sandbox = self.client.get(sandbox_id)
            token = self.auth_broker.access_token(force_refresh=force_refresh)
            result = self.client.exec(
                sandbox,
                _COMMAND,
                env={
                    "TOMO_INBOUND_JSON": encode_inbound(request_id, envelope),
                    "TOMO_CORE_DATA_DIR": self.data_dir,
                    "TOMO_INSTANCE_ID": tomo_id,
                    "TOMO_SUPERGROK_ACCESS_TOKEN": token,
                },
                timeout=_EXEC_TIMEOUT_SECONDS,
            )
        except TimeoutError as error:
            raise SandboxDispatchError("sandbox_timeout") from error
        except DaytonaClientError as error:
            raise SandboxDispatchError("sandbox_exec_failed") from error
        except SandboxDispatchError:
            raise
        except Exception as error:
            raise SandboxDispatchError("access_token_failed") from error
        if result.exit_code != 0:
            raise SandboxDispatchError("sandbox_timeout" if result.exit_code == 124 else "sandbox_exec_failed")
        try:
            return parse_result_marker(result.output, request_id)
        except SandboxProtocolError:
            raise
        except ValueError as error:
            raise SandboxDispatchError("invalid_result") from error
