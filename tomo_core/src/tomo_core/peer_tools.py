from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .tools import BoundTool, ToolRegistry, ToolSpec


_MAX_RESPONSE_BYTES = 65536
_POLL_SECONDS = 0.25
_POLL_TIMEOUT_SECONDS = 60
_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "denied", "confirmation_pending"}
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
        return _relationship_observation(
            self.request("GET", "/v1/peer-agent/relationships", raw=True)
        )

    def inspect_request(self, request_id: str) -> dict[str, object]:
        return self.request(
            "GET", f"/v1/peer-agent/requests/{quote(request_id, safe='')}"
        )

    def ask(self, arguments: dict[str, object]) -> dict[str, object]:
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
                "List relationships available to this conversation.",
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
                "Ask a connected peer a bounded question and wait for its terminal result.",
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
        BoundTool(ToolSpec("peer_resume", "Retrieve the latest safe result for an existing peer thread.", {"type": "object", "properties": {"thread_id": identifier}, "required": ["thread_id"], "additionalProperties": False}, read_only=True, parallel_safe=True, internal_context=False, unattended_safe=False), client.resume),
    )
    return ToolRegistry(tools)


def _observation(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {"ok": False, "status": "failed"}
    if value.get("ok") is False:
        return {"ok": False, "status": "failed"}
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
    return result


def _relationship_observation(value: object) -> dict[str, object]:
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
                safe.append(
                    {
                        "peer_handle": handle[:32],
                        "status": status[:32],
                    }
                )
    return {"ok": True, "status": "completed", "relationships": safe}
