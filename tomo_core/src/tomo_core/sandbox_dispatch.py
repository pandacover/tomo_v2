"""Host-side execution boundary for one sandboxed inbound turn."""

from __future__ import annotations

from collections import defaultdict
from threading import Lock
from typing import Callable

from .daytona_client import DaytonaClient, DaytonaClientError
from .hosted_auth import HostedGrokAuth
from .models import InboundEnvelope, OutboundBubble
from .sandbox_protocol import SandboxProtocolError, encode_inbound, parse_result_marker
from .sandbox_registry import SandboxRegistry


DATA_DIR = "/home/daytona/.tomo"
_COMMAND = "/opt/tomo/.venv/bin/tomo-core sandbox-inbound"


class SandboxDispatchError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"sandbox dispatch failed: {code}")


class SandboxDispatch:
    _locks: defaultdict[str, Lock] = defaultdict(Lock)

    def __init__(
        self,
        registry: SandboxRegistry,
        daytona: DaytonaClient,
        access_token: Callable[[], str] | None = None,
        *,
        data_dir: str = DATA_DIR,
    ) -> None:
        self.registry = registry
        self.daytona = daytona
        self.access_token = access_token or _fresh_hosted_access_token
        self.data_dir = data_dir

    def dispatch(self, tomo_id: str, request_id: str, inbound: InboundEnvelope) -> list[OutboundBubble]:
        with self._locks[tomo_id]:
            record = self.registry.get(tomo_id)
            if record is None or record.status != "ready" or not record.sandbox_id:
                raise SandboxDispatchError("sandbox_not_ready")
            try:
                sandbox = self.daytona.get(record.sandbox_id)
                token = self.access_token()
                result = self.daytona.exec(
                    sandbox,
                    _COMMAND,
                    env={
                        "TOMO_INBOUND_JSON": encode_inbound(request_id, inbound),
                        "TOMO_CORE_DATA_DIR": self.data_dir,
                        "TOMO_INSTANCE_ID": tomo_id,
                        "TOMO_SUPERGROK_ACCESS_TOKEN": token,
                    },
                )
            except DaytonaClientError as error:
                raise SandboxDispatchError("sandbox_exec_failed") from error
            except Exception as error:
                raise SandboxDispatchError("access_token_failed") from error
            return self._parse(result.output, request_id)

    @staticmethod
    def _parse(output: str, request_id: str) -> list[OutboundBubble]:
        try:
            return parse_result_marker(output, request_id)
        except SandboxProtocolError as error:
            raise SandboxDispatchError(error.code) from error
        except ValueError as error:
            raise SandboxDispatchError("invalid_result") from error


def _fresh_hosted_access_token() -> str:
    """Refresh the host-managed credential immediately before a sandbox turn."""
    return HostedGrokAuth.from_environment().access_token()
