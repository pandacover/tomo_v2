"""Versioned JSON boundary between the host and a sandboxed runtime."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Iterator, TypeAlias

from .conversation import ConversationCompleted, ConversationMove, UtteranceReady
from .conversation.models import ConversationResult, MoveConfidence, MovePlan
from .conversation.parsing import ConversationOutputError, parse_utterances
from .models import InboundEnvelope, InboundMessage, InputBurst, MessageAttachment, OutboundBubble, ResponseContract

PROTOCOL_VERSION = 2
LEGACY_PROTOCOL_VERSION = 1
EVENT_MARKER = "TOMO_SANDBOX_EVENT="
RESULT_MARKER = "TOMO_SANDBOX_RESULT="
MAX_BUBBLES = 4
MAX_BUBBLE_CHARS = 4096
MAX_ERROR_TRACEBACK_FRAMES = 12
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_PTY_PREFIX_RE = re.compile(r"(?:[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f]+|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[ -/]*[@-~])*\Z")
_EXCEPTION_CLASS_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_TRACEBACK_BASENAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,255}\Z")
_TRACEBACK_FUNCTION_RE = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]{0,127}|<[A-Za-z_][A-Za-z0-9_]{0,127}>)\Z")


@dataclass(frozen=True)
class SandboxUtteranceEvent:
    sequence: int
    move: ConversationMove
    text: str


@dataclass(frozen=True)
class SandboxCompletedEvent:
    sequence: int
    result: dict[str, Any]


@dataclass(frozen=True)
class SandboxTracebackFrame:
    basename: str
    function: str
    line: int


@dataclass(frozen=True)
class SandboxErrorEvent:
    sequence: int
    code: str
    exception_class: str | None = None
    traceback: tuple[SandboxTracebackFrame, ...] = ()


SandboxEvent: TypeAlias = SandboxUtteranceEvent | SandboxCompletedEvent | SandboxErrorEvent


def encode_inbound(request_id: str, burst: InputBurst | InboundEnvelope) -> str:
    """Serialize a v2 inbound request for the sandbox."""
    _validate_request_id(request_id)
    if isinstance(burst, InboundEnvelope):
        burst = InputBurst(
            burst_id=f"legacy-{burst.message_id}",
            generation_id=f"legacy-{burst.message_id}",
            revision=1,
            messages=(InboundMessage(1, int(burst.native_metadata.get("update_id", 0)), burst),),
        )
    if not isinstance(burst, InputBurst):
        raise TypeError("burst must be an InputBurst")
    return _encode(
        {
            "version": PROTOCOL_VERSION,
            "type": "inbound",
            "request_id": request_id,
            "burst": _burst_to_dict(burst),
        }
    )


def decode_inbound(payload: str) -> tuple[str, InputBurst]:
    """Parse and validate a v2 inbound request from the host."""
    message = _decode(payload, "inbound")
    raw_burst = message.get("burst")
    if not isinstance(raw_burst, dict):
        raise ValueError("inbound burst must be an object")
    return message["request_id"], _burst_from_dict(raw_burst)


def encode_event(request_id: str, generation_id: str, sequence: int, event: object) -> str:
    _validate_request_id(request_id)
    _validate_generation_id(generation_id)
    if not isinstance(sequence, int) or sequence < 0:
        raise ValueError("event sequence must be non-negative")
    payload: dict[str, Any] = {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "generation_id": generation_id,
        "sequence": sequence,
    }
    conversation_event = getattr(event, "event", event)
    if isinstance(conversation_event, UtteranceReady):
        bubble = getattr(event, "bubble", None)
        text = getattr(bubble, "text", conversation_event.text)
        payload.update(
            {
                "type": "utterance",
                "move": conversation_event.move.value,
                "text": text,
            }
        )
    elif isinstance(conversation_event, ConversationCompleted):
        result = conversation_event.result
        payload.update(
            {
                "type": "completed",
                "result": {
                    "logical_text": result.logical_text,
                    "utterances": list(result.utterances),
                    "plan": {
                        "primary_move": result.plan.primary.value,
                        "supporting_moves": [move.value for move in result.plan.supporting],
                        "move_sequence": [move.value for move in result.plan.sequence],
                        "response_goal": result.plan.response_goal,
                        "confidence": result.plan.confidence.value,
                    },
                },
            }
        )
    elif isinstance(event, SandboxErrorEvent):
        if not event.code.strip():
            raise ValueError("error code must be non-empty")
        error = {"code": event.code}
        _validate_error_diagnostics(event.exception_class, event.traceback)
        if event.exception_class is not None:
            error["exception_class"] = event.exception_class
        if event.traceback:
            error["traceback"] = [asdict(frame) for frame in event.traceback]
        payload.update({"type": "error", "error": error})
    else:
        raise TypeError("unsupported sandbox event")
    return _encode(payload)


def iter_event_markers(
    chunks: Iterable[str],
    expected_request_id: str,
    expected_generation_id: str,
    contract: ResponseContract | None = None,
) -> Iterator[SandboxEvent]:
    _validate_request_id(expected_request_id)
    _validate_generation_id(expected_generation_id)
    contract = contract or ResponseContract()
    buffer = ""
    expected_sequence = 0
    seen: dict[int, str] = {}
    emitted_utterances: list[str] = []
    emitted_moves: list[ConversationMove] = []
    terminal = False

    def process_line(line: str) -> SandboxEvent | None:
        nonlocal expected_sequence, terminal
        marker_position = line.find(EVENT_MARKER)
        if marker_position < 0 or not _PTY_PREFIX_RE.fullmatch(line[:marker_position]):
            return None
        payload = line[marker_position + len(EVENT_MARKER) :]
        message = _decode_event(payload, expected_request_id, expected_generation_id)
        sequence = message["sequence"]
        canonical = _encode(message)
        previous = seen.get(sequence)
        if previous is not None:
            if previous != canonical:
                raise ValueError("conflicting duplicate sandbox event")
            return None
        if terminal:
            raise ValueError("sandbox event after terminal")
        if sequence != expected_sequence:
            raise ValueError("sandbox event sequence gap")
        seen[sequence] = canonical
        expected_sequence += 1
        event = _event_from_message(message, contract)
        if isinstance(event, SandboxUtteranceEvent):
            if len(emitted_utterances) >= contract.max_utterances:
                raise ValueError("sandbox event stream exceeds utterance contract")
            emitted_utterances.append(event.text)
            emitted_moves.append(event.move)
        if isinstance(event, SandboxCompletedEvent):
            completed_utterances, completed_moves = _completed_result_parts(event.result, contract)
            if tuple(emitted_utterances) != completed_utterances:
                raise ValueError("completed utterances do not match streamed utterances")
            if tuple(emitted_moves) != completed_moves:
                raise ValueError("completed moves do not match streamed moves")
            terminal = True
        elif isinstance(event, SandboxErrorEvent):
            terminal = True
        return event

    for chunk in chunks:
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            event = process_line(line.rstrip("\r"))
            if event is not None:
                yield event
    if buffer:
        event = process_line(buffer.rstrip("\r"))
        if event is not None:
            yield event
    if not terminal:
        raise ValueError("sandbox event stream missing terminal event")


class SandboxProtocolError(ValueError):
    """A typed error returned by a valid sandbox result."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"sandbox returned {code}")


