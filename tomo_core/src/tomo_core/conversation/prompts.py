from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from ..context import ContextSnapshot
from ..models import InboundEnvelope, InputBurst
from .models import ConversationMove, ConversationRequest, MovePlan, TurnBudget
from .moves import render_move_procedures


def build_segment_messages(
    request: ConversationRequest,
    context: ContextSnapshot,
    budget: TurnBudget,
    *,
    segment_index: int,
    plan: MovePlan | None = None,
    prior_messages: Sequence[dict[str, object]] = (),
    tools_available: Sequence[dict[str, object]] = (),
) -> list[dict[str, object]]:
    """Build provider messages for one TurnRun model-generation segment."""
    if not isinstance(request, ConversationRequest):
        raise ValueError("request must be a ConversationRequest")
    if not isinstance(context, ContextSnapshot):
        raise ValueError("context must be a ContextSnapshot")
    if not isinstance(budget, TurnBudget):
        raise ValueError("budget must be a TurnBudget")
    if not isinstance(segment_index, int) or isinstance(segment_index, bool) or segment_index < 0:
        raise ValueError("segment_index must be a non-negative integer")
    if segment_index == 0:
        if plan is not None:
            raise ValueError("the first segment must not receive a fixed plan")
    elif not isinstance(plan, MovePlan):
        raise ValueError("later segments require a fixed MovePlan")

    normalized_prior = _normalized_provider_messages(prior_messages)
    tool_schemas = _tool_schemas(tools_available)
    if segment_index == 0:
        system = _first_segment_system(request.soul, budget, tool_schemas)
    else:
        system = _later_segment_system(request.soul, budget, plan, tool_schemas)
    visible_frames = [{"role": "assistant", "content": frame} for frame in context.visible_frames]
    return [
        {"role": "system", "content": system},
        *context.history,
        *visible_frames,
        {"role": "user", "content": _user_payload(request.burst)},
        *normalized_prior,
    ]


def build_segment_repair_messages(
    request: ConversationRequest,
    context: ContextSnapshot,
    budget: TurnBudget,
    plan: MovePlan,
    safe_code: str,
    prior_messages: Sequence[dict[str, object]] = (),
) -> list[dict[str, object]]:
    """Build a replacement segment that retains an already validated turn plan."""
    messages = build_segment_messages(request, context, budget, segment_index=1, plan=plan, prior_messages=prior_messages)
    frame_count = _frame_count_phrase(budget.max_frames_per_segment)
    messages.append(
        {
            "role": "system",
            "content": (
                f"the previous segment violated the JSONL contract: {safe_code}.\n"
                f"replace it with {frame_count} frame JSONL records only. preserve the fixed turn plan exactly. "
                "do not emit a turn_plan, native tool call, explanation, markdown, or any text outside JSONL records."
            ),
        }
    )
    return messages


def build_first_segment_repair_messages(
    request: ConversationRequest,
    context: ContextSnapshot,
    budget: TurnBudget,
    safe_code: str,
    prior_messages: Sequence[dict[str, object]] = (),
) -> list[dict[str, object]]:
    """Build a tool-free replacement when no valid turn plan was produced."""
    messages = build_segment_messages(
        request,
        context,
        budget,
        segment_index=0,
        prior_messages=prior_messages,
        tools_available=(),
    )
    frame_count = _frame_count_phrase(budget.max_frames_per_segment)
    messages.append(
        {
            "role": "system",
            "content": (
                f"the previous segment violated the JSONL contract: {safe_code}.\n"
                "replace the entire segment. begin with exactly one turn_plan JSONL record, then emit "
                f"{frame_count} frame JSONL records. native tools are disabled for this repair. "
                "do not emit a native tool call, explanation, markdown, or any text outside JSONL records."
            ),
        }
    )
    return messages


def _first_segment_system(soul: str, budget: TurnBudget, tool_schemas: tuple[dict[str, object], ...]) -> str:
    frame_count = _frame_count_phrase(budget.max_frames_per_segment)
    tool_guidance = (
        "batch independent related native tool calls in one assistant response. tool announcements are optional social output, never execution telemetry; do not add redundant completion messages."
        if tool_schemas
        else "native tools are unavailable. do not call tools. finish with final frame JSONL records."
    )
    return (
        "you are tomo. follow the supplied SOUL completely.\n"
        "produce JSON Lines only. emit one JSON object per physical line, with no code fences, backticks, or prose outside records.\n"
        f"generate exactly one internal turn_plan JSONL record before any frame record; after that plan and before frames, emit zero to five memory_control records. then zero to {_frame_count_limit(budget.max_frames_per_segment)} frame JSONL records.\n"
        '{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"move_sequence":["answer"],"response_goal":"...","confidence":"low","reaction":null}\n'
        '{"type":"frame","text":"..."}\n'
        "memory_control records are optional and internal. use them only when information may be useful in future conversations; preserve uncertainty with confidence and sources rather than suppressing it. never emit credentials or authentication secrets.\n"
        "the turn_plan fields are primary_move, supporting_moves, response_goal, confidence, and reaction. reaction is null by default or exactly one supported emoji. use it very sparsely; use null for commands, auth, errors, routine acknowledgements, ambiguity, corrections, opt-outs, serious, sensitive, or distressing content. never mention reactions to the user. moves are turn-level purposes, never frame or bubble sections; MovePlan does not determine frame count.\n"
        f"ordinary completion uses {frame_count} frames. when making native tool calls, emit zero or one useful pre-tool frame.\n"
        f"target one to two sentences per frame and no more than {frame_count} frames this segment. hard runtime limits: "
        f"at most {budget.max_frames_per_segment} frames per segment, {budget.max_sentences_per_frame} sentences per frame, and {budget.max_chars_per_frame} characters per frame.\n"
        "never use markdown, internal labels, em dashes, or en dashes in frame text. never claim an action happened without a supplied observation.\n"
        f"{tool_guidance}\n"
        "memory and session context hydration is silent unless the user explicitly requests it. do not offer mutation, booking, purchase, send, delete, or other side-effect capabilities unless an exposed bound tool and confirmation path exist.\n"
        f"allowed native tool schemas: {json.dumps(tool_schemas, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"<TOMO_SOUL>\n{soul}\n</TOMO_SOUL>\n\n"
        f"move planning vocabulary:\n{render_move_procedures(tuple(ConversationMove))}"
    )


