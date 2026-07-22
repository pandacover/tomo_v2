"""Single-turn stdin entrypoint for a sandboxed Tomo runtime."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import os
import time
from datetime import datetime, timezone
from typing import TextIO
import traceback

import httpx

from .conversation.parsing import ConversationOutputError
from .cron_tools import CronApiClient, cron_registry
from .peer_tools import PeerApiClient, peer_registry
from .tools import ToolRegistry
from .models import AutomationTurn, OutboundBubble, PeerTurn, RuntimeConfig
from .providers import ProviderAdapter
from .runtime import PersonalAgentRuntime, RuntimeCompleted, RuntimeFrameReady, RuntimeReactionReady, StaleSessionRevisionError
from .vision import VisionInterpreter
from .sandbox_protocol import EVENT_MARKER, SandboxErrorEvent, SandboxStaleEvent, SandboxTracebackFrame, decode_turn, encode_event
from . import latency_trace


_MAX_FAILURE_EVENT_CHARS = 900
_MAX_EXCEPTION_CLASS_CHARS = 64
_MAX_TRACEBACK_BASENAME_CHARS = 48
_MAX_TRACEBACK_FUNCTION_CHARS = 64


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

    def react_to_message(self, actor_id: str, message_id: str, emoji: str) -> None:
        pass


def build_runtime(provider: ProviderAdapter, config: RuntimeConfig, *, generation_id: str | None = None, automation: bool = False, peer: bool = False, vision_interpreter: VisionInterpreter | None = None) -> PersonalAgentRuntime:
    if config.owner_id is None:
        raise SandboxInboundError("missing_owner_id")
    config = replace(config, local_work_dir=config.local_work_dir or "/tmp/tomo-core-sqlite")
    tools = ToolRegistry()
    control_url = os.getenv("TOMO_CRON_CONTROL_URL")
    capability = os.getenv("TOMO_CRON_CAPABILITY")
    context = (os.getenv("TOMO_CRON_OWNER_ID"), os.getenv("TOMO_CRON_ACTOR_ID"), os.getenv("TOMO_CRON_DESTINATION"), os.getenv("TOMO_CRON_SESSION_ID"))
    if not automation and not peer and generation_id and control_url and capability and all(context):
        tools = tools.extend(cron_registry(CronApiClient(control_url, capability, *context), generation_id))
    peer_control_url = os.getenv("TOMO_PEER_CONTROL_URL")
    peer_capability = os.getenv("TOMO_PEER_CAPABILITY")
    peer_context = (os.getenv("TOMO_PEER_OWNER_ID"), os.getenv("TOMO_PEER_ACTOR_ID"), os.getenv("TOMO_PEER_DESTINATION"), os.getenv("TOMO_PEER_SESSION_ID"), os.getenv("TOMO_PEER_GENERATION_ID"))
    if not automation and not peer and generation_id and peer_control_url and peer_capability and all(peer_context) and peer_context[-1] == generation_id:
        tools = tools.extend(peer_registry(PeerApiClient(peer_control_url, peer_capability, *peer_context)))
    return PersonalAgentRuntime(provider=provider, telegram=CollectingTelegramSink(), config=config, tool_registry=tools, vision_interpreter=vision_interpreter)


def run_once(
    stdin: TextIO,
    stdout: TextIO,
    *,
    config: RuntimeConfig,
    provider: ProviderAdapter,
    vision_interpreter: VisionInterpreter | None = None,
    secret_values: tuple[str, ...] = (),
) -> int:
    """Read one protocol envelope, run it locally, and emit incremental v3 frames."""
    request_id = "unknown"
    generation_id = "unknown"
    sequence = 0
    try:
        request_id, turn = decode_turn(stdin.read())
        generation_id = turn.generation_id
    except Exception as error:
        _raise_failure(stdout, "invalid_request", error, secret_values, request_id, generation_id)

    try:
        sink_token = latency_trace.bind_sandbox_sink(stdout.write)
        # The host measures PTY-ready through receipt of this entry marker.
        latency_trace.emit_sandbox("sandbox_runtime_entry", elapsed_ms=0)
        build_started_at = time.monotonic()
        runtime_kwargs = {"generation_id": generation_id}
        if isinstance(turn, AutomationTurn):
            runtime_kwargs["automation"] = True
        elif isinstance(turn, PeerTurn):
            runtime_kwargs["peer"] = True
        if vision_interpreter is not None:
            runtime_kwargs["vision_interpreter"] = vision_interpreter
        runtime = build_runtime(provider, config, **runtime_kwargs)
        latency_trace.emit_sandbox("sandbox_runtime_build", elapsed_ms=max(0, int((time.monotonic() - build_started_at) * 1000)))
        if isinstance(turn, PeerTurn):
            expires_at = datetime.fromisoformat(turn.expires_at.replace("Z", "+00:00"))
            iterator = runtime.handle_peer_turn_iter(
                turn, is_active=lambda: datetime.now(timezone.utc) < expires_at
            )
        else:
            iterator = runtime.handle_automation_turn_iter(turn) if isinstance(turn, AutomationTurn) else runtime.handle_telegram_burst_iter(turn)
        for event in iterator:
            if not isinstance(event, (RuntimeReactionReady, RuntimeFrameReady, RuntimeCompleted)):
                raise TypeError("runtime emitted an unsupported sandbox event")
            expected_reaction_binding = None
            if isinstance(event, RuntimeReactionReady):
                if isinstance(turn, (AutomationTurn, PeerTurn)):
                    raise TypeError("automation turns cannot emit reactions")
                expected_reaction_binding = (
                    config.owner_id,
                    turn.latest.actor_id,
                    str(turn.latest.native_metadata.get("chat_id") or turn.latest.actor_id),
                    turn.latest.message_id,
                    turn.generation_id,
                    turn.revision,
                )
            _write_payload(stdout, encode_event(request_id, generation_id, sequence, event, expected_reaction_binding=expected_reaction_binding))
            sequence += 1
            if isinstance(event, RuntimeCompleted):
                return 0
        _write_payload(stdout, encode_event(request_id, generation_id, sequence, SandboxErrorEvent(sequence, "runtime_missing_terminal")))
        return 1
    except StaleSessionRevisionError as error:
        _write_payload(stdout, encode_event(request_id, generation_id, sequence, SandboxStaleEvent(sequence, error.current_revision)))
        return 0
    except ConversationOutputError as error:
        code = _safe_diagnostic_name(error.code, _MAX_EXCEPTION_CLASS_CHARS, secret_values)
        _raise_failure(stdout, code, error, secret_values, request_id, generation_id, sequence)
    except httpx.HTTPStatusError as error:
        code = "auth_expired" if error.response.status_code == 401 else "provider_failed"
        _raise_failure(stdout, code, error, secret_values, request_id, generation_id, sequence)
    except Exception as error:
        _raise_failure(stdout, "runtime_failed", error, secret_values, request_id, generation_id, sequence)
    finally:
        if "sink_token" in locals():
            latency_trace.reset_sandbox_sink(sink_token)


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
    frames = tuple(
        SandboxTracebackFrame(
            basename=_safe_diagnostic_name(
                os.path.basename(frame.filename.replace("\\", "/")), _MAX_TRACEBACK_BASENAME_CHARS, secret_values
            ),
            function=_safe_diagnostic_name(frame.name, _MAX_TRACEBACK_FUNCTION_CHARS, secret_values),
            line=frame.lineno,
        )
        for frame in traceback.extract_tb(error.__traceback__)[-12:]
    )
    _write_payload(
        stdout,
        _failure_payload(
            request_id,
            generation_id,
            sequence,
            code,
            _safe_diagnostic_name(type(error).__name__, _MAX_EXCEPTION_CLASS_CHARS, secret_values),
            frames,
        ),
    )
    raise SandboxInboundError(code) from None


def _failure_payload(
    request_id: str,
    generation_id: str,
    sequence: int,
    code: str,
    exception_class: str,
    frames: tuple[SandboxTracebackFrame, ...],
) -> str:
    while frames:
        payload = encode_event(
            request_id,
            generation_id,
            sequence,
            SandboxErrorEvent(sequence, code, exception_class=exception_class, traceback=frames),
        )
        if len(EVENT_MARKER) + len(payload) <= _MAX_FAILURE_EVENT_CHARS:
            return payload
        frames = frames[1:]
    payload = encode_event(request_id, generation_id, sequence, SandboxErrorEvent(sequence, code, exception_class=exception_class))
    if len(EVENT_MARKER) + len(payload) <= _MAX_FAILURE_EVENT_CHARS:
        return payload
    return encode_event(request_id, generation_id, sequence, SandboxErrorEvent(sequence, code))


def _safe_diagnostic_name(value: str, maximum: int, secret_values: tuple[str, ...]) -> str:
    for secret in secret_values:
        if secret:
            value = value.replace(secret, "_")
    safe = "".join(character if character.isascii() and (character.isalnum() or character == "_") else "_" for character in value)
    return (safe[:maximum] or "unknown").lstrip("0123456789") or "unknown"


def _write_payload(stdout: TextIO, payload: str) -> None:
    stdout.write(f"{EVENT_MARKER}{payload}\n")
    stdout.flush()