def encode_result(request_id: str, bubbles: list[OutboundBubble]) -> str:
    """Serialize a successful legacy v1 sandbox result."""
    _validate_request_id(request_id)
    _validate_bubbles(bubbles)
    return _encode(
        {
            "version": LEGACY_PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": True,
            "bubbles": [asdict(bubble) for bubble in bubbles],
        }
    )


def encode_error(request_id: str, code: str) -> str:
    """Serialize a typed legacy v1 sandbox failure without exception details."""
    _validate_request_id(request_id)
    if not isinstance(code, str) or not code:
        raise ValueError("error code must be a non-empty string")
    return _encode(
        {
            "version": LEGACY_PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": False,
            "error": {"code": code},
        }
    )


def parse_result_marker(output: str, expected_request_id: str) -> list[OutboundBubble]:
    """Extract the sole legacy marked result from sandbox stdout and validate its request ID."""
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


def _burst_to_dict(burst: InputBurst) -> dict[str, Any]:
    return {
        "burst_id": burst.burst_id,
        "generation_id": burst.generation_id,
        "revision": burst.revision,
        "visible_assistant_utterances": list(burst.visible_assistant_utterances),
        "accepted_generation_ids": list(burst.accepted_generation_ids),
        "messages": [
            {"ordinal": message.ordinal, "update_id": message.update_id, "envelope": asdict(message.envelope)}
            for message in burst.messages
        ],
    }


