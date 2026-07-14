from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from types import MappingProxyType
from typing import Mapping, Sequence, TypeAlias

from ..models import InboundEnvelope, InboundMessage, InputBurst
from ..personal_data import MemoryControl, MemoryGovernanceControl, MemoryWriteControl, OwnerSettingControl, PendingMemoryActionControl


class ConversationMove(str, Enum):
    ACKNOWLEDGE = "acknowledge"
    ANSWER = "answer"
    CLARIFY = "clarify"
    EXPLORE = "explore"
    CHALLENGE = "challenge"
    REASSURE = "reassure"
    JOKE = "joke"
    ACT = "act"
    REPAIR = "repair"
    REFUSE = "refuse"


class MoveConfidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SegmentFinish(str, Enum):
    COMPLETE = "complete"
    TOOL_BATCH = "tool_batch"
    PARTIAL = "partial"
    FAILED = "failed"


class TurnRunStatus(str, Enum):
    COMPLETED = "completed"
    COMPLETED_PARTIAL = "completed_partial"
    CANCELLED = "cancelled"
    FAILED = "failed"


REACTION_EMOJI_OPTIONS = ("👍", "❤️", "😂", "🔥", "🥰", "👏", "🤔", "👀", "🙏", "🫡")
REACTION_EMOJI_ALLOWLIST = frozenset(REACTION_EMOJI_OPTIONS)


@dataclass(frozen=True)
class ReactionIntent:
    emoji: str

    def __post_init__(self) -> None:
        if self.emoji not in REACTION_EMOJI_ALLOWLIST:
            raise ValueError("reaction emoji is not allowed")


def _compact_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    value = value.strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError(f"{field_name} must be a compact nonblank string")
    return value


def _nonblank_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} must be nonblank")
    return value


def _nonnegative_integer(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class Frame:
    segment_index: int
    frame_index: int
    text: str

    def __post_init__(self) -> None:
        _nonnegative_integer(self.segment_index, "frame segment_index")
        _nonnegative_integer(self.frame_index, "frame frame_index")
        object.__setattr__(self, "text", _compact_text(self.text, "frame text"))


@dataclass(frozen=True)
class TurnBudget:
    max_model_segments: int
    max_tool_rounds: int
    max_tool_calls: int
    max_visible_segments: int
    max_frames_per_segment: int
    max_sentences_per_frame: int
    max_chars_per_frame: int
    max_contract_repairs: int = 1
    max_elapsed_seconds: float = 120.0

    def __post_init__(self) -> None:
        for field_name in ("max_model_segments", "max_visible_segments", "max_frames_per_segment", "max_sentences_per_frame", "max_chars_per_frame"):
            if _nonnegative_integer(getattr(self, field_name), field_name) < 1:
                raise ValueError(f"{field_name} must be positive")
        for field_name in ("max_tool_rounds", "max_tool_calls", "max_contract_repairs"):
            _nonnegative_integer(getattr(self, field_name), field_name)
        if self.max_frames_per_segment > 3:
            raise ValueError("max_frames_per_segment cannot exceed 3")
        if self.max_sentences_per_frame > 3:
            raise ValueError("max_sentences_per_frame cannot exceed 3")
        if self.max_chars_per_frame > 4096:
            raise ValueError("max_chars_per_frame cannot exceed 4096")
        if self.max_tool_rounds > self.max_tool_calls:
            raise ValueError("max_tool_rounds cannot exceed max_tool_calls")
        if not isinstance(self.max_elapsed_seconds, (int, float)) or isinstance(self.max_elapsed_seconds, bool) or not isfinite(self.max_elapsed_seconds) or self.max_elapsed_seconds <= 0:
            raise ValueError("max_elapsed_seconds must be positive")

    def validate_usage(self, usage: "TurnUsage") -> None:
        if not isinstance(usage, TurnUsage):
            raise ValueError("usage must be a TurnUsage")
        limits = {
            "model_segments": self.max_model_segments,
            "tool_rounds": self.max_tool_rounds,
            "tool_calls": self.max_tool_calls,
            "visible_segments": self.max_visible_segments,
            "contract_repairs": self.max_contract_repairs,
        }
        for field_name, limit in limits.items():
            if getattr(usage, field_name) > limit:
                raise ValueError(f"usage {field_name} exceeds its budget")


@dataclass(frozen=True)
class TurnUsage:
    model_segments: int = 0
    tool_rounds: int = 0
    tool_calls: int = 0
    visible_segments: int = 0
    contract_repairs: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("model_segments", "tool_rounds", "tool_calls", "visible_segments", "contract_repairs"):
            _nonnegative_integer(getattr(self, field_name), field_name)
        for field_name in ("input_tokens", "output_tokens"):
            value = getattr(self, field_name)
            if value is not None:
                _nonnegative_integer(value, field_name)
        if self.tool_rounds > self.tool_calls:
            raise ValueError("tool_rounds cannot exceed tool_calls")


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "call_id", _compact_text(self.call_id, "tool call_id"))
        object.__setattr__(self, "name", _compact_text(self.name, "tool name"))
        if not isinstance(self.arguments, Mapping):
            raise ValueError("tool arguments must be a mapping")
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


