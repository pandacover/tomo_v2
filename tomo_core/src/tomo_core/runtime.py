from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Literal, TypeAlias

from .conversation import ConversationEngine, ConversationRequest, FrameReady, MemoryControlReady, ReactionIntent, ReactionWindowReady, TurnRunCompleted, TurnRunStarted
from .delivery import sanitize_style, strip_markdown
from .graph import build_langgraph_or_linear
from .models import InboundEnvelope, InputBurst, OutboundBubble, RuntimeConfig
from .providers import ProviderAdapter
from .reaction_service import ReactionDeliveryKey, ReactionService
from .legacy_session_import import import_legacy_sessions
from .memory_governance import MemoryGovernanceService
from .personal_data import (MemoryContextQuery, MemoryWriteControl,
                            PersonalDataRepository, StorageBusyError,
                            StorageCapabilityError, StorageSearchError)
from .personal_search_tools import personal_search_registry
from .sqlite_personal_data import SqlitePersonalDataRepository
from .sessions import StoredMessage
from .soul import load_soul
from .telegram import TelegramDeliverySink
from .tools import ToolRegistry


@dataclass(frozen=True)
class RuntimeFrameReady:
    event: FrameReady
    bubble: OutboundBubble


@dataclass(frozen=True)
class RuntimeCompleted:
    event: TurnRunCompleted


@dataclass(frozen=True)
class RuntimeReactionReady:
    owner_id: str
    actor_id: str
    chat_id: str
    target_message_id: str
    generation_id: str
    revision: int
    emoji: str

    def __post_init__(self) -> None:
        ReactionIntent(self.emoji)


class StaleSessionRevisionError(RuntimeError):
    def __init__(self, current_revision: int) -> None:
        self.current_revision = current_revision
        super().__init__("stale_session_revision")


RuntimeEvent: TypeAlias = RuntimeReactionReady | RuntimeFrameReady | RuntimeCompleted


@dataclass(frozen=True)
class MemoryControlDiagnostic:
    reason_code: Literal["secret_detected", "invalid_provenance", "stale_governance_or_revision", "storage_failure"]


class _OwnerSessionStore:
    """Compatibility facade backed by the owner-scoped repository, not JSON."""

    def __init__(self, repository: PersonalDataRepository, owner_id: str) -> None:
        self._repository = repository
        self._owner_id = owner_id

    def load(self, session_key: str):
        return self._repository.load_session(self._owner_id, session_key)


