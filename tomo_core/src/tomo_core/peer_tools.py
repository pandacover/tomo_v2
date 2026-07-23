from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .peer_safety import ordinary_request_requires_grounding
from .tools import BoundTool, ToolRegistry, ToolSpec


_MAX_RESPONSE_BYTES = 65536
_POLL_SECONDS = 0.25
_POLL_TIMEOUT_SECONDS = 60
_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "denied", "confirmation_pending"}
)
_UNAVAILABLE_FAILURE_CODES = frozenset(
    {
        "access_token_failed",
        "auth_expired",
        "peer_execution_failed",
        "peer_installation_missing",
        "peer_request_failed",
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
        "volume_create_failed",
    }
)
_SECRET_FIELDS = frozenset(
    {
        "authorization",
        "capability",
        "credential",
        "credentials",
        "key",
        "secret",
        "token",
    }
)


@dataclass(frozen=True)
class PeerApiClient:
    base_url: str
    capability: str
    owner_id: str
    actor_id: str
    destination: str
    session_id: str
    generation_id: str
    timeout: float = 10
    opener: Callable[..., object] = urlopen
    sleeper: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC)
    _listed_relationships: dict[str, dict[str, object]] = field(default_factory=dict, init=False, repr=False)

    def request(
        self, method: str, path: str, body: dict[str, object] | None = None, *, raw: bool = False
    ) -> dict[str, object]:
        if not self.base_url.startswith(("https://", "http://")):
            result = {"ok": False, "error": "peer_unavailable"}
            return result if raw else _observation(result)
        headers = {
            "Authorization": f"Bearer {self.capability}",
            "Accept": "application/json",
            "X-Tomo-Owner-Id": self.owner_id,
            "X-Tomo-Actor-Id": self.actor_id,
            "X-Tomo-Destination": self.destination,
            "X-Tomo-Session-Id": self.session_id,
            "X-Tomo-Generation-Id": self.generation_id,
        }
        payload = None
        if body is not None:
            payload = json.dumps(
                body, ensure_ascii=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            request = Request(
                self.base_url.rstrip("/") + path,
                data=payload,
                headers=headers,
                method=method,
            )
            with self.opener(request, timeout=self.timeout) as response:  # type: ignore[union-attr]
                response_bytes = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(response_bytes) > _MAX_RESPONSE_BYTES:
                result = {"ok": False, "error": "peer_invalid_response"}
                return result if raw else _observation(result)
            result = json.loads(response_bytes.decode("utf-8"))
        except HTTPError as error:
            result = {
                "ok": False,
                "error": "peer_not_found"
                if error.code == 404
                else "peer_unauthorized"
                if error.code in {401, 403}
                else "peer_request_failed",
            }
            return result if raw else _observation(result)
        except (URLError, OSError, ValueError, json.JSONDecodeError):
            result = {"ok": False, "error": "peer_unavailable"}
            return result if raw else _observation(result)
        return result if raw else _observation(result)

    def list_relationships(self) -> dict[str, object]:
        observation = _relationship_observation(
            self.request("GET", "/v1/peer-agent/relationships", raw=True), self.utc_now()
        )
        self._listed_relationships.clear()
        relationships = observation.get("relationships")
        if isinstance(relationships, list):
            self._listed_relationships.update(
                {
                    relationship["peer_handle"]: relationship
                    for relationship in relationships
                    if isinstance(relationship, dict) and isinstance(relationship.get("peer_handle"), str)
                }
            )
        return observation

    def inspect_request(self, request_id: str) -> dict[str, object]:
        return self.request(
            "GET", f"/v1/peer-agent/requests/{quote(request_id, safe='')}"
        )

    def ask(self, arguments: dict[str, object]) -> dict[str, object]:
        handle = arguments.get("peer_handle")
        relationship = self._listed_relationships.get(handle) if isinstance(handle, str) else None
        if not isinstance(relationship, dict) or not (
            relationship.get("status") == "active"
            and relationship.get("can_ask") is True
            and relationship.get("peer_auto_reply") is True
        ):
            return {
                "ok": False,
                "status": "failed",
                "error_code": "peer_connection_unavailable",
            }
        if arguments.get("disclosure_kind") == "ordinary_message" and ordinary_request_requires_grounding(arguments.get("message"), handle):
            return {
                "ok": False,
                "status": "failed",
                "error_code": "peer_grounding_required",
            }
        body = {
            "peerHandle": arguments["peer_handle"],
            "purpose": arguments["purpose"],
            "disclosureKind": arguments["disclosure_kind"],
            "message": arguments["message"],
            "callId": arguments["call_id"],
        }
        if "thread_id" in arguments:
            body["threadId"] = arguments["thread_id"]
        submitted = self._raw_request("POST", "/v1/peer-agent/requests", body)
        request_id = submitted.get("requestId") if isinstance(submitted, dict) else None
        thread_id = submitted.get("threadId") if isinstance(submitted, dict) else None
        if not isinstance(request_id, str) or not request_id:
            return _observation(submitted)
        deadline = self.clock() + _POLL_TIMEOUT_SECONDS
        while self.clock() < deadline:
            self.sleeper(_POLL_SECONDS)
            inspected = self._raw_request("GET", f"/v1/peer-agent/requests/{quote(request_id, safe='')}")
            if isinstance(inspected, dict) and inspected.get("status") in _TERMINAL_STATUSES:
                return _observation(inspected)
            if isinstance(inspected, dict) and inspected.get("ok") is False:
                return _observation(inspected)
        return {
            "ok": True,
            "status": "pending",
            **({"thread_id": thread_id[:256]} if isinstance(thread_id, str) and thread_id else {}),
        }

    def resume(self, arguments: dict[str, object]) -> dict[str, object]:
        return self.request("GET", f"/v1/peer-agent/threads/{quote(str(arguments['thread_id']), safe='')}")

    def _raw_request(self, method: str, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
        # Keep protocol identifiers only inside the polling loop.
        return self.request(method, path, body, raw=True)


def peer_registry(client: PeerApiClient) -> ToolRegistry:
    identifier = {"type": "string", "minLength": 1, "maxLength": 256}
    handle = {"type": "string", "pattern": "^[a-z0-9_]{3,32}$"}
    tools = (
        BoundTool(
            ToolSpec(
                "peer_list",
                "List connected Tomos and their safe readiness fields. Follow the indexed tomo-connections contract.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                read_only=True,
                parallel_safe=True,
                internal_context=False,
                unattended_safe=False,
            ),
            lambda _arguments: client.list_relationships(),
        ),
        BoundTool(
            ToolSpec(
                "peer_ask",
                "Ask a connected Tomo under the indexed tomo-connections contract and wait for its result.",
                {
                    "type": "object",
                    "properties": {
                        "peer_handle": handle,
                        "purpose": identifier,
                        "disclosure_kind": {
                            "type": "string",
                            "enum": ["ordinary_message", "availability", "sensitive"],
                        },
                        "message": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 2000,
                        },
                        "thread_id": identifier,
                    },
                    "required": [
                        "peer_handle",
                        "purpose",
                        "disclosure_kind",
                        "message",
                    ],
                    "additionalProperties": False,
                },
                read_only=False,
                parallel_safe=False,
                internal_context=False,
                unattended_safe=False,
            ),
            client.ask,
        ),
        BoundTool(ToolSpec("peer_resume", "Retrieve a safe peer result under the indexed tomo-connections contract.", {"type": "object", "properties": {"thread_id": identifier}, "required": ["thread_id"], "additionalProperties": False}, read_only=True, parallel_safe=True, internal_context=False, unattended_safe=False), client.resume),
    )
    return ToolRegistry(tools)


def _observation(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {"ok": False, "status": "failed"}
    error_code = _public_error_code(
        value.get("errorCode", value.get("error_code", value.get("error")))
    )
    if value.get("ok") is False:
        result: dict[str, object] = {"ok": False, "status": "failed"}
        if error_code is not None:
            result["error_code"] = error_code
        return result
    status = value.get("status")
    result: dict[str, object] = {"ok": True, "status": status[:64] if isinstance(status, str) else "unknown"}
    handle = value.get("peerHandle", value.get("peer_handle"))
    if isinstance(handle, str): result["peer_handle"] = handle[:32]
    thread = value.get("threadId", value.get("thread_id"))
    if isinstance(thread, str): result["thread_id"] = thread[:256]
    response = value.get("response")
    frames = response.get("frames") if isinstance(response, dict) else value.get("frames")
    if isinstance(frames, list): result["frames"] = [frame[:2000] for frame in frames[:3] if isinstance(frame, str)]
    expiry = value.get("expiresAt", value.get("expires_at"))
    if result["status"] in {"pending", "confirmation_pending"} and isinstance(expiry, str):
        result["expires_at"] = expiry[:64]
    if result["status"] in {"failed", "denied"} and error_code is not None:
        result["error_code"] = error_code
    return result


def _public_error_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if value in {
        "peer_connection_unavailable",
        "peer_grounding_required",
        "peer_invalid_response",
        "peer_timeout",
        "peer_unavailable",
    }:
        return value
    if value == "sandbox_timeout":
        return "peer_timeout"
    if value == "invalid_result":
        return "peer_invalid_response"
    if value in _UNAVAILABLE_FAILURE_CODES:
        return "peer_unavailable"
    if value in {"peer_not_found", "peer_unauthorized"}:
        return "peer_connection_unavailable"
    return None


def _effective_grant_flags(status: str, grant: object, peer_grant: object, now: datetime) -> tuple[bool, bool, bool]:
    if status != "active":
        return False, False, False
    return (
        _grant_allows(grant, "communicate", now),
        _grant_allows(peer_grant, "autoReply", now),
        _grant_allows(peer_grant, "shareAvailability", now),
    )


def _grant_allows(grant: object, flag: str, now: datetime) -> bool:
    if not isinstance(grant, dict) or grant.get(flag) is not True:
        return False
    expires_at = grant.get("expiresAt", grant.get("expires_at"))
    if expires_at is None:
        return True
    if not isinstance(expires_at, str):
        return False
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expiry.tzinfo is None:
        return False
    return expiry > now


def _relationship_observation(value: object, now: datetime) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("ok") is False:
        return {"ok": False, "status": "failed", "relationships": []}
    rows = value.get("relationships")
    safe: list[dict[str, object]] = []
    if isinstance(rows, list):
        for row in rows[:100]:
            if not isinstance(row, dict):
                continue
            handle = row.get("peerHandle", row.get("peer_handle"))
            status = row.get("status")
            if isinstance(handle, str) and isinstance(status, str):
                grant = row.get("grant")
                peer_grant = row.get("peerGrant", row.get("peer_grant"))
                can_ask, peer_auto_reply, peer_share_availability = _effective_grant_flags(status, grant, peer_grant, now)
                safe.append(
                    {
                        "peer_handle": handle[:32],
                        "status": status[:32],
                        "can_ask": can_ask,
                        "peer_auto_reply": peer_auto_reply,
                        "peer_share_availability": peer_share_availability,
                    }
                )
    return {"ok": True, "status": "completed", "relationships": safe}
