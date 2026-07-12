from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import replace

from ..context import ContextHydrator
from ..models import ResponseContract
from ..providers import ProviderAdapter, ProviderSetupRequired, ProviderStreamCompleted, ProviderTextDelta, ProviderToolCallReady
from ..tool_execution import ToolBatchCancelled, ToolBatchValidationError, ToolExecutor
from ..tools import ToolRegistry
from .framing import SegmentFrameParser
from .models import ConversationRequest, Frame, FrameReady, MovePlan, SegmentFinish, SegmentResult, TurnBudget, TurnRunCompleted, TurnRunEvent, TurnRunResult, TurnRunStarted, TurnRunStatus, TurnUsage
from .parsing import ConversationOutputError
from .prompts import build_segment_messages, build_segment_repair_messages


class ConversationEngine:
    def __init__(
        self,
        provider: ProviderAdapter,
        contract: ResponseContract | None = None,
        budget: TurnBudget | None = None,
        context_hydrator: ContextHydrator | None = None,
        tool_registry: ToolRegistry | None = None,
        tool_executor: ToolExecutor | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self.provider = provider
        self.contract = contract or ResponseContract()
        if tool_executor is not None and not isinstance(tool_executor, ToolExecutor):
            raise ValueError("tool_executor must be a ToolExecutor")
        if tool_registry is not None and tool_executor is not None and tool_registry is not tool_executor.registry:
            raise ValueError("tool_registry and tool_executor must be the same object")
        self.tool_registry = tool_registry or (tool_executor.registry if tool_executor is not None else ToolRegistry())
        self.tool_executor = tool_executor or ToolExecutor(self.tool_registry)
        self.budget = budget or TurnBudget(
            6 if self.tool_registry.schemas() else 1,
            5 if self.tool_registry.schemas() else 0,
            5 if self.tool_registry.schemas() else 0,
            3 if self.tool_registry.schemas() else 1,
            min(self.contract.max_utterances, 3),
            self.contract.max_sentences_per_utterance,
            800,
        )
        self.context_hydrator = context_hydrator or ContextHydrator()
        self.monotonic_clock = monotonic_clock or time.monotonic

    def respond(self, request: ConversationRequest) -> TurnRunResult:
        for event in self.respond_iter(request):
            if isinstance(event, TurnRunCompleted):
                return event.result
        raise RuntimeError("conversation turn did not complete")

    def respond_iter(self, request: ConversationRequest, *, is_active: Callable[[], bool] | None = None) -> Iterator[TurnRunEvent]:
        active = is_active or (lambda: True)
        try:
            yield from self._respond_iter(request, active)
        except ProviderSetupRequired as error:
            if active():
                yield from self._setup_required(error.user_message, active)

    def _respond_iter(self, request: ConversationRequest, is_active: Callable[[], bool]) -> Iterator[TurnRunEvent]:
        if not is_active():
            return
        context = self.context_hydrator.hydrate(request)
        if not is_active():
            return
        started_at = self.monotonic_clock()
        plan: MovePlan | None = None
        segments: list[SegmentResult] = []
        frames: list[Frame] = []
        prior_messages: list[dict[str, object]] = []
        repairs = 0
        input_tokens: int | None = None
        output_tokens: int | None = None

        def expired() -> bool:
            return self.monotonic_clock() - started_at >= self.budget.max_elapsed_seconds

        def completed(status: TurnRunStatus) -> TurnRunCompleted:
            assert plan is not None
            usage = TurnUsage(len(segments), sum(s.finish is SegmentFinish.TOOL_BATCH for s in segments), sum(len(s.tool_calls) for s in segments), sum(bool(s.frames) for s in segments), repairs, input_tokens, output_tokens)
            self.budget.validate_usage(usage)
            return TurnRunCompleted(TurnRunResult(plan, tuple(segments), tuple(frames), usage, status))

        index = 0
        while index < self.budget.max_model_segments:
            if not is_active():
                return
            if expired():
                if segments and frames:
                    if not is_active():
                        return
                    yield completed(TurnRunStatus.COMPLETED_PARTIAL)
                    return
                raise ConversationOutputError("elapsed_budget_exhausted")
            allow_tools = bool(self.tool_registry.schemas()) and index < self.budget.max_model_segments - 1 and sum(bool(s.frames) for s in segments) < self.budget.max_visible_segments - 1 and sum(s.finish is SegmentFinish.TOOL_BATCH for s in segments) < self.budget.max_tool_rounds and sum(len(s.tool_calls) for s in segments) < self.budget.max_tool_calls
            schemas = self.tool_registry.schemas() if allow_tools else ()
            replacement = False
            while True:
                remaining_frames = 3 - len(frames)
                if remaining_frames < 1:
                    if frames:
                        yield completed(TurnRunStatus.COMPLETED_PARTIAL)
                        return
                    raise ConversationOutputError("frame_limit")
                segment_budget = replace(self.budget, max_frames_per_segment=min(self.budget.max_frames_per_segment, remaining_frames))
                parser = SegmentFrameParser(index, first_segment=plan is None, budget=segment_budget)
                if replacement and plan is not None:
                    messages = build_segment_repair_messages(request, context, segment_budget, plan, "replacement_required", prior_messages=prior_messages)
                    schemas = ()
                else:
                    messages = build_segment_messages(request, context, segment_budget, segment_index=index, plan=plan, prior_messages=prior_messages, tools_available=schemas)
                raw: list[str] = []
                native_calls: list[ProviderToolCallReady] = []
                segment_frames: list[Frame] = []
                terminal: ProviderStreamCompleted | None = None
                failure: ConversationOutputError | None = None
                try:
                    stream = self.provider.stream(messages, tools=schemas, actor_id=request.burst.latest.actor_id)
                except ProviderSetupRequired as error:
                    if not segments:
                        raise
                    if not is_active():
                        return
                    if frames:
                        segments.append(SegmentResult(index, (), (), SegmentFinish.FAILED))
                        if not is_active():
                            return
                        yield completed(TurnRunStatus.COMPLETED_PARTIAL)
                        return
                    assert plan is not None
                    if expired():
                        raise ConversationOutputError("elapsed_budget_exhausted")
                    frame = Frame(index, 0, error.user_message)
                    segments.append(SegmentResult(index, (frame,), (), SegmentFinish.COMPLETE))
                    frames.append(frame)
                    if not is_active():
                        return
                    yield FrameReady(len(frames) - 1, frame)
                    if not is_active():
                        return
                    if expired():
                        segments[-1] = SegmentResult(index, (frame,), (), SegmentFinish.PARTIAL)
                        if not is_active():
                            return
                        yield completed(TurnRunStatus.COMPLETED_PARTIAL)
                        return
                    if not is_active():
                        return
                    yield completed(TurnRunStatus.COMPLETED)
                    return
                except Exception:
                    failure = ConversationOutputError("provider_stream_failure")
                    stream = None
                try:
                    for event in stream or ():
                        if not is_active():
                            return
                        if expired():
                            failure = ConversationOutputError("elapsed_budget_exhausted")
                            break
                        try:
                            if terminal is not None:
                                raise ConversationOutputError("event_after_completion")
                            if isinstance(event, ProviderTextDelta):
                                raw.append(event.text)
                                for chunk in event.text.splitlines(keepends=True):
                                    for record in parser.feed(chunk):
                                        if isinstance(record, MovePlan):
                                            if expired():
                                                failure = ConversationOutputError("elapsed_budget_exhausted")
                                                break
                                            if plan is not None and record != plan:
                                                raise ConversationOutputError("conflicting_plan")
                                            plan = record
                                            if not is_active():
                                                return
                                            yield TurnRunStarted(plan)
                                        else:
                                            if len(frames) >= 3:
                                                raise ConversationOutputError("frame_limit")
                                            if (not segment_frames and sum(bool(s.frames) for s in segments) >= self.budget.max_visible_segments):
                                                raise ConversationOutputError("visible_segment_limit")
                                            if expired():
                                                failure = ConversationOutputError("elapsed_budget_exhausted")
                                                break
                                            segment_frames.append(record)
                                            frames.append(record)
                                            if not is_active():
                                                return
                                            yield FrameReady(len(frames) - 1, record)
                            elif isinstance(event, ProviderToolCallReady):
                                native_calls.append(event)
                            elif isinstance(event, ProviderStreamCompleted):
                                terminal = event
                                if event.input_tokens is not None:
                                    input_tokens = (input_tokens or 0) + event.input_tokens
                                if event.output_tokens is not None:
                                    output_tokens = (output_tokens or 0) + event.output_tokens
                            else:
                                raise ConversationOutputError("invalid_provider_event")
                        except ConversationOutputError as error:
                            failure = error
                            break
                except Exception:
                    if failure is None:
                        failure = ConversationOutputError("provider_stream_failure")
                finally:
                    close = getattr(stream, "close", None)
                    if callable(close):
                        close()
                if not is_active():
                    return
                if failure is None and terminal is None:
                    failure = ConversationOutputError("missing_completion")
                if failure is None:
                    try:
                        for record in parser.finish():
                            if not is_active():
                                return
                            if expired():
                                raise ConversationOutputError("elapsed_budget_exhausted")
                            if isinstance(record, MovePlan):
                                if plan is not None and record != plan:
                                    raise ConversationOutputError("conflicting_plan")
                                plan = record
                                if not is_active():
                                    return
                                yield TurnRunStarted(plan)
                            else:
                                if len(frames) >= 3:
                                    raise ConversationOutputError("frame_limit")
                                if (not segment_frames and sum(bool(s.frames) for s in segments) >= self.budget.max_visible_segments):
                                    raise ConversationOutputError("visible_segment_limit")
                                segment_frames.append(record)
                                frames.append(record)
                                if not is_active():
                                    return
                                yield FrameReady(len(frames) - 1, record)
                    except ConversationOutputError as error:
                        failure = error
                tool_finish = terminal is not None and terminal.finish_reason == "tool_calls"
                if failure is None and ((terminal.finish_reason == "stop" and native_calls) or (tool_finish and not native_calls) or (terminal.finish_reason not in {"stop", "tool_calls"})):
                    failure = ConversationOutputError("mismatched_tool_completion")
                if failure is None and tool_finish and not schemas:
                    failure = ConversationOutputError("native_tool_unavailable")
                if failure is None and tool_finish and len(segment_frames) > 1:
                    failure = ConversationOutputError("tool_frame_limit")
                if failure is None and tool_finish:
                    if not is_active():
                        return
                    if expired():
                        failure = ConversationOutputError("elapsed_budget_exhausted")
                    elif len(segment_frames) > 1:
                        failure = ConversationOutputError("tool_frame_limit")
                if failure is None and tool_finish:
                    prior_call_ids = {call.call_id for segment in segments for call in segment.tool_calls}
                    if any(call.call_id in prior_call_ids for call in native_calls):
                        failure = ConversationOutputError("duplicate_tool_call_id")
                    else:
                        try:
                            batch = self.tool_executor.execute_batch(native_calls, self.budget.max_tool_calls - sum(len(s.tool_calls) for s in segments), is_active)
                        except ToolBatchCancelled:
                            return
                        except ToolBatchValidationError as error:
                            failure = ConversationOutputError(error.code)
                        except Exception:
                            failure = ConversationOutputError("tool_executor_failure")
                        else:
                            segments.append(SegmentResult(index, tuple(segment_frames), batch.tool_calls, SegmentFinish.TOOL_BATCH))
                            prior_messages.append(_assistant_continuation("".join(raw) or None, batch.tool_calls))
                            prior_messages.extend(_tool_continuation(observation) for observation in batch.observations)
                            if not is_active():
                                return
                            if expired():
                                if frames:
                                    yield completed(TurnRunStatus.COMPLETED_PARTIAL)
                                    return
                                raise ConversationOutputError("elapsed_budget_exhausted")
                            index += 1
                            break
                if failure is not None:
                    if segment_frames:
                        segments.append(SegmentResult(index, tuple(segment_frames), (), SegmentFinish.PARTIAL))
                        if not is_active():
                            return
                        yield completed(TurnRunStatus.COMPLETED_PARTIAL)
                        return
                    if frames:
                        segments.append(SegmentResult(index, (), (), SegmentFinish.FAILED))
                        if not is_active():
                            return
                        yield completed(TurnRunStatus.COMPLETED_PARTIAL)
                        return
                    if expired():
                        raise ConversationOutputError("elapsed_budget_exhausted")
                    if segments or repairs >= self.budget.max_contract_repairs:
                        raise ConversationOutputError(failure.code)
                    repairs += 1
                    replacement = True
                    continue
                if plan is None or not segment_frames:
                    if segments or repairs >= self.budget.max_contract_repairs:
                        raise ConversationOutputError("missing_frame")
                    repairs += 1
                    replacement = True
                    continue
                segments.append(SegmentResult(index, tuple(segment_frames), (), SegmentFinish.COMPLETE))
                if not is_active() or expired():
                    if not is_active():
                        return
                    segments[-1] = SegmentResult(index, tuple(segment_frames), (), SegmentFinish.PARTIAL)
                    yield completed(TurnRunStatus.COMPLETED_PARTIAL)
                    return
                yield completed(TurnRunStatus.COMPLETED)
                return
        if segments and frames:
            if not is_active():
                return
            yield completed(TurnRunStatus.COMPLETED_PARTIAL)
            return
        raise ConversationOutputError("model_segment_budget_exhausted")

    def _setup_required(self, user_message: str, is_active: Callable[[], bool]) -> Iterator[TurnRunEvent]:
        plan = MovePlan.direct_answer()
        frame = Frame(0, 0, user_message)
        if not is_active():
            return
        yield TurnRunStarted(plan)
        if not is_active():
            return
        yield FrameReady(0, frame)
        usage = TurnUsage(model_segments=1, visible_segments=1)
        if not is_active():
            return
        yield TurnRunCompleted(TurnRunResult(plan, (SegmentResult(0, (frame,), (), SegmentFinish.COMPLETE),), (frame,), usage, TurnRunStatus.COMPLETED))


def _assistant_continuation(content: str | None, calls) -> dict[str, object]:
    return {"role": "assistant", "content": content, "tool_calls": [{"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": json.dumps(dict(call.arguments), ensure_ascii=True, separators=(",", ":"), allow_nan=False)}} for call in calls]}

def _tool_continuation(observation) -> dict[str, object]:
    content: dict[str, object] = {"name": observation.name, "ok": observation.ok, "content": observation.content}
    if observation.error_code is not None:
        content["error_code"] = observation.error_code
    return {"role": "tool", "tool_call_id": observation.call_id, "content": json.dumps(content, ensure_ascii=True, separators=(",", ":"), allow_nan=False)}