class PersonalAgentRuntime:
    def __init__(
        self,
        provider: ProviderAdapter,
        telegram: TelegramDeliverySink,
        config: RuntimeConfig | None = None,
        tool_registry: ToolRegistry | None = None,
        personal_data_repository: PersonalDataRepository | None = None,
    ) -> None:
        self.config = config or RuntimeConfig()
        self.provider = provider
        self.personal_data = personal_data_repository or SqlitePersonalDataRepository(
            Path(self.config.data_dir) / "tomo.sqlite3", local_work_dir=self.config.local_work_dir
        )
        if self.config.owner_id is None:
            raise ValueError("owner_id is required for persisted runtime state")
        self.owner_id = self.config.owner_id
        # Personal search is mandatory on every runtime path.  Caller tools extend it;
        # duplicate names fail closed instead of replacing owner-bound tools.
        if not provider.supports_tool_calls:
            if tool_registry is not None and tool_registry.schemas():
                raise ValueError("provider does not support tool calls")
            self.tool_registry = ToolRegistry()
        else:
            self.tool_registry = personal_search_registry(self.personal_data, self.owner_id)
            if tool_registry is not None:
                self.tool_registry = self.tool_registry.extend(tool_registry)
        if self.tool_registry.schemas() and not provider.supports_tool_calls:
            raise ValueError("provider does not support tool calls")
        contract = self.config.response_contract
        budget = self.config.tool_turn_budget if self.tool_registry.schemas() else self.config.ordinary_turn_budget
        self.conversation = ConversationEngine(provider, contract=contract, budget=budget, tool_registry=self.tool_registry)
        self.telegram = telegram
        self.reactions = ReactionService()
        self.memory_control_diagnostics: list[MemoryControlDiagnostic] = []
        self.sessions = _OwnerSessionStore(self.personal_data, self.owner_id)
        import_legacy_sessions(self.personal_data, self.owner_id, self.config.data_dir)
        self.graph = self._build_graph()

    def handle_telegram_text(self, envelope: InboundEnvelope) -> list[dict[str, str | None]]:
        if envelope.connector != "telegram":
            raise ValueError("milestone 1 only supports telegram envelopes")
        from .models import InboundMessage, InputBurst
        burst = InputBurst(f"legacy-{envelope.message_id}", f"legacy-{envelope.message_id}", 1, (InboundMessage(1, int(envelope.native_metadata.get("update_id", 0)), envelope),))
        bubbles = []
        for event in self.handle_telegram_burst_iter(burst):
            if isinstance(event, RuntimeReactionReady):
                try:
                    self.telegram.react_to_message(envelope.actor_id, envelope.message_id, event.emoji)
                except Exception:
                    # Reactions are optional delivery effects; never lose text on failure.
                    pass
            elif isinstance(event, RuntimeFrameReady):
                bubbles.append(event.bubble)
        self.telegram.send_bubbles(envelope.actor_id, bubbles)
        return [
            {"text": bubble.text, "reply_to_message_id": bubble.reply_to_message_id}
            for bubble in bubbles
        ]

    def handle_telegram_burst_iter(
        self,
        burst: InputBurst,
        *,
        is_active: Callable[[], bool] | None = None,
    ) -> Iterator[RuntimeEvent]:
        if burst.latest.connector != "telegram":
            raise ValueError("runtime only supports telegram bursts")
        is_active = is_active or (lambda: True)
        self.telegram.start_typing(burst.latest.actor_id)
        session = self.personal_data.load_session(self.owner_id, burst.latest.session_key)
        session.accept_generations(burst.accepted_generation_ids)
        for message in burst.messages:
            session.append_inbound_once(message, burst.burst_id)
        if not self.personal_data.save_session(self.owner_id, session, generation_id=burst.generation_id, revision=burst.revision):
            current_revision = self.personal_data.current_session_revision(self.owner_id, burst.latest.session_key)
            if current_revision is None:
                raise RuntimeError("stale_session_revision_unavailable")
            raise StaleSessionRevisionError(current_revision)
        if not is_active():
            return

        try:
            memories = self.personal_data.memory_context(MemoryContextQuery(self.owner_id, "\n".join(m.envelope.text for m in burst.messages)))
            pending_actions = self.personal_data.pending_memory_actions(self.owner_id, burst.latest.session_key)
        except (StorageBusyError, StorageSearchError, StorageCapabilityError):
            memories = ()
            pending_actions = ()
        memory_block = _memory_data_block(memories, pending_actions)
        history = list(session.model_history_for_burst(burst.burst_id))
        if memory_block:
            history.append({"role": "user", "content": memory_block})
        request = ConversationRequest(
            burst=burst,
            soul=load_soul(Path(self.config.soul_path)),
            history=tuple(history),
        )
        governance_revision = self.personal_data.memory_settings(self.owner_id).governance_revision
        conversation_events = self.conversation.respond_iter(request, is_active=is_active)
        delivered: list[OutboundBubble] = []
        reaction_emoji: str | None = None
        chat_id = str(burst.latest.native_metadata.get("chat_id") or burst.latest.actor_id)
        reaction_key = ReactionDeliveryKey(self.owner_id, chat_id, burst.generation_id, burst.revision, burst.latest.message_id)

        def emit_reaction() -> RuntimeReactionReady | None:
            if reaction_emoji is None:
                return None
            try:
                enabled = self.personal_data.memory_settings(self.owner_id).reactions_enabled
            except Exception:
                enabled = False
            if self.reactions.admit(reaction_key, reactions_enabled=enabled, is_active=is_active):
                return RuntimeReactionReady(self.owner_id, burst.latest.actor_id, chat_id, burst.latest.message_id, burst.generation_id, burst.revision, reaction_emoji)
            return None
        while True:
            if not is_active():
                return
            try:
                event = next(conversation_events)
            except StopIteration:
                return
            if not is_active():
                return
            if isinstance(event, TurnRunStarted):
                reaction_emoji = event.plan.reaction.emoji if event.plan.reaction is not None else None
                continue
            if isinstance(event, MemoryControlReady):
                if isinstance(event.control, MemoryWriteControl) and _safe_memory_control(event.control, burst, session, set(event.tool_observation_ids)):
                    try:
                        if not self.personal_data.stage_memory_controls(self.owner_id, burst.latest.session_key, burst.generation_id, event.segment_index, governance_revision, (event.control,), revision=burst.revision):
                            self._record_memory_diagnostic("stale_governance_or_revision")
                    except (StorageBusyError, StorageCapabilityError, ValueError):
                        self._record_memory_diagnostic("storage_failure")
                elif isinstance(event.control, MemoryWriteControl):
                    self._record_memory_diagnostic(_memory_control_rejection_code(event.control, burst, session, set(event.tool_observation_ids)))
                elif not isinstance(event.control, MemoryWriteControl):
                    # Direct user governance is authoritative immediately, unlike staged writes.
                    try:
                        MemoryGovernanceService(self.personal_data).apply(
                            self.owner_id,
                            burst.latest.session_key,
                            "\n".join(message.envelope.text for message in burst.messages),
                            event.control,
                        )
                    except (StorageBusyError, ValueError):
                        pass
                continue
            if isinstance(event, ReactionWindowReady):
                ready = emit_reaction()
                if ready is not None:
                    yield ready
                continue
            if isinstance(event, FrameReady):
                ready = emit_reaction()
                if ready is not None:
                    yield ready
                if not is_active():
                    return
                bubble = self._compose_progressive_bubble(event, burst.latest.message_id, bool(delivered))
                if not is_active():
                    return
                delivered.append(bubble)
                # A visible frame is never released until its generation row is durable.
                if not self._persist_provisional_frame(session, burst, event, delivered, is_active):
                    return
                if not is_active():
                    return
                yield RuntimeFrameReady(event=event, bubble=bubble)
                continue
            if isinstance(event, TurnRunCompleted):
                if not is_active():
                    return
                if not self._persist_completed_burst(session, burst, event, delivered, is_active):
                    return
                if not is_active():
                    return
                yield RuntimeCompleted(event=event)
                return

    def _build_graph(self):
        return build_langgraph_or_linear(
            [
                ("load_context", self._load_context),
                ("run_conversation", self._run_conversation),
                ("compose_delivery", self._compose_delivery),
                ("persist_turn", self._persist_turn),
            ]
        )

    def _load_context(self, state):
        envelope: InboundEnvelope = state["envelope"]
        session = self.personal_data.load_session(self.owner_id, envelope.session_key)
        soul = load_soul(Path(self.config.soul_path))
        try:
            memory_data = _memory_data_block(self.personal_data.memory_context(MemoryContextQuery(self.owner_id, envelope.text)), self.personal_data.pending_memory_actions(self.owner_id, envelope.session_key))
        except (StorageBusyError, StorageSearchError, StorageCapabilityError):
            memory_data = None
        return {"session": session, "soul": soul, "memory_data": memory_data}

    def _run_conversation(self, state):
        envelope: InboundEnvelope = state["envelope"]
        history = state["session"].model_history()
        if state.get("memory_data"):
            history = [*history, {"role": "user", "content": state["memory_data"]}]
        request = ConversationRequest.from_history(
            envelope=envelope,
            soul=state["soul"],
            history=history,
        )
        return {"conversation_result": self.conversation.respond(request)}

    def _compose_delivery(self, state):
        envelope: InboundEnvelope = state["envelope"]
        result = state["conversation_result"]
        bubbles = self._compose_result_bubbles(result.frames, envelope.message_id)
        return {"bubbles": bubbles}

    def _persist_turn(self, state):
        envelope: InboundEnvelope = state["envelope"]
        session = state["session"]
        result = state["conversation_result"]
        session.append(
            StoredMessage(
                role="user",
                content=envelope.text,
                metadata={"connector": envelope.connector, "actor_id": envelope.actor_id, "message_id": envelope.message_id},
            )
        )
        session.append(
            StoredMessage(
                role="assistant",
                content=result.logical_text,
                metadata={
                    "provider": self.provider.name,
                    "generation_id": f"legacy-{envelope.message_id}",
                    "generation_status": "provisional",
                    "turn_status": result.status.value,
                    "conversation": self._compact_plan(result),
                    "frames": self._frame_metadata(result),
                    "segments": self._segment_metadata(result),
                    "usage": self._usage_metadata(result),
                    "delivery_bubbles": [bubble.text for bubble in state["bubbles"]],
                },
            )
        )
        self.personal_data.save_session(self.owner_id, session)

    @staticmethod
    def _compact_plan(result) -> dict[str, object]:
        return {
            "primary_move": result.plan.primary.value,
            "supporting_moves": [move.value for move in result.plan.supporting],
            "move_sequence": [move.value for move in result.plan.sequence],
            "response_goal": result.plan.response_goal,
            "confidence": result.plan.confidence.value,
        }

    @staticmethod
    def _frame_metadata(result) -> list[dict[str, object]]:
        return [
            {"segment_index": frame.segment_index, "frame_index": frame.frame_index, "text": frame.text}
            for frame in result.frames
        ]

    @staticmethod
    def _segment_metadata(result) -> list[dict[str, object]]:
        return [
            {"index": segment.index, "finish": segment.finish.value,
             "frame_count": len(segment.frames), "tool_call_count": len(segment.tool_calls)}
            for segment in result.segments
        ]

    @staticmethod
    def _usage_metadata(result) -> dict[str, int | None]:
        usage = result.usage
        return {"model_segments": usage.model_segments, "tool_rounds": usage.tool_rounds,
                "tool_calls": usage.tool_calls, "visible_segments": usage.visible_segments,
                "contract_repairs": usage.contract_repairs, "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens}
        return {"session": session}

    def _compose_result_bubbles(self, frames, reply_to_message_id: str) -> list[OutboundBubble]:
        return [self._compose_progressive_bubble(FrameReady(index, frame), reply_to_message_id, index > 0) for index, frame in enumerate(frames)]

    def _compose_progressive_bubble(self, event: FrameReady, reply_to_message_id: str, has_prior_delivery: bool) -> OutboundBubble:
        text = sanitize_style(strip_markdown(event.frame.text))
        if not text:
            raise ValueError("utterance cannot be empty after delivery cleanup")
        return OutboundBubble(text=text, reply_to_message_id=None if has_prior_delivery else reply_to_message_id)

    def _persist_completed_burst(
        self,
        session,
        burst: InputBurst,
        event: TurnRunCompleted,
        delivered: list[OutboundBubble],
        is_active: Callable[[], bool],
    ) -> bool:
        if not is_active():
            return False
        result = event.result
        logical_parts = (*burst.visible_assistant_utterances, *(frame.text for frame in result.frames))
        session.append(
            StoredMessage(
                role="assistant",
                content=" ".join(part.strip() for part in logical_parts if part.strip()),
                metadata={
                    "provider": self.provider.name,
                    "generation_id": burst.generation_id,
                    "generation_status": "provisional",
                    "burst_id": burst.burst_id,
                    "revision": burst.revision,
                    "conversation": self._compact_plan(result),
                    "turn_status": result.status.value,
                    "frames": self._frame_metadata(result),
                    "segments": self._segment_metadata(result),
                    "usage": self._usage_metadata(result),
                    "delivery_bubbles": [bubble.text for bubble in delivered],
                },
            )
        )
        return self.personal_data.save_session(self.owner_id, session, generation_id=burst.generation_id, revision=burst.revision)

    def _persist_provisional_frame(self, session, burst, event, delivered, is_active) -> bool:
        if not is_active():
            return False
        parts = (*burst.visible_assistant_utterances, *(bubble.text for bubble in delivered))
        metadata = {
            "provider": self.provider.name, "generation_id": burst.generation_id,
            "generation_status": "provisional", "burst_id": burst.burst_id,
            "revision": burst.revision,
            "delivery_bubbles": [bubble.text for bubble in delivered],
        }
        existing_index = next((index for index, message in enumerate(session.messages) if message.role == "assistant" and message.metadata.get("generation_id") == burst.generation_id), None)
        if existing_index is None:
            session.append(StoredMessage("assistant", " ".join(parts), metadata=metadata))
        else:
            existing = session.messages[existing_index]
            session.messages[existing_index] = StoredMessage(existing.role, " ".join(parts), existing.timestamp, {**existing.metadata, **metadata})
        if not is_active():
            return False
        return self.personal_data.save_session(self.owner_id, session, generation_id=burst.generation_id, revision=burst.revision)

    def _record_memory_diagnostic(self, reason_code: Literal["secret_detected", "invalid_provenance", "stale_governance_or_revision", "storage_failure"]) -> None:
        if len(self.memory_control_diagnostics) < 32:
            self.memory_control_diagnostics.append(MemoryControlDiagnostic(reason_code))


def _safe_memory_control(control: MemoryWriteControl, burst: InputBurst, session, tool_observation_ids: set[str] | None = None) -> bool:
    """Reject credential-like controls without retaining or reporting their content."""
    if _contains_secret((control.kind, control.subject_key, control.topic, control.statement, control.value)):
        return False
    current_ids = {message.envelope.message_id for message in burst.messages}
    session_ids = {str(message.metadata.get("_canonical_message_id")) for message in session.messages if message.metadata.get("_canonical_message_id")}
    observations = tool_observation_ids or set()
    return all(
        (source.source_kind == "current_message" and source.source_id in current_ids)
        or (source.source_kind == "session_message" and source.source_id in current_ids | session_ids)
        or (source.source_kind == "tool_observation" and source.source_id in observations)
        or (source.source_kind in {"assistant_conclusion", "inference"} and source.source_id == burst.generation_id)
        for source in control.sources
    )


def _memory_control_rejection_code(control: MemoryWriteControl, burst: InputBurst, session, tool_observation_ids: set[str] | None = None) -> Literal["secret_detected", "invalid_provenance"]:
    if _contains_secret((control.kind, control.subject_key, control.topic, control.statement, control.value)):
        return "secret_detected"
    return "invalid_provenance"


def _contains_secret(values: object) -> bool:
    import re
    def walk(value: object, name: str = "") -> bool:
        lowered = name.casefold()
        if any(token in lowered for token in ("password", "secret", "token", "api_key", "apikey", "private_key", "credential", "recovery")):
            return True
        if isinstance(value, dict):
            return any(walk(item, str(key)) for key, item in value.items())
        if isinstance(value, (list, tuple)):
            return any(walk(item) for item in value)
        if not isinstance(value, str):
            return False
        return bool(re.search(r"(?:bearer|basic)\s+[A-Za-z0-9._~+/-]{12,}|-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\b(?:sk|pk|rk|ghp|xox[baprs])[-_A-Za-z0-9]{12,}\b", value, re.I))
    return walk(values)


def _memory_data_block(memories, pending_actions=()) -> str | None:
    if not memories and not pending_actions:
        return None
    prefix = "PERSONAL_MEMORY_DATA_UNTRUSTED:\n"
    actions = [{"id": action.id, "targets": list(action.target_statements)} for action in pending_actions]
    while actions and len((prefix + json.dumps({"memories": [], "pending_actions": actions}, ensure_ascii=True, separators=(",", ":"))).encode("utf-8")) > 2500:
        actions.pop()
    records = []
    for memory in memories:
        record = {"id": memory.id, "statement": memory.statement, "value": memory.value,
                  "epistemic_kind": memory.epistemic_kind, "confidence": memory.confidence,
                  "valid_from": memory.valid_from, "valid_until": memory.valid_until,
                  "sources": [source.source_kind for source in memory.sources]}
        candidate = records + [record]
        if len((prefix + json.dumps({"memories": candidate, "pending_actions": actions}, ensure_ascii=True, separators=(",", ":"))).encode("utf-8")) > 2500:
            continue
        records = candidate
    payload = {"memories": records, "pending_actions": actions}
    return prefix + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
