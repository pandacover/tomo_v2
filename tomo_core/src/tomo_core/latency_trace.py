"""Temporary, privacy-safe hosted latency telemetry."""

from __future__ import annotations

import hashlib
import hmac
import os
import sys


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
})
_OUTCOMES = frozenset({"ok", "error", "send_complete", "origin_to_delivery"})
_COUNTS = ("attempt", "model_segments", "tool_rounds", "tool_calls", "contract_repairs", "visible_segments")


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


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