def _later_segment_system(soul: str, budget: TurnBudget, plan: MovePlan, tool_schemas: tuple[dict[str, object], ...]) -> str:
    supporting = ",".join(move.value for move in plan.supporting) or "none"
    frame_count = _frame_count_phrase(budget.max_frames_per_segment)
    completion_guidance = (
        f"either make another native tool round with zero or one useful pre-tool frame, or complete with {frame_count} final frames."
        if tool_schemas
        else f"native tools are unavailable. do not call tools. complete with {frame_count} final frame JSONL records."
    )
    return (
        "you are tomo. follow the supplied SOUL completely.\n"
        "produce JSON Lines only. emit one JSON object per physical line, with no code fences, backticks, or prose outside records. do not emit a turn_plan record; reuse the fixed turn plan. emit zero to five optional internal memory_control records before any frame record.\n"
        '{"type":"frame","text":"..."}\n'
        "remember information autonomously only when future usefulness justifies it. qualify uncertain observations with confidence and sources rather than omitting them. never emit credentials or authentication secrets.\n"
        f"fixed turn plan: primary_move={plan.primary.value}; supporting_moves={supporting}; confidence={plan.confidence.value}.\n"
        f"{completion_guidance}\n"
        "original request and conversation history remain valid context for final frames. claims about tool outcomes or actions must be grounded in supplied tool observations.\n"
        "target one to two sentences per frame. hard runtime limits: "
        f"at most {budget.max_frames_per_segment} frames per segment, {budget.max_sentences_per_frame} sentences per frame, and {budget.max_chars_per_frame} characters per frame.\n"
        "never use markdown, internal labels, em dashes, or en dashes in frame text. never claim an action happened without a supplied observation.\n"
        "tool announcements are optional social output, never execution telemetry. memory and session context hydration is silent unless the user explicitly requests it.\n"
        "do not offer mutation, booking, purchase, send, delete, or other side-effect capabilities unless an exposed bound tool and confirmation path exist.\n"
        f"allowed native tool schemas: {json.dumps(tool_schemas, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"<TOMO_SOUL>\n{soul}\n</TOMO_SOUL>"
    )


def _frame_count_limit(max_frames: int) -> str:
    return ("one", "two", "three")[max_frames - 1]


def _frame_count_phrase(max_frames: int) -> str:
    return "one" if max_frames == 1 else f"one to {_frame_count_limit(max_frames)}"


def _normalized_provider_messages(messages: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            raise ValueError("prior messages must be mappings")
        role = message.get("role")
        content = message.get("content")
        if role == "assistant" and content is None:
            normalized.append(dict(message))
            continue
        if role not in {"assistant", "tool"} or not isinstance(content, str):
            raise ValueError("prior messages must be normalized assistant or tool messages")
        normalized.append(dict(message))
    return normalized


def _tool_schemas(tools_available: Sequence[dict[str, object]]) -> tuple[dict[str, object], ...]:
    schemas: list[dict[str, object]] = []
    for tool in tools_available:
        if not isinstance(tool, Mapping):
            raise ValueError("tool schemas must be mappings")
        schemas.append(dict(tool))
    return tuple(schemas)


def _visible_context(inbound: InboundEnvelope | InputBurst) -> list[dict[str, str]]:
    if isinstance(inbound, InputBurst):
        return [{"role": "assistant", "content": utterance} for utterance in inbound.visible_assistant_utterances]
    return []


def _user_payload(inbound: InboundEnvelope | InputBurst) -> str:
    if isinstance(inbound, InboundEnvelope):
        if inbound.attachments:
            return json.dumps(
                {
                    "message_id": inbound.message_id,
                    "content": inbound.text,
                    "attachments": [_prompt_attachment(attachment) for attachment in inbound.attachments],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return inbound.text
    if len(inbound.messages) == 1 and not inbound.messages[0].envelope.attachments:
        return inbound.messages[0].envelope.text
    return json.dumps(
        {
            "incoming_messages": [
                {
                    "label": f"msg_{message.ordinal}",
                    "update_id": message.update_id,
                    "message_id": message.envelope.message_id,
                    "sent_at": message.envelope.timestamp,
                    "content": message.envelope.text,
                    "attachments": [_prompt_attachment(attachment) for attachment in message.envelope.attachments],
                }
                for message in inbound.messages
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _prompt_attachment(attachment) -> dict:
    payload = {"kind": attachment.kind}
    if attachment.mime_type:
        payload["mime_type"] = attachment.mime_type
    metadata = {
        key: value
        for key, value in attachment.metadata.items()
        if key in {"width", "height", "file_size"} and isinstance(value, (int, float, str))
    }
    if metadata:
        payload["metadata"] = metadata
    return payload
