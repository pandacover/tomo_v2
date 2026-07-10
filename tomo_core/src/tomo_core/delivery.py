from __future__ import annotations

import re

from .models import OutboundBubble, ResponseContract

_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=\S)|(?<=[。！？])", re.UNICODE)
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
    parts = [part.strip() for part in _SENTENCE_BOUNDARY_RE.split(text) if part.strip()]
    return parts or [text.strip()]


class DeliveryPlanner:
    def __init__(
        self,
        min_bubbles: int = 1,
        max_bubbles: int = 4,
        max_sentences_per_bubble: int = 3,
        *,
        contract: ResponseContract | None = None,
    ) -> None:
        self.contract = contract or ResponseContract(
            min_utterances=min_bubbles,
            max_utterances=max_bubbles,
            max_sentences_per_utterance=max_sentences_per_bubble,
        )
        self.min_bubbles = self.contract.min_utterances
        self.max_bubbles = self.contract.max_utterances
        self.max_sentences_per_bubble = self.contract.max_sentences_per_utterance

    def compose(self, text: str, reply_to_message_id: str) -> list[OutboundBubble]:
        """Compatibility fallback for older direct-draft callers."""
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
        if len(groups) > self.max_bubbles:
            raise ValueError("draft exceeds delivery bubble limit")
        if not groups:
            groups = ["got it."]

        # model_probability(1, 4): never produce zero bubbles. the model/draft may be long,
        # but the validator clamps physical telegram messages to 1..4.
        bubbles = [OutboundBubble(text=group) for group in groups]
        bubbles[0] = OutboundBubble(text=bubbles[0].text, reply_to_message_id=reply_to_message_id)
        return bubbles

    def compose_utterances(self, utterances: tuple[str, ...], reply_to_message_id: str) -> list[OutboundBubble]:
        if not self.min_bubbles <= len(utterances) <= self.max_bubbles:
            raise ValueError("utterance count violates delivery contract")
        cleaned = tuple(sanitize_style(strip_markdown(item)) for item in utterances)
        if any(not item for item in cleaned):
            raise ValueError("utterances cannot be empty")
        if any(len(split_sentences(item)) > self.max_sentences_per_bubble for item in cleaned):
            raise ValueError("utterance exceeds sentence limit")
        bubbles = [OutboundBubble(text=item) for item in cleaned]
        bubbles[0] = OutboundBubble(text=bubbles[0].text, reply_to_message_id=reply_to_message_id)
        return bubbles