@dataclass(frozen=True)
class ToolObservation:
    call_id: str
    name: str
    ok: bool
    content: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "call_id", _compact_text(self.call_id, "tool observation call_id"))
        object.__setattr__(self, "name", _compact_text(self.name, "tool observation name"))
        if not isinstance(self.ok, bool):
            raise ValueError("tool observation ok must be a boolean")
        object.__setattr__(self, "content", _nonblank_text(self.content, "tool observation content"))
        if self.error_code is not None:
            object.__setattr__(self, "error_code", _compact_text(self.error_code, "tool observation error_code"))


@dataclass(frozen=True)
class SegmentResult:
    index: int
    frames: tuple[Frame, ...]
    tool_calls: tuple[ToolCall, ...]
    finish: SegmentFinish
    memory_controls: tuple[MemoryControl, ...] = ()

    def __post_init__(self) -> None:
        _nonnegative_integer(self.index, "segment index")
        frames = tuple(self.frames)
        tool_calls = tuple(self.tool_calls)
        memory_controls = tuple(self.memory_controls)
        if len(frames) > 3:
            raise ValueError("a segment can contain at most three frames")
        if any(not isinstance(frame, Frame) for frame in frames):
            raise ValueError("segment frames must be Frames")
        if any(frame.segment_index != self.index for frame in frames):
            raise ValueError("segment frame indices must match the segment")
        if [frame.frame_index for frame in frames] != list(range(len(frames))):
            raise ValueError("segment frame indices must be contiguous from zero")
        if any(not isinstance(call, ToolCall) for call in tool_calls):
            raise ValueError("segment tool calls must be ToolCalls")
        if any(not isinstance(control, (MemoryWriteControl, MemoryGovernanceControl, PendingMemoryActionControl, OwnerSettingControl)) for control in memory_controls):
            raise ValueError("segment memory controls must be MemoryControls")
        if len(memory_controls) > 5:
            raise ValueError("a segment can contain at most five memory controls")
        if len({call.call_id for call in tool_calls}) != len(tool_calls):
            raise ValueError("segment tool call IDs must be unique")
        finish = SegmentFinish(self.finish)
        if finish is SegmentFinish.FAILED:
            if frames or tool_calls:
                raise ValueError("a failed segment cannot contain frames or tool calls")
        elif finish is SegmentFinish.TOOL_BATCH:
            if not tool_calls:
                raise ValueError("a tool batch segment requires tool calls")
        elif tool_calls:
            raise ValueError("only a tool batch segment can contain tool calls")
        elif not frames:
            raise ValueError("a completed or partial segment requires frames")
        object.__setattr__(self, "frames", frames)
        object.__setattr__(self, "tool_calls", tool_calls)
        object.__setattr__(self, "memory_controls", memory_controls)
        object.__setattr__(self, "finish", finish)


@dataclass(frozen=True)
class TurnRunResult:
    plan: "MovePlan"
    segments: tuple[SegmentResult, ...]
    frames: tuple[Frame, ...]
    usage: TurnUsage
    status: TurnRunStatus

    def __post_init__(self) -> None:
        if not isinstance(self.plan, MovePlan):
            raise ValueError("turn run requires a move plan")
        segments = tuple(self.segments)
        frames = tuple(self.frames)
        if not segments or any(not isinstance(segment, SegmentResult) for segment in segments):
            raise ValueError("turn run requires segment results")
        if [segment.index for segment in segments] != list(range(len(segments))):
            raise ValueError("turn run segment indices must be contiguous from zero")
        expected_frames = tuple(frame for segment in segments for frame in segment.frames)
        if frames != expected_frames:
            raise ValueError("turn run frames must flatten segment frames in order")
        tool_call_ids = [call.call_id for segment in segments for call in segment.tool_calls]
        if len(set(tool_call_ids)) != len(tool_call_ids):
            raise ValueError("turn run tool call IDs must be unique")
        if sum(len(segment.memory_controls) for segment in segments) > 8:
            raise ValueError("a turn run can contain at most eight memory controls")
        if not isinstance(self.usage, TurnUsage):
            raise ValueError("turn run requires usage")
        expected_usage = {
            "model_segments": len(segments),
            "tool_rounds": sum(segment.finish is SegmentFinish.TOOL_BATCH for segment in segments),
            "tool_calls": sum(len(segment.tool_calls) for segment in segments),
            "visible_segments": sum(bool(segment.frames) for segment in segments),
        }
        for field_name, expected in expected_usage.items():
            if getattr(self.usage, field_name) != expected:
                raise ValueError(f"turn run usage {field_name} must match its segments")
        status = TurnRunStatus(self.status)
        if status is TurnRunStatus.COMPLETED and segments[-1].finish is not SegmentFinish.COMPLETE:
            raise ValueError("a completed turn run must end with a complete segment")
        if status is TurnRunStatus.COMPLETED_PARTIAL:
            if not frames or segments[-1].finish not in {SegmentFinish.PARTIAL, SegmentFinish.FAILED, SegmentFinish.TOOL_BATCH}:
                raise ValueError("a partial turn run requires visible frames and a partial boundary")
        if status is TurnRunStatus.FAILED and segments[-1].finish is not SegmentFinish.FAILED:
            raise ValueError("a failed turn run must end with a failed segment")
        object.__setattr__(self, "segments", segments)
        object.__setattr__(self, "frames", frames)
        object.__setattr__(self, "status", status)

    @property
    def logical_text(self) -> str:
        return " ".join(frame.text for frame in self.frames)


