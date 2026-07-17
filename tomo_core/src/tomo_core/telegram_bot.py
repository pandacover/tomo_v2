from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .grok_auth import GrokAuthStore
from .models import InboundEnvelope
from .oauth import OAuthError, OAuthManager
from .runtime import PersonalAgentRuntime
from .telegram import TelegramSendReceipt


class TelegramBotApiError(RuntimeError):
    pass


class TelegramDeliveryError(TelegramBotApiError):
    def __init__(self, code: str, *, uncertain: bool) -> None:
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


class TelegramReactionError(RuntimeError):
    """Safe classification for a best-effort reaction failure."""

    def __init__(self, code: str, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


TELEGRAM_COMMANDS = [{"command": "connect", "description": "connect supergrok or calendar"}]


@dataclass
class TelegramBotApiClient:
    token: str
    base_url: str = "https://api.telegram.org"
    timeout: float = 30.0

    @property
    def api_url(self) -> str:
        return f"{self.base_url}/bot{self.token}"

    def request(self, method: str, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        req_timeout = timeout if timeout is not None else self.timeout
        response = httpx.post(f"{self.api_url}/{method}", json=payload, timeout=req_timeout)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise TelegramBotApiError(f"telegram_{method}_rejected")
        return data

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self.request("getUpdates", payload, timeout=float(timeout) + 5.0).get("result", [])

    def send_typing(self, actor_id: str) -> None:
        self.request("sendChatAction", {"chat_id": actor_id, "action": "typing"}, timeout=2.0)

    def send_message(
        self,
        actor_id: str,
        text: str,
        reply_to_message_id: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> TelegramSendReceipt:
        payload: dict[str, Any] = {"chat_id": actor_id, "text": text}
        if reply_to_message_id:
            payload["reply_parameters"] = {"message_id": int(reply_to_message_id)}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            data = self.request("sendMessage", payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise TelegramDeliveryError("telegram_sendMessage_transport", uncertain=False) from error
        except (httpx.HTTPStatusError, TelegramBotApiError) as error:
            raise TelegramDeliveryError("telegram_sendMessage_rejected", uncertain=False) from error
        except httpx.TimeoutException as error:
            raise TelegramDeliveryError("telegram_sendMessage_timeout", uncertain=True) from error
        except (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError, httpx.ProtocolError, httpx.DecodingError, ValueError) as error:
            raise TelegramDeliveryError("telegram_sendMessage_protocol", uncertain=True) from error
        except Exception as error:
            raise TelegramDeliveryError("telegram_sendMessage_uncertain", uncertain=True) from error
        result = data.get("result") if isinstance(data, dict) else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None:
            raise TelegramDeliveryError("telegram_sendMessage_missing_receipt", uncertain=True)
        return TelegramSendReceipt(str(message_id))

    def set_message_reaction(self, actor_id: str, message_id: str, emoji: str) -> None:
        try:
            numeric_message_id = int(message_id)
        except (TypeError, ValueError) as error:
            raise TelegramReactionError("telegram_reaction_invalid_target", False) from error
        try:
            self.request("setMessageReaction", {"chat_id": actor_id, "message_id": numeric_message_id, "reaction": [{"type": "emoji", "emoji": emoji}], "is_big": False})
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise TelegramReactionError("telegram_reaction_retryable", True) from error
        except Exception as error:
            raise TelegramReactionError("telegram_reaction_rejected", False) from error

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self.request("answerCallbackQuery", payload)

    def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        self.request("setMyCommands", {"commands": commands})


def envelope_from_update(update: dict[str, Any]) -> InboundEnvelope | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if chat.get("type") != "private":
        return None
    text = message.get("text")
    if not text:
        return None
    actor_id = str(sender.get("id") or chat.get("id"))
    message_id = str(message["message_id"])
    return InboundEnvelope(
        connector="telegram",
        actor_id=actor_id,
        message_id=message_id,
        text=text,
        native_metadata={"update_id": update.get("update_id"), "chat_id": chat.get("id")},
    )


@dataclass
class TelegramPollingBot:
    client: TelegramBotApiClient
    runtime: PersonalAgentRuntime | None
    oauth: OAuthManager | None = None
    poll_timeout: int = 30
    idle_sleep_seconds: float = 0.2
    on_error: Callable[[Exception], None] | None = None

    def run_forever(self) -> None:
        self.register_commands()
        offset: int | None = None
        while True:
            offset = self.poll_once(offset)
            time.sleep(self.idle_sleep_seconds)

    def register_commands(self) -> None:
        if hasattr(self.client, "set_my_commands"):
            self.client.set_my_commands(TELEGRAM_COMMANDS)

    def poll_once(self, offset: int | None = None) -> int | None:
        updates = self.client.get_updates(offset=offset, timeout=self.poll_timeout)
        next_offset = offset
        for update in updates:
            if "update_id" in update:
                next_offset = int(update["update_id"]) + 1
            self.process_update(update)
        return next_offset

    def process_update(self, update: dict[str, Any]) -> None:
        """Process one already-received update; local polling calls this directly."""
        if self._handle_connect_update(update):
            return
        envelope = envelope_from_update(update)
        if envelope is None or self.runtime is None:
            return
        try:
            self.runtime.handle_telegram_text(envelope)
        except Exception as exc:
            if self.on_error:
                self.on_error(exc)
            # The durable router owns retry policy, so it must observe failures
            # even when a caller also wants them reported.
            raise

    def _handle_connect_update(self, update: dict[str, Any]) -> bool:
        if "callback_query" in update:
            return self._handle_callback_query(update["callback_query"])
        message = update.get("message")
        if not isinstance(message, dict):
            return False
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if chat.get("type") != "private":
            return False
        actor_id = str(sender.get("id") or chat.get("id"))
        text = (message.get("text") or "").strip()
        message_id = str(message.get("message_id"))
        if text == "/connect":
            self._send_connect_menu(actor_id, message_id)
            return True
        if self.oauth and (self.oauth.has_pending(actor_id) or "code=" in text or text.startswith("http://") or text.startswith("https://")):
            try:
                provider, _token = self.oauth.complete_any_pending(actor_id, text)
            except OAuthError as exc:
                message = str(exc)
                if "does not include code" in message:
                    message = "that redirect url does not include the oauth code. paste the code shown on the xai screen instead."
                else:
                    message = f"oauth callback failed: {message}"
                self.client.send_message(actor_id, message, reply_to_message_id=message_id)
                return True
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text[:500] if exc.response is not None else str(exc)
                self.client.send_message(actor_id, f"oauth token exchange failed: {detail}", reply_to_message_id=message_id)
                return True
            except Exception as exc:
                self.client.send_message(actor_id, f"oauth callback failed: {exc}", reply_to_message_id=message_id)
                return True
            self.client.send_message(actor_id, f"{provider} connected.", reply_to_message_id=message_id)
            return True
        if text.startswith("/"):
            return True
        return False

    def _send_connect_menu(self, actor_id: str, reply_to_message_id: str | None = None) -> None:
        self.client.send_message(
            actor_id,
            "connect a service:",
            reply_to_message_id=reply_to_message_id,
            reply_markup={
                "inline_keyboard": [
                    [{"text": "supergrok oauth", "callback_data": "connect:supergrok"}],
                    [{"text": "google calendar", "callback_data": "connect:google_calendar"}],
                ]
            },
        )

    def _handle_callback_query(self, callback: dict[str, Any]) -> bool:
        data = callback.get("data")
        if not isinstance(data, str) or not data.startswith("connect:"):
            return False
        provider = data.split(":", 1)[1]
        if provider == "xai":
            provider = "supergrok"
        actor_id = str((callback.get("from") or {}).get("id"))
        message = callback.get("message") or {}
        reply_to = str(message.get("message_id")) if message.get("message_id") is not None else None
        callback_id = str(callback.get("id"))
        if hasattr(self.client, "answer_callback_query"):
            try:
                self.client.answer_callback_query(callback_id, f"opening {provider.replace('_calendar', '')} connect")
            except Exception:
                # Telegram callback queries can expire or be invalid after a restart.
                # A failed acknowledgement should not kill the bot or block sending
                # the actual auth link/message.
                pass
        if self.oauth is None:
            self.client.send_message(actor_id, "oauth is not configured yet.", reply_to_message_id=reply_to)
            return True
        try:
            auth_url = self.oauth.begin(actor_id, provider)
        except OAuthError as exc:
            if provider == "supergrok" and self._import_grok_login_token(actor_id, reply_to):
                return True
            self.client.send_message(actor_id, str(exc), reply_to_message_id=reply_to)
            return True
        label = "google calendar" if provider == "google_calendar" else provider
        self.client.send_message(
            actor_id,
            f"open this {label} auth link, approve access, then paste the redirected url back here:\n{auth_url}",
            reply_to_message_id=reply_to,
        )
        return True

    def _import_grok_login_token(self, actor_id: str, reply_to: str | None) -> bool:
        if self.oauth is None:
            return False
        token = GrokAuthStore().payload()
        if token:
            token_payload = {
                **token,
                "provider": "supergrok",
                "actor_id": actor_id,
                "source": "grok_login_auth_json",
            }
            self.oauth.token_path("supergrok", actor_id).write_text(
                __import__("json").dumps(token_payload, indent=2), encoding="utf-8"
            )
            self.client.send_message(
                actor_id,
                "supergrok oauth connected from grok login.",
                reply_to_message_id=reply_to,
            )
            return True
        self.client.send_message(
            actor_id,
            "i do not have a supergrok oauth app configured yet. run `grok login` locally, then press supergrok oauth again and i will import ~/.grok/auth.json into .tomo_core/oauth.",
            reply_to_message_id=reply_to,
        )
        return True
