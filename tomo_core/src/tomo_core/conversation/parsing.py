from __future__ import annotations

import json
import re

from ..delivery import split_sentences
from ..models import ResponseContract
from .models import ConversationMove, MoveConfidence, MovePlan

_REQUIRED_MOVE_PLAN_KEYS = {"primary_move", "supporting_moves", "response_goal", "confidence"}
_ALLOWED_MOVE_PLAN_KEYS = {*_REQUIRED_MOVE_PLAN_KEYS, "move_sequence"}
_MARKDOWN_RE = re.compile(
    r"(?:^|\n)\s*(?:#{1,6}\s|>|[-*+]\s)|`|\[[^\]]+\]\([^)]+\)|\*\*[^*]+\*\*|(?<!\*)\*[^*\n]+\*(?!\*)"
)
_INTERNAL_LABEL_RE = re.compile(
    r"\b(?:primary move|supporting moves?|response goal|selected procedures?|chain[- ]of[- ]thought)\b|<\/?TOMO_SOUL>",
    re.IGNORECASE,
)


class ConversationOutputError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"invalid conversation output: {code}")


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
    )


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
