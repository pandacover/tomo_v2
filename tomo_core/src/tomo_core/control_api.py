from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .onboarding_store import TelegramOnboardingStore


class InstallLinkRequest(BaseModel):
    user_id: str = Field(alias="userId", min_length=1)
    email: str | None = None


class InstallLinkResponse(BaseModel):
    dm_url: str = Field(alias="dmUrl")
    browser_url: str = Field(alias="browserUrl")
    expires_at: int = Field(alias="expiresAt")


def create_app(data_dir: str | Path | None = None, api_key: str | None = None, bot_username: str | None = None) -> FastAPI:
    resolved_data_dir = Path(data_dir or os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core"))
    resolved_api_key = api_key if api_key is not None else os.getenv("TOMO_CONTROL_API_KEY")
    resolved_bot_username = bot_username or os.getenv("TOMO_TELEGRAM_GLOBAL_BOT_USERNAME")
    store: TelegramOnboardingStore | None = None
    app = FastAPI(title="tomo core control api")

    def onboarding_store() -> TelegramOnboardingStore:
        nonlocal store
        if store is None:
            store = TelegramOnboardingStore(resolved_data_dir)
        return store

    @app.get("/v1/health")
    def health() -> dict[str, str]:
        return {"ok": "true"}

    @app.post("/v1/onboarding/telegram/install-link", response_model=InstallLinkResponse, response_model_by_alias=True)
    def create_install_link(body: InstallLinkRequest, x_api_key: str | None = Header(default=None)) -> InstallLinkResponse:
        if resolved_api_key and x_api_key != resolved_api_key:
            raise HTTPException(status_code=401, detail="invalid api key")
        if not resolved_bot_username:
            raise HTTPException(status_code=503, detail="telegram global bot username is not configured")
        link = onboarding_store().create_install_link(user_id=body.user_id, bot_username=resolved_bot_username)
        return InstallLinkResponse(dmUrl=link.dm_url, browserUrl=link.browser_url, expiresAt=link.expires_at)

    return app


app = create_app()
