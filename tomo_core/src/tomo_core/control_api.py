from __future__ import annotations

import os
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Callable, Literal, NamedTuple, Protocol

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from .cron_capability import CronCapability, CronCapabilityError, load_or_create_key, verify_capability
from .cron_models import CronJob, JobIntent, LifecyclePolicy, ScheduleSpec
from .cron_store import CronStore
from .onboarding_store import TelegramOnboardingStore
from .attachment_capability import AttachmentCapability, AttachmentCapabilityError, load_or_create_attachment_key, verify_attachment_capability
from .telegram_bot import TelegramBotApiClient, TelegramBotApiError
from .vision import DownloadedAttachment


class TelegramFileSource(Protocol):
    def fetch(self, file_id: str) -> DownloadedAttachment: ...


class AttachmentCapabilityContext(NamedTuple):
    token: str
    owner_id: str
    generation_id: str


class AttachmentResolveRequest(BaseModel):
    file_id: str = Field(alias="fileId", min_length=1, max_length=4096)
    model_config = {"extra": "forbid"}


class InstallLinkRequest(BaseModel):
    user_id: str = Field(alias="userId", min_length=1)
    email: str | None = None


class InstallLinkResponse(BaseModel):
    dm_url: str = Field(alias="dmUrl")
    browser_url: str = Field(alias="browserUrl")
    expires_at: int = Field(alias="expiresAt")


class CronOnceScheduleRequest(BaseModel):
    kind: Literal["once"]
    at: str = Field(max_length=64)
    model_config = {"extra": "forbid"}


