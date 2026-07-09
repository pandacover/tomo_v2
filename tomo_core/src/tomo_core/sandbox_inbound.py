"""Single-turn stdin entrypoint for a sandboxed Tomo runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TextIO

from .models import InboundEnvelope, RuntimeConfig
from .providers import ProviderAdapter
from .runtime import PersonalAgentRuntime
from .sandbox_protocol import decode_inbound

RESULT_MARKER = "TOMO_SANDBOX_RESULT:"


class SandboxInboundError(RuntimeError):
    """A safe, machine-readable failure from the sandbox stdin boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"sandbox inbound failed: {code}")


@dataclass
class _NoopTelegramSink:
    """Keeps the runtime's delivery interface local to this one-turn process."""

    def start_typing(self, actor_id: str) -> None:
        pass

    def send_bubbles(self, actor_id: str, bubbles: object) -> None:
        pass


def build_runtime(provider: ProviderAdapter, data_dir: str) -> PersonalAgentRuntime:
    return PersonalAgentRuntime(provider=provider, telegram=_NoopTelegramSink(), config=RuntimeConfig(data_dir=data_dir))


def run_once(
    stdin: TextIO,
    stdout: TextIO,
    *,
    data_dir: str,
    provider: ProviderAdapter,
    secret_values: tuple[str, ...] = (),
) -> int:
    """Read one protocol envelope, run it locally, and emit exactly one result."""
    try:
        request_id, envelope = decode_inbound(stdin.read())
    except Exception as error:
        _raise_failure(stdout, "invalid_inbound", error, secret_values)

    try:
        delivered = build_runtime(provider, data_dir).handle_telegram_text(envelope)
        _write_result(stdout, {"ok": True, "request_id": request_id, "bubbles": delivered})
        return 0
    except Exception as error:
        _raise_failure(stdout, "runtime_failed", error, secret_values)


def emit_failure(stdout: TextIO, code: str) -> None:
    _write_result(stdout, {"ok": False, "error": {"code": code}})


def _raise_failure(stdout: TextIO, code: str, error: Exception, secret_values: tuple[str, ...]) -> None:
    # Never serialize or surface exception text: providers and HTTP libraries can include credentials.
    emit_failure(stdout, code)
    raise SandboxInboundError(code) from _safe_cause(error, secret_values)


def _safe_cause(error: Exception, secret_values: tuple[str, ...]) -> Exception:
    message = str(error)
    for secret in secret_values:
        message = message.replace(secret, "[redacted]")
    return RuntimeError(message)


def _write_result(stdout: TextIO, result: dict[str, object]) -> None:
    stdout.write(f"{RESULT_MARKER}{json.dumps(result, separators=(',', ':'), ensure_ascii=True)}\n")
    stdout.flush()
