"""Host-side execution boundary for one sandboxed inbound turn."""

from __future__ import annotations

import json
import shlex
from collections import defaultdict
from threading import Lock
from typing import Callable

from .daytona_client import DaytonaClient, DaytonaClientError
from .grok_auth import GrokAuthStore
from .hosted_auth import HostedGrokAuth
from .models import InboundEnvelope, OutboundBubble
from .sandbox_protocol import encode_inbound
from .sandbox_registry import SandboxRegistry


DATA_DIR = "/home/daytona/.tomo"
RESULT_MARKER = "TOMO_SANDBOX_RESULT:"
_COMMAND = "tomo-core sandbox inbound --once"


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
                    self._command(encode_inbound(request_id, inbound)),
                    env={"TOMO_DATA_DIR": self.data_dir, "TOMO_SUPERGROK_ACCESS_TOKEN": token},
                )
            except DaytonaClientError as error:
                raise SandboxDispatchError("sandbox_exec_failed") from error
            except Exception as error:
                raise SandboxDispatchError("access_token_failed") from error
            return self._parse(result.output, request_id)

    @staticmethod
    def _command(payload: str) -> str:
        # Daytona's process API accepts a command string, so quote untrusted JSON before piping it to fixed argv.
        return f"printf %s {shlex.quote(payload)} | {_COMMAND}"

    @staticmethod
    def _parse(output: str, request_id: str) -> list[OutboundBubble]:
        marked = [line[len(RESULT_MARKER) :] for line in output.splitlines() if line.startswith(RESULT_MARKER)]
        if len(marked) != 1:
            raise SandboxDispatchError("invalid_result")
        try:
            message = json.loads(marked[0])
            if not isinstance(message, dict) or message.get("ok") is not True or message.get("request_id") != request_id:
                raise ValueError
            raw_bubbles = message["bubbles"]
            if not isinstance(raw_bubbles, list):
                raise ValueError
            bubbles = [OutboundBubble(**bubble) for bubble in raw_bubbles]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise SandboxDispatchError("invalid_result") from error
        if not 1 <= len(bubbles) <= 4 or any(not bubble.text or len(bubble.text) > 4096 for bubble in bubbles):
            raise SandboxDispatchError("invalid_result")
        return bubbles


def _fresh_hosted_access_token() -> str:
    """Refresh the host-managed credential immediately before a sandbox turn."""
    HostedGrokAuth.from_environment().bootstrap()
    token = GrokAuthStore().access_token()
    if not token:
        raise RuntimeError("hosted access token unavailable")
    return token
