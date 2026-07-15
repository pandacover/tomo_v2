from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from .models import ConversationMove, MoveConfidence, MovePlan, REACTION_EMOJI_OPTIONS, ReactionIntent


_CANONICAL_PLAN_EXAMPLE = (
    '{"type":"turn_plan","primary_move":"answer","supporting_moves":[],'
    '"response_goal":"answer the user","confidence":"low","reaction":null}'
)


def render_first_segment_contract(
    *,
    max_frames: int,
    max_sentences: int,
    max_chars: int,
    native_tools_available: bool,
) -> str:
    """Render the complete JSONL grammar for an initial model segment."""
    return _render_segment_contract(
        max_frames=max_frames,
        max_sentences=max_sentences,
        max_chars=max_chars,
        native_tools_available=native_tools_available,
        first_segment=True,
    )


def render_later_segment_contract(
    *,
    max_frames: int,
    max_sentences: int,
    max_chars: int,
    native_tools_available: bool,
) -> str:
    """Render the complete JSONL grammar for a fixed-plan model segment."""
    return _render_segment_contract(
        max_frames=max_frames,
        max_sentences=max_sentences,
        max_chars=max_chars,
        native_tools_available=native_tools_available,
        first_segment=False,
    )


def _render_segment_contract(
    *,
    max_frames: int,
    max_sentences: int,
    max_chars: int,
    native_tools_available: bool,
    first_segment: bool,
) -> str:
    frame_range = f"1 to {max_frames}"
    moves = ", ".join(move.value for move in ConversationMove)
    reactions = ", ".join(REACTION_EMOJI_OPTIONS)
    plan_rules = (
        "SHOULD emit one turn_plan before controls or frames; make it useful. It is advisory metadata, not a frame.\n"
        f"Use exactly these turn_plan fields and values: primary_move and supporting_moves use {moves}; "
        "response_goal is compact single-line text; confidence is low, medium, or high; "
        f"reaction is one of {reactions} or null.\n"
        f"canonical plan example: {_CANONICAL_PLAN_EXAMPLE}\n"
    ) if first_segment else "do not emit a turn_plan; reuse the fixed turn plan.\n"
    tool_rules = (
        "native tool completion permits zero or one pre-tool frame; native tool calls are provider-native, never JSONL records.\n"
        if native_tools_available
        else "native tools are unavailable. Do not call tools.\n"
    )
    memory_rules = (
        "memory_control records are optional, strictly validated, and must precede frame records.\n"
        if first_segment
        else "use only optional memory_control records before frame records; they are strictly validated.\n"
    )
    return (
        "OUTPUT CONTRACT\n"
        "Emit one JSON object per physical line. No prose, Markdown, fences, or backticks outside records.\n"
        f"{plan_rules}"
        f"{memory_rules}"
        '{"type":"frame","text":"..."}\n'
        f"ordinary completion requires {frame_range} frame records. Each frame has at most "
        f"{max_sentences} sentences per frame and {max_chars} characters per frame.\n"
        f"{tool_rules}"
        "An explicit repair instruction overrides advisory planning and optional controls.\n"
    )


class PlanSource(str, Enum):
    MODEL = "model"
    NORMALIZED = "normalized"
    SYNTHESIZED = "synthesized"


@dataclass(frozen=True)
class PlanResolution:
    plan: MovePlan
    source: PlanSource


def resolve_advisory_plan(payload: Mapping[str, object]) -> PlanResolution:
    normalized = set(payload) != {
        "primary_move",
        "supporting_moves",
        "response_goal",
        "confidence",
        "reaction",
    }
    try:
        primary = ConversationMove(payload.get("primary_move"))
    except (TypeError, ValueError):
        primary = ConversationMove.ANSWER
        normalized = True

    supporting: list[ConversationMove] = []
    raw_supporting = payload.get("supporting_moves")
    if not isinstance(raw_supporting, list):
        normalized = True
    else:
        for raw_move in raw_supporting:
            try:
                move = ConversationMove(raw_move)
            except (TypeError, ValueError):
                normalized = True
                continue
            if move == primary or move in supporting or len(supporting) == 2:
                normalized = True
                continue
            supporting.append(move)

    response_goal = payload.get("response_goal")
    if not isinstance(response_goal, str):
        response_goal = MovePlan.direct_answer().response_goal
        normalized = True
    else:
        stripped_goal = response_goal.strip()
        if stripped_goal != response_goal:
            normalized = True
        response_goal = stripped_goal
        if not response_goal or "\n" in response_goal or "\r" in response_goal or len(response_goal) > 240:
            response_goal = MovePlan.direct_answer().response_goal
            normalized = True

    try:
        confidence = MoveConfidence(payload.get("confidence"))
    except (TypeError, ValueError):
        confidence = MoveConfidence.LOW
        normalized = True

    reaction = _parse_optional_reaction(payload.get("reaction"))
    if payload.get("reaction") is not None and reaction is None:
        normalized = True

    return PlanResolution(
        plan=MovePlan(
            primary=primary,
            supporting=tuple(supporting),
            response_goal=response_goal,
            confidence=confidence,
            sequence=None,
            reaction=reaction,
        ),
        source=PlanSource.NORMALIZED if normalized else PlanSource.MODEL,
    )


def synthesized_direct_plan() -> PlanResolution:
    return PlanResolution(MovePlan.direct_answer(), PlanSource.SYNTHESIZED)


def synthesized_tool_plan() -> PlanResolution:
    return PlanResolution(
        MovePlan(
            primary=ConversationMove.ACT,
            supporting=(ConversationMove.ANSWER,),
            response_goal=MovePlan.direct_answer().response_goal,
            confidence=MoveConfidence.LOW,
            sequence=None,
            reaction=None,
        ),
        PlanSource.SYNTHESIZED,
    )


def _parse_optional_reaction(value: object) -> ReactionIntent | None:
    if value is None:
        return None
    try:
        return ReactionIntent(value)
    except (TypeError, ValueError):
        return None
