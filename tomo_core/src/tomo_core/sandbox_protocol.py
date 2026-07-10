"""Versioned JSON boundary between the host and a sandboxed runtime."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any

from .models import InboundEnvelope, MessageAttachment, OutboundBubble

PROTOCOL_VERSION = 1
RESULT_MARKER = "TOMO_SANDBOX_RESULT="
MAX_BUBBLES = 4
MAX_BUBBLE_CHARS = 4096
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def encode_inbound(request_id: str, inbound: InboundEnvelope) -> str:
    """Serialize a v1 inbound request for the sandbox."""
    _validate_request_id(request_id)
    return _encode(
        {
            "version": PROTOCOL_VERSION,
            "type": "inbound",
            "request_id": request_id,
            "inbound": asdict(inbound),
        }
    )


def decode_inbound(payload: str) -> tuple[str, InboundEnvelope]:
    """Parse and validate a v1 inbound request from the host."""
    message = _decode(payload, "inbound")
    inbound = message.get("inbound")
    if not isinstance(inbound, dict):
        raise ValueError("inbound must be an object")

    attachments = inbound.get("attachments", [])
    if not isinstance(attachments, list) or not all(isinstance(item, dict) for item in attachments):
        raise ValueError("inbound attachments must be an array of objects")
    try:
        envelope = InboundEnvelope(
            connector=_required_string(inbound, "connector"),
            actor_id=_required_string(inbound, "actor_id"),
            message_id=_required_string(inbound, "message_id"),
            text=_required_string(inbound, "text"),
            timestamp=_required_string(inbound, "timestamp"),
            attachments=tuple(MessageAttachment(**attachment) for attachment in attachments),
            location=inbound.get("location"),
            native_metadata=inbound.get("native_metadata", {}),
        )
    except (TypeError, ValueError) as error:
        raise ValueError("invalid inbound envelope") from error
    if envelope.connector != "telegram":
        raise ValueError("unsupported connector")
    if not isinstance(envelope.native_metadata, dict):
        raise ValueError("inbound native_metadata must be an object")
    if envelope.location is not None and not isinstance(envelope.location, dict):
        raise ValueError("inbound location must be an object")
    return message["request_id"], envelope


class SandboxProtocolError(ValueError):
    """A typed error returned by a valid sandbox result."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"sandbox returned {code}")


def encode_result(request_id: str, bubbles: list[OutboundBubble]) -> str:
    """Serialize a successful v1 sandbox result."""
    _validate_request_id(request_id)
    _validate_bubbles(bubbles)
    return _encode(
        {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": True,
            "bubbles": [asdict(bubble) for bubble in bubbles],
        }
    )


def encode_error(request_id: str, code: str) -> str:
    """Serialize a typed v1 sandbox failure without exception details."""
    _validate_request_id(request_id)
    if not isinstance(code, str) or not code:
        raise ValueError("error code must be a non-empty string")
    return _encode(
        {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": False,
            "error": {"code": code},
        }
    )


def parse_result_marker(output: str, expected_request_id: str) -> list[OutboundBubble]:
    """Extract the sole marked result from sandbox stdout and validate its request ID."""
    _validate_request_id(expected_request_id)
    marked_results = [line[len(RESULT_MARKER) :] for line in output.splitlines() if line.startswith(RESULT_MARKER)]
    if len(marked_results) != 1:
        raise ValueError("sandbox output must contain exactly one result marker")
    message = _decode_result(marked_results[0])
    if message["request_id"] != expected_request_id:
        raise ValueError("result request_id does not match inbound request")

    if message["ok"] is False:
        error = message.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not error["code"]:
            raise ValueError("result error must contain a code")
        if "bubbles" in message:
            raise ValueError("error result must not contain bubbles")
        raise SandboxProtocolError(error["code"])
    if message["ok"] is not True or "error" in message:
        raise ValueError("result ok must be a boolean with matching payload")
    raw_bubbles = message.get("bubbles")
    if not isinstance(raw_bubbles, list):
        raise ValueError("result bubbles must be an array")
    try:
        bubbles = [OutboundBubble(**bubble) for bubble in raw_bubbles if isinstance(bubble, dict)]
    except TypeError as error:
        raise ValueError("invalid result bubble") from error
    if len(bubbles) != len(raw_bubbles):
        raise ValueError("result bubbles must be objects")
    _validate_bubbles(bubbles)
    return bubbles


def _decode_result(payload: str) -> dict[str, Any]:
    try:
        message = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("protocol payload must be JSON") from error
    if not isinstance(message, dict):
        raise ValueError("protocol payload must be an object")
    if message.get("version") != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    _validate_request_id(message.get("request_id"))
    if not isinstance(message.get("ok"), bool):
        raise ValueError("result ok must be a boolean")
    return message


def _decode(payload: str, message_type: str) -> dict[str, Any]:
    try:
        message = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("protocol payload must be JSON") from error
    if not isinstance(message, dict):
        raise ValueError("protocol payload must be an object")
    if message.get("version") != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    if message.get("type") != message_type:
        raise ValueError(f"expected {message_type} payload")
    _validate_request_id(message.get("request_id"))
    return message


def _encode(message: dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=True)


def _validate_request_id(request_id: object) -> None:
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError("request_id must be 1-128 URL-safe characters")


def _required_string(message: dict[str, Any], name: str) -> str:
    value = message.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"inbound {name} must be a non-empty string")
    return value


def _validate_bubbles(bubbles: list[OutboundBubble]) -> None:
    if not 1 <= len(bubbles) <= MAX_BUBBLES:
        raise ValueError("result must contain 1 to 4 bubbles")
    for bubble in bubbles:
        if not isinstance(bubble.text, str) or not bubble.text or len(bubble.text) > MAX_BUBBLE_CHARS:
            raise ValueError("bubble text must contain 1 to 4096 characters")
        if bubble.reply_to_message_id is not None and not isinstance(bubble.reply_to_message_id, str):
            raise ValueError("bubble reply_to_message_id must be a string or null")
