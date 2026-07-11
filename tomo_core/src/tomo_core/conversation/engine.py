from __future__ import annotations

from typing import Callable, Iterator

from ..models import ResponseContract
from ..providers import ProviderAdapter, ProviderSetupRequired
from .models import ConversationCompleted, ConversationEvent, ConversationRequest, ConversationResult, ConversationStarted, MovePlan, UtteranceReady
from .parsing import ConversationOutputError, parse_move_plan, parse_utterance, parse_utterances
from .prompts import build_move_selection_messages, build_realization_messages, build_repair_messages, build_step_realization_messages


class ConversationEngine:
    def __init__(self, provider: ProviderAdapter, contract: ResponseContract | None = None) -> None:
        self.provider = provider
        self.contract = contract or ResponseContract()

    def respond(self, request: ConversationRequest) -> ConversationResult:
        try:
            return self._respond(request)
        except ProviderSetupRequired as error:
            return ConversationResult(plan=MovePlan.direct_answer(), utterances=(error.user_message,))

    def respond_iter(self, request: ConversationRequest, *, is_active: Callable[[], bool] | None = None) -> Iterator[ConversationEvent]:
        try:
            yield from self._respond_iter(request, is_active=is_active or (lambda: True))
        except ProviderSetupRequired as error:
            plan = MovePlan.direct_answer()
            yield ConversationStarted(plan)
            yield UtteranceReady(0, plan.primary, error.user_message)
            yield ConversationCompleted(ConversationResult(plan=plan, utterances=(error.user_message,)))

    def _respond(self, request: ConversationRequest) -> ConversationResult:
        actor_id = request.burst.latest.actor_id
        selection_messages = build_move_selection_messages(request.soul, request.history, request.burst)
        plan = parse_move_plan(self.provider.complete(selection_messages, actor_id=actor_id))
        realization_messages = build_realization_messages(request.soul, request.history, request.burst, plan, self.contract)
        raw_realization = self.provider.complete(realization_messages, actor_id=actor_id)
        try:
            utterances = parse_utterances(raw_realization, self.contract)
        except ConversationOutputError as error:
            repair_messages = build_repair_messages(realization_messages, raw_realization, error.code)
            utterances = parse_utterances(self.provider.complete(repair_messages, actor_id=actor_id), self.contract)
        return ConversationResult(plan=plan, utterances=utterances)

    def _respond_iter(self, request: ConversationRequest, *, is_active: Callable[[], bool]) -> Iterator[ConversationEvent]:
        actor_id = request.burst.latest.actor_id
        selection_messages = build_move_selection_messages(request.soul, request.history, request.burst)
        if not is_active():
            return
        plan = parse_move_plan(self.provider.complete(selection_messages, actor_id=actor_id))
        if not is_active():
            return
        yield ConversationStarted(plan)
        emitted: list[str] = []
        for sequence, move in enumerate(plan.sequence):
            messages = build_step_realization_messages(request, plan, move, tuple(emitted), self.contract)
            if not is_active():
                return
            raw = self.provider.complete(messages, actor_id=actor_id)
            if not is_active():
                return
            try:
                text = parse_utterance(raw, self.contract)
            except ConversationOutputError as error:
                repair_messages = build_repair_messages(messages, raw, error.code)
                if not is_active():
                    return
                text = parse_utterance(self.provider.complete(repair_messages, actor_id=actor_id), self.contract)
                if not is_active():
                    return
            emitted.append(text)
            yield UtteranceReady(sequence, move, text)
        yield ConversationCompleted(ConversationResult(plan=plan, utterances=tuple(emitted)))
