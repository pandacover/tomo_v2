from __future__ import annotations

import json
import re
from typing import Callable

from .personal_data import MemorySearchQuery, PersonalDataRepository, SessionSearchQuery, StorageSearchError
from .tools import BoundTool, ToolRegistry, ToolSpec


def peer_personal_search_registry(
    repository: PersonalDataRepository,
    owner_id: str,
    scope: str,
    *,
    observe: Callable[[tuple[dict[str, object], ...]], None] | None = None,
) -> ToolRegistry:
    """Expose a fixed, typed owner projection for an authorized peer turn only."""
    if scope not in {"availability", "calendar_detail", "contact_email", "contact_phone", "precise_location"}:
        return ToolRegistry()
    terms = {
        "availability": ("availability", "free", "busy", "schedule"),
        "calendar_detail": ("calendar", "meeting", "schedule"),
        "contact_email": ("email", "contact"),
        "contact_phone": ("phone", "contact"),
        "precise_location": ("location", "latitude", "longitude"),
    }[scope]

    def project(_arguments: dict[str, object]) -> object:
        hits = []
        seen = set()
        try:
            for term in terms:
                for hit in repository.search_memories(MemorySearchQuery(owner_id, term, 8)):
                    if hit.memory.id not in seen:
                        seen.add(hit.memory.id)
                        hits.append(hit)
        except StorageSearchError:
            return {"ok": False, "candidates": []}
        candidates = []
        for hit in hits:
            if hit.memory.subject_key != "self":
                continue
            value = json.dumps({"statement": hit.memory.statement, "value": hit.memory.value}, ensure_ascii=True)
            candidate = _peer_candidate(scope, value)
            if candidate is not None:
                candidates.append(candidate)
            if len(candidates) == 3:
                break
        if observe is not None:
            observe(tuple(candidates))
        return {"ok": True, "scope": scope, "candidates": candidates}

    return ToolRegistry((BoundTool(ToolSpec(
        "personal_search", "Retrieve bounded typed owner candidates for the authorized peer scope.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        read_only=True, parallel_safe=True, internal_context=False, unattended_safe=True,
    ), project),))


def _peer_candidate(scope: str, value: str) -> dict[str, object] | None:
    if scope == "contact_email":
        match = re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", value)
        return None if match is None else {"email": match.group(0)}
    if scope == "contact_phone":
        match = re.search(r"\+[1-9]\d{7,14}\b", value)
        return None if match is None else {"phone": match.group(0)}
    if scope == "precise_location":
        match = re.search(r"(?:latitude|lat)\D{0,12}(-?\d{1,2}(?:\.\d+)?)\D{0,30}(?:longitude|lon)\D{0,12}(-?\d{1,3}(?:\.\d+)?)", value, re.I)
        if match is not None and -90 <= float(match.group(1)) <= 90 and -180 <= float(match.group(2)) <= 180:
            return {"latitude": float(match.group(1)), "longitude": float(match.group(2))}
    if scope == "calendar_detail":
        match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b.*?\b([0-2]\d:[0-5]\d)\b(?:.*?\b([0-2]\d:[0-5]\d)\b)?", value)
        return None if match is None else {"date": match.group(1), "start": match.group(2), "end": match.group(3)}
    if scope == "availability":
        lowered = value.lower()
        status = next((item for item in ("free", "busy") if item in lowered), "unknown")
        window = next((item for item in ("morning", "afternoon", "evening", "day") if item in lowered), "unknown")
        return {"status": status, "window": window}
    return None


def personal_search_registry(repository: PersonalDataRepository, owner_id: str) -> ToolRegistry:
    """Create read-only personal-data tools bound to one runtime owner."""
    def search_memories(arguments: dict[str, object]) -> object:
        query = arguments.get("query")
        limit = arguments.get("limit", 8)
        if query is not None and (not isinstance(query, str) or len(query) > 500):
            raise ValueError("invalid_query")
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("invalid_limit")
        try:
            hits = repository.search_memories(MemorySearchQuery(owner_id, query, max(1, min(limit, 8))))
        except StorageSearchError:
            return {"ok": False, "error": "personal_search_unavailable"}
        return {"ok": True, "memories": [{
            "id": hit.memory.id, "statement": hit.memory.statement, "value": hit.memory.value,
            "epistemic_kind": hit.memory.epistemic_kind, "confidence": hit.memory.confidence,
            "salience": hit.memory.salience, "surface_scope": hit.memory.surface_scope,
            "valid_from": hit.memory.valid_from, "valid_until": hit.memory.valid_until,
            "sources": [{"kind": source.source_kind, "available": source.available} for source in hit.memory.sources],
        } for hit in hits]}

    def search_sessions(arguments: dict[str, object]) -> object:
        query = arguments.get("query")
        limit = arguments.get("limit", 5)
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError("invalid_query")
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("invalid_limit")
        try:
            hits = repository.search_sessions(SessionSearchQuery(owner_id, query, max(1, min(limit, 5))))
        except StorageSearchError:
            return {"ok": False, "error": "personal_search_unavailable"}
        return {"ok": True, "sessions": [{
            "session_id": hit.session_id, "session_key": hit.session_key, "connector": hit.connector,
            "message_id": hit.matched_message_id, "role": hit.matched_role, "text": hit.matched_text,
            "timestamp": hit.timestamp, "score": hit.score,
            "context": [{"role": item.role, "text": item.content, "timestamp": item.timestamp} for item in hit.context],
        } for hit in hits]}

    schemas = (
        BoundTool(ToolSpec("search_memories", "Search this owner's retained personal memories.", {
            "type": "object", "properties": {
                "query": {"type": "string", "maxLength": 500},
                "limit": {"type": "integer", "minimum": 1, "maximum": 8},
            }, "additionalProperties": False,
        }, unattended_safe=True), search_memories),
        BoundTool(ToolSpec("search_sessions", "Search this owner's accepted conversation history.", {
            "type": "object", "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5},
            }, "required": ["query"], "additionalProperties": False,
        }, unattended_safe=True), search_sessions),
    )
    return ToolRegistry(schemas)
