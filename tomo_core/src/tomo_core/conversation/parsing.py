from __future__ import annotations

import json
import re
from datetime import datetime
from math import isfinite

from ..delivery import split_sentences
from ..models import ResponseContract
from ..personal_data import MemoryControl, MemoryGovernanceControl, MemorySourceRef, MemoryWriteControl, OwnerSettingControl, PendingMemoryActionControl
from .models import ConversationMove, MoveConfidence, MovePlan, ReactionIntent

_REQUIRED_MOVE_PLAN_KEYS = {"primary_move", "supporting_moves", "response_goal", "confidence"}
_ALLOWED_MOVE_PLAN_KEYS = {*_REQUIRED_MOVE_PLAN_KEYS, "move_sequence", "reaction"}
_MARKDOWN_RE = re.compile(
    r"(?:^|\n)\s*(?:#{1,6}\s|>|[-*+]\s)|`|\[[^\]]+\]\([^)]+\)|\*\*[^*]+\*\*|(?<!\*)\*[^*\n]+\*(?!\*)"
)
_INTERNAL_LABEL_RE = re.compile(
    r"\b(?:primary move|supporting moves?|response goal|selected procedures?|chain[- ]of[- ]thought)\b|<\/?TOMO_SOUL>",
    re.IGNORECASE,
)
_WRITE_CONTROL_KEYS = {"action", "authority", "user_intent_excerpt", "memory_id", "kind", "subject_key", "topic", "value", "statement", "confidence", "salience", "surface_scope", "valid_from", "valid_until", "sources"}
_GOVERNANCE_CONTROL_KEYS = {"action", "target_memory_ids", "user_intent_excerpt"}
_PENDING_CONTROL_KEYS = {"action", "pending_action_id", "user_intent_excerpt"}
_SETTING_CONTROL_KEYS = {"action", "setting", "enabled", "user_intent_excerpt"}


class ConversationOutputError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"invalid conversation output: {code}")


def _parse_optional_reaction(value: object) -> ReactionIntent | None:
    if value is None:
        return None
    try:
        return ReactionIntent(value)
    except (TypeError, ValueError):
        return None


def _parse_strict_move_plan_payload(payload: object) -> MovePlan:
    if not isinstance(payload, dict) or set(payload) - _ALLOWED_MOVE_PLAN_KEYS or not _REQUIRED_MOVE_PLAN_KEYS <= set(payload):
        raise ValueError("invalid move plan keys")
    supporting = payload["supporting_moves"]
    if not isinstance(supporting, list):
        raise ValueError("supporting_moves must be a list")
    response_goal = payload["response_goal"]
    if not isinstance(response_goal, str):
        raise ValueError("response_goal must be text")
    return MovePlan(
        primary=ConversationMove(payload["primary_move"]),
        supporting=tuple(ConversationMove(item) for item in supporting),
        response_goal=response_goal,
        confidence=MoveConfidence(payload["confidence"]),
        sequence=tuple(ConversationMove(item) for item in payload.get("move_sequence", (payload["primary_move"], *supporting))),
        reaction=_parse_optional_reaction(payload.get("reaction")),
    )


def _parse_memory_control_payload(payload: object) -> MemoryControl:
    if not isinstance(payload, dict):
        raise ValueError("memory control must be an object")
    action = payload.get("action")
    if action in {"upsert", "add", "remove", "archive", "disable_by_agent"}:
        if set(payload) != _WRITE_CONTROL_KEYS:
            raise ValueError("invalid memory write keys")
        authority = _choice(payload["authority"], "authority", {"autonomous", "explicit_user"})
        excerpt = _nullable_text(payload["user_intent_excerpt"], "user_intent_excerpt", 240)
        memory_id = _nullable_compact(payload["memory_id"], "memory_id", 200)
        kind = _compact_limited(payload["kind"], "kind", 100)
        subject_key = _compact_limited(payload["subject_key"], "subject_key", 200)
        topic = _compact_limited(payload["topic"], "topic", 200)
        value = payload["value"]
        try:
            serialized_value = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("value must be JSON") from None
        if len(serialized_value) > 4096:
            raise ValueError("value exceeds size limit")
        statement = _nonblank_limited(payload["statement"], "statement", 1000)
        confidence = _unit_interval(payload["confidence"], "confidence")
        salience = _unit_interval(payload["salience"], "salience")
        scope = _choice(payload["surface_scope"], "surface_scope", {"always", "contextual", "archive"})
        valid_from = _nullable_timestamp(payload["valid_from"], "valid_from")
        valid_until = _nullable_timestamp(payload["valid_until"], "valid_until")
        if valid_from is not None and valid_until is not None and valid_from > valid_until:
            raise ValueError("valid_until precedes valid_from")
        sources = _parse_sources(payload["sources"])
        return MemoryWriteControl(action, authority, excerpt, memory_id, kind, subject_key, topic, value, statement, confidence, salience, scope, valid_from, valid_until, sources)
    if action in {"disable_by_user", "request_delete"}:
        if set(payload) != _GOVERNANCE_CONTROL_KEYS:
            raise ValueError("invalid memory governance keys")
        targets = _compact_list(payload["target_memory_ids"], "target_memory_ids", 8)
        return MemoryGovernanceControl(action, targets, _nonblank_limited(payload["user_intent_excerpt"], "user_intent_excerpt", 240))
    if action in {"confirm_delete", "cancel_delete"}:
        if set(payload) != _PENDING_CONTROL_KEYS:
            raise ValueError("invalid pending memory action keys")
        return PendingMemoryActionControl(action, _compact_limited(payload["pending_action_id"], "pending_action_id", 200), _nonblank_limited(payload["user_intent_excerpt"], "user_intent_excerpt", 240))
    if action == "set_owner_setting":
        if set(payload) != _SETTING_CONTROL_KEYS or not isinstance(payload["enabled"], bool):
            raise ValueError("invalid owner setting control")
        setting = _choice(payload["setting"], "setting", {"capture_enabled", "retrieval_enabled", "reactions_enabled"})
        return OwnerSettingControl(action, setting, payload["enabled"], _nonblank_limited(payload["user_intent_excerpt"], "user_intent_excerpt", 240))
    raise ValueError("invalid memory control action")


