from __future__ import annotations

import re

from .models import OutboundBubble

_SENTENCE_RE = re.compile(r"[^.!?。！？]+[.!?。！？]?", re.UNICODE)
_MARKDOWN_CHARS_RE = re.compile(r"[*_`#>]+")
_BANNED_DASHES_RE = re.compile(r"\s*[—–]\s*")


def strip_markdown(text: str) -> str:
    cleaned = _MARKDOWN_CHARS_RE.sub("", text)
    cleaned = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1: \2", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def sanitize_style(text: str) -> str:
    cleaned = _BANNED_DASHES_RE.sub(", ", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def split_sentences(text: str) -> list[str]:
    parts = [part.strip() for part in _SENTENCE_RE.findall(text) if part.strip()]
    return parts or [text.strip()]


class DeliveryPlanner:
    def __init__(self, min_bubbles: int = 1, max_bubbles: int = 4, max_sentences_per_bubble: int = 2) -> None:
        if min_bubbles < 1:
            raise ValueError("min_bubbles must be at least 1")
        if max_bubbles > 4:
            raise ValueError("max_bubbles cannot exceed 4")
        self.min_bubbles = min_bubbles
        self.max_bubbles = max_bubbles
        self.max_sentences_per_bubble = max_sentences_per_bubble

    def compose(self, text: str, reply_to_message_id: str) -> list[OutboundBubble]:
        plain = sanitize_style(strip_markdown(text))
        sentences = split_sentences(plain)
        groups: list[str] = []
        current: list[str] = []

        for sentence in sentences:
            current.append(sentence)
            if len(current) >= self.max_sentences_per_bubble:
                groups.append(" ".join(current).strip())
                current = []
        if current:
            groups.append(" ".join(current).strip())

        groups = [group for group in groups if group]
        groups = groups[: self.max_bubbles]
        if not groups:
            groups = ["got it."]

        # model_probability(1, 4): never produce zero bubbles. the model/draft may be long,
        # but the validator clamps physical telegram messages to 1..4.
        bubbles = [OutboundBubble(text=group) for group in groups]
        bubbles[0] = OutboundBubble(text=bubbles[0].text, reply_to_message_id=reply_to_message_id)
        return bubbles
