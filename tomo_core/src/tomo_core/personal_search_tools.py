from __future__ import annotations

import json

from .personal_data import MemorySearchQuery, PersonalDataRepository, SessionSearchQuery, StorageSearchError
from .tools import BoundTool, ToolRegistry, ToolSpec


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