class CronIntervalScheduleRequest(BaseModel):
    kind: Literal["interval"]
    every_seconds: float = Field(alias="everySeconds", gt=0)
    starts_at: str | None = Field(default=None, alias="startsAt", max_length=64)
    model_config = {"extra": "forbid"}

    @field_validator("every_seconds", mode="before")
    @classmethod
    def validate_every_seconds(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("everySeconds must be a finite number")
        return value


class CronExpressionScheduleRequest(BaseModel):
    kind: Literal["cron"]
    expression: str = Field(max_length=128)
    timezone_name: str = Field(default="UTC", alias="timezoneName", max_length=128)
    model_config = {"extra": "forbid"}


class CronDelayScheduleRequest(BaseModel):
    kind: Literal["delay"]
    after_seconds: float = Field(alias="afterSeconds", gt=0, le=31536000)
    model_config = {"extra": "forbid"}

    @field_validator("after_seconds", mode="before")
    @classmethod
    def validate_after_seconds(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("afterSeconds must be a finite number")
        return value


CronScheduleRequest = Annotated[
    CronOnceScheduleRequest | CronIntervalScheduleRequest | CronExpressionScheduleRequest | CronDelayScheduleRequest,
    Field(discriminator="kind"),
]


class CronLifecycleRequest(BaseModel):
    ends_at: str | None = Field(default=None, alias="endsAt", max_length=64)
    max_successful_runs: int | None = Field(default=None, alias="maxSuccessfulRuns")
    model_config = {"extra": "forbid"}


class CronJobRequest(BaseModel):
    intent: str = Field(min_length=1, max_length=4000)
    constraints: list[str] = Field(default_factory=list, max_length=32)
    schedule: CronScheduleRequest
    lifecycle: CronLifecycleRequest = Field(default_factory=CronLifecycleRequest)
    revision: int | None = Field(default=None, ge=1)
    model_config = {"extra": "forbid"}

    @field_validator("constraints")
    @classmethod
    def validate_constraint_budget(cls, values: list[str]) -> list[str]:
        constraints = [value.strip() for value in values]
        if any(not value or len(value) > 1000 for value in constraints):
            raise ValueError("constraints must contain non-blank strings of at most 1000 characters")
        if sum(len(value) for value in constraints) > 8000:
            raise ValueError("constraints cannot exceed 8000 characters")
        return constraints


class CronRevisionRequest(BaseModel):
    revision: int = Field(ge=1)
    model_config = {"extra": "forbid"}


def create_app(data_dir: str | Path | None = None, api_key: str | None = None, bot_username: str | None = None, now: Callable[[], datetime] | None = None, telegram_files: TelegramFileSource | None = None, attachment_key: bytes | None = None) -> FastAPI:
    resolved_data_dir = Path(data_dir or os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core"))
    resolved_api_key = api_key if api_key is not None else os.getenv("TOMO_CONTROL_API_KEY")
    resolved_bot_username = bot_username or os.getenv("TOMO_TELEGRAM_GLOBAL_BOT_USERNAME")
    store: TelegramOnboardingStore | None = None
    cron_store: CronStore | None = None
    cron_key: bytes | None = None
    attachment_signing_key = attachment_key
    attachment_source = telegram_files
    clock = now or (lambda: datetime.now(timezone.utc))
    app = FastAPI(title="tomo core control api")

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
        # Never reflect non-finite invalid input through JSON serialization.
        return JSONResponse(status_code=422, content={"detail": [{key: item[key] for key in ("type", "loc", "msg")} for item in error.errors()]})

    def onboarding_store() -> TelegramOnboardingStore:
        nonlocal store
        if store is None:
            store = TelegramOnboardingStore(resolved_data_dir)
        return store

    def jobs() -> CronStore:
        nonlocal cron_store
        if cron_store is None:
            cron_store = CronStore(resolved_data_dir)
        return cron_store

    def cron_capability(request: Request, operation: str) -> CronCapability:
        nonlocal cron_key
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="invalid capability")
        if cron_key is None:
            cron_key = load_or_create_key(resolved_data_dir)
        try:
            capability = verify_capability(cron_key, authorization[7:], operation=operation)
        except CronCapabilityError:
            raise HTTPException(status_code=401, detail="invalid capability") from None
        context = (
            request.headers.get("x-tomo-owner-id"),
            request.headers.get("x-tomo-actor-id"),
            request.headers.get("x-tomo-destination"),
            request.headers.get("x-tomo-session-id"),
        )
        if context != (capability.owner_id, capability.actor_id, capability.destination, capability.session_id):
            raise HTTPException(status_code=401, detail="invalid capability")
        return capability

    def attachment_capability(request: Request) -> AttachmentCapabilityContext:
        nonlocal attachment_signing_key
        authorization = request.headers.get("authorization", "")
        owner_id = request.headers.get("x-tomo-owner-id")
        generation_id = request.headers.get("x-tomo-generation-id")
        if not authorization.startswith("Bearer ") or not owner_id or not generation_id:
            raise HTTPException(status_code=401, detail="invalid capability")
        if attachment_signing_key is None:
            attachment_signing_key = load_or_create_attachment_key(resolved_data_dir)
        return AttachmentCapabilityContext(authorization[7:], owner_id, generation_id)

    @app.get("/v1/health")
    def health() -> dict[str, str]:
        return {"ok": "true"}

    @app.post("/v1/attachments/resolve")
    def resolve_attachment(body: AttachmentResolveRequest, request: Request) -> Response:
        capability_context = attachment_capability(request)
        try:
            verify_attachment_capability(attachment_signing_key, capability_context.token, body.file_id, owner_id=capability_context.owner_id, generation_id=capability_context.generation_id, now=int(clock().timestamp()))
        except AttachmentCapabilityError:
            raise HTTPException(status_code=401, detail="invalid capability") from None
        nonlocal attachment_source
        if attachment_source is None:
            token_value = os.getenv("TOMO_TELEGRAM_GLOBAL_BOT_TOKEN")
            if not token_value:
                raise HTTPException(status_code=503, detail="attachment service unavailable")
            attachment_source = TelegramBotApiClient(token_value)
        try:
            attachment = attachment_source.fetch(body.file_id)
        except TelegramBotApiError as error:
            if error.args == ("telegram_file_too_large",):
                raise HTTPException(status_code=413, detail="attachment too large") from None
            raise HTTPException(status_code=503, detail="attachment service unavailable") from None
        if not isinstance(attachment, DownloadedAttachment):
            raise HTTPException(status_code=503, detail="attachment service unavailable")
        if len(attachment.data) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="attachment too large")
        return Response(attachment.data, media_type=attachment.mime_type, headers={"Cache-Control": "no-store"})

    @app.post("/v1/onboarding/telegram/install-link", response_model=InstallLinkResponse, response_model_by_alias=True)
    def create_install_link(body: InstallLinkRequest, x_api_key: str | None = Header(default=None)) -> InstallLinkResponse:
        if resolved_api_key and x_api_key != resolved_api_key:
            raise HTTPException(status_code=401, detail="invalid api key")
        if not resolved_bot_username:
            raise HTTPException(status_code=503, detail="telegram global bot username is not configured")
        link = onboarding_store().create_install_link(user_id=body.user_id, bot_username=resolved_bot_username)
        return InstallLinkResponse(dmUrl=link.dm_url, browserUrl=link.browser_url, expiresAt=link.expires_at)

    @app.post("/v1/cron/jobs")
    def create_cron_job(body: CronJobRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, object]:
        capability = cron_capability(request, "create")
        if not idempotency_key or len(idempotency_key) > 256:
            raise HTTPException(status_code=400, detail="missing idempotency key")
        job_id = hashlib.sha256(f"{capability.owner_id}\ncreate\n{idempotency_key}".encode("utf-8")).hexdigest()[:32]
        try:
            job = CronJob(job_id, capability.owner_id, capability.destination, JobIntent(body.intent, tuple(body.constraints)), _schedule(body.schedule, clock()), _lifecycle(body.lifecycle))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="invalid cron job") from None
        try:
            created, _ = jobs().mutate_idempotently(capability.owner_id, "create", idempotency_key, _canonical(body), job=job)
        except ValueError as error:
            if str(error) == "idempotency conflict":
                raise HTTPException(status_code=409, detail="cron idempotency conflict") from None
            raise
        return {"ok": True, "job": _job(created)}

    @app.get("/v1/cron/jobs")
    def list_cron_jobs(request: Request) -> dict[str, object]:
        capability = cron_capability(request, "list")
        return {"ok": True, "jobs": [_job(job) for job in jobs().list(capability.owner_id)]}

    @app.get("/v1/cron/jobs/{job_id}")
    def inspect_cron_job(job_id: str, request: Request) -> dict[str, object]:
        capability = cron_capability(request, "inspect")
        return {"ok": True, "job": _required_job(capability.owner_id, job_id)}

    @app.patch("/v1/cron/jobs/{job_id}")
    def update_cron_job(job_id: str, body: CronJobRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, object]:
        capability = cron_capability(request, "update")
        _idempotency_key(idempotency_key)
        current = jobs().get(capability.owner_id, job_id)
        if current is None:
            raise HTTPException(status_code=404, detail="cron job not found")
        try:
            replacement = CronJob(job_id, capability.owner_id, capability.destination, JobIntent(body.intent, tuple(body.constraints)), _schedule(body.schedule, clock()), _lifecycle(body.lifecycle))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="invalid cron job") from None
        if body.revision is None:
            raise HTTPException(status_code=422, detail="missing cron revision")
        try:
            updated, _ = jobs().mutate_idempotently(capability.owner_id, "update", idempotency_key, _canonical({"jobId": job_id, **body.model_dump(by_alias=True)}), job=replacement, revision=body.revision)
        except ValueError as error:
            if str(error) == "idempotency conflict": raise HTTPException(status_code=409, detail="cron idempotency conflict") from None
            raise
        if updated is None:
            raise HTTPException(status_code=409, detail="cron revision conflict")
        return {"ok": True, "job": _job(updated)}

    def action(job_id: str, body: CronRevisionRequest, request: Request, operation: str, idempotency_key: str | None) -> dict[str, object]:
        capability = cron_capability(request, operation)
        _idempotency_key(idempotency_key)
        if jobs().get(capability.owner_id, job_id) is None:
            raise HTTPException(status_code=404, detail="cron job not found")
        try:
            changed, _ = jobs().mutate_idempotently(capability.owner_id, operation, idempotency_key, _canonical({"jobId": job_id, **body.model_dump(by_alias=True)}), job_id=job_id, revision=body.revision)
        except ValueError as error:
            if str(error) == "idempotency conflict": raise HTTPException(status_code=409, detail="cron idempotency conflict") from None
            raise
        if changed is None:
            raise HTTPException(status_code=409, detail="cron revision conflict")
        return {"ok": True, "job": _job(changed)}

    @app.post("/v1/cron/jobs/{job_id}/pause")
    def pause_cron_job(job_id: str, body: CronRevisionRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, object]: return action(job_id, body, request, "pause", idempotency_key)
    @app.post("/v1/cron/jobs/{job_id}/resume")
    def resume_cron_job(job_id: str, body: CronRevisionRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, object]: return action(job_id, body, request, "resume", idempotency_key)
    @app.post("/v1/cron/jobs/{job_id}/run-now")
    def run_cron_job(job_id: str, body: CronRevisionRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, object]: return action(job_id, body, request, "run_now", idempotency_key)
    @app.post("/v1/cron/jobs/{job_id}/delete")
    def delete_cron_job(job_id: str, body: CronRevisionRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, object]: return action(job_id, body, request, "delete", idempotency_key)

    @app.get("/v1/cron/jobs/{job_id}/history")
    def cron_history(job_id: str, request: Request) -> dict[str, object]:
        capability = cron_capability(request, "history")
        _required_job(capability.owner_id, job_id)
        return {"ok": True, "history": list(jobs().history(capability.owner_id, job_id))}

    def _required_job(owner_id: str, job_id: str):
        job = jobs().get(owner_id, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="cron job not found")
        return job

    return app