@dataclass(frozen=True)
class FrameReady:
    sequence: int
    frame: Frame

    def __post_init__(self) -> None:
        _nonnegative_integer(self.sequence, "frame sequence")
        if not isinstance(self.frame, Frame):
            raise ValueError("frame ready requires a Frame")


@dataclass(frozen=True)
class MemoryControlReady:
    segment_index: int
    control: MemoryControl
    tool_observation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _nonnegative_integer(self.segment_index, "memory control segment_index")
        if not isinstance(self.control, (MemoryWriteControl, MemoryGovernanceControl, PendingMemoryActionControl, OwnerSettingControl)):
            raise ValueError("memory control ready requires a MemoryControl")
        if any(not isinstance(observation_id, str) or not observation_id for observation_id in self.tool_observation_ids):
            raise ValueError("memory control tool observation IDs must be nonempty text")


@dataclass(frozen=True)
class TurnRunStarted:
    plan: "MovePlan"

    def __post_init__(self) -> None:
        if not isinstance(self.plan, MovePlan):
            raise ValueError("turn run start requires a move plan")


@dataclass(frozen=True)
class ReactionWindowReady:
    """Segment zero controls are complete; a reaction may precede tool execution."""


@dataclass(frozen=True)
class TurnRunCompleted:
    result: TurnRunResult

    def __post_init__(self) -> None:
        if not isinstance(self.result, TurnRunResult):
            raise ValueError("turn run completion requires a TurnRunResult")


@dataclass(frozen=True)
class ConversationRequest:
    burst: InputBurst
    soul: str
    history: tuple[dict[str, str], ...]

    @property
    def envelope(self) -> InboundEnvelope:
        return self.burst.latest

    @classmethod
    def from_history(cls, *, envelope: InboundEnvelope, soul: str, history: Sequence[dict[str, str]]) -> "ConversationRequest":
        normalized: list[dict[str, str]] = []
        for item in history:
            role = item.get("role")
            content = item.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                raise ValueError("conversation history must contain user or assistant text messages")
            normalized.append({"role": role, "content": content})
        burst = InputBurst(
            burst_id=f"legacy-{envelope.message_id}",
            generation_id=f"legacy-{envelope.message_id}",
            revision=1,
            messages=(InboundMessage(1, int(envelope.native_metadata.get("update_id", 0)), envelope),),
        )
        return cls(burst=burst, soul=soul, history=tuple(normalized))


@dataclass(frozen=True)
class MovePlan:
    primary: ConversationMove
    supporting: tuple[ConversationMove, ...]
    response_goal: str
    confidence: MoveConfidence
    sequence: tuple[ConversationMove, ...] | None = None
    reaction: ReactionIntent | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.response_goal, str):
            raise ValueError("response_goal must be text")
        response_goal = self.response_goal.strip()
        if not response_goal:
            raise ValueError("response_goal cannot be empty")
        if len(response_goal) > 240 or "\n" in response_goal or "\r" in response_goal:
            raise ValueError("response_goal must be a compact single line")
        primary = ConversationMove(self.primary)
        supporting = tuple(ConversationMove(item) for item in self.supporting)
        confidence = MoveConfidence(self.confidence)
        object.__setattr__(self, "primary", primary)
        object.__setattr__(self, "supporting", supporting)
        object.__setattr__(self, "response_goal", response_goal)
        object.__setattr__(self, "confidence", confidence)
        if self.reaction is not None and not isinstance(self.reaction, ReactionIntent):
            raise ValueError("reaction must be a ReactionIntent or None")
        if len(supporting) > 2:
            raise ValueError("a move plan can have at most two supporting moves")
        ordered = (primary, *supporting)
        if len(set(ordered)) != len(ordered):
            raise ValueError("move plan moves must be unique")
        sequence = tuple(ConversationMove(item) for item in (self.sequence or ordered))
        expected = {primary, *supporting}
        if len(sequence) != len(expected) or set(sequence) != expected:
            raise ValueError("move sequence must contain every selected move exactly once")
        object.__setattr__(self, "sequence", sequence)

    @property
    def ordered_moves(self) -> tuple[ConversationMove, ...]:
        return self.sequence

    @classmethod
    def direct_answer(cls) -> "MovePlan":
        return cls(
            primary=ConversationMove.ANSWER,
            supporting=(),
            response_goal="respond directly and honestly to the user's latest message",
            confidence=MoveConfidence.LOW,
            sequence=(ConversationMove.ANSWER,), reaction=None,
        )


TurnRunEvent: TypeAlias = TurnRunStarted | ReactionWindowReady | MemoryControlReady | FrameReady | TurnRunCompleted
