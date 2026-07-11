"""Single-turn stdin entrypoint for a sandboxed Tomo runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TextIO

import httpx

from .models import OutboundBubble, RuntimeConfig
from .providers import ProviderAdapter
from .runtime import PersonalAgentRuntime
from .runtime import RuntimeCompleted, RuntimeUtteranceReady
from .sandbox_protocol import EVENT_MARKER, SandboxErrorEvent, decode_inbound, encode_event


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
    """Read one protocol envelope, run it locally, and emit incremental v2 events."""
    request_id = "unknown"
    generation_id = "unknown"
    sequence = 0
    try:
        request_id, burst = decode_inbound(stdin.read())
        generation_id = burst.generation_id
    except Exception as error:
        _raise_failure(stdout, "invalid_request", error, secret_values, request_id, generation_id)

    try:
        runtime = build_runtime(provider, config)
        for event in runtime.handle_telegram_burst_iter(burst):
            _write_payload(stdout, encode_event(request_id, generation_id, sequence, event))
            sequence += 1
            if isinstance(event, RuntimeCompleted):
                return 0
        return 0
    except httpx.HTTPStatusError as error:
        code = "auth_expired" if error.response.status_code == 401 else "provider_failed"
        _raise_failure(stdout, code, error, secret_values, request_id, generation_id, sequence)
    except Exception as error:
        _raise_failure(stdout, "runtime_failed", error, secret_values, request_id, generation_id, sequence)


def emit_failure(stdout: TextIO, code: str, request_id: str = "unknown") -> None:
    _write_payload(stdout, encode_event(request_id, "unknown", 0, SandboxErrorEvent(0, code)))


def _raise_failure(
    stdout: TextIO,
    code: str,
    error: Exception,
    secret_values: tuple[str, ...],
    request_id: str,
    generation_id: str,
    sequence: int = 0,
) -> None:
    # Never serialize or surface exception text: providers and HTTP libraries can include credentials.
    _write_payload(stdout, encode_event(request_id, generation_id, sequence, SandboxErrorEvent(sequence, code)))
    raise SandboxInboundError(code) from None


def _write_payload(stdout: TextIO, payload: str) -> None:
    stdout.write(f"{EVENT_MARKER}{payload}\n")
    stdout.flush()
