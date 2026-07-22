"""Versioned JSON boundary between the host and a sandboxed runtime."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, Iterator, TypeAlias

from .conversation import ConversationMove, FrameReady, MoveConfidence, MovePlan, ReactionIntent, SegmentFinish, TurnBudget, TurnRunCompleted, TurnRunStatus, TurnUsage
from .conversation.parsing import _validate_strict_frame_text, parse_utterance, parse_utterances
from .models import AutomationTurn, InboundEnvelope, InboundMessage, InputBurst, MessageAttachment, OutboundBubble, PeerTurn, ReplyContext, ResponseContract, RuntimeConfig
from .runtime import RuntimeCompleted, RuntimeFrameReady, RuntimeReactionReady
from .latency_trace import SANDBOX_LATENCY_MARKER

INBOUND_PROTOCOL_VERSION = 2
AUTOMATION_PROTOCOL_VERSION = 6
PEER_PROTOCOL_VERSION = 1
PROTOCOL_VERSION = 5
LEGACY_EVENT_PROTOCOL_VERSION = 2
LEGACY_V3_EVENT_PROTOCOL_VERSION = 3
LEGACY_V4_EVENT_PROTOCOL_VERSION = 4
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
_SAFE_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_SANDBOX_LATENCY_PHASES = frozenset({
    "sandbox_runtime_entry",
    "sandbox_context_hydration",
    "sandbox_provider_attempt",
    "sandbox_provider_attempt_start",
    "sandbox_provider_first_text_delta",
    "sandbox_provider_move_plan_validated",
    "sandbox_provider_first_frame_validated",
    "sandbox_provider_stream_completed",
    "sandbox_tool_batch",
    "sandbox_checkpoint_inbound",
    "sandbox_checkpoint_frame",
    "sandbox_checkpoint_complete",
    "sandbox_runtime_build",
    "sandbox_session_load",
    "sandbox_memory_hydration",
    "sandbox_prompt_prepare",
    "sandbox_attachment_fetch",
    "sandbox_image_normalize",
    "sandbox_vision_provider_attempt",
    "sandbox_vision_observation_ready",
})
_SANDBOX_LATENCY_COUNTS = frozenset({
    "attempt",
    "segment",
    "repair",
    "model_segments",
    "tool_rounds",
    "tool_calls",
    "contract_repairs",
    "visible_segments",
    "suspended_ms",
    "active_ms",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "output_chars_through_first_frame",
    "first_frame_chars",
    "plan_model",
    "plan_normalized",
    "plan_synthesized",
    "image_count",
    "input_bytes",
    "normalized_bytes",
    "width",
    "height",
})


@dataclass(frozen=True)
class SandboxFrameEvent:
    sequence: int
    segment_index: int
    frame_index: int
    text: str
    legacy_move: ConversationMove | None = None


@dataclass(frozen=True)
class SandboxCompletedEvent:
    sequence: int
    result: dict[str, Any]


@dataclass(frozen=True)
class SandboxReactionEvent:
    sequence: int
    owner_id: str
    actor_id: str
    chat_id: str
    target_message_id: str
    generation_id: str
    revision: int
    emoji: str


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


@dataclass(frozen=True)
class SandboxStaleEvent:
    sequence: int
    current_revision: int


SandboxEvent: TypeAlias = SandboxReactionEvent | SandboxFrameEvent | SandboxCompletedEvent | SandboxErrorEvent | SandboxStaleEvent
SandboxLatency: TypeAlias = tuple[str, str, int, dict[str, int]]


def encode_inbound(request_id: str, burst: InputBurst | InboundEnvelope) -> str:
    """Serialize a v2 inbound request for the sandbox during rollout."""
    _validate_request_id(request_id)
    if isinstance(burst, InboundEnvelope):
        burst = InputBurst(f"legacy-{burst.message_id}", f"legacy-{burst.message_id}", 1, (InboundMessage(1, int(burst.native_metadata.get("update_id", 0)), burst),))
    if not isinstance(burst, InputBurst):
        raise TypeError("burst must be an InputBurst")
    return _encode({"version": INBOUND_PROTOCOL_VERSION, "type": "inbound", "request_id": request_id, "burst": _burst_to_dict(burst)})


def decode_inbound(payload: str) -> tuple[str, InputBurst]:
    """Parse identical-shape v2 through v5 inbound data during rollout."""
    message = _decode(payload, "inbound", {INBOUND_PROTOCOL_VERSION, LEGACY_V3_EVENT_PROTOCOL_VERSION, LEGACY_V4_EVENT_PROTOCOL_VERSION, PROTOCOL_VERSION})
    if not isinstance(message.get("burst"), dict):
        raise ValueError("inbound burst must be an object")
    return message["request_id"], _burst_from_dict(message["burst"])


def encode_automation(request_id: str, turn: AutomationTurn) -> str:
    _validate_request_id(request_id)
    if not isinstance(turn, AutomationTurn):
        raise TypeError("turn must be an AutomationTurn")
    return _encode({"version": AUTOMATION_PROTOCOL_VERSION, "type": "automation", "request_id": request_id, "turn": asdict(turn)})


def decode_automation(payload: str) -> tuple[str, AutomationTurn]:
    message = _decode(payload, "automation", {AUTOMATION_PROTOCOL_VERSION})
    if set(message) != {"version", "type", "request_id", "turn"} or not isinstance(message["turn"], dict):
        raise ValueError("automation payload contains unsupported fields")
    try:
        return message["request_id"], AutomationTurn(**message["turn"])
    except (TypeError, ValueError) as error:
        raise ValueError("invalid automation turn") from error


def encode_peer(request_id: str, turn: PeerTurn) -> str:
    _validate_request_id(request_id)
    if not isinstance(turn, PeerTurn):
        raise TypeError("turn must be a PeerTurn")
    return _encode({"version": PEER_PROTOCOL_VERSION, "type": "peer", "request_id": request_id, "turn": asdict(turn)})


def decode_peer(payload: str) -> tuple[str, PeerTurn]:
    message = _decode(payload, "peer", {PEER_PROTOCOL_VERSION})
    if set(message) != {"version", "type", "request_id", "turn"} or not isinstance(message["turn"], dict):
        raise ValueError("peer payload contains unsupported fields")
    expected = {"generation_id", "revision", "relationship_id", "thread_id", "request_id", "peer_handle", "purpose", "disclosure_kind", "message", "expires_at", "prior_exchanges", "disclosure_scope"}
    if set(message["turn"]) != expected:
        raise ValueError("peer turn contains unsupported fields")
    try:
        turn = PeerTurn(**message["turn"])
    except (TypeError, ValueError) as error:
        raise ValueError("invalid peer turn") from error
    if turn.request_id != message["request_id"]:
        raise ValueError("peer request binding does not match")
    return message["request_id"], turn


def decode_turn(payload: str) -> tuple[str, InputBurst | AutomationTurn | PeerTurn]:
    message = _json_object(payload)
    if message.get("type") == "automation": return decode_automation(payload)
    if message.get("type") == "peer": return decode_peer(payload)
    return decode_inbound(payload)


def encode_event(request_id: str, generation_id: str, sequence: int, event: object, *, expected_reaction_binding: tuple[str, str, str, str, str, int] | None = None) -> str:
    """Serialize reaction, frame, completion, stale, or safe error events as v5."""
    _validate_request_id(request_id)
    _validate_generation_id(generation_id)
    _nonnegative(sequence, "event sequence")
    payload: dict[str, Any] = {"version": PROTOCOL_VERSION, "request_id": request_id, "generation_id": generation_id, "sequence": sequence}
    if isinstance(event, RuntimeReactionReady):
        binding = (event.owner_id, event.actor_id, event.chat_id, event.target_message_id, event.generation_id, event.revision)
        if event.generation_id != generation_id or (expected_reaction_binding is not None and binding != expected_reaction_binding):
            raise ValueError("reaction generation does not match event generation")
        payload.update({"type": "reaction", "owner_id": event.owner_id, "actor_id": event.actor_id, "chat_id": event.chat_id,
                        "target_message_id": event.target_message_id, "reaction_generation_id": event.generation_id,
                        "revision": event.revision, "emoji": ReactionIntent(event.emoji).emoji})
        return _encode(payload)
    elif isinstance(event, RuntimeFrameReady):
        inner = event.event
    elif isinstance(event, FrameReady):
        inner = event
    elif isinstance(event, RuntimeCompleted):
        inner = event.event
    elif isinstance(event, TurnRunCompleted):
        inner = event
    else:
        inner = None
    if isinstance(inner, FrameReady):
        frame = inner.frame
        payload.update({"type": "frame", "segment_index": frame.segment_index, "frame_index": frame.frame_index, "text": frame.text})
    elif isinstance(inner, TurnRunCompleted):
        payload.update({"type": "completed", "result": _safe_result(inner.result)})
    elif isinstance(event, SandboxErrorEvent):
        _validate_error_diagnostics(event.exception_class, event.traceback)
        if not isinstance(event.code, str) or not _SAFE_ERROR_CODE_RE.fullmatch(event.code):
            raise ValueError("error code must be non-empty")
        error: dict[str, Any] = {"code": event.code}
        if event.exception_class is not None:
            error["exception_class"] = event.exception_class
        if event.traceback:
            error["traceback"] = [asdict(frame) for frame in event.traceback]
        payload.update({"type": "error", "error": error})
    elif isinstance(event, SandboxStaleEvent):
        _nonnegative(event.current_revision, "stale current_revision")
        payload.update({"type": "stale", "current_revision": event.current_revision})
    else:
        raise TypeError("unsupported sandbox event")
    return _encode(payload)


def iter_event_markers(chunks: Iterable[str], expected_request_id: str, expected_generation_id: str, contract: ResponseContract | None = None, budget: TurnBudget | None = None, *, expected_reaction_binding: tuple[str, str, str, str, int] | None = None, on_latency: Callable[[str, str, int, dict[str, int]], None] | None = None) -> Iterator[SandboxEvent]:
    """Parse one strictly version-consistent v2 through v5 event stream."""
    _validate_request_id(expected_request_id)
    _validate_generation_id(expected_generation_id)
    contract = contract or ResponseContract()
    budget = budget or RuntimeConfig().tool_turn_budget
    buffer, expected_sequence, stream_version, terminal = "", 0, None, False
    seen: dict[int, str] = {}
    frames: list[SandboxFrameEvent] = []
    reaction_seen = False

    def process(line: str) -> SandboxEvent | None:
        nonlocal expected_sequence, stream_version, terminal, reaction_seen
        latency_position = line.find(SANDBOX_LATENCY_MARKER)
        if latency_position >= 0 and _PTY_PREFIX_RE.fullmatch(line[:latency_position]):
            try:
                telemetry = parse_latency_marker(line[latency_position + len(SANDBOX_LATENCY_MARKER):])
            except ValueError:
                # Telemetry is observational: malformed markers cannot fail a turn.
                return None
            if on_latency is not None:
                try:
                    on_latency(*telemetry)
                except Exception:
                    pass
            return None
        position = line.find(EVENT_MARKER)
        if position < 0 or not _PTY_PREFIX_RE.fullmatch(line[:position]):
            return None
        message = _decode_event(line[position + len(EVENT_MARKER):], expected_request_id, expected_generation_id)
        if stream_version is None:
            stream_version = message["version"]
        elif stream_version != message["version"]:
            raise ValueError("mixed sandbox event protocol versions")
        canonical, sequence = _encode(message), message["sequence"]
        if sequence in seen:
            if seen[sequence] != canonical:
                raise ValueError("conflicting duplicate sandbox event")
            return None
        if terminal:
            raise ValueError("sandbox event after terminal")
        if sequence != expected_sequence:
            raise ValueError("sandbox event sequence gap")
        event = _event_from_message(message, contract, budget, expected_reaction_binding)
        if isinstance(event, SandboxStaleEvent) and message["version"] == PROTOCOL_VERSION and sequence != 0:
            raise ValueError("v5 stale event must be sequence zero")
        if isinstance(event, SandboxReactionEvent):
            if message["version"] < LEGACY_V4_EVENT_PROTOCOL_VERSION or reaction_seen or frames:
                raise ValueError("invalid sandbox reaction ordering")
            reaction_seen = True
        elif isinstance(event, SandboxFrameEvent):
            if message["version"] >= LEGACY_V3_EVENT_PROTOCOL_VERSION:
                _validate_stream_frame(event, frames, budget)
            elif len(frames) >= contract.max_utterances:
                raise ValueError("sandbox event stream exceeds utterance contract")
            frames.append(event)
        elif isinstance(event, SandboxCompletedEvent):
            if message["version"] >= LEGACY_V3_EVENT_PROTOCOL_VERSION:
                completed_frames = _v3_result_parts(event.result, budget)
                if tuple((frame.segment_index, frame.frame_index, frame.text) for frame in frames) != completed_frames:
                    raise ValueError("completed frames do not match streamed frames")
            else:
                completed_utterances, completed_moves = _v2_result_parts(event.result, contract)
                if tuple(frame.text for frame in frames) != completed_utterances or tuple(frame.legacy_move for frame in frames) != completed_moves:
                    raise ValueError("completed v2 result does not match streamed utterances")
            terminal = True
        elif isinstance(event, SandboxErrorEvent):
            terminal = True
        elif isinstance(event, SandboxStaleEvent):
            terminal = True
        seen[sequence] = canonical
        expected_sequence += 1
        return event

    for chunk in chunks:
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            event = process(line.rstrip("\r"))
            if event is not None:
                yield event
    if buffer:
        event = process(buffer.rstrip("\r"))
        if event is not None:
            yield event
    if not terminal:
        raise ValueError("sandbox event stream missing terminal event")


def parse_latency_marker(payload: str) -> SandboxLatency:
    """Accept only the fixed, ID-free sandbox telemetry wire schema."""
    fields = payload.split(" ")
    if not fields or any("=" not in field for field in fields):
        raise ValueError("invalid sandbox latency marker")
    values = dict(field.split("=", 1) for field in fields)
    if len(values) != len(fields) or set(values) - {"phase", "outcome", "elapsed_ms", *_SANDBOX_LATENCY_COUNTS}:
        raise ValueError("invalid sandbox latency fields")
    if values.get("phase") not in _SANDBOX_LATENCY_PHASES or values.get("outcome") not in {"ok", "error"}:
        raise ValueError("invalid sandbox latency name")
    if "elapsed_ms" not in values:
        raise ValueError("sandbox latency requires elapsed_ms")
    counts: dict[str, int] = {}
    for name, value in values.items():
        if name in {"phase", "outcome"}:
            continue
        if not value.isascii() or not value.isdecimal():
            raise ValueError("sandbox latency values must be nonnegative integers")
        counts[name] = int(value)
    return values["phase"], values["outcome"], counts.pop("elapsed_ms"), counts


def _safe_result(result: object) -> dict[str, Any]:
    frames = [{"segment_index": frame.segment_index, "frame_index": frame.frame_index, "text": frame.text} for frame in result.frames]
    return {"logical_text": result.logical_text, "frames": frames, "status": result.status.value, "plan": _plan_dict(result.plan), "segments": [{"index": segment.index, "finish": segment.finish.value, "frame_count": len(segment.frames), "tool_call_count": len(segment.tool_calls)} for segment in result.segments], "usage": {"model_segments": result.usage.model_segments, "tool_rounds": result.usage.tool_rounds, "tool_calls": result.usage.tool_calls, "visible_segments": result.usage.visible_segments, "contract_repairs": result.usage.contract_repairs, "input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens}}


def _plan_dict(plan: object) -> dict[str, Any]:
    return {"primary_move": plan.primary.value, "supporting_moves": [move.value for move in plan.supporting], "move_sequence": [move.value for move in plan.sequence], "response_goal": plan.response_goal, "confidence": plan.confidence.value}


def _decode_event(payload: str, request_id: str, generation_id: str) -> dict[str, Any]:
    message = _json_object(payload)
    version = message.get("version")
    if version not in {LEGACY_EVENT_PROTOCOL_VERSION, LEGACY_V3_EVENT_PROTOCOL_VERSION, LEGACY_V4_EVENT_PROTOCOL_VERSION, PROTOCOL_VERSION}:
        raise ValueError("unsupported protocol version")
    if message.get("request_id") != request_id or message.get("generation_id") != generation_id:
        raise ValueError("event IDs do not match inbound request")
    _nonnegative(message.get("sequence"), "event sequence")
    allowed = ({"utterance", "completed", "error"} if version == LEGACY_EVENT_PROTOCOL_VERSION
               else {"frame", "completed", "error"} if version == LEGACY_V3_EVENT_PROTOCOL_VERSION
               else {"reaction", "frame", "completed", "error"} if version == LEGACY_V4_EVENT_PROTOCOL_VERSION
               else {"reaction", "frame", "completed", "error", "stale"})
    if message.get("type") not in allowed:
        raise ValueError("unsupported sandbox event type")
    return message


def _event_from_message(message: dict[str, Any], contract: ResponseContract, budget: TurnBudget, expected_reaction_binding: tuple[str, str, str, str, int] | None = None) -> SandboxEvent:
    event_type, sequence = message["type"], message["sequence"]
    if event_type == "reaction":
        if set(message) != {"version", "request_id", "generation_id", "sequence", "type", "owner_id", "actor_id", "chat_id", "target_message_id", "reaction_generation_id", "revision", "emoji"}:
            raise ValueError("reaction event contains unsupported fields")
        try:
            owner_id, actor_id, chat_id, target_message_id = message.get("owner_id"), message.get("actor_id"), message.get("chat_id"), message.get("target_message_id")
            reaction_generation_id, revision = message.get("reaction_generation_id"), message.get("revision")
            if any(not isinstance(value, str) or not value for value in (owner_id, actor_id, chat_id, target_message_id, reaction_generation_id)):
                raise ValueError
            _nonnegative(revision, "reaction revision")
            if reaction_generation_id != message["generation_id"]:
                raise ValueError
            binding = (owner_id, actor_id, chat_id, target_message_id, revision)
            if expected_reaction_binding is not None and binding != expected_reaction_binding:
                raise ValueError
            return SandboxReactionEvent(sequence, owner_id, actor_id, chat_id, target_message_id, reaction_generation_id, revision, ReactionIntent(message.get("emoji")).emoji)
        except ValueError as error:
            raise ValueError("invalid reaction binding or emoji") from error
    if event_type == "frame":
        if set(message) != {"version", "request_id", "generation_id", "sequence", "type", "segment_index", "frame_index", "text"}:
            raise ValueError("frame event contains unsupported fields")
        segment_index, frame_index, text = message.get("segment_index"), message.get("frame_index"), message.get("text")
        _nonnegative(segment_index, "frame segment_index")
        _nonnegative(frame_index, "frame frame_index")
        _validate_frame_text(text, budget)
        return SandboxFrameEvent(sequence, segment_index, frame_index, text)
    if event_type == "utterance":
        if set(message) != {"version", "request_id", "generation_id", "sequence", "type", "move", "text"}:
            raise ValueError("utterance event contains unsupported fields")
        text = message.get("text")
        try:
            validated_text = parse_utterance(json.dumps({"utterance": text}), contract)
        except Exception as error:
            raise ValueError("utterance text violates response contract") from error
        if text != validated_text or len(text) > MAX_BUBBLE_CHARS:
            raise ValueError("utterance text violates response contract")
        try:
            move = ConversationMove(message.get("move"))
        except ValueError as error:
            raise ValueError("invalid utterance move") from error
        return SandboxFrameEvent(sequence, 0, sequence, validated_text, move)
    if event_type == "completed":
        if set(message) != {"version", "request_id", "generation_id", "sequence", "type", "result"} or not isinstance(message.get("result"), dict):
            raise ValueError("completed event requires only a result")
        return SandboxCompletedEvent(sequence, message["result"])
    if event_type == "stale":
        if set(message) != {"version", "request_id", "generation_id", "sequence", "type", "current_revision"}:
            raise ValueError("stale event contains unsupported fields")
        _nonnegative(message.get("current_revision"), "stale current_revision")
        return SandboxStaleEvent(sequence, message["current_revision"])
    if set(message) != {"version", "request_id", "generation_id", "sequence", "type", "error"}:
        raise ValueError("error event contains unsupported fields")
    error = message["error"]
    if not isinstance(error, dict) or set(error) - {"code", "exception_class", "traceback"} or not _SAFE_ERROR_CODE_RE.fullmatch(error.get("code", "")):
        raise ValueError("error event requires a safe code")
    raw_frames = error.get("traceback", [])
    if not isinstance(raw_frames, list) or any(not isinstance(frame, dict) or set(frame) != {"basename", "function", "line"} for frame in raw_frames):
        raise ValueError("invalid error traceback")
    traceback = tuple(SandboxTracebackFrame(frame["basename"], frame["function"], frame["line"]) for frame in raw_frames)
    _validate_error_diagnostics(error.get("exception_class"), traceback)
    return SandboxErrorEvent(sequence, error["code"], error.get("exception_class"), traceback)


def _validate_stream_frame(frame: SandboxFrameEvent, frames: list[SandboxFrameEvent], budget: TurnBudget) -> None:
    if len(frames) >= 3:
        raise ValueError("sandbox event stream exceeds frame budget")
    if frame.segment_index >= budget.max_model_segments:
        raise ValueError("frame segment index exceeds model segment budget")
    if frame.segment_index not in {prior.segment_index for prior in frames} and len({prior.segment_index for prior in frames}) >= budget.max_visible_segments:
        raise ValueError("frame exceeds visible segment budget")
    if frame.frame_index >= budget.max_frames_per_segment:
        raise ValueError("frame index exceeds frame budget")
    if frames:
        prior = frames[-1]
        if frame.segment_index < prior.segment_index:
            raise ValueError("streamed segment indices must be nondecreasing")
        expected = prior.frame_index + 1 if frame.segment_index == prior.segment_index else 0
        if frame.frame_index != expected:
            raise ValueError("streamed frame indices must be contiguous")
    elif frame.frame_index != 0:
        raise ValueError("first frame in a segment must have index zero")


def _v3_result_parts(result: dict[str, Any], budget: TurnBudget) -> tuple[tuple[int, int, str], ...]:
    if set(result) != {"logical_text", "frames", "status", "plan", "segments", "usage"}:
        raise ValueError("completed result contains unsupported fields")
    frames, segments, usage = result["frames"], result["segments"], result["usage"]
    if not isinstance(frames, list) or not isinstance(segments, list) or not isinstance(usage, dict):
        raise ValueError("completed result fields have invalid types")
    tuples = []
    for frame in frames:
        if not isinstance(frame, dict) or set(frame) != {"segment_index", "frame_index", "text"}:
            raise ValueError("completed frame contains unsupported fields")
        _nonnegative(frame.get("segment_index"), "completed frame segment_index")
        _nonnegative(frame.get("frame_index"), "completed frame frame_index")
        _validate_frame_text(frame.get("text"), budget)
        tuples.append((frame["segment_index"], frame["frame_index"], frame["text"]))
    probe = [SandboxFrameEvent(index, *frame) for index, frame in enumerate(tuples)]
    for item in probe:
        _validate_stream_frame(item, probe[:item.sequence], budget)
    _validate_plan(result["plan"])
    if result.get("status") not in {TurnRunStatus.COMPLETED.value, TurnRunStatus.COMPLETED_PARTIAL.value, TurnRunStatus.APPROVAL_NEEDED.value}:
        raise ValueError("completed result status is invalid")
    expected_segments = list(range(len(segments)))
    if [segment.get("index") if isinstance(segment, dict) else None for segment in segments] != expected_segments:
        raise ValueError("completed segment indices must be contiguous")
    frame_counts = {index: 0 for index in expected_segments}
    for segment_index, _, _ in tuples:
        if segment_index not in frame_counts:
            raise ValueError("completed frame references an unknown segment")
        frame_counts[segment_index] += 1
    tool_rounds = tool_calls = visible = 0
    for segment in segments:
        if set(segment) != {"index", "finish", "frame_count", "tool_call_count"}:
            raise ValueError("completed segment contains unsupported fields")
        finish = segment.get("finish")
        if finish not in {item.value for item in SegmentFinish}:
            raise ValueError("completed segment finish is invalid")
        _nonnegative(segment.get("frame_count"), "segment frame_count")
        _nonnegative(segment.get("tool_call_count"), "segment tool_call_count")
        if segment["frame_count"] != frame_counts[segment["index"]] or segment["frame_count"] > budget.max_frames_per_segment:
            raise ValueError("completed segment frame count is inconsistent")
        if finish == SegmentFinish.TOOL_BATCH.value:
            if not segment["tool_call_count"]: raise ValueError("tool batch requires tool calls")
            if segment["frame_count"] > 1: raise ValueError("tool batch may contain at most one frame")
            tool_rounds += 1
        elif segment["tool_call_count"]: raise ValueError("only tool batches may contain tool calls")
        if finish == SegmentFinish.FAILED.value and segment["frame_count"]: raise ValueError("failed segment cannot contain frames")
        if finish in {SegmentFinish.COMPLETE.value, SegmentFinish.PARTIAL.value} and not segment["frame_count"]: raise ValueError("visible segment requires frames")
        tool_calls += segment["tool_call_count"]
        visible += bool(segment["frame_count"])
    if result["status"] == TurnRunStatus.COMPLETED.value and (not segments or segments[-1]["finish"] != SegmentFinish.COMPLETE.value or any(segment["finish"] != SegmentFinish.TOOL_BATCH.value for segment in segments[:-1])):
        raise ValueError("completed result must end with a complete segment")
    if result["status"] == TurnRunStatus.COMPLETED_PARTIAL.value and (
        not tuples or not segments or segments[-1]["finish"] not in {SegmentFinish.PARTIAL.value, SegmentFinish.FAILED.value, SegmentFinish.TOOL_BATCH.value} or any(segment["finish"] != SegmentFinish.TOOL_BATCH.value for segment in segments[:-1])
    ):
        raise ValueError("partial completed result has an invalid terminal segment")
    if result["status"] == TurnRunStatus.APPROVAL_NEEDED.value and (
        tuples or not segments or segments[-1]["finish"] != SegmentFinish.FAILED.value or any(segment["finish"] != SegmentFinish.TOOL_BATCH.value for segment in segments[:-1])
    ):
        raise ValueError("approval-needed result has an invalid terminal segment")
    expected_usage = {"model_segments": len(segments), "tool_rounds": tool_rounds, "tool_calls": tool_calls, "visible_segments": visible}
    if set(usage) != {"model_segments", "tool_rounds", "tool_calls", "visible_segments", "contract_repairs", "input_tokens", "output_tokens"}:
        raise ValueError("completed usage contains unsupported fields")
    for key, value in usage.items():
        if key in {"input_tokens", "output_tokens"} and value is None: continue
        _nonnegative(value, f"usage {key}")
    if any(usage[key] != value for key, value in expected_usage.items()):
        raise ValueError("completed usage is inconsistent")
    try:
        budget.validate_usage(TurnUsage(usage["model_segments"], usage["tool_rounds"], usage["tool_calls"], usage["visible_segments"], usage["contract_repairs"], usage["input_tokens"], usage["output_tokens"]))
    except ValueError as error:
        raise ValueError("completed usage exceeds budget") from error
    if result["logical_text"] != " ".join(frame[2] for frame in tuples):
        raise ValueError("completed logical_text must match frames exactly")
    return tuple(tuples)


def _validate_plan(plan: object) -> None:
    if not isinstance(plan, dict) or set(plan) != {"primary_move", "supporting_moves", "move_sequence", "response_goal", "confidence"}:
        raise ValueError("completed plan contains unsupported fields")
    if not isinstance(plan["supporting_moves"], list) or not isinstance(plan["move_sequence"], list):
        raise ValueError("completed plan moves must be arrays")
    try:
        MovePlan(ConversationMove(plan["primary_move"]), tuple(ConversationMove(move) for move in plan["supporting_moves"]), plan["response_goal"], MoveConfidence(plan["confidence"]), tuple(ConversationMove(move) for move in plan["move_sequence"]))
    except (TypeError, ValueError) as error:
        raise ValueError("completed plan is inconsistent") from error


def _v2_result_parts(result: dict[str, Any], contract: ResponseContract) -> tuple[tuple[str, ...], tuple[ConversationMove, ...]]:
    if not isinstance(result.get("utterances"), list) or not isinstance(result.get("logical_text"), str):
        raise ValueError("invalid v2 completed result")
    try:
        utterances = parse_utterances(json.dumps({"utterances": result["utterances"]}), contract)
    except Exception as error:
        raise ValueError("invalid v2 utterance") from error
    plan = result.get("plan")
    _validate_plan(plan)
    moves = tuple(ConversationMove(move) for move in plan["move_sequence"])
    if len(moves) != len(utterances) or result["logical_text"] != " ".join(utterances): raise ValueError("inconsistent v2 completed result")
    return utterances, moves


def _validate_frame_text(text: object, budget: TurnBudget) -> None:
    try:
        validated = _validate_strict_frame_text(text, max_chars=min(MAX_BUBBLE_CHARS, budget.max_chars_per_frame), max_sentences=budget.max_sentences_per_frame)
    except ValueError as error:
        raise ValueError("frame text violates frame contract") from error
    if text != validated:
        raise ValueError("frame text violates frame contract")


def _nonnegative(value: object, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0: raise ValueError(f"{name} must be non-negative")


def _burst_to_dict(burst: InputBurst) -> dict[str, Any]:
    return {"burst_id": burst.burst_id, "generation_id": burst.generation_id, "revision": burst.revision, "visible_assistant_utterances": list(burst.visible_assistant_utterances), "accepted_generation_ids": list(burst.accepted_generation_ids), "messages": [{"ordinal": item.ordinal, "update_id": item.update_id, "envelope": asdict(item.envelope)} for item in burst.messages]}


def _burst_from_dict(raw: dict[str, Any]) -> InputBurst:
    try:
        messages = raw["messages"]
        if not isinstance(messages, list): raise ValueError
        return InputBurst(raw["burst_id"], raw["generation_id"], raw["revision"], tuple(InboundMessage(item["ordinal"], item["update_id"], _envelope_from_dict(item["envelope"])) for item in messages), tuple(raw.get("visible_assistant_utterances", ())), tuple(raw.get("accepted_generation_ids", ())))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid inbound burst") from error


def _envelope_from_dict(raw: object) -> InboundEnvelope:
    if not isinstance(raw, dict): raise ValueError("inbound envelope must be an object")
    try:
        attachments = raw.get("attachments", [])
        if not isinstance(attachments, list) or not all(isinstance(item, dict) for item in attachments): raise ValueError
        reply = raw.get("reply_context")
        if reply is not None and not isinstance(reply, dict):
            raise ValueError
        return InboundEnvelope(
            connector=raw["connector"], actor_id=raw["actor_id"], message_id=raw["message_id"], text=raw["text"],
            timestamp=raw.get("timestamp", ""), attachments=tuple(MessageAttachment(**item) for item in attachments),
            location=raw.get("location"), native_metadata=raw.get("native_metadata", {}),
            reply_context=ReplyContext(**reply) if reply is not None else None,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid inbound envelope") from error


class SandboxProtocolError(ValueError):
    def __init__(self, code: str) -> None: self.code = code; super().__init__(f"sandbox returned {code}")


def encode_result(request_id: str, bubbles: list[OutboundBubble]) -> str:
    _validate_request_id(request_id); _validate_bubbles(bubbles)
    return _encode({"version": LEGACY_PROTOCOL_VERSION, "request_id": request_id, "ok": True, "bubbles": [asdict(item) for item in bubbles]})


def encode_error(request_id: str, code: str) -> str:
    _validate_request_id(request_id)
    if not isinstance(code, str) or not code: raise ValueError("error code must be a non-empty string")
    return _encode({"version": LEGACY_PROTOCOL_VERSION, "request_id": request_id, "ok": False, "error": {"code": code}})


def parse_result_marker(output: str, expected_request_id: str) -> list[OutboundBubble]:
    marked = [line[len(RESULT_MARKER):] for line in output.splitlines() if line.startswith(RESULT_MARKER)]
    if len(marked) != 1: raise ValueError("sandbox output must contain exactly one result marker")
    message = _json_object(marked[0])
    if message.get("version") != LEGACY_PROTOCOL_VERSION or message.get("request_id") != expected_request_id: raise ValueError("invalid result marker")
    if message.get("ok") is False: raise SandboxProtocolError(message.get("error", {}).get("code", "unknown"))
    if message.get("ok") is not True or not isinstance(message.get("bubbles"), list): raise ValueError("invalid result")
    try: bubbles = [OutboundBubble(**item) for item in message["bubbles"]]
    except (TypeError, ValueError) as error: raise ValueError("invalid result bubble") from error
    _validate_bubbles(bubbles); return bubbles


def _decode(payload: str, message_type: str, versions: set[int]) -> dict[str, Any]:
    message = _json_object(payload)
    if message.get("version") not in versions: raise ValueError("unsupported protocol version")
    if message.get("type") != message_type: raise ValueError(f"expected {message_type} payload")
    _validate_request_id(message.get("request_id")); return message


def _json_object(payload: str) -> dict[str, Any]:
    try: message = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error: raise ValueError("protocol payload must be JSON") from error
    if not isinstance(message, dict): raise ValueError("protocol payload must be an object")
    return message


def _encode(message: dict[str, Any]) -> str: return json.dumps(message, separators=(",", ":"), ensure_ascii=True)
def _validate_request_id(value: object) -> None:
    if not isinstance(value, str) or not _REQUEST_ID_RE.fullmatch(value): raise ValueError("request_id must be 1-128 URL-safe characters")
def _validate_generation_id(value: object) -> None:
    if not isinstance(value, str) or not value.strip(): raise ValueError("generation_id must be non-empty")
def _validate_error_diagnostics(exception_class: object, traceback: object) -> None:
    if exception_class is not None and (not isinstance(exception_class, str) or not _EXCEPTION_CLASS_RE.fullmatch(exception_class)): raise ValueError("error exception_class must be a safe class name")
    if not isinstance(traceback, tuple) or len(traceback) > MAX_ERROR_TRACEBACK_FRAMES: raise ValueError("error traceback must contain at most 12 frames")
    for frame in traceback:
        if (not isinstance(frame, SandboxTracebackFrame) or not isinstance(frame.basename, str) or not _TRACEBACK_BASENAME_RE.fullmatch(frame.basename)
                or not isinstance(frame.function, str) or not _TRACEBACK_FUNCTION_RE.fullmatch(frame.function)
                or not isinstance(frame.line, int) or isinstance(frame.line, bool) or frame.line < 1):
            raise ValueError("invalid error traceback frame")
def _validate_bubbles(bubbles: list[OutboundBubble]) -> None:
    if not 1 <= len(bubbles) <= MAX_BUBBLES: raise ValueError("result must contain 1 to 4 bubbles")
    if any(not isinstance(item.text, str) or not item.text or len(item.text) > MAX_BUBBLE_CHARS for item in bubbles): raise ValueError("invalid bubble")