def _burst_from_dict(raw: dict[str, Any]) -> InputBurst:
    messages = raw.get("messages")
    if not isinstance(messages, list):
        raise ValueError("inbound burst messages must be an array")
    try:
        return InputBurst(
            burst_id=_required_string(raw, "burst_id"),
            generation_id=_required_string(raw, "generation_id"),
            revision=raw["revision"],
            visible_assistant_utterances=tuple(raw.get("visible_assistant_utterances", ())),
            accepted_generation_ids=tuple(raw.get("accepted_generation_ids", ())),
            messages=tuple(
                InboundMessage(
                    ordinal=message["ordinal"],
                    update_id=message["update_id"],
                    envelope=_envelope_from_dict(message.get("envelope")),
                )
                for message in messages
                if isinstance(message, dict)
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid inbound burst") from error


def _envelope_from_dict(inbound: object) -> InboundEnvelope:
    if not isinstance(inbound, dict):
        raise ValueError("inbound envelope must be an object")
    attachments = inbound.get("attachments", [])
    if not isinstance(attachments, list) or not all(isinstance(item, dict) for item in attachments):
        raise ValueError("inbound attachments must be an array of objects")
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
    if envelope.connector != "telegram":
        raise ValueError("unsupported connector")
    if not isinstance(envelope.native_metadata, dict):
        raise ValueError("inbound native_metadata must be an object")
    if envelope.location is not None and not isinstance(envelope.location, dict):
        raise ValueError("inbound location must be an object")
    return envelope


def _decode_event(payload: str, expected_request_id: str, expected_generation_id: str) -> dict[str, Any]:
    try:
        message = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("protocol payload must be JSON") from error
    if not isinstance(message, dict):
        raise ValueError("protocol payload must be an object")
    if message.get("version") != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    if message.get("request_id") != expected_request_id:
        raise ValueError("event request_id does not match inbound request")
    if message.get("generation_id") != expected_generation_id:
        raise ValueError("event generation_id does not match inbound request")
    sequence = message.get("sequence")
    if not isinstance(sequence, int) or sequence < 0:
        raise ValueError("event sequence must be non-negative")
    event_type = message.get("type")
    if event_type not in {"utterance", "completed", "error"}:
        raise ValueError("unsupported sandbox event type")
    return message


def _event_from_message(message: dict[str, Any], contract: ResponseContract) -> SandboxEvent:
    sequence = message["sequence"]
    event_type = message["type"]
    if event_type == "utterance":
        text = message.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_BUBBLE_CHARS:
            raise ValueError("utterance text must contain 1 to 4096 characters")
        try:
            utterance = parse_utterances(json.dumps({"utterances": [text]}), contract)[0]
        except ConversationOutputError as error:
            raise ValueError(f"utterance violates response contract: {error.code}") from error
        return SandboxUtteranceEvent(sequence=sequence, move=ConversationMove(message.get("move")), text=utterance)
    if event_type == "completed":
        result = message.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("logical_text"), str) or not result["logical_text"].strip():
            raise ValueError("completed event requires a result")
        _completed_result_parts(result, contract)
        return SandboxCompletedEvent(sequence=sequence, result=result)
    error = message.get("error")
    if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not error["code"].strip():
        raise ValueError("error event requires a code")
    if set(error) - {"code", "exception_class", "traceback"}:
        raise ValueError("error event contains unsupported diagnostic fields")
    exception_class = error.get("exception_class")
    raw_traceback = error.get("traceback", [])
    if not isinstance(raw_traceback, list):
        raise ValueError("error traceback must be an array")
    try:
        traceback = tuple(
            SandboxTracebackFrame(
                basename=frame["basename"],
                function=frame["function"],
                line=frame["line"],
            )
            for frame in raw_traceback
            if isinstance(frame, dict)
        )
    except (KeyError, TypeError) as decode_error:
        raise ValueError("invalid error traceback frame") from decode_error
    if len(traceback) != len(raw_traceback):
        raise ValueError("error traceback frames must be objects")
    if any(set(frame) != {"basename", "function", "line"} for frame in raw_traceback):
        raise ValueError("error traceback frames contain unsupported fields")
    _validate_error_diagnostics(exception_class, traceback)
    return SandboxErrorEvent(sequence=sequence, code=error["code"], exception_class=exception_class, traceback=traceback)


def _completed_result_parts(result: dict[str, Any], contract: ResponseContract) -> tuple[tuple[str, ...], tuple[ConversationMove, ...]]:
    utterances = result.get("utterances")
    if not isinstance(utterances, list):
        raise ValueError("completed result utterances must be an array")
    try:
        cleaned = parse_utterances(json.dumps({"utterances": utterances}), contract)
    except ConversationOutputError as error:
        raise ValueError(f"completed result violates response contract: {error.code}") from error
    plan = result.get("plan")
    if not isinstance(plan, dict):
        raise ValueError("completed result requires a plan")
    raw_sequence = plan.get("move_sequence")
    if not isinstance(raw_sequence, list):
        raise ValueError("completed plan requires a move_sequence")
    try:
        moves = tuple(ConversationMove(move) for move in raw_sequence)
    except ValueError as error:
        raise ValueError("completed plan contains an invalid move") from error
    if len(moves) != len(cleaned):
        raise ValueError("completed plan move_sequence must match utterance count")
    primary = plan.get("primary_move")
    supporting = plan.get("supporting_moves")
    response_goal = plan.get("response_goal")
    confidence = plan.get("confidence")
    logical_text = result.get("logical_text")
    if not isinstance(supporting, list):
        raise ValueError("completed plan requires supporting_moves")
    try:
        move_plan = MovePlan(
            ConversationMove(primary),
            tuple(ConversationMove(move) for move in supporting),
            response_goal,
            MoveConfidence(confidence),
            moves,
        )
        conversation_result = ConversationResult(move_plan, cleaned)
    except (TypeError, ValueError) as error:
        raise ValueError("completed plan is inconsistent") from error
    if primary not in raw_sequence:
        raise ValueError("completed plan primary must be in move_sequence")
    if set(supporting) - set(raw_sequence) or len(supporting) != len(set(supporting)):
        raise ValueError("completed plan supporting_moves must be unique members of move_sequence")
    if logical_text != conversation_result.logical_text:
        raise ValueError("completed logical_text must match utterances exactly")
    return cleaned, moves


def _decode_result(payload: str) -> dict[str, Any]:
    try:
        message = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("protocol payload must be JSON") from error
    if not isinstance(message, dict):
        raise ValueError("protocol payload must be an object")
    if message.get("version") != LEGACY_PROTOCOL_VERSION:
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


def _validate_generation_id(generation_id: object) -> None:
    if not isinstance(generation_id, str) or not generation_id.strip():
        raise ValueError("generation_id must be non-empty")


def _validate_error_diagnostics(exception_class: object, traceback: object) -> None:
    if exception_class is not None and (
        not isinstance(exception_class, str) or not _EXCEPTION_CLASS_RE.fullmatch(exception_class)
    ):
        raise ValueError("error exception_class must be a safe class name")
    if not isinstance(traceback, tuple) or len(traceback) > MAX_ERROR_TRACEBACK_FRAMES:
        raise ValueError("error traceback must contain at most 12 frames")
    for frame in traceback:
        if not isinstance(frame, SandboxTracebackFrame):
            raise ValueError("error traceback frames must be SandboxTracebackFrame values")
        if not isinstance(frame.basename, str) or not _TRACEBACK_BASENAME_RE.fullmatch(frame.basename):
            raise ValueError("error traceback basename must be a filename")
        if not isinstance(frame.function, str) or not _TRACEBACK_FUNCTION_RE.fullmatch(frame.function):
            raise ValueError("error traceback function must be a safe function name")
        if not isinstance(frame.line, int) or isinstance(frame.line, bool) or frame.line < 1:
            raise ValueError("error traceback line must be positive")


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
