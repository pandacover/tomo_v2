from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator, Literal, TypeAlias

from .conversation import ConversationEngine, ConversationRequest, FrameReady, MemoryControlReady, ReactionIntent, ReactionWindowReady, TurnRunCompleted, TurnRunStarted
from .delivery import sanitize_style, strip_markdown
from .graph import build_langgraph_or_linear
from .models import AutomationTurn, InboundEnvelope, InputBurst, OutboundBubble, PeerTurn, RuntimeConfig
from .providers import ProviderAdapter
from .reaction_service import ReactionDeliveryKey, ReactionService
from .memory_governance import MemoryGovernanceService
from .memory import ProactiveMemoryExtractor
from .personal_data import (MemoryContextQuery, MemoryWriteControl,
                            PersonalDataRepository, StorageBusyError,
                            StorageCapabilityError, StorageSearchError)
from .personal_search_tools import peer_personal_search_registry, personal_search_registry
from .sqlite_personal_data import SqlitePersonalDataRepository
from .sessions import ConversationSession, StoredMessage
from .soul import load_soul
from .telegram import TelegramDeliverySink
from .tool_execution import ToolExecutor
from .tools import ToolRegistry
from .vision import VisionInterpreter, VisionObservation
from . import latency_trace


_MAX_VISION_IMAGES_PER_TURN = 8


def _is_low_stakes_casual_input(text: str) -> bool:
    """Return True if text is a low-stakes casual burst that should bypass pre-turn memory hydration."""
    cleaned = text.strip().lower()
    if not cleaned:
        return True
    words = cleaned.split()
    if len(words) > 8:
        return False
    info_keywords = {
        "why", "how", "what", "where", "when", "who", "which",
        "can", "could", "would", "is", "are", "will", "fix", "code", "bug",
        "error", "server", "crash", "deploy", "build", "remember",
        "saved", "delete", "forget", "help", "explain", "meaning",
        "vocabulary", "normal", "speak", "talk", "tone",
        "friend", "passed", "died", "away", "yesterday", "hurt", "sad", "sick",
        "migration", "work", "definitely"
    }
    return not any(word.strip("?,.!\"'") in info_keywords for word in words)



def _peer_frame_grounded(
    text: str, scope: str, candidates: list[dict[str, object]]
) -> bool:
    if scope in {"none", "commitment_proposal"}:
        return True
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return False
    if not isinstance(value, dict):
        return False
    if scope == "availability" and value == {
        "status": "unknown",
        "window": "unknown",
    }:
        return True
    return value in candidates


