from __future__ import annotations

from pathlib import Path

from .conversation import ConversationEngine, ConversationRequest
from .delivery import DeliveryPlanner
from .graph import build_langgraph_or_linear
from .models import InboundEnvelope, RuntimeConfig
from .providers import ProviderAdapter
from .sessions import JsonSessionStore, StoredMessage
from .soul import load_soul
from .telegram import TelegramDeliverySink


class PersonalAgentRuntime:
    def __init__(
        self,
        provider: ProviderAdapter,
        telegram: TelegramDeliverySink,
        config: RuntimeConfig | None = None,
    ) -> None:
        self.config = config or RuntimeConfig()
        self.provider = provider
        contract = self.config.response_contract
        self.conversation = ConversationEngine(provider, contract=contract)
        self.telegram = telegram
        self.sessions = JsonSessionStore(self.config.data_dir)
        self.delivery = DeliveryPlanner(contract=contract)
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
        bubbles = self.delivery.compose_utterances(result.utterances, reply_to_message_id=envelope.message_id)
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
                    "conversation": {
                        "primary_move": result.plan.primary.value,
                        "supporting_moves": [move.value for move in result.plan.supporting],
                        "response_goal": result.plan.response_goal,
                        "confidence": result.plan.confidence.value,
                    },
                    "delivery_bubbles": [bubble.text for bubble in state["bubbles"]],
                },
            )
        )
        self.sessions.save(session)
        return {"session": session}
