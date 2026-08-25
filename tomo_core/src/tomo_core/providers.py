from __future__ import annotations

import codecs
import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol, TypeAlias

import httpx

from .grok_auth import GrokAuthStore
from .oauth import OAuthManager


@dataclass(frozen=True)
class ProviderTextDelta:
    text: str


@dataclass(frozen=True)
class ProviderToolCallReady:
    call_id: str
    name: str
    arguments_json: str

    def __post_init__(self) -> None:
        if not self.call_id.strip() or not self.name.strip():
            raise ValueError("tool call id and name are required")


@dataclass(frozen=True)
class ProviderStreamCompleted:
    finish_reason: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None

    def __post_init__(self) -> None:
        if not self.finish_reason.strip():
            raise ValueError("finish reason is required")
        for count in (self.input_tokens, self.output_tokens, self.reasoning_tokens):
            if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
                raise ValueError("usage counts must be nonnegative integers")


ProviderStreamEvent: TypeAlias = ProviderTextDelta | ProviderToolCallReady | ProviderStreamCompleted

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_AGENT_MODEL = "deepseek/deepseek-v4-flash-0731"
DEFAULT_OPENROUTER_VISION_MODEL = "meta/muse-spark-1.2-contributor"


class ProviderStreamError(ValueError):
    """A privacy-safe, stable diagnostic for OpenAI-compatible streams."""

    def __init__(self, code: str) -> None:
        if not code or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in code):
            raise ValueError("provider stream error code must be lowercase snake case")
        self.code = code
        super().__init__(code)


