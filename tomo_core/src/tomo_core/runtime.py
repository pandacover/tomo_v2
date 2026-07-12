from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, TypeAlias

from .conversation import ConversationEngine, ConversationRequest, FrameReady, TurnRunCompleted, TurnRunStarted
from .delivery import sanitize_style, strip_markdown
from .graph import build_langgraph_or_linear
from .models import InboundEnvelope, InputBurst, OutboundBubble, RuntimeConfig
from .providers import ProviderAdapter
from .sessions import JsonSessionStore, StoredMessage
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


RuntimeEvent: TypeAlias = RuntimeFrameReady | RuntimeCompleted


class PersonalAgentRuntime:
    def __init__(
        self,
        provider: ProviderAdapter,
        telegram: TelegramDeliverySink,
        config: RuntimeConfig | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.config = config or RuntimeConfig()
        self.provider = provider
        self.tool_registry = tool_registry if tool_registry is not None else ToolRegistry()
        if self.tool_registry.schemas() and not provider.supports_tool_calls:
            raise ValueError("provider does not support tool calls")
        contract = self.config.response_contract
        budget = self.config.tool_turn_budget if self.tool_registry.schemas() else self.config.ordinary_turn_budget
        self.conversation = ConversationEngine(provider, contract=contract, budget=budget, tool_registry=self.tool_registry)
        self.telegram = telegram
        self.sessions = JsonSessionStore(self.config.data_dir)
        self.graph = self._build_graph()

    def handle_telegram_text(self, envelope: InboundEnvelope) -> list[dict[str, str | None]]:
        if envelope.connector != "telegram":
            raise ValueError("milestone 1 only supports telegram envelopes")
        self.telegram.start_typing(envelope.actor_id)
        state = self.graph({"envelope": envelope})
        bubbles = state["bubbles"]
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
        session = self.sessions.load(burst.latest.session_key)
        session.accept_generations(burst.accepted_generation_ids)
        for message in burst.messages:
            session.append_inbound_once(message, burst.burst_id)
        self.sessions.save_atomic(session)
        if not is_active():
            return

        request = ConversationRequest(
            burst=burst,
            soul=load_soul(Path(self.config.soul_path)),
            history=tuple(session.model_history_for_burst(burst.burst_id)),
        )
        conversation_events = self.conversation.respond_iter(request, is_active=is_active)
        delivered: list[OutboundBubble] = []
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
                continue
            if isinstance(event, FrameReady):
                if not is_active():
                    return
                bubble = self._compose_progressive_bubble(event, burst.latest.message_id, bool(delivered))
                if not is_active():
                    return
                delivered.append(bubble)
                if not is_active():
                    return
                yield RuntimeFrameReady(event=event, bubble=bubble)
                continue
            if isinstance(event, TurnRunCompleted):
                if not is_active():
                    return
                self._persist_completed_burst(session, burst, event, delivered, is_active)
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
        session = self.sessions.load(envelope.session_key)
        soul = load_soul(Path(self.config.soul_path))
        return {"session": session, "soul": soul}

    def _run_conversation(self, state):
        envelope: InboundEnvelope = state["envelope"]
        request = ConversationRequest.from_history(
            envelope=envelope,
            soul=state["soul"],
            history=state["session"].model_history(),
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
        self.sessions.save(session)
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
    ) -> None:
        if not is_active():
            return
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
        self.sessions.save_atomic(session)

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
            {
                "index": segment.index,
                "finish": segment.finish.value,
                "frame_count": len(segment.frames),
                "tool_call_count": len(segment.tool_calls),
            }
            for segment in result.segments
        ]

    @staticmethod
    def _usage_metadata(result) -> dict[str, int | None]:
        usage = result.usage
        return {
            "model_segments": usage.model_segments,
            "tool_rounds": usage.tool_rounds,
            "tool_calls": usage.tool_calls,
            "visible_segments": usage.visible_segments,
            "contract_repairs": usage.contract_repairs,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        }
