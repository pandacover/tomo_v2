from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .grok_auth import GrokAuthStore
from .oauth import OAuthManager


class ProviderAdapter(Protocol):
    name: str
    supports_images_in: bool
    supports_images_out: bool
    supports_tool_calls: bool

    def complete(self, messages: list[dict[str, str]], actor_id: str | None = None) -> str:
        ...


@dataclass
class XaiApiProvider:
    api_key: str
    model: str = "grok-4.5"
    base_url: str = "https://api.x.ai/v1"
    reasoning_effort: str | None = None

    name: str = "xai_api"
    supports_images_in: bool = True
    supports_images_out: bool = False
    supports_tool_calls: bool = True

    def complete(self, messages: list[dict[str, str]], actor_id: str | None = None) -> str:
        request_body = {"model": self.model, "messages": messages}
        if self.reasoning_effort:
            request_body["reasoning_effort"] = self.reasoning_effort
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=request_body,
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        return payload["choices"][0]["message"]["content"]


@dataclass
class SuperGrokTokenStore:
    access_token: str = field(repr=False)


@dataclass
class SuperGrokOAuthProvider:
    token_store: SuperGrokTokenStore
    model: str = "grok-4.5"
    base_url: str = "https://api.x.ai/v1"
    reasoning_effort: str = "high"

    name: str = "supergrok_oauth"
    supports_images_in: bool = True
    supports_images_out: bool = False
    supports_tool_calls: bool = True

    def complete(self, messages: list[dict[str, str]], actor_id: str | None = None) -> str:
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.token_store.access_token}"},
            json={"model": self.model, "messages": messages, "reasoning_effort": self.reasoning_effort},
            timeout=60,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


def supergrok_oauth_provider_from_access_token(
    access_token: str, *, model: str = "grok-4.5", reasoning_effort: str = "high"
) -> SuperGrokOAuthProvider:
    """Build a fixed-token provider without retaining the token in repr output."""
    if not access_token:
        raise ValueError("SuperGrok access token is required")
    return SuperGrokOAuthProvider(token_store=SuperGrokTokenStore(access_token=access_token), model=model, reasoning_effort=reasoning_effort)


@dataclass
class OAuthBackedSuperGrokProvider:
    oauth: OAuthManager
    model: str = "grok-4.5"
    base_url: str = "https://api.x.ai/v1"
    reasoning_effort: str = "high"

    name: str = "supergrok_oauth_dynamic"
    supports_images_in: bool = True
    supports_images_out: bool = False
    supports_tool_calls: bool = True

    def complete(self, messages: list[dict[str, str]], actor_id: str | None = None) -> str:
        if not actor_id:
            return "use /connect to connect supergrok oauth first."
        token_path = self.oauth.token_path("supergrok", actor_id)
        if not token_path.exists():
            return "use /connect to connect supergrok oauth first."
        token = json.loads(token_path.read_text(encoding="utf-8"))
        access_token = token.get("access_token")
        if not access_token:
            return "use /connect to connect supergrok oauth first."
        return SuperGrokOAuthProvider(
            token_store=SuperGrokTokenStore(access_token=access_token),
            model=self.model,
            base_url=self.base_url,
            reasoning_effort=self.reasoning_effort,
        ).complete(messages, actor_id=actor_id)


@dataclass
class GrokAuthProvider:
    auth_store: GrokAuthStore
    model: str = "grok-4.5"
    base_url: str = "https://api.x.ai/v1"

    name: str = "grok_login_auth"
    supports_images_in: bool = True
    supports_images_out: bool = False
    supports_tool_calls: bool = True

    def complete(self, messages: list[dict[str, str]], actor_id: str | None = None) -> str:
        access_token = self.auth_store.access_token()
        if not access_token:
            return "run grok login or grok login --device-auth first, then restart me."
        return XaiApiProvider(api_key=access_token, model=self.model, base_url=self.base_url).complete(messages, actor_id=actor_id)


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

    def complete(self, messages: list[dict[str, str]], actor_id: str | None = None) -> str:
        return self.response
