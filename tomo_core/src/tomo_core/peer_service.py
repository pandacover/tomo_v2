from __future__ import annotations

import logging
from datetime import datetime, timedelta
from threading import Event, Thread
from typing import Callable

from .models import PeerTurn
from .peer_exchange import PeerExchange
from .peer_safety import contains_unsafe_content, render_peer_response
from .peer_store import utc_now
from .sandbox_protocol import (
    SandboxCompletedEvent,
    SandboxErrorEvent,
    SandboxFrameEvent,
    SandboxStaleEvent,
    SandboxTracebackFrame,
)


_logger = logging.getLogger(__name__)
_SAFE_DISPATCH_CODES = frozenset(
    {
        "access_token_failed",
        "auth_expired",
        "invalid_result",
        "provider_failed",
        "provider_stream_failure",
        "runtime_failed",
        "runtime_missing_terminal",
        "sandbox_create_failed",
        "sandbox_delete_failed",
        "sandbox_exec_failed",
        "sandbox_lookup_failed",
        "sandbox_not_ready",
        "sandbox_smoke_failed",
        "sandbox_timeout",
        "volume_create_failed",
    }
)


class PeerService:
    """Durable peer request worker; responses remain in PeerExchange, never Telegram."""

    def __init__(
        self,
        exchange: PeerExchange,
        installations: object,
        dispatch: object,
        *,
        send_notice: Callable | None = None,
        clock: Callable = utc_now,
        poll_seconds: float = 1.0,
    ) -> None:
        self.exchange = exchange
        self.installations = installations
        self.dispatch = dispatch
        self.send_notice = send_notice
        self.clock = clock
        self.poll_seconds = poll_seconds
        self._stop = Event()
        self._thread: Thread | None = None
        self._last_pruned_at: datetime | None = None

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.run_once()
            except Exception:
                # The durable leases are the recovery mechanism. A single storage,
                # transport, or clock failure must not silently kill the worker.
                continue

    def run_once(self) -> bool:
        if self._stop.is_set():
            return False
        now = self.clock()
        if (
            self._last_pruned_at is None
            or now - self._last_pruned_at >= timedelta(days=1)
        ):
            try:
                self.exchange.prune(now=now)
            except Exception:
                pass
            finally:
                self._last_pruned_at = now
        notice = self.exchange.claim_notice(now=now, lease_seconds=180)
        if notice is not None:
            recipient = self.exchange.notice_recipient(
                notice.pending_id, notice.lease_token, now=now
            )
            installation = (
                None
                if recipient is None
                else self.installations.installation_for_tomo(recipient)
            )
            if installation is None:
                self.exchange.complete_notice(
                    notice.pending_id, notice.lease_token, "pending", now=now
                )
                return True
            with self.exchange.notice_delivery_guard(
                notice, now=self.clock()
            ) as source_active:
                if not source_active or not self.exchange.begin_notice_attempt(
                    notice, now=self.clock()
                ):
                    return True
                try:
                    if self.send_notice is None:
                        raise RuntimeError("notice_sender_missing")
                    self.send_notice(installation, notice.text)
                except Exception:
                    self.exchange.complete_notice(
                        notice.pending_id,
                        notice.lease_token,
                        "unknown",
                        now=self.clock(),
                    )
                else:
                    self.exchange.complete_notice(
                        notice.pending_id,
                        notice.lease_token,
                        "sent",
                        now=self.clock(),
                    )
            return True
        claim = self.exchange.claim(now=now, lease_seconds=15)
        if claim is None:
            return False
        installation = self.installations.installation_for_tomo(
            claim.request.recipient_owner_id
        )
        if installation is None:
            error_code = "peer_installation_missing"
            deferred = self.exchange.defer(
                claim.request.request_id,
                claim.lease_token,
                error_code=error_code,
                now=now,
            )
            if not deferred:
                self.exchange.fail(
                    claim.request.recipient_owner_id,
                    claim.request.request_id,
                    claim.lease_token,
                    ("unable to answer right now",),
                    relationship_revision=claim.relationship_revision,
                    grant_revisions=claim.grant_revisions,
                    error_code=error_code,
                    now=now,
                )
            self._log_failure(claim, error_code, deferred=deferred)
            return True
        inspection = self.exchange.inspect_request(
            claim.request.recipient_owner_id, claim.request.request_id
        )
        execution_context = self.exchange.request_execution_context(claim.request.request_id)
        if execution_context is None:
            return True
        purpose, execution_deadline = execution_context
        turn = PeerTurn(
            claim.request.request_id,
            claim.request.thread_sequence,
            claim.request.relationship_id,
            claim.request.thread_id,
            claim.request.request_id,
            inspection.peer_handle,
            purpose,
            claim.request.kind.value,
            claim.request.text,
            execution_deadline.isoformat(),
            self.exchange.prior_completed_exchanges(claim.request.request_id),
            claim.request.disclosure_scope,
        )
        frames: list[str] = []
        completed = False

        def active() -> bool:
            return not self._stop.is_set() and self.exchange.claim_is_active(
                claim.request.request_id, claim.lease_token, now=self.clock(),
                relationship_revision=claim.relationship_revision, grant_revisions=claim.grant_revisions,
            )

        heartbeat_stop = Event()

        def renew_lease() -> None:
            while not heartbeat_stop.wait(5):
                if not self.exchange.heartbeat(
                    claim.request.request_id, claim.lease_token, now=self.clock(),
                    lease_seconds=15,
                    relationship_revision=claim.relationship_revision,
                    grant_revisions=claim.grant_revisions,
                ):
                    heartbeat_stop.set()
                    return

        try:
            if not self.exchange.heartbeat(
                claim.request.request_id,
                claim.lease_token,
                now=self.clock(),
                lease_seconds=15,
                relationship_revision=claim.relationship_revision,
                grant_revisions=claim.grant_revisions,
            ):
                return True
            heartbeat = Thread(target=renew_lease, daemon=True)
            heartbeat.start()
            for event in self.dispatch.iter_peer_events(
                installation,
                turn,
                turn.generation_id,
                turn.session_key,
                is_active=active,
            ):
                if not active():
                    return True
                if not self.exchange.heartbeat(
                    claim.request.request_id,
                    claim.lease_token,
                    now=self.clock(),
                    lease_seconds=15,
                    relationship_revision=claim.relationship_revision,
                    grant_revisions=claim.grant_revisions,
                ):
                    return True
                if isinstance(event, SandboxFrameEvent):
                    if len(frames) == 3:
                        raise ValueError("too_many_frames")
                    typed = claim.request.kind.value != "ordinary_message"
                    if typed and frames:
                        raise ValueError("too_many_frames")
                    rendered = (
                        render_peer_response(claim.request.disclosure_scope, event.text)
                        if typed
                        else event.text
                    )
                    if rendered is None or contains_unsafe_content(rendered) or sum(len(frame) for frame in frames) + len(rendered) > 2000 or self.exchange.output_is_unsafe(claim.request.request_id, rendered):
                        raise ValueError("unsafe_content")
                    frames.append(rendered)
                elif isinstance(event, SandboxCompletedEvent):
                    completed = event.result.get("status") in {
                        "completed",
                        "completed_partial",
                    }
                elif isinstance(event, (SandboxErrorEvent, SandboxStaleEvent)):
                    raise RuntimeError("peer_execution_failed")
            if not completed or not frames:
                raise ValueError("peer_missing_completion")
            self.exchange.complete(
                claim.request.recipient_owner_id,
                claim.request.request_id,
                claim.lease_token,
                tuple(frames),
                relationship_revision=claim.relationship_revision,
                grant_revisions=claim.grant_revisions,
                now=self.clock(),
            )
        except ValueError as error:
            if active():
                error_code = _failure_code(error)
                self.exchange.fail(
                    claim.request.recipient_owner_id,
                    claim.request.request_id,
                    claim.lease_token,
                    ("unable to answer right now",),
                    relationship_revision=claim.relationship_revision,
                    grant_revisions=claim.grant_revisions,
                    error_code=error_code,
                    now=self.clock(),
                )
                self._log_failure(claim, error_code, deferred=False, error=error)
        except Exception as error:
            if active():
                error_code = _failure_code(error)
                deferred = self.exchange.defer(
                    claim.request.request_id,
                    claim.lease_token,
                    error_code=error_code,
                    now=self.clock(),
                )
                if not deferred:
                    self.exchange.fail(
                        claim.request.recipient_owner_id,
                        claim.request.request_id,
                        claim.lease_token,
                        ("unable to answer right now",),
                        relationship_revision=claim.relationship_revision,
                        grant_revisions=claim.grant_revisions,
                        error_code=error_code,
                        now=self.clock(),
                    )
                self._log_failure(claim, error_code, deferred=deferred, error=error)
        finally:
            heartbeat_stop.set()
            if 'heartbeat' in locals():
                heartbeat.join(timeout=1)
        return True

    @staticmethod
    def _log_failure(
        claim,
        error_code: str,
        *,
        deferred: bool,
        error: Exception | None = None,
    ) -> None:
        exception_class = getattr(error, "exception_class", None)
        raw_frames = getattr(error, "traceback_frames", ())
        traceback_frames = tuple(
            frame for frame in raw_frames if isinstance(frame, SandboxTracebackFrame)
        )
        diagnostics = ""
        if isinstance(exception_class, str):
            diagnostics += f" exception_class={exception_class}"
        if traceback_frames:
            diagnostics += " traceback=" + ",".join(
                f"{frame.basename}:{frame.function}:{frame.line}"
                for frame in traceback_frames
            )
        _logger.warning(
            "peer request execution %s request_id=%s recipient=%s attempt=%d error_code=%s%s",
            "deferred" if deferred else "failed",
            claim.request.request_id,
            claim.request.recipient_owner_id,
            claim.attempt_count,
            error_code,
            diagnostics,
        )


def _failure_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in _SAFE_DISPATCH_CODES:
        return code
    if isinstance(error, ValueError):
        return "peer_invalid_response"
    return "peer_execution_failed"
