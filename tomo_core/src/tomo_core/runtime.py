from __future__ import annotations

from pathlib import Path

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
        self.telegram = telegram
        self.sessions = JsonSessionStore(self.config.data_dir)
        self.delivery = DeliveryPlanner(
            min_bubbles=self.config.min_bubbles,
            max_bubbles=self.config.max_bubbles,
            max_sentences_per_bubble=self.config.max_sentences_per_bubble,
        )
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
                ("draft_answer", self._draft_answer),
                ("compose_delivery", self._compose_delivery),
                ("persist_turn", self._persist_turn),
            ]
        )

    def _load_context(self, state):
        envelope: InboundEnvelope = state["envelope"]
        session = self.sessions.load(envelope.session_key)
        soul = load_soul(Path(self.config.soul_path))
        messages = [
            {
                "role": "system",
                "content": (
                    f"{soul}\n\n"
                    "runtime rules: reply in plain text. do not use markdown. "
                    "do not mention invisible tool execution. stay conversational."
                ),
            },
            *session.model_history(),
            {"role": "user", "content": envelope.text},
        ]
        return {"session": session, "soul": soul, "messages": messages}

    def _draft_answer(self, state):
        envelope: InboundEnvelope = state["envelope"]
        answer = self.provider.complete(state["messages"], actor_id=envelope.actor_id)
        return {"answer": answer}

    def _compose_delivery(self, state):
        envelope: InboundEnvelope = state["envelope"]
        bubbles = self.delivery.compose(state["answer"], reply_to_message_id=envelope.message_id)
        return {"bubbles": bubbles}

    def _persist_turn(self, state):
        envelope: InboundEnvelope = state["envelope"]
        session = state["session"]
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
                content=state["answer"],
                metadata={
                    "provider": self.provider.name,
                    "delivery_bubbles": [bubble.text for bubble in state["bubbles"]],
                },
            )
        )
        self.sessions.save(session)
        return {"session": session}