app = create_app()


def _timestamp(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def _idempotency_key(value: str | None) -> str:
    if not value or len(value) > 256:
        raise HTTPException(status_code=400, detail="missing idempotency key")
    return value


def _canonical(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(by_alias=True)
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _schedule(value: CronScheduleRequest, now: datetime | None = None) -> ScheduleSpec:
    if value.kind == "once": return ScheduleSpec.once(_timestamp(value.at))  # type: ignore[arg-type]
    if value.kind == "interval": return ScheduleSpec.interval(value.every_seconds, starts_at=_timestamp(value.starts_at))  # type: ignore[arg-type]
    if value.kind == "cron": return ScheduleSpec.cron(value.expression or "", value.timezone_name)
    if value.kind == "delay":
        if now is None or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("delay requires timezone-aware current time")
        return ScheduleSpec.once(now.astimezone(timezone.utc) + timedelta(seconds=value.after_seconds))  # type: ignore[arg-type]
    raise ValueError("invalid schedule")


def _lifecycle(value: CronLifecycleRequest) -> LifecyclePolicy:
    return LifecyclePolicy(_timestamp(value.ends_at), value.max_successful_runs)


def _job(job: CronJob) -> dict[str, object]:
    schedule = {"kind": job.schedule.kind, "at": job.schedule.at.isoformat() if job.schedule.at else None, "everySeconds": job.schedule.every.total_seconds() if job.schedule.every else None, "expression": job.schedule.expression, "timezoneName": job.schedule.timezone_name, "startsAt": job.schedule.starts_at.isoformat() if job.schedule.starts_at else None}
    lifecycle = {"endsAt": job.lifecycle.ends_at.isoformat() if job.lifecycle.ends_at else None, "maxSuccessfulRuns": job.lifecycle.max_successful_runs}
    return {"jobId": job.job_id, "intent": job.intent.text, "constraints": list(job.intent.constraints), "schedule": schedule, "lifecycle": lifecycle, "status": job.status.value, "revision": job.revision}