def _provider_headers(base_url: str, access_token: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {access_token}"}
    if "openrouter.ai" in base_url:
        headers["HTTP-Referer"] = "https://github.com/pandacover/tomo_v2"
        headers["X-Title"] = "Tomo"
    return headers


def _httpx_verify() -> str | bool:
    cloudflare_ca = Path("/etc/cloudflare/certs/cloudflare-containers-ca.crt")
    system_ca = Path("/etc/ssl/certs/ca-certificates.crt")
    if cloudflare_ca.is_file() and system_ca.is_file():
        combined = Path("/tmp/tomo-ca-bundle.pem")
        combined.write_bytes(system_ca.read_bytes() + b"\n" + cloudflare_ca.read_bytes())
        return str(combined)
    if cloudflare_ca.is_file():
        return str(cloudflare_ca)
    if system_ca.is_file():
        return str(system_ca)
    return True


class ProviderAdapter(Protocol):
    name: str
    supports_images_in: bool
    supports_images_out: bool
    supports_tool_calls: bool

    def stream(
        self,
        messages: list[dict[str, object]],
        *,
        tools: tuple[dict[str, object], ...] = (),
        actor_id: str | None = None,
    ) -> Iterator[ProviderStreamEvent]:
        ...

    def complete(self, messages: list[dict[str, str]], actor_id: str | None = None) -> str:
        ...


class ProviderSetupRequired(RuntimeError):
    def __init__(self, user_message: str) -> None:
        self.user_message = user_message
        super().__init__("provider setup required")


@dataclass
class _ToolCallParts:
    call_id: str | None = None
    name: str | None = None
    arguments: str = ""
    received_arguments: bool = False


def _stream_openai_compatible(
    *,
    base_url: str,
    access_token: str,
    model: str,
    messages: list[dict[str, object]],
    tools: tuple[dict[str, object], ...],
    reasoning_effort: str | None,
    store: bool | None,
    response_format: dict[str, object] | None = None,
) -> Iterator[ProviderStreamEvent]:
    request_body: dict[str, object] = {"model": model, "messages": messages, "stream": True}
    if tools:
        request_body["tools"] = list(tools)
    if reasoning_effort:
        request_body["reasoning_effort"] = reasoning_effort
    if store is not None:
        request_body["store"] = store
    if response_format is not None:
        request_body["response_format"] = response_format
        if "openrouter.ai" in base_url:
            # Structured-output support is endpoint-specific on OpenRouter.
            # Do not let routing silently choose an endpoint that ignores the
            # response schema used by the conversation repair path.
            request_body["provider"] = {"require_parameters": True}
            json_schema = response_format.get("json_schema")
            if (
                model == DEFAULT_OPENROUTER_AGENT_MODEL
                and isinstance(json_schema, dict)
                and json_schema.get("name") == "tomo_frame_repair"
            ):
                # This reasoning model can otherwise spend a completion on
                # thinking without returning user-visible content. Reserve a
                # bounded completion budget and keep reasoning out of the SSE.
                request_body.pop("reasoning_effort", None)
                request_body["max_tokens"] = 4096
                request_body["reasoning"] = {"effort": "high", "exclude": True}

    tool_calls: dict[int, _ToolCallParts] = {}
    tool_call_indices: dict[str, int] = {}
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    saw_done = False
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""

    def parse_usage(payload: dict[str, object]) -> None:
        nonlocal input_tokens, output_tokens, reasoning_tokens
        usage = payload.get("usage")
        if usage is None:
            return
        if not isinstance(usage, dict):
            raise ProviderStreamError("invalid_usage")
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        details = usage.get("completion_tokens_details")
        if details is not None and not isinstance(details, dict):
            raise ProviderStreamError("invalid_usage")
        reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
        for value in (prompt, completion, reasoning):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ProviderStreamError("invalid_usage")
        if prompt is not None:
            input_tokens = prompt
        if completion is not None:
            output_tokens = completion
        if reasoning is not None:
            reasoning_tokens = reasoning

    def consume_data(data: str) -> Iterator[ProviderTextDelta]:
        nonlocal finish_reason, saw_done
        if data == "[DONE]":
            if saw_done:
                raise ProviderStreamError("duplicate_completion")
            saw_done = True
            return
        if saw_done:
            raise ProviderStreamError("data_after_completion")
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ProviderStreamError("malformed_json") from exc
        if not isinstance(payload, dict):
            raise ProviderStreamError("invalid_payload")
        parse_usage(payload)
        if payload.get("error"):
            raise ProviderStreamError("provider_error_event")
        choices = payload.get("choices")
        if choices is None:
            return
        if not isinstance(choices, list):
            raise ProviderStreamError("invalid_choices_type")
        if not choices:
            return
        if len(choices) != 1 or not isinstance(choices[0], dict):
            raise ProviderStreamError("invalid_choices")
        choice = choices[0]
        current_finish = choice.get("finish_reason")
        if isinstance(current_finish, str) and current_finish.strip():
            if finish_reason is None:
                finish_reason = current_finish
        elif current_finish not in (None, "", "null"):
            raise ProviderStreamError("invalid_finish_reason")
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            raise ProviderStreamError("invalid_delta")
        content = delta.get("content")
        if isinstance(content, str) and content:
            yield ProviderTextDelta(content)
        elif content not in (None, ""):
            raise ProviderStreamError("invalid_content")
        native_calls = delta.get("tool_calls")
        if native_calls is not None:
            if not isinstance(native_calls, list):
                raise ProviderStreamError("invalid_tool_calls")
            for native_call in native_calls:
                if not isinstance(native_call, dict):
                    raise ProviderStreamError("invalid_tool_call")
                index = native_call.get("index")
                if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                    raise ProviderStreamError("invalid_tool_call_index")
                parts = tool_calls.setdefault(index, _ToolCallParts())
                call_id = native_call.get("id")
                if call_id is not None:
                    if not isinstance(call_id, str) or not call_id.strip() or (parts.call_id is not None and parts.call_id != call_id):
                        raise ProviderStreamError("conflicting_tool_call_id")
                    existing_index = tool_call_indices.get(call_id)
                    if existing_index is not None and existing_index != index:
                        raise ProviderStreamError("conflicting_tool_call_id")
                    tool_call_indices[call_id] = index
                    parts.call_id = call_id
                function = native_call.get("function")
                if function is not None:
                    if not isinstance(function, dict):
                        raise ProviderStreamError("invalid_tool_function")
                    name = function.get("name")
                    if name is not None:
                        if not isinstance(name, str) or not name.strip() or (parts.name is not None and parts.name != name):
                            raise ProviderStreamError("conflicting_tool_call_name")
                        parts.name = name
                    if "arguments" in function:
                        arguments = function["arguments"]
                        if not isinstance(arguments, str):
                            raise ProviderStreamError("invalid_tool_arguments")
                        parts.arguments += arguments
                        parts.received_arguments = True

    def consume_sse_event(raw_event: str) -> Iterator[ProviderTextDelta]:
        lines = raw_event.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        data_lines = [line[5:].lstrip(" ") for line in lines if line.startswith("data:")]
        if not data_lines:
            if any(line and not line.startswith((":", "event:", "id:", "retry:")) for line in lines):
                raise ProviderStreamError("invalid_sse_event")
            return
        yield from consume_data("\n".join(data_lines))

    with httpx.stream(
        "POST",
        f"{base_url}/chat/completions",
        headers=_provider_headers(base_url, access_token),
        json=request_body,
        timeout=180,
        verify=_httpx_verify(),
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_raw():
            if not isinstance(chunk, bytes):
                raise ProviderStreamError("invalid_chunk")
            buffer += decoder.decode(chunk)
            while "\n\n" in buffer or "\r\n\r\n" in buffer:
                separator = "\r\n\r\n" if "\r\n\r\n" in buffer and (
                    "\n\n" not in buffer or buffer.index("\r\n\r\n") <= buffer.index("\n\n")) else "\n\n"
                raw_event, buffer = buffer.split(separator, 1)
                yield from consume_sse_event(raw_event)
        buffer += decoder.decode(b"", final=True)
        if buffer.strip():
            yield from consume_sse_event(buffer)

    if finish_reason is None and saw_done:
        finish_reason = "stop"
    if finish_reason is None:
        raise ProviderStreamError("missing_terminal_completion")
    if tool_calls and finish_reason != "tool_calls":
        raise ProviderStreamError("tool_calls_without_finish")
    if finish_reason == "tool_calls":
        if not tool_calls:
            raise ProviderStreamError("tool_finish_without_calls")
        for index in sorted(tool_calls):
            parts = tool_calls[index]
            if not parts.call_id or not parts.name or not parts.received_arguments:
                raise ProviderStreamError("incomplete_tool_call")
            yield ProviderToolCallReady(parts.call_id, parts.name, parts.arguments)
    yield ProviderStreamCompleted(finish_reason, input_tokens, output_tokens, reasoning_tokens)


class _OpenAICompatibleProvider:
    def _stream_with_token(
        self,
        access_token: str,
        messages: list[dict[str, object]],
        *,
        tools: tuple[dict[str, object], ...] = (),
        response_format: dict[str, object] | None = None,
    ) -> Iterator[ProviderStreamEvent]:
        return _stream_openai_compatible(
            base_url=self.base_url,
            access_token=access_token,
            model=self.model,
            messages=messages,
            tools=tools,
            reasoning_effort=self.reasoning_effort,
            store=self.store,
            response_format=response_format,
        )

    # Compatibility collector for callers that have not migrated to streaming.
    def complete(self, messages: list[dict[str, str]], actor_id: str | None = None) -> str:
        text: list[str] = []
        for event in self.stream(messages, actor_id=actor_id):
            if isinstance(event, ProviderTextDelta):
                text.append(event.text)
            elif isinstance(event, ProviderToolCallReady):
                raise ValueError("complete does not support tool calls")
        return "".join(text)


@dataclass
class XaiApiProvider(_OpenAICompatibleProvider):
    api_key: str
    model: str = "grok-4.5"
    base_url: str = "https://api.x.ai/v1"
    reasoning_effort: str | None = None
    store: bool | None = None

    name: str = "xai_api"
    supports_images_in: bool = True
    supports_images_out: bool = False
    supports_tool_calls: bool = True

    def stream(self, messages: list[dict[str, object]], *, tools: tuple[dict[str, object], ...] = (), actor_id: str | None = None) -> Iterator[ProviderStreamEvent]:
        return self._stream_with_token(self.api_key, messages, tools=tools)

    def stream_structured(self, messages: list[dict[str, object]], *, response_format: dict[str, object], actor_id: str | None = None) -> Iterator[ProviderStreamEvent]:
        return self._stream_with_token(self.api_key, messages, response_format=copy.deepcopy(response_format))


@dataclass
class SuperGrokTokenStore:
    access_token: str = field(repr=False)


@dataclass
class SuperGrokOAuthProvider(_OpenAICompatibleProvider):
    token_store: SuperGrokTokenStore
    model: str = "grok-4.5"
    base_url: str = "https://api.x.ai/v1"
    reasoning_effort: str = "high"
    store: bool | None = None

    name: str = "supergrok_oauth"
    supports_images_in: bool = True
    supports_images_out: bool = False
    supports_tool_calls: bool = True

    def stream(self, messages: list[dict[str, object]], *, tools: tuple[dict[str, object], ...] = (), actor_id: str | None = None) -> Iterator[ProviderStreamEvent]:
        return self._stream_with_token(self.token_store.access_token, messages, tools=tools)

    def stream_structured(self, messages: list[dict[str, object]], *, response_format: dict[str, object], actor_id: str | None = None) -> Iterator[ProviderStreamEvent]:
        return self._stream_with_token(self.token_store.access_token, messages, response_format=copy.deepcopy(response_format))


def supergrok_oauth_provider_from_access_token(access_token: str, *, model: str = "grok-4.5", reasoning_effort: str = "high", store: bool | None = None) -> SuperGrokOAuthProvider:
    """Build a fixed-token provider without retaining the token in repr output."""
    if not access_token:
        raise ValueError("SuperGrok access token is required")
    return SuperGrokOAuthProvider(token_store=SuperGrokTokenStore(access_token=access_token), model=model, reasoning_effort=reasoning_effort, store=store)


@dataclass
class OAuthBackedSuperGrokProvider:
    oauth: OAuthManager
    model: str = "grok-4.5"
    base_url: str = "https://api.x.ai/v1"
    reasoning_effort: str = "high"
    store: bool | None = None

    name: str = "supergrok_oauth_dynamic"
    supports_images_in: bool = True
    supports_images_out: bool = False
    supports_tool_calls: bool = True

    def stream(self, messages: list[dict[str, object]], *, tools: tuple[dict[str, object], ...] = (), actor_id: str | None = None) -> Iterator[ProviderStreamEvent]:
        if not actor_id:
            raise ProviderSetupRequired("use /connect to connect supergrok oauth first.")
        token_path = self.oauth.token_path("supergrok", actor_id)
        if not token_path.exists():
            raise ProviderSetupRequired("use /connect to connect supergrok oauth first.")
        token = json.loads(token_path.read_text(encoding="utf-8"))
        access_token = token.get("access_token")
        if not access_token:
            raise ProviderSetupRequired("use /connect to connect supergrok oauth first.")
        return SuperGrokOAuthProvider(
            token_store=SuperGrokTokenStore(access_token=access_token), model=self.model, base_url=self.base_url, reasoning_effort=self.reasoning_effort, store=self.store
        ).stream(messages, tools=tools, actor_id=actor_id)

    def stream_structured(self, messages: list[dict[str, object]], *, response_format: dict[str, object], actor_id: str | None = None) -> Iterator[ProviderStreamEvent]:
        if not actor_id:
            raise ProviderSetupRequired("use /connect to connect supergrok oauth first.")
        token_path = self.oauth.token_path("supergrok", actor_id)
        if not token_path.exists():
            raise ProviderSetupRequired("use /connect to connect supergrok oauth first.")
        token = json.loads(token_path.read_text(encoding="utf-8"))
        access_token = token.get("access_token")
        if not access_token:
            raise ProviderSetupRequired("use /connect to connect supergrok oauth first.")
        return SuperGrokOAuthProvider(
            token_store=SuperGrokTokenStore(access_token=access_token), model=self.model, base_url=self.base_url, reasoning_effort=self.reasoning_effort, store=self.store
        ).stream_structured(messages, response_format=response_format, actor_id=actor_id)

    def complete(self, messages: list[dict[str, str]], actor_id: str | None = None) -> str:
        return _collect_text(self.stream(messages, actor_id=actor_id))


@dataclass
class GrokAuthProvider:
    auth_store: GrokAuthStore
    model: str = "grok-4.5"
    base_url: str = "https://api.x.ai/v1"
    reasoning_effort: str | None = None
    store: bool | None = None

    name: str = "grok_login_auth"
    supports_images_in: bool = True
    supports_images_out: bool = False
    supports_tool_calls: bool = True

    def stream(self, messages: list[dict[str, object]], *, tools: tuple[dict[str, object], ...] = (), actor_id: str | None = None) -> Iterator[ProviderStreamEvent]:
        access_token = self.auth_store.access_token()
        if not access_token:
            raise ProviderSetupRequired("run grok login or grok login --device-auth first, then restart me.")
        return XaiApiProvider(api_key=access_token, model=self.model, base_url=self.base_url, reasoning_effort=self.reasoning_effort, store=self.store).stream(messages, tools=tools, actor_id=actor_id)

    def stream_structured(self, messages: list[dict[str, object]], *, response_format: dict[str, object], actor_id: str | None = None) -> Iterator[ProviderStreamEvent]:
        access_token = self.auth_store.access_token()
        if not access_token:
            raise ProviderSetupRequired("run grok login or grok login --device-auth first, then restart me.")
        return XaiApiProvider(api_key=access_token, model=self.model, base_url=self.base_url, reasoning_effort=self.reasoning_effort, store=self.store).stream_structured(messages, response_format=response_format, actor_id=actor_id)

    def complete(self, messages: list[dict[str, str]], actor_id: str | None = None) -> str:
        return _collect_text(self.stream(messages, actor_id=actor_id))


def _collect_text(events: Iterator[ProviderStreamEvent]) -> str:
    text: list[str] = []
    for event in events:
        if isinstance(event, ProviderTextDelta):
            text.append(event.text)
        elif isinstance(event, ProviderToolCallReady):
            raise ValueError("complete does not support tool calls")
    return "".join(text)


# Backcompat aliases for older tests/imports. New code should use XaiApiProvider
# for xAI API keys and SuperGrokOAuthProvider for Grok/SuperGrok OAuth tokens.
XaiOAuthTokenStore = SuperGrokTokenStore
XaiGrokOAuthProvider = XaiApiProvider
OAuthBackedXaiProvider = OAuthBackedSuperGrokProvider


@dataclass
class StaticProvider:
    response: str
    name: str = "static_test_provider"
    supports_images_in: bool = False
    supports_images_out: bool = False
    supports_tool_calls: bool = False

    def stream(self, messages: list[dict[str, object]], *, tools: tuple[dict[str, object], ...] = (), actor_id: str | None = None) -> Iterator[ProviderStreamEvent]:
        if tools:
            raise ValueError("static provider does not support tool calls")
        system_text = "\n".join(message["content"] for message in messages if message.get("role") == "system" and isinstance(message.get("content"), str))
        if "SHOULD emit one turn_plan before controls or frames" in system_text:
            plan = {
                "type": "turn_plan",
                "primary_move": "answer",
                "supporting_moves": [],
                "response_goal": "return the configured static smoke response",
                "confidence": "high",
                "reaction": None,
            }
            frame = {"type": "frame", "text": self.response}
            yield ProviderTextDelta("\n".join(json.dumps(record, separators=(",", ":")) for record in (plan, frame)))
        elif "do not emit a turn_plan; reuse the fixed turn plan" in system_text:
            yield ProviderTextDelta(json.dumps({"type": "frame", "text": self.response}, separators=(",", ":")))
        else:
            yield ProviderTextDelta(self.complete(messages, actor_id=actor_id))
        yield ProviderStreamCompleted("stop")

    def complete(self, messages: list[dict[str, str]], actor_id: str | None = None) -> str:
        system_text = "\n".join(message["content"] for message in messages if message.get("role") == "system")
        if '"primary_move"' in system_text and '"supporting_moves"' in system_text:
            return json.dumps({"primary_move": "answer", "supporting_moves": [], "response_goal": "return the configured static smoke response", "confidence": "high"})
        if '"utterance"' in system_text:
            return json.dumps({"utterance": self.response})
        if '"utterances"' in system_text:
            return json.dumps({"utterances": [self.response]})
        return self.response