def _selected_image_attachments(burst: InputBurst):
    seen: set[tuple[object, ...]] = set()
    selected = 0
    for message in burst.messages:
        for attachment_index, attachment in enumerate(message.envelope.attachments):
            if attachment.kind != "image":
                continue
            if attachment.file_id:
                identity: tuple[object, ...] = ("file_id", attachment.file_id)
            elif attachment.url:
                identity = ("url", attachment.url)
            elif attachment.path:
                identity = ("path", attachment.path)
            else:
                identity = ("message", message.envelope.message_id, attachment_index)
            if identity in seen:
                continue
            seen.add(identity)
            yield message, attachment_index, attachment
            selected += 1
            if selected == _MAX_VISION_IMAGES_PER_TURN:
                return


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
        vision_interpreter: VisionInterpreter | None = None,
        memory_extractor: ProactiveMemoryExtractor | None = None,
    ) -> None:
        self.config = config or RuntimeConfig()
        self.provider = provider
        self.vision_interpreter = vision_interpreter
        if memory_extractor is not None:
            self.memory_extractor: ProactiveMemoryExtractor | None = memory_extractor
        elif os.getenv("TOMO_MEMORY_EXTRACTOR_ENABLED") == "1" or os.getenv("TOMO_MEMORY_EXTRACTOR_MODEL"):
            self.memory_extractor = ProactiveMemoryExtractor(provider=provider, min_confidence=0.5)
        else:
            self.memory_extractor = None
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
        yield from self._handle_turn_iter(burst, is_active=is_active)

    def handle_automation_turn_iter(
        self,
        turn: AutomationTurn,
        *,
        is_active: Callable[[], bool] | None = None,
    ) -> Iterator[RuntimeEvent]:
        if not isinstance(turn, AutomationTurn):
            raise TypeError("turn must be an AutomationTurn")
        original_registry = self.conversation.tool_registry
        original_executor = self.conversation.tool_executor
        unattended = self.tool_registry.unattended()
        self.conversation.tool_registry = unattended
        self.conversation.tool_executor = ToolExecutor(unattended)
        try:
            yield from self._handle_turn_iter(turn, is_active=is_active)
        finally:
            self.conversation.tool_registry = original_registry
            self.conversation.tool_executor = original_executor

    def handle_peer_turn_iter(self, turn: PeerTurn, *, is_active: Callable[[], bool] | None = None) -> Iterator[RuntimeEvent]:
        if not isinstance(turn, PeerTurn):
            raise TypeError("turn must be a PeerTurn")
        original_registry, original_executor = self.conversation.tool_registry, self.conversation.tool_executor
        peer_candidates: list[dict[str, object]] = []
        peer_safe = peer_personal_search_registry(
            self.personal_data,
            self.owner_id,
            turn.disclosure_scope,
            observe=lambda values: peer_candidates.extend(values),
        )
        self.conversation.tool_registry, self.conversation.tool_executor = (
            peer_safe,
            ToolExecutor(peer_safe),
        )
        try:
            yield from self._handle_turn_iter(
                turn, is_active=is_active, peer_candidates=peer_candidates
            )
        finally:
            self.conversation.tool_registry, self.conversation.tool_executor = original_registry, original_executor

    def _handle_turn_iter(
        self,
        burst: InputBurst | AutomationTurn | PeerTurn,
        *,
        is_active: Callable[[], bool] | None = None,
        peer_candidates: list[dict[str, object]] | None = None,
    ) -> Iterator[RuntimeEvent]:
        automation, peer = isinstance(burst, AutomationTurn), isinstance(burst, PeerTurn)
        if not automation and not peer and burst.latest.connector != "telegram":
            raise ValueError("runtime only supports telegram bursts")
        is_active = is_active or (lambda: True)
        if peer and not is_active():
            return
        actor_id = burst.actor_id if automation else "peer" if peer else burst.latest.actor_id
        session_key = burst.session_key if (automation or peer) else burst.latest.session_key
        if not peer:
            self.telegram.start_typing(actor_id)
        session_load_started_at = time.monotonic()
        # Peer input and model output are ephemeral; PeerStore is their only durable home.
        session = ConversationSession(session_key) if peer else self.personal_data.load_session(self.owner_id, session_key)
        if is_active():
            latency_trace.emit_sandbox("sandbox_session_load", elapsed_ms=max(0, int((time.monotonic() - session_load_started_at) * 1000)))
        if automation:
            session.append_automation_once(burst)
        elif peer:
            session.append_peer_once(burst)
        else:
            session.accept_generations(burst.accepted_generation_ids)
            for message in burst.messages:
                session.append_inbound_once(message, burst.burst_id)
        checkpoint_started_at = time.monotonic()
        saved = True if peer else self.personal_data.save_session(self.owner_id, session, generation_id=burst.generation_id, revision=burst.revision, is_active=is_active)
        if is_active():
            latency_trace.emit_sandbox("sandbox_checkpoint_inbound", outcome="ok" if saved else "error", elapsed_ms=max(0, int((time.monotonic() - checkpoint_started_at) * 1000)))
        if not saved:
            if not is_active():
                return
            current_revision = self.personal_data.current_session_revision(self.owner_id, session_key)
            if current_revision is None:
                raise RuntimeError("stale_session_revision_unavailable")
            raise StaleSessionRevisionError(current_revision)
        if not is_active():
            return

        observations: tuple[VisionObservation, ...] = ()
        if not automation and not peer and self.vision_interpreter is not None:
            observed: list[VisionObservation] = []
            for message, attachment_index, attachment in _selected_image_attachments(burst):
                if not is_active():
                    return
                observed.append(self.vision_interpreter.observe(attachment, message.envelope.text, message_id=message.envelope.message_id, attachment_index=attachment_index, actor_id=message.envelope.actor_id))
            if not is_active():
                return
            observations = tuple(observed)
            if observations:
                session.record_vision_observations(burst.burst_id, observations)
                if not is_active():
                    return
                if not self.personal_data.save_session(self.owner_id, session, generation_id=burst.generation_id, revision=burst.revision, is_active=is_active):
                    if not is_active():
                        return
                    current_revision = self.personal_data.current_session_revision(self.owner_id, session_key)
                    if current_revision is None:
                        raise RuntimeError("stale_session_revision_unavailable")
                    raise StaleSessionRevisionError(current_revision)
                if not is_active():
                    return

        memory_started_at = time.monotonic()
        try:
            query_text = burst.intent if automation else burst.message if peer else "\n".join(m.envelope.text for m in burst.messages)
            memories = () if peer else self.personal_data.memory_context(MemoryContextQuery(self.owner_id, query_text))
            pending_actions = () if peer else self.personal_data.pending_memory_actions(self.owner_id, session_key)
        except (StorageBusyError, StorageSearchError, StorageCapabilityError):
            memories = ()
            pending_actions = ()
        if is_active():
            latency_trace.emit_sandbox("sandbox_memory_hydration", elapsed_ms=max(0, int((time.monotonic() - memory_started_at) * 1000)))
        memory_block = _memory_data_block(memories, pending_actions)
        prompt_started_at = time.monotonic()
        history = list(session.model_history_for_burst(burst.run_id if automation else burst.request_id if peer else burst.burst_id))
        if memory_block:
            history.append({"role": "user", "content": memory_block})
        prompt_burst = (
            replace(burst, visible_assistant_utterances=())
            if isinstance(burst, InputBurst) and _burst_has_visible_peer_exchange(session, burst)
            else burst
        )
        request = ConversationRequest(
            burst=prompt_burst,
            soul=load_soul(Path(self.config.soul_path)),
            history=tuple(history),
            vision_observations=observations,
        )
        governance_revision = 0 if peer else self.personal_data.memory_settings(self.owner_id).governance_revision
        if is_active():
            latency_trace.emit_sandbox("sandbox_prompt_prepare", elapsed_ms=max(0, int((time.monotonic() - prompt_started_at) * 1000)))
        conversation_events = self.conversation.respond_iter(request, is_active=is_active)
        delivered: list[OutboundBubble] = []
        reaction_emoji: str | None = None
        chat_id = burst.chat_id if automation else "" if peer else str(burst.latest.native_metadata.get("chat_id") or burst.latest.actor_id)
        reaction_key = None if automation or peer else ReactionDeliveryKey(self.owner_id, chat_id, burst.generation_id, burst.revision, burst.latest.message_id)

        def emit_reaction() -> RuntimeReactionReady | None:
            if automation or peer or reaction_emoji is None:
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
                if automation or peer:
                    self._record_memory_diagnostic("invalid_provenance")
                elif isinstance(event.control, MemoryWriteControl) and _safe_memory_control(event.control, burst, session, set(event.tool_observation_ids)):
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
                            expected_generation_id=burst.generation_id,
                            expected_revision=burst.revision,
                            is_active=is_active,
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
                if peer and not _peer_frame_grounded(
                    event.frame.text, burst.disclosure_scope, peer_candidates or []
                ):
                    return
                ready = emit_reaction()
                if ready is not None:
                    yield ready
                if not is_active():
                    return
                bubble = self._compose_progressive_bubble(event, None if automation or peer else burst.latest.message_id, bool(delivered))
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
            # Relevance gating: skip automatic memory context block for low-stakes casual inputs
            if _is_low_stakes_casual_input(envelope.text):
                memory_data = None
            else:
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
        current_revision = self.personal_data.current_session_revision(self.owner_id, session.session_key)
        self.personal_data.save_session(
            self.owner_id,
            session,
            generation_id=f"legacy-{envelope.message_id}",
            revision=(current_revision or 0) + 1,
        )

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

    def _compose_progressive_bubble(self, event: FrameReady, reply_to_message_id: str | None, has_prior_delivery: bool) -> OutboundBubble:
        text = sanitize_style(strip_markdown(event.frame.text))
        if not text:
            raise ValueError("utterance cannot be empty after delivery cleanup")
        return OutboundBubble(text=text, reply_to_message_id=None if has_prior_delivery else reply_to_message_id)

    def _persist_completed_burst(
        self,
        session,
        burst: InputBurst | AutomationTurn | PeerTurn,
        event: TurnRunCompleted,
        delivered: list[OutboundBubble],
        is_active: Callable[[], bool],
    ) -> bool:
        if not is_active():
            return False
        if isinstance(burst, PeerTurn):
            return True
        result = event.result
        peer_exchange = _result_used_peer_exchange(result) or _burst_has_visible_peer_exchange(session, burst)
        logical_parts = (*getattr(burst, "visible_assistant_utterances", ()), *(frame.text for frame in result.frames))
        session.append(
            StoredMessage(
                role="assistant",
                content=" ".join(part.strip() for part in logical_parts if part.strip()),
                metadata={
                    "provider": self.provider.name,
                    "generation_id": burst.generation_id,
                    "generation_status": "provisional",
                    "burst_id": burst.run_id if isinstance(burst, AutomationTurn) else burst.request_id if isinstance(burst, PeerTurn) else burst.burst_id,
                    "revision": burst.revision,
                    "conversation": self._compact_plan(result),
                    "turn_status": result.status.value,
                    "frames": self._frame_metadata(result),
                    "segments": self._segment_metadata(result),
                    "usage": self._usage_metadata(result),
                    "delivery_bubbles": [bubble.text for bubble in delivered],
                    **({"peer_exchange": True} if peer_exchange else {}),
                },
            )
        )
        if self.memory_extractor and not isinstance(burst, (AutomationTurn, PeerTurn)) and not peer_exchange:
            try:
                settings = self.personal_data.memory_settings(self.owner_id)
                if settings.capture_enabled:
                    user_text = "\n".join(msg.envelope.text for msg in burst.messages) if hasattr(burst, "messages") else ""
                    assistant_text = " ".join(part.strip() for part in logical_parts if part.strip())
                    from .sqlite_personal_data import utc_now_iso
                    extracted = self.memory_extractor.extract_memories(
                        user_text, assistant_text, burst.generation_id, utc_now_iso()
                    )
                    for control in extracted:
                        if _safe_memory_control(control, burst, session):
                            try:
                                self.personal_data.stage_memory_controls(
                                    self.owner_id,
                                    burst.latest.session_key,
                                    burst.generation_id,
                                    0,
                                    settings.governance_revision,
                                    (control,),
                                    revision=burst.revision,
                                )
                            except Exception:
                                pass
            except Exception:
                pass
        checkpoint_started_at = time.monotonic()
        saved = self.personal_data.save_session(self.owner_id, session, generation_id=burst.generation_id, revision=burst.revision, is_active=is_active)
        if is_active():
            latency_trace.emit_sandbox("sandbox_checkpoint_complete", outcome="ok" if saved else "error", elapsed_ms=max(0, int((time.monotonic() - checkpoint_started_at) * 1000)))
        return saved

    def _persist_provisional_frame(self, session, burst: InputBurst | AutomationTurn | PeerTurn, event, delivered, is_active) -> bool:
        if not is_active():
            return False
        if isinstance(burst, PeerTurn):
            return True
        peer_exchange = event.frame.source == "peer_exchange" or _burst_has_visible_peer_exchange(session, burst)
        parts = (*getattr(burst, "visible_assistant_utterances", ()), *(bubble.text for bubble in delivered))
        metadata = {
            "provider": self.provider.name, "generation_id": burst.generation_id,
            "generation_status": "provisional", "burst_id": burst.run_id if isinstance(burst, AutomationTurn) else burst.request_id if isinstance(burst, PeerTurn) else burst.burst_id,
            "revision": burst.revision,
            "delivery_bubbles": [bubble.text for bubble in delivered],
            **({"peer_exchange": True} if peer_exchange else {}),
        }
        existing_index = next((index for index, message in enumerate(session.messages) if message.role == "assistant" and message.metadata.get("generation_id") == burst.generation_id), None)
        if existing_index is None:
            session.append(StoredMessage("assistant", " ".join(parts), metadata=metadata))
        else:
            existing = session.messages[existing_index]
            session.messages[existing_index] = StoredMessage(existing.role, " ".join(parts), existing.timestamp, {**existing.metadata, **metadata})
        if not is_active():
            return False
        checkpoint_started_at = time.monotonic()
        saved = self.personal_data.save_session(self.owner_id, session, generation_id=burst.generation_id, revision=burst.revision, is_active=is_active)
        if is_active():
            latency_trace.emit_sandbox("sandbox_checkpoint_frame", outcome="ok" if saved else "error", elapsed_ms=max(0, int((time.monotonic() - checkpoint_started_at) * 1000)))
        return saved

    def _record_memory_diagnostic(self, reason_code: Literal["secret_detected", "invalid_provenance", "stale_governance_or_revision", "storage_failure"]) -> None:
        if len(self.memory_control_diagnostics) < 32:
            self.memory_control_diagnostics.append(MemoryControlDiagnostic(reason_code))


