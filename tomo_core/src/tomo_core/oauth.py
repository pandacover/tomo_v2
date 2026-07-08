from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx


@dataclass(frozen=True)
class OAuthProviderConfig:
    provider: str
    client_id: str
    auth_url: str
    token_url: str
    redirect_uri: str
    scopes: tuple[str, ...]
    client_secret: str | None = None


class OAuthError(RuntimeError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def parse_code_and_state(callback_or_code: str) -> tuple[str, str | None]:
    value = callback_or_code.strip()
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        params = parse_qs(parsed.query)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        if not code:
            raise OAuthError("callback url does not include code")
        return code, state
    return value, None


class OAuthManager:
    def __init__(self, data_dir: str | Path, providers: dict[str, OAuthProviderConfig]) -> None:
        self.data_dir = Path(data_dir)
        self.oauth_dir = self.data_dir / "oauth"
        self.oauth_dir.mkdir(parents=True, exist_ok=True)
        self.providers = providers

    @staticmethod
    def default_providers(
        google_client_id: str | None = None,
        google_client_secret: str | None = None,
        supergrok_client_id: str | None = None,
        supergrok_client_secret: str | None = None,
        supergrok_auth_url: str = "https://auth.x.ai/oauth2/authorize",
        supergrok_token_url: str = "https://auth.x.ai/oauth2/token",
        # Backcompat args accepted, but mapped to SuperGrok OAuth.
        xai_client_id: str | None = None,
        xai_client_secret: str | None = None,
        xai_auth_url: str | None = None,
        xai_token_url: str | None = None,
    ) -> dict[str, OAuthProviderConfig]:
        providers: dict[str, OAuthProviderConfig] = {}
        # Same public xAI OAuth client used by `uv run tomo login` in the main Tomo repo.
        resolved_supergrok_client_id = supergrok_client_id or xai_client_id or "b1a00492-073a-47ea-816f-4c329264a828"
        providers["supergrok"] = OAuthProviderConfig(
            provider="supergrok",
            client_id=resolved_supergrok_client_id,
            client_secret=supergrok_client_secret or xai_client_secret,
            auth_url=xai_auth_url or supergrok_auth_url,
            token_url=xai_token_url or supergrok_token_url,
            redirect_uri="http://127.0.0.1:56121/callback",
            scopes=("openid", "profile", "email", "offline_access", "grok-cli:access", "api:access"),
        )
        if google_client_id:
            providers["google_calendar"] = OAuthProviderConfig(
                provider="google_calendar",
                client_id=google_client_id,
                client_secret=google_client_secret,
                auth_url="https://accounts.google.com/o/oauth2/v2/auth",
                token_url="https://oauth2.googleapis.com/token",
                redirect_uri="http://127.0.0.1:56121/callback",
                scopes=("https://www.googleapis.com/auth/calendar.events",),
            )
        return providers

    def begin(self, actor_id: str, provider: str) -> str:
        config = self._provider(provider)
        verifier, challenge = make_pkce_pair()
        state = secrets.token_urlsafe(24)
        pending = {
            "provider": provider,
            "actor_id": actor_id,
            "state": state,
            "code_verifier": verifier,
            "created_at": int(time.time()),
            "redirect_uri": config.redirect_uri,
            "code_challenge": challenge,
        }
        self._pending_path(provider, actor_id).write_text(json.dumps(pending, indent=2), encoding="utf-8")
        query = urlencode(
            {
                "response_type": "code",
                "client_id": config.client_id,
                "redirect_uri": config.redirect_uri,
                "scope": " ".join(config.scopes),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent",
                "plan": "generic",
                "referrer": "tomo-core",
            }
        )
        return f"{config.auth_url}?{query}"

    def complete(self, actor_id: str, provider: str, callback_or_code: str) -> dict[str, Any]:
        config = self._provider(provider)
        pending_path = self._pending_path(provider, actor_id)
        if not pending_path.exists():
            raise OAuthError(f"no pending {provider} oauth flow for this telegram user")
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        code, state = parse_code_and_state(callback_or_code)
        if state and state != pending.get("state"):
            raise OAuthError("oauth state mismatch")
        data = {
            "grant_type": "authorization_code",
            "client_id": config.client_id,
            "code": code,
            "redirect_uri": config.redirect_uri,
            "code_verifier": pending["code_verifier"],
            "code_challenge": pending.get("code_challenge", ""),
            "code_challenge_method": "S256",
        }
        if config.client_secret:
            data["client_secret"] = config.client_secret
        response = httpx.post(config.token_url, data=data, timeout=60)
        response.raise_for_status()
        token = response.json()
        token_payload = {
            **token,
            "provider": provider,
            "actor_id": actor_id,
            "saved_at": int(time.time()),
            "config": asdict(config),
        }
        self._token_path(provider, actor_id).write_text(json.dumps(token_payload, indent=2), encoding="utf-8")
        pending_path.unlink(missing_ok=True)
        return token_payload

    def has_pending(self, actor_id: str) -> bool:
        return bool(list(self.oauth_dir.glob(f"pending_*_{self._safe(actor_id)}.json")))

    def complete_any_pending(self, actor_id: str, callback_or_code: str) -> tuple[str, dict[str, Any]]:
        pending = sorted(self.oauth_dir.glob(f"pending_*_{self._safe(actor_id)}.json"))
        if not pending:
            raise OAuthError("no pending oauth flow for this telegram user")
        provider = pending[0].name.removeprefix("pending_").removesuffix(f"_{self._safe(actor_id)}.json")
        return provider, self.complete(actor_id, provider, callback_or_code)

    def token_path(self, provider: str, actor_id: str) -> Path:
        return self._token_path(provider, actor_id)

    def _provider(self, provider: str) -> OAuthProviderConfig:
        if provider == "xai" and "supergrok" in self.providers:
            provider = "supergrok"
        if provider not in self.providers:
            raise OAuthError(f"{provider} is not configured")
        return self.providers[provider]

    def _pending_path(self, provider: str, actor_id: str) -> Path:
        if provider == "xai":
            provider = "supergrok"
        return self.oauth_dir / f"pending_{provider}_{self._safe(actor_id)}.json"

    def _token_path(self, provider: str, actor_id: str) -> Path:
        if provider == "xai":
            provider = "supergrok"
        return self.oauth_dir / f"token_{provider}_{self._safe(actor_id)}.json"

    @staticmethod
    def _safe(value: str) -> str:
        return value.replace("/", "_").replace(":", "_")
