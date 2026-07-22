from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import secrets
from typing import Callable, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, StrictBool, StrictInt

from .onboarding_store import TelegramOnboardingStore
from .peer_capability import PeerCapabilityError, load_or_create_key, verify_capability
from .peer_exchange import PeerError, PeerExchange


class UserRequest(BaseModel):
    user_id: str = Field(alias="userId", min_length=1, max_length=256)
    model_config = {"extra": "forbid"}


class HandleRequest(UserRequest):
    handle: str = Field(pattern=r"^[a-z0-9_]{3,32}$")


class InvitationRequest(UserRequest):
    peer_handle: str = Field(alias="peerHandle", pattern=r"^[a-z0-9_]{3,32}$")


class GrantRequest(UserRequest):
    communicate: StrictBool
    auto_reply: StrictBool = Field(alias="autoReply")
    share_availability: StrictBool = Field(alias="shareAvailability")
    expected_revision: StrictInt = Field(alias="expectedRevision", ge=0)
    expires_at: datetime | None = Field(default=None, alias="expiresAt")


class SandboxAskRequest(BaseModel):
    peer_handle: str = Field(alias="peerHandle", pattern=r"^[a-z0-9_]{3,32}$")
    purpose: str = Field(min_length=1, max_length=256)
    disclosure_kind: Literal["ordinary_message", "availability", "sensitive"] = Field(
        alias="disclosureKind"
    )
    message: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = Field(
        default=None, alias="threadId", min_length=1, max_length=256
    )
    call_id: str = Field(alias="callId", min_length=1, max_length=256)
    model_config = {"extra": "forbid"}


class PurgeHistoryRequest(UserRequest):
    confirm: StrictBool


