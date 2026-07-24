"""Proactive background memory extraction module for Tomo."""

from __future__ import annotations

import json
import logging
from typing import Sequence

from ..personal_data import MemorySourceRef, MemoryWriteControl
from ..providers import ProviderAdapter, ProviderTextDelta

logger = logging.getLogger("tomo_core.memory.extractor")

EXTRACTION_SYSTEM_PROMPT = (
    "You are an autonomous memory extraction agent for a personal AI assistant.\n"
    "Your job is to analyze recent conversation turns between the owner (user) and assistant, "
    "and extract durable memories, preferences, facts, identity declarations, relationships, plans, or qualified inferences.\n\n"
    "Output ONLY a valid JSON array containing extracted candidate memory objects. If no notable memories are found, return [].\n"
    "Each object in the array MUST have the following schema:\n"
    "{\n"
    '  "kind": "fact" | "preference" | "relationship" | "plan" | "routine" | "inference",\n'
    '  "subject_key": "self" | string (identifying subject, e.g. "self" for owner),\n'
    '  "topic": string (short topic identifier, e.g. "name", "location", "coffee_preference"),\n'
    '  "value": string or object (the structured value),\n'
    '  "statement": string (human readable memory statement),\n'
    '  "confidence": float between 0.0 and 1.0,\n'
    '  "salience": float between 0.0 and 1.0,\n'
    '  "scope": "always" | "contextual" | "archive"\n'
    "}\n\n"
    "Scope guidelines:\n"
    "- 'always': Stable identity claims (name, nickname, primary location), core lifestyle choices.\n"
    "- 'contextual': Projects, temporary preferences, topic-specific context.\n"
    "- 'archive': Historical notes, completed plans.\n"
)


class ProactiveMemoryExtractor:
    """Extracts structured memories from completed conversational turn pairs."""

    def __init__(self, provider: ProviderAdapter | None = None, min_confidence: float = 0.5) -> None:
        self.provider = provider
        # Confidence threshold defaults to 0.5 per user specification
        self.min_confidence = min_confidence

    def extract_memories(
        self,
        user_text: str,
        assistant_text: str,
        generation_id: str,
        timestamp: str,
    ) -> tuple[MemoryWriteControl, ...]:
        """Analyze turn pair and return extracted MemoryWriteControls meeting confidence threshold."""
        if not self.provider or not user_text.strip():
            return ()

        prompt = (
            f"User message: {user_text}\n"
            f"Assistant response: {assistant_text}\n\n"
            "Extract all candidate memories from this exchange as JSON array:"
        )

        messages = [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        try:
            output_text = ""
            for event in self.provider.stream(messages):
                if isinstance(event, ProviderTextDelta):
                    output_text += event.text

            return self._parse_extraction_output(output_text, generation_id, timestamp)
        except Exception as error:
            logger.warning("Proactive memory extraction failed: %s", error)
            return ()

    def _parse_extraction_output(
        self,
        output_text: str,
        generation_id: str,
        timestamp: str,
    ) -> tuple[MemoryWriteControl, ...]:
        cleaned = output_text.strip()
        if not cleaned:
            return ()

        # Extract JSON substring if wrapped in markdown code fence
        if "```json" in cleaned:
            cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in cleaned:
            cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("Memory extractor received non-JSON output: %r", cleaned[:100])
            return ()

        if not isinstance(data, list):
            return ()

        results: list[MemoryWriteControl] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            control = self._build_control(item, generation_id, timestamp)
            if control is not None:
                results.append(control)

        return tuple(results)

    def _build_control(
        self,
        item: dict[str, object],
        generation_id: str,
        timestamp: str,
    ) -> MemoryWriteControl | None:
        kind = str(item.get("kind", "fact")).lower()
        if kind not in {"fact", "preference", "relationship", "plan", "routine", "inference"}:
            kind = "fact"

        subject_key = str(item.get("subject_key", "self")).strip() or "self"
        topic = str(item.get("topic", "general")).strip() or "general"
        statement = str(item.get("statement", "")).strip()
        if not statement:
            return None

        try:
            confidence = float(item.get("confidence", 0.8))
        except (ValueError, TypeError):
            confidence = 0.8

        if confidence < self.min_confidence:
            return None

        try:
            salience = float(item.get("salience", 0.8))
        except (ValueError, TypeError):
            salience = 0.8

        scope = str(item.get("scope", "contextual")).lower()
        if scope not in {"always", "contextual", "archive"}:
            scope = "contextual"

        # Force identity claims (e.g. name, nickname) to 'always' scope
        if subject_key == "self" and topic in {"name", "nickname", "identity"}:
            scope = "always"
            salience = max(salience, 0.95)
            confidence = max(confidence, 0.95)

        value = item.get("value", statement)
        source_kind = "current_message" if confidence >= 0.85 else "inference"

        return MemoryWriteControl(
            action="upsert",
            authority="autonomous",
            user_intent_excerpt=None,
            memory_id=None,
            kind=kind,
            subject_key=subject_key,
            topic=topic,
            value=value,
            statement=statement,
            confidence=confidence,
            salience=salience,
            surface_scope=scope,  # type: ignore[arg-type]
            valid_from=None,
            valid_until=None,
            sources=(
                MemorySourceRef(
                    source_kind=source_kind,  # type: ignore[arg-type]
                    source_id=generation_id,
                    observed_at=timestamp,
                ),
            ),
        )
