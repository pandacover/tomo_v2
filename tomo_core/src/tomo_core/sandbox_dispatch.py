"""Execution-only Telegram dispatch for a Daytona sandbox."""

from __future__ import annotations

import os
import json
import logging
import re
import time
import traceback
from collections import defaultdict
from threading import Lock
from typing import Callable, Iterator, Protocol

from .daytona_client import DaytonaClient, DaytonaClientError, SessionCommandHandle
from .daytona_supervisor import DaytonaSupervisor
from .conversation import TurnBudget
from .models import AutomationTurn, InboundEnvelope, InboundMessage, InputBurst, OutboundBubble, RuntimeConfig
from .telegram import photo_attachments_from_message, reply_context_from_message
from .onboarding_store import InterruptedGeneration, TelegramGenerationInput, TelegramGenerationWork, TelegramInstallation
from .sandbox_protocol import SandboxCompletedEvent, SandboxErrorEvent, SandboxEvent, SandboxFrameEvent, SandboxProtocolError, encode_automation, encode_inbound, iter_event_markers, parse_result_marker
from . import latency_trace
from .cron_capability import CronCapability, issue_capability


_COMMAND = "/opt/tomo/.venv/bin/tomo-core sandbox-inbound"
_EXEC_TIMEOUT_SECONDS = 120
_logger = logging.getLogger(__name__)


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
        data_dir: str,
        xai_model: str,
        xai_reasoning_effort: str,
        budget: TurnBudget | None = None,
        control_url: str | None = None,
        capability_key: bytes | None = None,
    ) -> None:
        self.supervisor = supervisor
        self.client = client
        self.auth_broker = auth_broker
        self.data_dir = data_dir
        self.xai_model = xai_model
        self.xai_reasoning_effort = xai_reasoning_effort
        self.budget = budget or RuntimeConfig().tool_turn_budget
        self.control_url = control_url
        self.capability_key = capability_key

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
                bubbles = self._execute(record.sandbox_id, installation, request_id, envelope)
            except SandboxProtocolError as error:
                if error.code != "auth_expired":
                    raise SandboxDispatchError(error.code) from error
                try:
                    bubbles = self._execute(record.sandbox_id, installation, request_id, envelope, force_refresh=True)
                except SandboxProtocolError as retry_error:
                    raise SandboxDispatchError(retry_error.code) from retry_error
            return bubbles

    def iter_telegram_events(
        self, installation: TelegramInstallation, work: TelegramGenerationWork, is_active: Callable[[], bool] | None = None
    ) -> Iterator[SandboxEvent]:
        # Scheduled turns share this owner lock, so a sandbox never has two session turns racing.
        with self._locks[installation.tomo_id]:
            yield from self._iter_telegram_events(installation, work, is_active)

    def _iter_telegram_events(
        self, installation: TelegramInstallation, work: TelegramGenerationWork, is_active: Callable[[], bool] | None = None
    ) -> Iterator[SandboxEvent]:
        is_active = is_active or (lambda: True)
        if not is_active():
            return
        dispatch_started_at = time.monotonic()
        latency_trace.emit(work.burst_id, "dispatch_start", elapsed_ms=0)
        reconcile_started_at = time.monotonic()
        record = self.supervisor.reconcile(installation.tomo_id)
        latency_trace.emit(work.burst_id, "sandbox_reconcile", elapsed_ms=max(0, int((time.monotonic() - reconcile_started_at) * 1000)))
        if not record.sandbox_id:
            raise SandboxDispatchError("sandbox_not_ready")
        lookup_started_at = time.monotonic()
        sandbox = self.client.get(record.sandbox_id)
        latency_trace.emit(work.burst_id, "sandbox_lookup", elapsed_ms=max(0, int((time.monotonic() - lookup_started_at) * 1000)))
        request_id = _request_id(work.generation_id)
        session_id = work.session_id
        burst = burst_from_work(installation, work)
        yielded = 0
        any_frame = False
        force_refresh = False
        for attempt in range(2):
            command: SessionCommandHandle | None = None
            try:
                if not is_active():
                    return
                oauth_started_at = time.monotonic()
                token = self.auth_broker.access_token(force_refresh=force_refresh)
                latency_trace.emit(work.burst_id, "oauth_access", elapsed_ms=max(0, int((time.monotonic() - oauth_started_at) * 1000)), attempt=attempt + 1)
                pty_started_at = time.monotonic()
                command = self.client.start_session_command(
                    sandbox,
                    session_id,
                    _COMMAND,
                    env={
                        "TOMO_INBOUND_JSON": encode_inbound(request_id, burst),
                        "TOMO_CORE_DATA_DIR": self.data_dir,
                        "TOMO_INSTANCE_ID": installation.tomo_id,
                        "TOMO_SUPERGROK_ACCESS_TOKEN": token,
                        "TOMO_CORE_SOUL": "/opt/tomo/SOUL.md",
                        "TOMO_XAI_MODEL": os.getenv("TOMO_XAI_MODEL", self.xai_model),
                        "TOMO_XAI_REASONING_EFFORT": os.getenv("TOMO_XAI_REASONING_EFFORT", self.xai_reasoning_effort),
                        **self._interactive_cron_env(installation),
                        **_latency_env(),
                    },
                    timeout=_EXEC_TIMEOUT_SECONDS,
                )
                latency_trace.emit(work.burst_id, "pty_ready", elapsed_ms=max(0, int((time.monotonic() - pty_started_at) * 1000)), attempt=attempt + 1)
                runtime_entry_started_at = time.monotonic()

                def forward_latency(phase: str, outcome: str, elapsed_ms: int, counts: dict[str, int]) -> None:
                    if not is_active():
                        return
                    # This host-owned interval is PTY ready through runtime entry.
                    if phase == "sandbox_runtime_entry":
                        elapsed_ms = max(0, int((time.monotonic() - runtime_entry_started_at) * 1000))
                    latency_trace.emit(work.burst_id, phase, outcome=outcome, elapsed_ms=elapsed_ms, **counts)

                for event in iter_event_markers(
                    self.client.iter_session_logs(sandbox, command),
                    expected_request_id=request_id,
                    expected_generation_id=work.generation_id,
                    budget=self.budget,
                    expected_reaction_binding=(installation.tomo_id, installation.actor_id, installation.chat_id, str(work.inputs[-1].message_id), work.revision),
                    on_latency=forward_latency,
                ):
                    if not is_active():
                        return
                    if isinstance(event, SandboxErrorEvent):
                        _logger.warning(
                            "sandbox runtime failure code=%s exception_class=%s traceback=%s",
                            event.code,
                            event.exception_class,
                            [(frame.basename, frame.function, frame.line) for frame in event.traceback],
                        )
                        if event.code == "auth_expired" and yielded == 0 and attempt == 0:
                            force_refresh = True
                            break
                        raise SandboxDispatchError(event.code)
                    yielded += 1
                    if not is_active():
                        return
                    if isinstance(event, SandboxFrameEvent) and not any_frame:
                        latency_trace.emit(work.burst_id, "sandbox_first_frame", elapsed_ms=max(0, int((time.monotonic() - dispatch_started_at) * 1000)))
                        any_frame = True
                    if isinstance(event, SandboxCompletedEvent):
                        usage = event.result.get("usage")
                        counts = {name: usage[name] for name in ("model_segments", "tool_rounds", "tool_calls", "contract_repairs", "visible_segments") if isinstance(usage, dict) and isinstance(usage.get(name), int)}
                        latency_trace.emit(work.burst_id, "sandbox_completed", elapsed_ms=max(0, int((time.monotonic() - dispatch_started_at) * 1000)), **counts)
                    yield event
                else:
                    exit_code = self.client.session_command_exit_code(sandbox, command)
                    if exit_code != 0:
                        raise SandboxDispatchError("sandbox_timeout" if exit_code == 124 else "sandbox_exec_failed")
                    return
                continue
            except ValueError as error:
                frames = [
                    (os.path.basename(frame.filename)[:128], frame.name[:128], frame.lineno)
                    for frame in traceback.extract_tb(error.__traceback__)[-8:]
                ]
                _logger.warning("sandbox parser failure code=invalid_result traceback=%s", frames)
                raise SandboxDispatchError("invalid_result") from error
            except DaytonaClientError as error:
                raise SandboxDispatchError("sandbox_exec_failed") from error
            except TimeoutError as error:
                raise SandboxDispatchError("sandbox_timeout") from error
            except SandboxDispatchError:
                raise
            except Exception as error:
                raise SandboxDispatchError("access_token_failed") from error
            finally:
                if command is not None:
                    try:
                        self.client.delete_session(sandbox, session_id)
                    except DaytonaClientError:
                        pass
        raise SandboxDispatchError("auth_expired")

    def _interactive_cron_env(self, installation: TelegramInstallation) -> dict[str, str]:
        if self.control_url is None or self.capability_key is None:
            return {}
        now = int(time.time())
        capability = CronCapability(installation.tomo_id, installation.actor_id, f"telegram:{installation.chat_id}", f"telegram:actor:{installation.actor_id}", now, now + 300)
        return {
            "TOMO_CRON_CONTROL_URL": self.control_url,
            "TOMO_CRON_CAPABILITY": issue_capability(self.capability_key, capability),
            "TOMO_CRON_OWNER_ID": installation.tomo_id,
            "TOMO_CRON_ACTOR_ID": installation.actor_id,
            "TOMO_CRON_DESTINATION": f"telegram:{installation.chat_id}",
            "TOMO_CRON_SESSION_ID": f"telegram:actor:{installation.actor_id}",
        }

    def iter_automation_events(
        self, installation: TelegramInstallation, turn: AutomationTurn, generation_id: str, session_id: str,
        is_active: Callable[[], bool] | None = None,
    ) -> Iterator[SandboxEvent]:
        """Run an automation turn through the same sandbox/auth/parser boundary."""
        if generation_id != turn.generation_id or turn.actor_id != installation.actor_id or turn.chat_id != installation.chat_id:
            raise SandboxDispatchError("invalid_result")
        is_active = is_active or (lambda: True)
        request_id = _request_id(generation_id)
        with self._locks[installation.tomo_id]:
            try:
                record = self.supervisor.reconcile(installation.tomo_id)
                if not record.sandbox_id:
                    raise SandboxDispatchError("sandbox_not_ready")
                sandbox = self.client.get(record.sandbox_id)
            except DaytonaClientError as error:
                raise SandboxDispatchError("sandbox_exec_failed") from error
            for attempt in range(2):
                command = None
                try:
                    if not is_active():
                        return
                    token = self.auth_broker.access_token(force_refresh=attempt == 1)
                    command = self.client.start_session_command(sandbox, session_id, _COMMAND, env={
                        "TOMO_AUTOMATION_JSON": encode_automation(request_id, turn),
                        "TOMO_CORE_DATA_DIR": self.data_dir,
                        "TOMO_INSTANCE_ID": installation.tomo_id,
                        "TOMO_SUPERGROK_ACCESS_TOKEN": token,
                        "TOMO_CORE_SOUL": "/opt/tomo/SOUL.md",
                        "TOMO_XAI_MODEL": os.getenv("TOMO_XAI_MODEL", self.xai_model),
                        "TOMO_XAI_REASONING_EFFORT": os.getenv("TOMO_XAI_REASONING_EFFORT", self.xai_reasoning_effort),
                        **_latency_env(),
                    }, timeout=_EXEC_TIMEOUT_SECONDS)
                    yielded = 0
                    for event in iter_event_markers(self.client.iter_session_logs(sandbox, command), request_id, generation_id, budget=self.budget):
                        if isinstance(event, SandboxErrorEvent):
                            if event.code == "auth_expired" and yielded == 0 and attempt == 0:
                                break
                            raise SandboxDispatchError(event.code)
                        yielded += 1
                        if not is_active():
                            return
                        yield event
                    else:
                        exit_code = self.client.session_command_exit_code(sandbox, command)
                        if exit_code:
                            raise SandboxDispatchError("sandbox_timeout" if exit_code == 124 else "sandbox_exec_failed")
                        return
                except (DaytonaClientError, TimeoutError) as error:
                    raise SandboxDispatchError("sandbox_timeout" if isinstance(error, TimeoutError) else "sandbox_exec_failed") from error
                except ValueError as error:
                    raise SandboxDispatchError("invalid_result") from error
                except SandboxDispatchError:
                    raise
                except Exception as error:
                    raise SandboxDispatchError("access_token_failed") from error
                finally:
                    if command is not None:
                        try:
                            self.client.delete_session(sandbox, session_id)
                        except DaytonaClientError:
                            pass
        raise SandboxDispatchError("auth_expired")

    def cancel_generation(self, interrupted: InterruptedGeneration) -> None:
        try:
            record = self.supervisor.reconcile(interrupted.tomo_id)
            if not record.sandbox_id:
                raise SandboxDispatchError("sandbox_not_ready")
            sandbox = self.client.get(record.sandbox_id)
            self.client.delete_session(sandbox, interrupted.session_id)
        except DaytonaClientError as error:
            raise SandboxDispatchError("sandbox_exec_failed") from error

    def _execute(
        self, sandbox_id: str, installation: TelegramInstallation, request_id: str, envelope: InboundEnvelope, *, force_refresh: bool = False
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
                    "TOMO_INSTANCE_ID": installation.tomo_id,
                    "TOMO_SUPERGROK_ACCESS_TOKEN": token,
                    "TOMO_CORE_SOUL": "/opt/tomo/SOUL.md",
                    "TOMO_XAI_MODEL": os.getenv("TOMO_XAI_MODEL", self.xai_model),
                    "TOMO_XAI_REASONING_EFFORT": os.getenv("TOMO_XAI_REASONING_EFFORT", self.xai_reasoning_effort),
                    **self._interactive_cron_env(installation),
                    **_latency_env(),
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
        try:
            bubbles = parse_result_marker(result.output, request_id)
        except SandboxProtocolError as error:
            if error.code == "auth_expired":
                raise
            if result.exit_code != 0:
                raise SandboxDispatchError("sandbox_timeout" if result.exit_code == 124 else "sandbox_exec_failed") from error
            raise
        except ValueError as error:
            if result.exit_code != 0:
                raise SandboxDispatchError("sandbox_timeout" if result.exit_code == 124 else "sandbox_exec_failed") from error
            raise SandboxDispatchError("invalid_result") from error
        if result.exit_code != 0:
            raise SandboxDispatchError("sandbox_timeout" if result.exit_code == 124 else "sandbox_exec_failed")
        return bubbles


def _request_id(generation_id: str) -> str:
    return f"telegram-generation-{_safe_id(generation_id)}"[:128]


def _session_id(generation_id: str) -> str:
    return f"telegram-{_safe_id(generation_id)}"[:128]


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    return safe or "generation"


def _latency_env() -> dict[str, str]:
    """Forward opt-in tracing configuration without ever placing it in output."""
    if os.getenv("TOMO_LATENCY_TRACE") != "1":
        return {}
    key = os.getenv("TOMO_LATENCY_TRACE_KEY")
    return {"TOMO_LATENCY_TRACE": "1"} if isinstance(key, str) and len(key) >= 32 else {}


def burst_from_work(installation: TelegramInstallation, work: TelegramGenerationWork) -> InputBurst:
    messages = tuple(_message_from_input(installation, item) for item in work.inputs)
    return InputBurst(
        burst_id=work.burst_id,
        generation_id=work.generation_id,
        revision=work.revision,
        messages=messages,
        visible_assistant_utterances=work.visible_assistant_utterances,
        accepted_generation_ids=work.accepted_generation_ids,
    )


def _message_from_input(installation: TelegramInstallation, item: TelegramGenerationInput) -> InboundMessage:
    payload = json.loads(item.payload)
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        raise ValueError("generation input payload must contain a message")
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    text = message.get("text")
    if not isinstance(text, str):
        text = message.get("caption")
    text = text if isinstance(text, str) else ""
    attachments = photo_attachments_from_message(message)
    if not text.strip() and not attachments:
        raise ValueError("generation input message must contain text or a supported attachment")
    envelope = InboundEnvelope(
        connector="telegram",
        actor_id=str(sender.get("id") or installation.actor_id),
        message_id=str(message.get("message_id") if message.get("message_id") is not None else item.message_id),
        text=text,
        timestamp=str(item.telegram_sent_at) if item.telegram_sent_at is not None else None,
        attachments=attachments,
        native_metadata={
            "update_id": item.update_id,
            "from_id": str(sender.get("id") or installation.actor_id),
            "tomo_id": installation.tomo_id,
        },
        reply_context=reply_context_from_message(message, chat.get("id")),
    )
    return InboundMessage(item.ordinal, item.update_id, envelope)
