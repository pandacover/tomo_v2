"""Single-turn stdin entrypoint for a sandboxed Tomo runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TextIO

import httpx

from .models import OutboundBubble, RuntimeConfig
from .providers import ProviderAdapter
from .runtime import PersonalAgentRuntime
from .sandbox_protocol import RESULT_MARKER, decode_inbound, encode_error, encode_result


class SandboxInboundError(RuntimeError):
    """A safe, machine-readable failure from the sandbox stdin boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"sandbox inbound failed: {code}")


@dataclass
class CollectingTelegramSink:
    """Captures delivery locally; sandbox turns must never call Telegram."""

    bubbles: list[OutboundBubble] = field(default_factory=list)

    def start_typing(self, actor_id: str) -> None:
        pass

    def send_bubbles(self, actor_id: str, bubbles: list[OutboundBubble]) -> None:
        self.bubbles.extend(bubbles)


def build_runtime(provider: ProviderAdapter, config: RuntimeConfig) -> PersonalAgentRuntime:
    return PersonalAgentRuntime(provider=provider, telegram=CollectingTelegramSink(), config=config)


def run_once(
    stdin: TextIO,
    stdout: TextIO,
    *,
    config: RuntimeConfig,
    provider: ProviderAdapter,
    secret_values: tuple[str, ...] = (),
) -> int:
    """Read one protocol envelope, run it locally, and emit exactly one result."""
    try:
        request_id, envelope = decode_inbound(stdin.read())
    except Exception as error:
        _raise_failure(stdout, "invalid_request", error, secret_values, "unknown")

    try:
        runtime = build_runtime(provider, config)
        runtime.handle_telegram_text(envelope)
        _write_payload(stdout, encode_result(request_id, runtime.telegram.bubbles))
        return 0
    except httpx.HTTPStatusError as error:
        code = "auth_expired" if error.response.status_code == 401 else "provider_failed"
        _raise_failure(stdout, code, error, secret_values, request_id)
    except Exception as error:
        _raise_failure(stdout, "runtime_failed", error, secret_values, request_id)


def emit_failure(stdout: TextIO, code: str, request_id: str = "unknown") -> None:
    _write_payload(stdout, encode_error(request_id, code))


def _raise_failure(stdout: TextIO, code: str, error: Exception, secret_values: tuple[str, ...], request_id: str) -> None:
    # Never serialize or surface exception text: providers and HTTP libraries can include credentials.
    emit_failure(stdout, code, request_id)
    raise SandboxInboundError(code) from None


def _write_payload(stdout: TextIO, payload: str) -> None:
    stdout.write(f"{RESULT_MARKER}{payload}\n")
    stdout.flush()