def create_peer_router(
    data_dir: str | Path,
    *,
    api_key: str | None = None,
    onboarding: TelegramOnboardingStore | None = None,
    peer_exchange: PeerExchange | None = None,
    peer_key: bytes | None = None,
    clock: Callable[[], int] | None = None,
) -> APIRouter:
    root, configured_key, store, exchange, signing_key = (
        Path(data_dir),
        api_key,
        onboarding,
        peer_exchange,
        peer_key,
    )
    now = clock or (lambda: int(datetime.now(timezone.utc).timestamp()))
    router = APIRouter()

    def onboarding_store() -> TelegramOnboardingStore:
        nonlocal store
        if store is None:
            store = TelegramOnboardingStore(root)
        return store

    def peers() -> PeerExchange:
        nonlocal exchange
        if exchange is None:
            exchange = PeerExchange(
                root,
                source_generation_active=onboarding_store().generation_not_superseded,
            )
        else:
            exchange.set_source_generation_active(
                onboarding_store().generation_not_superseded
            )
        return exchange

    def bff_owner(user_id: str, x_api_key: str | None) -> str:
        if (
            configured_key is None
            or x_api_key is None
            or not secrets.compare_digest(x_api_key, configured_key)
        ):
            raise HTTPException(status_code=401, detail="invalid api key")
        return onboarding_store().tomo_id_for_user(user_id)

    def sandbox_owner(request: Request, operation: str) -> str:
        nonlocal signing_key
        authorization = request.headers.get("authorization", "")
        bindings = tuple(
            request.headers.get(name)
            for name in (
                "x-tomo-owner-id",
                "x-tomo-actor-id",
                "x-tomo-destination",
                "x-tomo-session-id",
                "x-tomo-generation-id",
            )
        )
        if not authorization.startswith("Bearer ") or any(
            value is None for value in bindings
        ):
            raise HTTPException(status_code=401, detail="invalid capability")
        if signing_key is None:
            signing_key = load_or_create_key(root)
        try:
            claim = verify_capability(
                signing_key, authorization[7:], now=now(), operation=operation
            )
        except PeerCapabilityError:
            raise HTTPException(status_code=401, detail="invalid capability") from None
        owner, actor, destination, session, generation = bindings
        if (owner, actor, destination, session, generation) != (
            claim.owner_id,
            claim.actor_id,
            claim.destination,
            claim.session_id,
            claim.generation_id,
        ):
            raise HTTPException(status_code=401, detail="invalid capability")
        context = onboarding_store().active_generation_context(
            claim.owner_id, claim.generation_id
        )
        if context is None or (actor, destination, session) != (
            context.actor_id,
            f"telegram:{context.chat_id}",
            context.session_id,
        ):
            raise HTTPException(status_code=401, detail="invalid capability")
        return claim.owner_id

    def safe_peer_error(error: PeerError) -> HTTPException:
        code = str(error)
        status = (
            404
            if code in {"handle_not_found", "not_found"}
            else 409
            if code in {"stale_revision", "replayed"}
            else 429
            if code == "rate_limited"
            else 400
        )
        return HTTPException(status_code=status, detail="peer request rejected")

    @router.put("/v1/peers/me/handle")
    def set_handle(
        body: HandleRequest, x_api_key: str | None = Header(default=None)
    ) -> dict[str, bool]:
        peers().register_handle(bff_owner(body.user_id, x_api_key), body.handle)
        return {"ok": True}

    @router.get("/v1/peers/me")
    def bff_me(
        user_id: str = Query(alias="userId", min_length=1, max_length=256),
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, str | None]:
        return {"handle": peers().handle_for_owner(bff_owner(user_id, x_api_key))}

    @router.post("/v1/peers/relationships/{relationship_id}/purge-history")
    def bff_purge_history(
        relationship_id: str,
        body: PurgeHistoryRequest, x_api_key: str | None = Header(default=None)
    ) -> dict[str, bool]:
        try:
            peers().purge_owner_history(
                bff_owner(body.user_id, x_api_key), relationship_id, confirm=body.confirm
            )
        except PeerError as error:
            raise safe_peer_error(error) from None
        return {"ok": True}

    @router.get("/v1/peers/relationships")
    def bff_list_relationships(
        user_id: str = Query(alias="userId", min_length=1, max_length=256),
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        return {
            "relationships": [
                _relationship(item)
                for item in peers().list_relationships(bff_owner(user_id, x_api_key))
            ]
        }

    @router.get("/v1/peers/relationships/{relationship_id}/history")
    def bff_relationship_history(
        relationship_id: str,
        user_id: str = Query(alias="userId", min_length=1, max_length=256),
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        history = peers().relationship_history(
            bff_owner(user_id, x_api_key), relationship_id
        )
        if history is None:
            raise HTTPException(status_code=404, detail="peer request rejected")
        return {"history": [_history(item) for item in history]}

    @router.post("/v1/peers/invitations")
    def invite(
        body: InvitationRequest, x_api_key: str | None = Header(default=None)
    ) -> dict[str, object]:
        owner = bff_owner(body.user_id, x_api_key)
        try:
            peers().invite(owner, body.peer_handle)
        except PeerError:
            # Invitation submission must not reveal handle existence or state.
            pass
        return {"ok": True}

    @router.post("/v1/peers/relationships/{relationship_id}/accept")
    def accept(
        relationship_id: str,
        body: UserRequest,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, bool]:
        try:
            peers().accept(bff_owner(body.user_id, x_api_key), relationship_id)
        except PeerError as error:
            raise safe_peer_error(error) from None
        return {"ok": True}

    @router.patch("/v1/peers/relationships/{relationship_id}/grant")
    def grant(
        relationship_id: str,
        body: GrantRequest,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        try:
            value = peers().update_grant(
                bff_owner(body.user_id, x_api_key),
                relationship_id,
                None,
                body.communicate,
                body.auto_reply,
                body.share_availability,
                body.expected_revision,
                expires_at=body.expires_at,
            )
        except PeerError as error:
            raise safe_peer_error(error) from None
        return {
            "grant": {
                "communicate": value.communicate,
                "autoReply": value.auto_reply,
                "shareAvailability": value.share_availability,
                "revision": value.revision,
                "expiresAt": None
                if value.expires_at is None
                else value.expires_at.isoformat(),
            }
        }

    @router.post("/v1/peers/relationships/{relationship_id}/revoke")
    def revoke(
        relationship_id: str,
        body: UserRequest,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, bool]:
        try:
            peers().revoke(bff_owner(body.user_id, x_api_key), relationship_id)
        except PeerError as error:
            raise safe_peer_error(error) from None
        return {"ok": True}

    @router.post("/v1/peers/relationships/{relationship_id}/block")
    def block(
        relationship_id: str,
        body: UserRequest,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, bool]:
        try:
            peers().revoke(
                bff_owner(body.user_id, x_api_key), relationship_id, block=True
            )
        except PeerError as error:
            raise safe_peer_error(error) from None
        return {"ok": True}

    @router.get("/v1/peers/requests/{request_id}")
    def bff_inspect_request(
        request_id: str,
        user_id: str = Query(alias="userId", min_length=1, max_length=256),
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        return _inspection(
            peers().inspect_request(bff_owner(user_id, x_api_key), request_id)
        )

    @router.get("/v1/peer-agent/relationships")
    def list_relationships(request: Request) -> dict[str, object]:
        return {
            "relationships": [
                _relationship(item)
                for item in peers().list_relationships(
                    sandbox_owner(request, "list_relationships")
                )
            ]
        }

    @router.post("/v1/peer-agent/requests")
    def ask(body: SandboxAskRequest, request: Request) -> dict[str, object]:
        owner = sandbox_owner(request, "ask")
        try:
            result = peers().submit(
                owner,
                body.peer_handle,
                request.headers["x-tomo-generation-id"],
                body.call_id,
                body.disclosure_kind,
                body.message,
                purpose=body.purpose,
                thread_id=body.thread_id,
            )
        except PeerError as error:
            raise safe_peer_error(error) from None
        return {
            "requestId": result.request_id,
            "status": result.status,
            "peerHandle": result.peer_handle,
            "threadId": result.thread_id,
            "pendingId": result.pending_id,
        }

    @router.get("/v1/peer-agent/requests/{request_id}")
    def inspect_request(request_id: str, request: Request) -> dict[str, object]:
        return _inspection(
            peers().inspect_request(
                sandbox_owner(request, "inspect_request"), request_id
            )
        )

    @router.get("/v1/peer-agent/threads/{thread_id}")
    def resume_thread(thread_id: str, request: Request) -> dict[str, object]:
        return _inspection(peers().resume_thread(sandbox_owner(request, "inspect_request"), thread_id))

    return router


def _inspection(inspection: object) -> dict[str, object]:
    if inspection is None:
        raise HTTPException(status_code=404, detail="peer request not found")
    response = (
        None
        if inspection.response is None
        else {
            "frames": list(inspection.response.frames),
            "status": inspection.response.status.value,
            "createdAt": inspection.response.created_at.isoformat(),
        }
    )
    return {
        "requestId": inspection.request_id,
        "status": inspection.status,
        "peerHandle": inspection.peer_handle,
        "threadId": inspection.thread_id,
        "expiresAt": None
        if inspection.pending_expires_at is None
        else inspection.pending_expires_at.isoformat(),
        "response": response,
        "errorCode": inspection.error_code,
    }


def _relationship(relationship: object) -> dict[str, object]:
    return {
        "relationshipId": relationship.relationship_id,
        "peerHandle": relationship.peer_handle,
        "status": relationship.status.value,
        "grant": None
        if relationship.grant is None
        else {
            "communicate": relationship.grant.communicate,
            "autoReply": relationship.grant.auto_reply,
            "shareAvailability": relationship.grant.share_availability,
            "revision": relationship.grant.revision,
            "expiresAt": None
            if relationship.grant.expires_at is None
            else relationship.grant.expires_at.isoformat(),
        },
        "peerGrant": None
        if relationship.peer_grant is None
        else {
            "communicate": relationship.peer_grant.communicate,
            "autoReply": relationship.peer_grant.auto_reply,
            "shareAvailability": relationship.peer_grant.share_availability,
            "revision": relationship.peer_grant.revision,
            "expiresAt": None
            if relationship.peer_grant.expires_at is None
            else relationship.peer_grant.expires_at.isoformat(),
        },
        "peerCommunicate": relationship.peer_communicate,
        "peerAutoReply": relationship.peer_auto_reply,
        "peerShareAvailability": relationship.peer_share_availability,
        "relationshipRevision": relationship.relationship_revision,
        "expiresAt": None
        if relationship.expires_at is None
        else relationship.expires_at.isoformat(),
        "canAccept": relationship.can_accept,
    }


def _history(entry: object) -> dict[str, object]:
    return {
        "requestId": entry.request_id,
        "threadId": entry.thread_id,
        "direction": entry.direction,
        "kind": entry.kind.value,
        "status": entry.status.value,
        "createdAt": entry.created_at.isoformat(),
        "responseStatus": None
        if entry.response_status is None
        else entry.response_status.value,
    }