def _compact_limited(value: object, field_name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value or len(value.strip()) > limit:
        raise ValueError(f"invalid {field_name}")
    return value.strip()


def _nonblank_limited(value: object, field_name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"invalid {field_name}")
    return value.strip()


def _nullable_text(value: object, field_name: str, limit: int) -> str | None:
    return None if value is None else _nonblank_limited(value, field_name, limit)


def _nullable_compact(value: object, field_name: str, limit: int) -> str | None:
    return None if value is None else _compact_limited(value, field_name, limit)


def _choice(value: object, field_name: str, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"invalid {field_name}")
    return value


def _unit_interval(value: object, field_name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"invalid {field_name}")
    return float(value)


def _nullable_timestamp(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    value = _compact_limited(value, field_name, 64)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"invalid {field_name}") from None
    return value


def _compact_list(value: object, field_name: str, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > limit:
        raise ValueError(f"invalid {field_name}")
    items = tuple(_compact_limited(item, field_name, 200) for item in value)
    if len(set(items)) != len(items):
        raise ValueError(f"duplicate {field_name}")
    return items


def _parse_sources(value: object) -> tuple[MemorySourceRef, ...]:
    if not isinstance(value, list) or not value or len(value) > 8:
        raise ValueError("invalid sources")
    sources: list[MemorySourceRef] = []
    for source in value:
        if not isinstance(source, dict) or set(source) != {"source_kind", "source_id", "observed_at"}:
            raise ValueError("invalid source keys")
        sources.append(MemorySourceRef(
            _choice(source["source_kind"], "source_kind", {"current_message", "session_message", "tool_observation", "assistant_conclusion", "inference"}),
            _compact_limited(source["source_id"], "source_id", 200),
            _required_timestamp(source["observed_at"], "observed_at"),
        ))
    if len({(source.source_kind, source.source_id) for source in sources}) != len(sources):
        raise ValueError("duplicate sources")
    return tuple(sources)


def _required_timestamp(value: object, field_name: str) -> str:
    if value is None:
        raise ValueError(f"invalid {field_name}")
    timestamp = _nullable_timestamp(value, field_name)
    assert timestamp is not None
    return timestamp


def _validate_strict_frame_text(text: object, *, max_chars: int, max_sentences: int) -> str:
    if not isinstance(text, str) or not text.strip() or "\n" in text or "\r" in text:
        raise ValueError("invalid_frame")
    cleaned = text.strip()
    if len(cleaned) > max_chars:
        raise ValueError("frame_too_long")
    if "—" in cleaned or "–" in cleaned:
        raise ValueError("banned_dash")
    if _MARKDOWN_RE.search(cleaned):
        raise ValueError("markdown")
    if _INTERNAL_LABEL_RE.search(cleaned):
        raise ValueError("internal_label")
    if len(split_sentences(cleaned)) > max_sentences:
        raise ValueError("frame_sentence_limit")
    return cleaned


def parse_utterances(raw: str, contract: ResponseContract | None = None) -> tuple[str, ...]:
    contract = contract or ResponseContract()
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise ConversationOutputError("invalid_json") from None
    if not isinstance(payload, dict) or set(payload) != {"utterances"}:
        raise ConversationOutputError("invalid_shape")
    utterances = payload["utterances"]
    if not isinstance(utterances, list) or not contract.min_utterances <= len(utterances) <= contract.max_utterances:
        raise ConversationOutputError("invalid_utterance_count")
    if not all(isinstance(item, str) and item.strip() for item in utterances):
        raise ConversationOutputError("invalid_utterance")
    cleaned = tuple(item.strip() for item in utterances)
    if any("—" in item or "–" in item for item in cleaned):
        raise ConversationOutputError("banned_dash")
    if any(_MARKDOWN_RE.search(item) for item in cleaned):
        raise ConversationOutputError("markdown")
    if any(_INTERNAL_LABEL_RE.search(item) for item in cleaned):
        raise ConversationOutputError("internal_label")
    if any(len(split_sentences(item)) > contract.max_sentences_per_utterance for item in cleaned):
        raise ConversationOutputError("sentence_limit")
    return cleaned


def parse_utterance(raw: str, contract: ResponseContract | None = None) -> str:
    contract = contract or ResponseContract()
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise ConversationOutputError("invalid_json") from None
    if not isinstance(payload, dict) or set(payload) != {"utterance"}:
        raise ConversationOutputError("invalid_shape")
    utterance = payload["utterance"]
    if not isinstance(utterance, str) or not utterance.strip():
        raise ConversationOutputError("invalid_utterance")
    return parse_utterances(json.dumps({"utterances": [utterance]}), contract)[0]
