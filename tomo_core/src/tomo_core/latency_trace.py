"""Temporary, privacy-safe hosted latency telemetry."""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
from contextvars import ContextVar


_PHASES = frozenset({
    "dispatch_start",
    "telegram_origin_to_worker_start",
    "telegram_queue_wait",
    "telegram_first_delivery",
    "sandbox_reconcile",
    "sandbox_lookup",
    "oauth_access",
    "pty_ready",
    "sandbox_first_frame",
    "sandbox_completed",
    "sandbox_runtime_entry",
    "sandbox_context_hydration",
    "sandbox_provider_attempt",
    "sandbox_provider_attempt_start",
    "sandbox_provider_first_text_delta",
    "sandbox_provider_move_plan_validated",
    "sandbox_provider_first_frame_validated",
    "sandbox_provider_stream_completed",
    "sandbox_tool_batch",
    "sandbox_checkpoint_inbound",
    "sandbox_checkpoint_frame",
    "sandbox_checkpoint_complete",
    "sandbox_runtime_build",
    "sandbox_session_load",
    "sandbox_memory_hydration",
    "sandbox_prompt_prepare",
    "sandbox_attachment_fetch",
    "sandbox_image_normalize",
    "sandbox_vision_provider_attempt",
    "sandbox_vision_observation_ready",
})
_OUTCOMES = frozenset({"ok", "error", "send_complete", "origin_to_delivery"})
_COUNTS = ("attempt", "segment", "repair", "model_segments", "tool_rounds", "tool_calls", "contract_repairs", "visible_segments", "suspended_ms", "active_ms", "input_tokens", "output_tokens", "reasoning_tokens", "output_chars_through_first_frame", "first_frame_chars", "plan_model", "plan_normalized", "plan_synthesized", "image_count", "input_bytes", "normalized_bytes", "width", "height")
SANDBOX_LATENCY_MARKER = "TOMO_SANDBOX_LATENCY_V1="
_sandbox_sink: ContextVar[object | None] = ContextVar("sandbox_latency_sink", default=None)


def emit(source_id: str, phase: str, *, outcome: str = "ok", elapsed_ms: int = 0, **counts: int) -> None:
    """Write one fixed-schema trace line, or nothing when disabled/invalid."""
    if os.getenv("TOMO_LATENCY_TRACE") != "1":
        return
    try:
        key = os.getenv("TOMO_LATENCY_TRACE_KEY")
        if not isinstance(key, str) or len(key) < 32 or not isinstance(source_id, str):
            return
        if phase not in _PHASES or outcome not in _OUTCOMES or not _nonnegative_int(elapsed_ms):
            return
        if any(name not in _COUNTS or not _nonnegative_int(value) for name, value in counts.items()):
            return
        trace = hmac.new(key.encode("utf-8"), source_id.encode("utf-8"), hashlib.sha256).hexdigest()[:12]
        fields = [f"phase={phase}", f"outcome={outcome}", f"elapsed_ms={elapsed_ms}"]
        fields.extend(f"{name}={counts[name]}" for name in _COUNTS if name in counts)
        fields.append(f"trace={trace}")
        sys.stderr.write("[DEBUG-latency-v1] " + " ".join(fields) + "\n")
        sys.stderr.flush()
    except Exception:
        return


def bind_sandbox_sink(sink: object):
    """Install a request-scoped marker sink for sandbox-only telemetry."""
    return _sandbox_sink.set(sink)


def reset_sandbox_sink(token: object) -> None:
    _sandbox_sink.reset(token)


def emit_sandbox(phase: str, *, outcome: str = "ok", elapsed_ms: int = 0, **counts: int) -> None:
    """Forward fixed-schema sandbox timings; the host adds HMAC correlation."""
    if os.getenv("TOMO_LATENCY_TRACE") != "1":
        return
    try:
        sink = _sandbox_sink.get()
        if sink is None:
            return
        if phase not in _PHASES or outcome not in _OUTCOMES or not _nonnegative_int(elapsed_ms):
            return
        if any(name not in _COUNTS or not _nonnegative_int(value) for name, value in counts.items()):
            return
        fields = [f"phase={phase}", f"outcome={outcome}", f"elapsed_ms={elapsed_ms}"]
        fields.extend(f"{name}={counts[name]}" for name in _COUNTS if name in counts)
        sink(SANDBOX_LATENCY_MARKER + " ".join(fields) + "\n")
    except Exception:
        return


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
