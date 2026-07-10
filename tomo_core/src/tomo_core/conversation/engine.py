from __future__ import annotations

from ..models import ResponseContract
from ..providers import ProviderAdapter, ProviderSetupRequired
from .models import ConversationRequest, ConversationResult, MovePlan
from .parsing import ConversationOutputError, parse_move_plan, parse_utterances
from .prompts import build_move_selection_messages, build_realization_messages, build_repair_messages


class ConversationEngine:
    def __init__(self, provider: ProviderAdapter, contract: ResponseContract | None = None) -> None:
        self.provider = provider
        self.contract = contract or ResponseContract()

    def respond(self, request: ConversationRequest) -> ConversationResult:
        try:
            return self._respond(request)
        except ProviderSetupRequired as error:
            return ConversationResult(plan=MovePlan.direct_answer(), utterances=(error.user_message,))

    def _respond(self, request: ConversationRequest) -> ConversationResult:
        actor_id = request.envelope.actor_id
        selection_messages = build_move_selection_messages(request.soul, request.history, request.envelope)
        plan = parse_move_plan(self.provider.complete(selection_messages, actor_id=actor_id))
        realization_messages = build_realization_messages(request.soul, request.history, request.envelope, plan, self.contract)
        raw_realization = self.provider.complete(realization_messages, actor_id=actor_id)
        try:
            utterances = parse_utterances(raw_realization, self.contract)
        except ConversationOutputError as error:
            repair_messages = build_repair_messages(realization_messages, raw_realization, error.code)
            utterances = parse_utterances(self.provider.complete(repair_messages, actor_id=actor_id), self.contract)
        return ConversationResult(plan=plan, utterances=utterances)
