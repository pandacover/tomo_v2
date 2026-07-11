"""Execution-only Telegram dispatch for a Daytona sandbox."""

from __future__ import annotations

import os
import json
import logging
import re
from collections import defaultdict
from threading import Lock
from typing import Callable, Iterator, Protocol

from .daytona_client import DaytonaClient, DaytonaClientError, SessionCommandHandle
from .daytona_supervisor import DaytonaSupervisor
from .models import InboundEnvelope, InboundMessage, InputBurst, MessageAttachment, OutboundBubble
from .onboarding_store import InterruptedGeneration, TelegramGenerationInput, TelegramGenerationWork, TelegramInstallation
from .sandbox_protocol import SandboxErrorEvent, SandboxEvent, SandboxProtocolError, encode_inbound, iter_event_markers, parse_result_marker


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
    ) -> None:
        self.supervisor = supervisor
        self.client = client
        self.auth_broker = auth_broker
        self.data_dir = data_dir
        self.xai_model = xai_model
        self.xai_reasoning_effort = xai_reasoning_effort

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
                bubbles = self._execute(record.sandbox_id, installation.tomo_id, request_id, envelope)
            except SandboxProtocolError as error:
                if error.code != "auth_expired":
                    raise SandboxDispatchError(error.code) from error
                try:
                    bubbles = self._execute(record.sandbox_id, installation.tomo_id, request_id, envelope, force_refresh=True)
                except SandboxProtocolError as retry_error:
                    raise SandboxDispatchError(retry_error.code) from retry_error
            return bubbles

    def iter_telegram_events(
        self, installation: TelegramInstallation, work: TelegramGenerationWork, is_active: Callable[[], bool] | None = None
    ) -> Iterator[SandboxEvent]:
        record = self.supervisor.reconcile(installation.tomo_id)
        if not record.sandbox_id:
            raise SandboxDispatchError("sandbox_not_ready")
        sandbox = self.client.get(record.sandbox_id)
        request_id = _request_id(work.generation_id)
        session_id = work.session_id
        burst = burst_from_work(installation, work)
        yielded = 0
        force_refresh = False
        for attempt in range(2):
            command: SessionCommandHandle | None = None
            try:
                token = self.auth_broker.access_token(force_refresh=force_refresh)
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
                    },
                    timeout=_EXEC_TIMEOUT_SECONDS,
                )
                for event in iter_event_markers(
                    self.client.iter_session_logs(sandbox, command),
                    expected_request_id=request_id,
                    expected_generation_id=work.generation_id,
                ):
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
                    yield event
                else:
                    exit_code = self.client.session_command_exit_code(sandbox, command)
                    if exit_code != 0:
                        raise SandboxDispatchError("sandbox_timeout" if exit_code == 124 else "sandbox_exec_failed")
                    return
                continue
            except ValueError as error:
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
        self, sandbox_id: str, tomo_id: str, request_id: str, envelope: InboundEnvelope, *, force_refresh: bool = False
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
                    "TOMO_INSTANCE_ID": tomo_id,
                    "TOMO_SUPERGROK_ACCESS_TOKEN": token,
                    "TOMO_CORE_SOUL": "/opt/tomo/SOUL.md",
                    "TOMO_XAI_MODEL": os.getenv("TOMO_XAI_MODEL", self.xai_model),
                    "TOMO_XAI_REASONING_EFFORT": os.getenv("TOMO_XAI_REASONING_EFFORT", self.xai_reasoning_effort),
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
    attachments: tuple[MessageAttachment, ...] = ()
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        photo = max(
            (item for item in photos if isinstance(item, dict) and isinstance(item.get("file_id"), str)),
            key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0),
            default=None,
        )
        if photo is not None:
            attachments = (
                MessageAttachment(
                    kind="image",
                    file_id=photo["file_id"],
                    mime_type="image/jpeg",
                    metadata={key: photo[key] for key in ("width", "height", "file_size", "file_unique_id") if key in photo},
                ),
            )
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
    )
    return InboundMessage(item.ordinal, item.update_id, envelope)
