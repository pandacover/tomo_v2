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
from .. import latency_trace
from .contract import PlanSource, synthesized_tool_plan
from .framing import SegmentFrameParser
from .models import ConversationRequest, Frame, FrameReady, MemoryControlReady, MovePlan, ReactionWindowReady, SegmentFinish, SegmentResult, TurnBudget, TurnRunCompleted, TurnRunEvent, TurnRunResult, TurnRunStarted, TurnRunStatus, TurnUsage
from .parsing import ConversationOutputError
from .prompts import build_first_segment_repair_messages, build_segment_messages, build_segment_repair_messages


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
        hydration_started_at = self.monotonic_clock()
        context = self.context_hydrator.hydrate(request)
        if is_active():
            # Hydration ends before prompt construction; it excludes provider work.
            latency_trace.emit_sandbox("sandbox_context_hydration", elapsed_ms=max(0, int((self.monotonic_clock() - hydration_started_at) * 1000)))
        if not is_active():
            return
        started_at = self.monotonic_clock()
        plan: MovePlan | None = None
        segments: list[SegmentResult] = []
        frames: list[Frame] = []
        prior_messages: list[dict[str, object]] = []
        tool_observation_ids: set[str] = set()
        repairs = 0
        provider_attempts = 0
        input_tokens: int | None = None
        output_tokens: int | None = None
        reaction_window_emitted = False

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
            repair_code: str | None = None
            while True:
                remaining_frames = 3 - len(frames)
                if remaining_frames < 1:
                    if frames:
                        yield completed(TurnRunStatus.COMPLETED_PARTIAL)
                        return
                    raise ConversationOutputError("frame_limit")
                segment_budget = replace(self.budget, max_frames_per_segment=min(self.budget.max_frames_per_segment, remaining_frames))
                parser = SegmentFrameParser(index, first_segment=plan is None, budget=segment_budget)
                if replacement and plan is None:
                    messages = build_first_segment_repair_messages(
                        request,
                        context,
                        segment_budget,
                        repair_code or "replacement_required",
                        prior_messages=prior_messages,
                    )
                    schemas = ()
                elif replacement and plan is not None:
                    messages = build_segment_repair_messages(request, context, segment_budget, plan, repair_code or "replacement_required", prior_messages=prior_messages)
                    schemas = ()
                else:
                    messages = build_segment_messages(request, context, segment_budget, segment_index=index, plan=plan, prior_messages=prior_messages, tools_available=schemas)
                raw: list[str] = []
                native_calls: list[ProviderToolCallReady] = []
                segment_frames: list[Frame] = []
                segment_memory_controls = []
                terminal: ProviderStreamCompleted | None = None
                failure: ConversationOutputError | None = None
                attempt_started_at = self.monotonic_clock()
                provider_attempt_emitted = False
                provider_attempts += 1
                suspended_seconds = 0.0
                emitted_stages: set[str] = set()
                output_chars_through_first_frame = 0
                first_frame_chars: int | None = None

                def emit_provider_attempt(outcome: str) -> None:
                    nonlocal provider_attempt_emitted
                    if provider_attempt_emitted:
                        return
                    wall_ms = max(0, int((self.monotonic_clock() - attempt_started_at) * 1000))
                    suspended_ms = min(wall_ms, max(0, int(suspended_seconds * 1000)))
                    counts = provider_counts()
                    counts.update(suspended_ms=suspended_ms, active_ms=max(0, wall_ms - suspended_ms))
                    latency_trace.emit_sandbox("sandbox_provider_attempt", outcome=outcome, elapsed_ms=wall_ms, **counts)
                    provider_attempt_emitted = True

                def provider_counts(*, include_completion: bool = True) -> dict[str, int]:
                    counts = {"attempt": provider_attempts, "segment": index, "repair": repairs}
                    if include_completion:
                        if first_frame_chars is not None:
                            counts["output_chars_through_first_frame"] = output_chars_through_first_frame
                            counts["first_frame_chars"] = first_frame_chars
                        if terminal is not None:
                            if terminal.input_tokens is not None:
                                counts["input_tokens"] = terminal.input_tokens
                            if terminal.output_tokens is not None:
                                counts["output_tokens"] = terminal.output_tokens
                            if terminal.reasoning_tokens is not None:
                                counts["reasoning_tokens"] = terminal.reasoning_tokens
                    return counts

                def emit_provider_stage(phase: str, *, include_completion: bool = False, **stage_counts: int) -> None:
                    if phase in emitted_stages:
                        return
                    counts = provider_counts(include_completion=include_completion)
                    counts.update(stage_counts)
                    latency_trace.emit_sandbox(
                        phase,
                        elapsed_ms=max(0, int((self.monotonic_clock() - attempt_started_at) * 1000)),
                        **counts,
                    )
                    emitted_stages.add(phase)

                def emit_plan_validated(source: PlanSource) -> None:
                    emit_provider_stage(
                        "sandbox_provider_move_plan_validated",
                        **{f"plan_{source.value}": 1},
                    )

                def yield_provider_event(event: TurnRunEvent) -> Iterator[TurnRunEvent]:
                    nonlocal suspended_seconds
                    suspended_at = self.monotonic_clock()
                    try:
                        yield event
                    finally:
                        suspended_seconds += max(0.0, self.monotonic_clock() - suspended_at)

                def handle_record(record: object) -> Iterator[TurnRunEvent]:
                    nonlocal first_frame_chars, failure, plan, reaction_window_emitted
                    if isinstance(record, MovePlan):
                        if expired():
                            failure = ConversationOutputError("elapsed_budget_exhausted")
                            return
                        if plan is not None and record != plan:
                            raise ConversationOutputError("conflicting_plan")
                        plan = record
                        assert parser.plan_source is not None
                        emit_plan_validated(parser.plan_source)
                        if not is_active():
                            return
                        yield from yield_provider_event(TurnRunStarted(plan))
                    elif not isinstance(record, Frame):
                        if sum(len(segment.memory_controls) for segment in segments) + len(segment_memory_controls) >= 8:
                            raise ConversationOutputError("memory_control_turn_limit")
                        segment_memory_controls.append(record)
                        if not is_active():
                            return
                        yield from yield_provider_event(MemoryControlReady(index, record, tuple(sorted(tool_observation_ids))))
                    else:
                        if len(frames) >= 3:
                            raise ConversationOutputError("frame_limit")
                        if (not segment_frames and sum(bool(s.frames) for s in segments) >= self.budget.max_visible_segments):
                            raise ConversationOutputError("visible_segment_limit")
                        if expired():
                            failure = ConversationOutputError("elapsed_budget_exhausted")
                            return
                        segment_frames.append(record)
                        frames.append(record)
                        if first_frame_chars is None:
                            first_frame_chars = len(record.text)
                        emit_provider_stage("sandbox_provider_first_frame_validated", include_completion=True)
                        if index == 0 and plan is not None and plan.reaction is not None and not reaction_window_emitted:
                            reaction_window_emitted = True
                            if not is_active():
                                return
                            yield from yield_provider_event(ReactionWindowReady())
                        if not is_active():
                            return
                        yield from yield_provider_event(FrameReady(len(frames) - 1, record))

                emit_provider_stage("sandbox_provider_attempt_start")

                try:
                    stream = self.provider.stream(messages, tools=schemas, actor_id=request.burst.latest.actor_id)
                except ProviderSetupRequired as error:
                    emit_provider_attempt("error")
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
                stream_exhausted = False
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
                                if event.text:
                                    emit_provider_stage("sandbox_provider_first_text_delta")
                                raw.append(event.text)
                                for chunk in event.text.splitlines(keepends=True):
                                    if first_frame_chars is None:
                                        output_chars_through_first_frame += len(chunk)
                                    for record in parser.feed(chunk):
                                        yield from handle_record(record)
                                        if failure is not None:
                                            break
                            elif isinstance(event, ProviderToolCallReady):
                                native_calls.append(event)
                            elif isinstance(event, ProviderStreamCompleted):
                                terminal = event
                                if event.input_tokens is not None:
                                    input_tokens = (input_tokens or 0) + event.input_tokens
                                if event.output_tokens is not None:
                                    output_tokens = (output_tokens or 0) + event.output_tokens
                                emit_provider_stage("sandbox_provider_stream_completed", include_completion=True)
                            else:
                                raise ConversationOutputError("invalid_provider_event")
                        except ConversationOutputError as error:
                            failure = error
                            break
                    stream_exhausted = True
                except Exception:
                    if failure is None:
                        failure = ConversationOutputError("provider_stream_failure")
                finally:
                    close = getattr(stream, "close", None)
                    if callable(close):
                        close()
                    if not stream_exhausted:
                        emit_provider_attempt("error")
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
                            yield from handle_record(record)
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
                    prepared_calls = ()
                    prior_call_ids = {call.call_id for segment in segments for call in segment.tool_calls}
                    if any(call.call_id in prior_call_ids for call in native_calls):
                        failure = ConversationOutputError("duplicate_tool_call_id")
                    else:
                        try:
                            prepared_calls = self.tool_executor.prepare_batch(native_calls, self.budget.max_tool_calls - sum(len(s.tool_calls) for s in segments))
                        except ToolBatchValidationError as error:
                            failure = ConversationOutputError(error.code)
                if failure is None and tool_finish:
                    if index == 0 and plan is None:
                        resolution = synthesized_tool_plan()
                        plan = resolution.plan
                        emit_plan_validated(resolution.source)
                        if not is_active():
                            return
                        yield from yield_provider_event(TurnRunStarted(plan))
                    if index == 0 and plan is not None and plan.reaction is not None and not reaction_window_emitted:
                        reaction_window_emitted = True
                        yield from yield_provider_event(ReactionWindowReady())
                    if not is_active():
                        return
                    if expired():
                        failure = ConversationOutputError("elapsed_budget_exhausted")
                    elif len(segment_frames) > 1:
                        failure = ConversationOutputError("tool_frame_limit")
                if failure is None and tool_finish:
                    emit_provider_attempt("ok")
                    try:
                        tool_started_at = self.monotonic_clock()
                        batch = self.tool_executor.execute_prepared_batch(prepared_calls, is_active)
                    except ToolBatchCancelled:
                        if is_active():
                            latency_trace.emit_sandbox("sandbox_tool_batch", outcome="error", elapsed_ms=max(0, int((self.monotonic_clock() - tool_started_at) * 1000)), segment=index, repair=repairs)
                        return
                    except ToolBatchValidationError as error:
                        if is_active():
                            latency_trace.emit_sandbox("sandbox_tool_batch", outcome="error", elapsed_ms=max(0, int((self.monotonic_clock() - tool_started_at) * 1000)), segment=index, repair=repairs)
                        failure = ConversationOutputError(error.code)
                    except Exception:
                        if is_active():
                            latency_trace.emit_sandbox("sandbox_tool_batch", outcome="error", elapsed_ms=max(0, int((self.monotonic_clock() - tool_started_at) * 1000)), segment=index, repair=repairs)
                        failure = ConversationOutputError("tool_executor_failure")
                    else:
                        if is_active():
                            # A batch is all tools in one model segment, never individual tools.
                            latency_trace.emit_sandbox("sandbox_tool_batch", elapsed_ms=max(0, int((self.monotonic_clock() - tool_started_at) * 1000)), segment=index, repair=repairs)
                        segments.append(SegmentResult(index, tuple(segment_frames), batch.tool_calls, SegmentFinish.TOOL_BATCH, tuple(segment_memory_controls)))
                        prior_messages.append(_assistant_continuation("".join(raw) or None, batch.tool_calls))
                        prior_messages.extend(_tool_continuation(observation) for observation in batch.observations)
                        tool_observation_ids.update(observation.call_id for observation in batch.observations)
                        if not is_active():
                            return
                        if expired():
                            if frames:
                                yield completed(TurnRunStatus.COMPLETED_PARTIAL)
                                return
                            raise ConversationOutputError("elapsed_budget_exhausted")
                        index += 1
                        break
                if failure is None and (plan is None or not segment_frames):
                    failure = ConversationOutputError("missing_frame")
                # Finalize after parsing and contract validation, before any tool work.
                emit_provider_attempt("error" if failure is not None else "ok")
                if failure is not None:
                    if segment_frames:
                        segments.append(SegmentResult(index, tuple(segment_frames), (), SegmentFinish.PARTIAL, tuple(segment_memory_controls)))
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
                    repair_code = failure.code
                    repairs += 1
                    replacement = True
                    continue
                segments.append(SegmentResult(index, tuple(segment_frames), (), SegmentFinish.COMPLETE, tuple(segment_memory_controls)))
                if not is_active() or expired():
                    if not is_active():
                        return
                    segments[-1] = SegmentResult(index, tuple(segment_frames), (), SegmentFinish.PARTIAL, tuple(segment_memory_controls))
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