def _safe_memory_control(control: MemoryWriteControl, burst: InputBurst, session, tool_observation_ids: set[str] | None = None) -> bool:
    """Reject credential-like controls without retaining or reporting their content."""
    if _contains_secret((control.kind, control.subject_key, control.topic, control.statement, control.value)):
        return False
    current_ids = {message.envelope.message_id for message in burst.messages}
    session_ids = {
        str(message.metadata.get("_canonical_message_id"))
        for message in session.messages
        if message.metadata.get("_canonical_message_id")
        and not message.metadata.get("peer_exchange")
    }
    observations = tool_observation_ids or set()
    return all(
        (source.source_kind == "current_message" and source.source_id in current_ids)
        or (source.source_kind == "session_message" and source.source_id in current_ids | session_ids)
        or (source.source_kind == "tool_observation" and source.source_id in observations)
        or (source.source_kind in {"assistant_conclusion", "inference"} and source.source_id == burst.generation_id)
        for source in control.sources
    )


def _result_used_peer_exchange(result) -> bool:
    return any(
        call.name in {"peer_list", "peer_ask", "peer_resume"}
        for segment in result.segments
        for call in segment.tool_calls
    )


def _burst_has_visible_peer_exchange(session, burst) -> bool:
    visible = set(getattr(burst, "visible_assistant_utterances", ()))
    if not visible:
        return False
    return any(
        message.metadata.get("peer_exchange")
        and any(bubble in visible for bubble in message.metadata.get("delivery_bubbles", ()))
        for message in session.messages
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
