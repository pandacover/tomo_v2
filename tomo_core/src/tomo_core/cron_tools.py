from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote

from .tools import BoundTool, ToolRegistry, ToolSpec


def cron_idempotency_key(generation_id: str, operation: str, arguments: dict[str, object]) -> str:
    canonical = json.dumps(arguments, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(f"{generation_id}\n{operation}\n{canonical}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CronApiClient:
    control_url: str
    capability: str
    owner_id: str | None = None
    actor_id: str | None = None
    destination: str | None = None
    session_id: str | None = None
    opener: Callable[..., object] = urlopen

    def request(self, method: str, path: str, body: dict[str, object] | None = None, *, idempotency_key: str | None = None) -> dict[str, object]:
        if not self.control_url.startswith(("https://", "http://")):
            return {"ok": False, "error": "cron_unavailable"}
        payload = None if body is None else json.dumps(body, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.capability}", "Accept": "application/json"}
        if all((self.owner_id, self.actor_id, self.destination, self.session_id)):
            headers.update({
                "X-Tomo-Owner-Id": self.owner_id,
                "X-Tomo-Actor-Id": self.actor_id,
                "X-Tomo-Destination": self.destination,
                "X-Tomo-Session-Id": self.session_id,
            })
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            request = Request(self.control_url.rstrip("/") + path, data=payload, headers=headers, method=method)
            with self.opener(request, timeout=10) as response:  # type: ignore[union-attr]
                raw = response.read(65537)
                if len(raw) > 65536:
                    return {"ok": False, "error": "cron_invalid_response"}
                result = json.loads(raw.decode("utf-8"))
        except HTTPError as error:
            return {"ok": False, "error": "cron_not_found" if error.code == 404 else "cron_unauthorized" if error.code in {401, 403} else "cron_request_failed"}
        except (URLError, OSError, ValueError, json.JSONDecodeError):
            return {"ok": False, "error": "cron_unavailable"}
        if not isinstance(result, dict):
            return {"ok": False, "error": "cron_invalid_response"}
        return _bounded(result)


def cron_registry(client: CronApiClient, generation_id: str) -> ToolRegistry:
    def invoke(operation: str, method: str, path: str, mutating: bool):
        def call(arguments: dict[str, object]) -> object:
            path_args = {key: quote(str(value), safe="") for key, value in arguments.items()}
            body = {key: value for key, value in arguments.items() if key != "job_id"} if method in {"POST", "PATCH"} else None
            return client.request(method, path.format(**path_args), body,
                                  idempotency_key=cron_idempotency_key(generation_id, operation, arguments) if mutating else None)
        return call

    identifier = {"type": "string", "minLength": 1, "maxLength": 128}
    revision = {"type": "integer", "minimum": 1}
    timestamp = {"type": "string", "maxLength": 64}
    once = {"type": "object", "properties": {"kind": {"const": "once"}, "at": timestamp}, "required": ["kind", "at"], "additionalProperties": False}
    interval = {"type": "object", "properties": {"kind": {"const": "interval"}, "everySeconds": {"type": "number", "exclusiveMinimum": 0}, "startsAt": timestamp}, "required": ["kind", "everySeconds"], "additionalProperties": False}
    cron = {"type": "object", "properties": {"kind": {"const": "cron"}, "expression": {"type": "string", "minLength": 1, "maxLength": 128}, "timezoneName": {"type": "string", "minLength": 1, "maxLength": 128}}, "required": ["kind", "expression"], "additionalProperties": False}
    schedule = {"oneOf": [once, interval, cron]}
    lifecycle = {"type": "object", "properties": {"endsAt": {"type": "string", "maxLength": 64}, "maxSuccessfulRuns": {"type": "integer", "minimum": 1}}, "additionalProperties": False}
    tools = (
         BoundTool(ToolSpec("cron_create", "Create a scheduled Tomo job for this conversation.", {"type": "object", "properties": {"intent": {"type": "string", "minLength": 1, "maxLength": 4000}, "constraints": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 1000}, "maxItems": 32}, "schedule": schedule, "lifecycle": lifecycle}, "required": ["intent", "schedule"], "additionalProperties": False}, read_only=False, parallel_safe=False), invoke("create", "POST", "/v1/cron/jobs", True)),
        BoundTool(ToolSpec("cron_list", "List this owner's scheduled jobs.", {"type": "object", "properties": {}, "additionalProperties": False}), invoke("list", "GET", "/v1/cron/jobs", False)),
        BoundTool(ToolSpec("cron_inspect", "Inspect one scheduled job.", {"type": "object", "properties": {"job_id": identifier}, "required": ["job_id"], "additionalProperties": False}), invoke("inspect", "GET", "/v1/cron/jobs/{job_id}", False)),
         BoundTool(ToolSpec("cron_update", "Update an active scheduled job using its current revision.", {"type": "object", "properties": {"job_id": identifier, "revision": revision, "intent": {"type": "string", "minLength": 1, "maxLength": 4000}, "constraints": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 1000}, "maxItems": 32}, "schedule": schedule, "lifecycle": lifecycle}, "required": ["job_id", "revision", "intent", "schedule"], "additionalProperties": False}, read_only=False, parallel_safe=False), invoke("update", "PATCH", "/v1/cron/jobs/{job_id}", True)),
        *tuple(BoundTool(ToolSpec(f"cron_{name}", f"{label} a scheduled job.", {"type": "object", "properties": {"job_id": identifier, "revision": revision}, "required": ["job_id", "revision"], "additionalProperties": False}, read_only=False, parallel_safe=False), invoke(name, "POST", f"/v1/cron/jobs/{{job_id}}/{path}", True)) for name, path, label in (("pause", "pause", "Pause"), ("resume", "resume", "Resume"), ("run_now", "run-now", "Run"), ("delete", "delete", "Delete"))),
        BoundTool(ToolSpec("cron_history", "View bounded history for a scheduled job.", {"type": "object", "properties": {"job_id": identifier}, "required": ["job_id"], "additionalProperties": False}), invoke("history", "GET", "/v1/cron/jobs/{job_id}/history", False)),
    )
    return ToolRegistry(tools)


def _bounded(value: object, depth: int = 0) -> dict[str, object]:
    if not isinstance(value, dict) or depth > 4:
        return {"ok": False, "error": "cron_invalid_response"}
    result: dict[str, object] = {}
    for key, item in list(value.items())[:32]:
        if not isinstance(key, str) or key.casefold() in {"token", "key", "authorization", "secret"}:
            continue
        if isinstance(item, str): result[key] = item[:4000]
        elif item is None or isinstance(item, (bool, int, float)): result[key] = item
        elif isinstance(item, list): result[key] = [_bounded(entry, depth + 1) if isinstance(entry, dict) else str(entry)[:1000] for entry in item[:32]]
        elif isinstance(item, dict): result[key] = _bounded(item, depth + 1)
    return result
