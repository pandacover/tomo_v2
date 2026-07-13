from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Protocol

from .sessions import ConversationSession, StoredMessage


class PersonalDataError(Exception):
    """Safe base exception for personal-data adapters."""


class StorageBusyError(PersonalDataError):
    pass


class StorageCapabilityError(PersonalDataError):
    pass


class StorageSearchError(PersonalDataError):
    pass


@dataclass(frozen=True)
class SessionIdentity:
    id: str
    owner_id: str
    session_key: str
    connector: str
    actor_id: str


@dataclass(frozen=True)
class SessionSearchQuery:
    owner_id: str
    text: str
    limit: int = 5
    context_before: int = 2
    context_after: int = 2
    roles: tuple[str, ...] = ("user", "assistant")


@dataclass(frozen=True)
class SessionSearchHit:
    session_id: str; session_key: str; connector: str; matched_message_id: str
    matched_role: str; matched_text: str; timestamp: str; score: float
    context: tuple[StoredMessage, ...]


@dataclass(frozen=True)
class MemorySourceRef:
    source_kind: Literal["current_message", "session_message", "tool_observation", "assistant_conclusion", "inference"]
    source_id: str
    observed_at: str
    available: bool = True


@dataclass(frozen=True)
class MemoryWriteControl:
    action: Literal["upsert", "add", "remove", "archive", "disable_by_agent"]
    authority: Literal["autonomous", "explicit_user"]
    user_intent_excerpt: str | None; memory_id: str | None; kind: str; subject_key: str; topic: str
    value: object; statement: str; confidence: float; salience: float
    surface_scope: Literal["always", "contextual", "archive"]
    valid_from: str | None; valid_until: str | None; sources: tuple[MemorySourceRef, ...]


@dataclass(frozen=True)
class MemoryGovernanceControl:
    action: Literal["disable_by_user", "request_delete"]
    target_memory_ids: tuple[str, ...]
    user_intent_excerpt: str


@dataclass(frozen=True)
class PendingMemoryActionControl:
    action: Literal["confirm_delete", "cancel_delete"]
    pending_action_id: str
    user_intent_excerpt: str


@dataclass(frozen=True)
class OwnerSettingControl:
    action: Literal["set_owner_setting"]
    setting: Literal["capture_enabled", "retrieval_enabled", "reactions_enabled"]
    enabled: bool
    user_intent_excerpt: str


MemoryControl = MemoryWriteControl | MemoryGovernanceControl | PendingMemoryActionControl | OwnerSettingControl


@dataclass(frozen=True)
class MemoryRecord:
    id: str; owner_id: str; kind: str; subject_key: str; topic: str; value: object; statement: str
    epistemic_kind: Literal["user_stated", "session_derived", "tool_derived", "assistant_conclusion", "inferred"]
    status: Literal["provisional", "active", "archived", "disabled_by_agent", "disabled_by_user", "superseded"]
    confidence: float; salience: float; surface_scope: Literal["always", "contextual", "archive"]
    valid_from: str | None; valid_until: str | None; sources: tuple[MemorySourceRef, ...]


@dataclass(frozen=True)
class MemoryContextQuery:
    owner_id: str; text: str; always_limit: int = 16; contextual_limit: int = 8; total_chars: int = 2500


@dataclass(frozen=True)
class MemorySearchQuery:
    owner_id: str; text: str | None; limit: int = 8


@dataclass(frozen=True)
class MemorySearchHit:
    memory: MemoryRecord; score: float


@dataclass(frozen=True)
class MemoryGovernanceResult:
    outcome: Literal["applied", "pending_confirmation", "cancelled", "ambiguous", "not_found", "rejected"]
    target_memory_ids: tuple[str, ...] = (); pending_action_id: str | None = None


@dataclass(frozen=True)
class PendingMemoryAction:
    id: str
    target_memory_ids: tuple[str, ...]
    target_statements: tuple[str, ...]


@dataclass(frozen=True)
class OwnerMemorySettings:
    owner_id: str; capture_enabled: bool = True; retrieval_enabled: bool = True; reactions_enabled: bool = True; governance_revision: int = 0


class PersonalDataRepository(Protocol):
    def load_session(self, owner_id: str, session_key: str) -> ConversationSession: ...
    def save_session(self, owner_id: str, session: ConversationSession, *, generation_id: str | None = None, revision: int | None = None) -> bool: ...
    def accept_generations(self, owner_id: str, session_key: str, generation_ids: tuple[str, ...]) -> None: ...
    def search_sessions(self, query: SessionSearchQuery) -> tuple[SessionSearchHit, ...]: ...
    def memory_context(self, query: MemoryContextQuery) -> tuple[MemoryRecord, ...]: ...
    def search_memories(self, query: MemorySearchQuery) -> tuple[MemorySearchHit, ...]: ...
    def stage_memory_controls(self, owner_id: str, session_key: str, generation_id: str, segment_index: int, governance_revision: int, controls: tuple[MemoryWriteControl, ...], *, revision: int | None = None) -> bool: ...
    def apply_user_memory_control(self, owner_id: str, session_key: str, control: MemoryControl) -> MemoryGovernanceResult: ...
    def pending_memory_actions(self, owner_id: str, session_key: str) -> tuple[PendingMemoryAction, ...]: ...
    def memory_settings(self, owner_id: str) -> OwnerMemorySettings: ...
    def update_memory_setting(self, owner_id: str, setting: str, enabled: bool) -> OwnerMemorySettings: ...
    def delete_session(self, owner_id: str, session_id: str, cascade_memories: bool = False) -> None: ...
    def delete_owner(self, owner_id: str) -> None: ...
    def rebuild_index(self, owner_id: str | None = None) -> None: ...
    def integrity_check(self) -> bool: ...
    def prune_provisional_artifacts(self, older_than: str) -> int: ...


class PersonalDataTransferRepository(Protocol):
    """Backend-neutral canonical transfer capability."""
    def export_owner_records(self, owner_id: str) -> Iterable[dict[str, object]]: ...
    def import_owner_records(self, owner_id: str, records: Iterable[dict[str, object]]) -> None: ...
