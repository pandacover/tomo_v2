from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import replace

import httpx

from ..context import ContextHydrator
from ..delivery import split_sentences
from ..models import AutomationTurn, PeerTurn, ResponseContract
from ..providers import ProviderAdapter, ProviderSetupRequired, ProviderStreamCompleted, ProviderStreamError, ProviderTextDelta, ProviderToolCallReady
from ..tool_execution import ToolBatchCancelled, ToolBatchValidationError, ToolExecutor
from ..tools import ToolRegistry
from .. import latency_trace
from .contract import PlanSource, synthesized_tool_plan
from .framing import SegmentFrameParser
from .models import ConversationRequest, Frame, FrameReady, MemoryControlReady, MovePlan, ReactionWindowReady, SegmentFinish, SegmentResult, TurnBudget, TurnRunCompleted, TurnRunEvent, TurnRunResult, TurnRunStarted, TurnRunStatus, TurnUsage
from .parsing import ConversationOutputError
from .prompts import build_first_segment_repair_messages, build_segment_messages, build_segment_repair_messages


def _provider_failure_code(error: BaseException) -> str:
    if isinstance(error, httpx.TimeoutException):
        return "provider_timeout"
    if isinstance(error, httpx.ConnectError):
        return "provider_connect"
    if isinstance(error, ProviderStreamError):
        return f"provider_stream_{error.code}"
    if isinstance(error, ValueError):
        message = str(error).split(":")[-1].strip().lower()
        slug = "".join(character if character.isalnum() else "_" for character in message).strip("_")[:48]
        if slug:
            return f"provider_{slug}"
    name = type(error).__name__.lower()
    slug = "".join(character if character.isalnum() else "_" for character in name).strip("_")[:40]
    return f"provider_{slug}" if slug else "provider_stream_failure"


def should_repair_performative_output(user_text: str, frames: Sequence[Frame]) -> bool:
    """Return True if a casual short input receives a bloated or performative multi-sentence response."""
    cleaned_input = user_text.strip().lower()
    if not cleaned_input:
        return False
    words_input = cleaned_input.split()
    if len(words_input) > 8:
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
    if any(word.strip("?,.!\"'") in info_keywords for word in words_input):
        return False

    full_output = " ".join(frame.text for frame in frames).strip()
    words_output = full_output.split()
    sentences = [s for s in full_output.replace("!", ".").replace("?", ".").split(".") if s.strip()]

    return len(words_output) > 16 or len(sentences) > 1



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
        mutating_execution_attempted = False
        pending_peer_resume = False
        peer_tool_used = False

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
                if pending_peer_resume:
                    frame = _peer_frame(
                        index,
                        0,
                        _bounded_peer_text(
                            _PEER_PENDING,
                            self.budget.max_chars_per_frame,
                            self.budget.max_sentences_per_frame,
                        ),
                    )
                    segments.append(SegmentResult(index, (frame,), (), SegmentFinish.COMPLETE))
                    frames.append(frame)
                    yield FrameReady(len(frames) - 1, frame)
                    if not is_active():
                        return
                    yield completed(TurnRunStatus.COMPLETED)
                    return
                if segments and frames:
                    if not is_active():
                        return
                    yield completed(TurnRunStatus.COMPLETED_PARTIAL)
                    return
                raise ConversationOutputError("elapsed_budget_exhausted")
            allow_tools = (not mutating_execution_attempted or pending_peer_resume) and bool(self.tool_registry.schemas()) and index < self.budget.max_model_segments - 1 and sum(bool(s.frames) for s in segments) < self.budget.max_visible_segments - 1 and sum(s.finish is SegmentFinish.TOOL_BATCH for s in segments) < self.budget.max_tool_rounds and sum(len(s.tool_calls) for s in segments) < self.budget.max_tool_calls
            schemas = self.tool_registry.schemas() if allow_tools else ()
            if pending_peer_resume:
                schemas = tuple(schema for schema in schemas if schema.get("function", {}).get("name") == "peer_resume")
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
                    schemas = schemas if not mutating_execution_attempted else ()
                    messages = build_first_segment_repair_messages(
                        request,
                        context,
                        segment_budget,
                        repair_code or "replacement_required",
                        prior_messages=prior_messages,
                        tools_available=schemas,
                    )
                elif replacement and plan is not None:
                    schemas = schemas if not mutating_execution_attempted else ()
                    messages = build_segment_repair_messages(request, context, segment_budget, plan, repair_code or "replacement_required", prior_messages=prior_messages, tools_available=schemas)
                else:
                    messages = build_segment_messages(request, context, segment_budget, segment_index=index, plan=plan, prior_messages=prior_messages, tools_available=schemas)
                raw: list[str] = []
                native_calls: list[ProviderToolCallReady] = []
                segment_frames: list[Frame] = []
                segment_memory_controls = []
                pending_memory_control_events: list[tuple[int, object, tuple[str, ...]]] = []
                terminal: ProviderStreamCompleted | None = None
                failure: ConversationOutputError | None = None
                attempt_started_at = self.monotonic_clock()
                provider_attempt_emitted = False
                provider_attempts += 1
                suspended_seconds = 0.0
                emitted_stages: set[str] = set()
                output_chars_through_first_frame = 0
                first_frame_chars: int | None = None
                emitted_frame_count = 0

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
                        if pending_peer_resume or peer_tool_used:
                            # Peer exchange is ephemeral and cannot author personal memory.
                            return
                        if sum(len(segment.memory_controls) for segment in segments) + len(segment_memory_controls) >= 8:
                            raise ConversationOutputError("memory_control_turn_limit")
                        segment_memory_controls.append(record)
                        pending_memory_control_events.append((index, record, tuple(sorted(tool_observation_ids))))
                        if not schemas:
                            yield from emit_segment_memory_controls()
                    else:
                        if pending_peer_resume:
                            # Until resume completes, model-authored frames are ungrounded.
                            return
                        if peer_tool_used:
                            record = replace(record, source="peer_exchange")
                        if len(frames) + len(segment_frames) - emitted_frame_count >= 3:
                            raise ConversationOutputError("frame_limit")
                        if (not segment_frames and sum(bool(s.frames) for s in segments) >= self.budget.max_visible_segments):
                            raise ConversationOutputError("visible_segment_limit")
                        if expired():
                            failure = ConversationOutputError("elapsed_budget_exhausted")
                            return
                        segment_frames.append(record)
                        if first_frame_chars is None:
                            first_frame_chars = len(record.text)
                        emit_provider_stage("sandbox_provider_first_frame_validated", include_completion=True)
                        if not schemas:
                            yield from emit_segment_frames()

                def emit_segment_frames() -> Iterator[TurnRunEvent]:
                    nonlocal reaction_window_emitted, emitted_frame_count
                    if index == 0 and plan is not None and plan.reaction is not None and not reaction_window_emitted:
                        reaction_window_emitted = True
                        if not is_active():
                            return
                        yield from yield_provider_event(ReactionWindowReady())
                    for frame in segment_frames[emitted_frame_count:]:
                        frames.append(frame)
                        emitted_frame_count += 1
                        if not is_active():
                            return
                        yield from yield_provider_event(FrameReady(len(frames) - 1, frame))

                emitted_memory_control_count = 0

                def emit_segment_memory_controls() -> Iterator[TurnRunEvent]:
                    nonlocal emitted_memory_control_count
                    for segment_index, control, observation_ids in pending_memory_control_events[emitted_memory_control_count:]:
                        emitted_memory_control_count += 1
                        if not is_active():
                            return
                        yield from yield_provider_event(MemoryControlReady(segment_index, control, observation_ids))

                emit_provider_stage("sandbox_provider_attempt_start")

                try:
                    if isinstance(request.burst, AutomationTurn):
                        actor_id = request.burst.actor_id
                    elif isinstance(request.burst, PeerTurn):
                        actor_id = None
                    else:
                        actor_id = request.burst.latest.actor_id
                    stream = self.provider.stream(messages, tools=schemas, actor_id=actor_id)
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
                except httpx.HTTPStatusError:
                    raise
                except Exception as error:
                    failure = ConversationOutputError(_provider_failure_code(error))
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
                except httpx.HTTPStatusError:
                    raise
                except Exception as error:
                    if failure is None:
                        failure = ConversationOutputError(_provider_failure_code(error))
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
                mutating_tool_segment = any(self.tool_registry.is_mutating(native_call.name) for native_call in native_calls)
                peer_tool_segment = any(native_call.name in {"peer_list", "peer_ask", "peer_resume"} for native_call in native_calls)
                offered_tool_names = {
                    function["name"]
                    for schema in schemas
                    if isinstance((function := schema.get("function")), dict) and isinstance(function.get("name"), str)
                }
                blocked_tool_segment = bool(native_calls) and all(self.tool_registry.is_blocked(call.name) for call in native_calls)
                if failure is None and tool_finish and not blocked_tool_segment and any(call.name not in offered_tool_names for call in native_calls):
                    failure = ConversationOutputError("native_tool_unavailable")
                if failure is None and tool_finish and blocked_tool_segment:
                    if plan is None:
                        resolution = synthesized_tool_plan()
                        plan = resolution.plan
                        emit_plan_validated(resolution.source)
                        if not is_active():
                            return
                        yield from yield_provider_event(TurnRunStarted(plan))
                    # Do not retain or surface blocked tool names or arguments.
                    segments.append(SegmentResult(index, (), (), SegmentFinish.FAILED))
                    yield completed(TurnRunStatus.APPROVAL_NEEDED)
                    return
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
                        if not is_active():
                            return
                        reaction_window_emitted = True
                        yield from yield_provider_event(ReactionWindowReady())
                    if not is_active():
                        return
                    if expired():
                        failure = ConversationOutputError("elapsed_budget_exhausted")
                    elif len(segment_frames) > 1:
                        failure = ConversationOutputError("tool_frame_limit")
                if failure is None and tool_finish:
                    if mutating_tool_segment:
                        mutating_execution_attempted = True
                    if peer_tool_segment:
                        peer_tool_used = True
                    if not peer_tool_segment:
                        yield from emit_segment_memory_controls()
                        if not is_active():
                            return
                    if mutating_tool_segment or peer_tool_segment:
                        # Peer and mutating tools cannot ground pre-observation announcements.
                        segment_frames.clear()
                    else:
                        # A validated read-only announcement may stay progressive.
                        yield from emit_segment_frames()
                        if not is_active():
                            return
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
                        peer_pending = _peer_is_pending(batch)
                        if peer_tool_segment and expired() and not peer_pending:
                            segments.append(SegmentResult(index, (), batch.tool_calls, SegmentFinish.TOOL_BATCH))
                            if frames:
                                yield completed(TurnRunStatus.COMPLETED_PARTIAL)
                                return
                            raise ConversationOutputError("elapsed_budget_exhausted")
                        peer_frames = _terminal_peer_frames(
                            index,
                            batch,
                            max_frames=segment_budget.max_frames_per_segment,
                            max_chars_per_frame=segment_budget.max_chars_per_frame,
                            max_sentences_per_frame=segment_budget.max_sentences_per_frame,
                        )
                        if peer_frames is not None:
                            # Peer-owned terminal frames cannot ground pre-result memory controls.
                            segments.append(SegmentResult(index, (), batch.tool_calls, SegmentFinish.TOOL_BATCH))
                            segment_frames[:] = [_peer_frame(index + 1, frame.frame_index, frame.text) for frame in peer_frames]
                            segments.append(SegmentResult(index + 1, tuple(segment_frames), (), SegmentFinish.COMPLETE))
                            if not is_active():
                                return
                            yield from emit_segment_frames()
                            if not is_active():
                                return
                            yield completed(TurnRunStatus.COMPLETED)
                            return
                        if peer_pending:
                            pending_peer_resume = True
                        segments.append(SegmentResult(index, tuple(segment_frames), batch.tool_calls, SegmentFinish.TOOL_BATCH, () if peer_tool_segment else tuple(segment_memory_controls)))
                        prior_messages.append(_assistant_continuation("".join(raw) or None, batch.tool_calls))
                        prior_messages.extend(_tool_continuation(observation) for observation in batch.observations)
                        tool_observation_ids.update(
                            observation.call_id
                            for observation in batch.observations
                            if _observation_can_ground(observation)
                        )
                        if not is_active():
                            return
                        if expired():
                            if peer_pending:
                                segment_frames[:] = [_peer_frame(index + 1, 0, _bounded_peer_text(_PEER_PENDING, segment_budget.max_chars_per_frame, segment_budget.max_sentences_per_frame))]
                                segments.append(SegmentResult(index + 1, tuple(segment_frames), (), SegmentFinish.COMPLETE))
                                yield from emit_segment_frames()
                                if not is_active():
                                    return
                                yield completed(TurnRunStatus.COMPLETED)
                                return
                            if frames:
                                yield completed(TurnRunStatus.COMPLETED_PARTIAL)
                                return
                            raise ConversationOutputError("elapsed_budget_exhausted")
                        index += 1
                        break
                if failure is not None and peer_tool_segment and not pending_peer_resume:
                    # Validation and execution failures provide no peer observation to ground a claim.
                    emit_provider_attempt("error")
                    segment_frames[:] = [
                        _peer_frame(
                            index,
                            0,
                            _bounded_peer_text(
                                _PEER_NO_ANSWER,
                                segment_budget.max_chars_per_frame,
                                segment_budget.max_sentences_per_frame,
                            ),
                        )
                    ]
                    segment_memory_controls.clear()
                    if plan is None:
                        resolution = synthesized_tool_plan()
                        plan = resolution.plan
                        emit_plan_validated(resolution.source)
                        if not is_active():
                            return
                        yield from yield_provider_event(TurnRunStarted(plan))
                    segments.append(SegmentResult(index, tuple(segment_frames), (), SegmentFinish.COMPLETE))
                    yield from emit_segment_frames()
                    if not is_active():
                        return
                    yield completed(TurnRunStatus.COMPLETED)
                    return
                if pending_peer_resume:
                    emit_provider_attempt("error" if failure is not None else "ok")
                    segment_frames[:] = [_peer_frame(index, 0, _bounded_peer_text(_PEER_PENDING, segment_budget.max_chars_per_frame, segment_budget.max_sentences_per_frame))]
                    failure = None
                    segments.append(SegmentResult(index, tuple(segment_frames), (), SegmentFinish.COMPLETE))
                    yield from emit_segment_frames()
                    if not is_active():
                        return
                    yield completed(TurnRunStatus.COMPLETED)
                    return
                if failure is None and (plan is None or not segment_frames):
                    failure = ConversationOutputError("missing_frame")
                # Finalize after parsing and contract validation, before any tool work.
                emit_provider_attempt("error" if failure is not None else "ok")
                if failure is not None:
                    if mutating_tool_segment or peer_tool_segment:
                        # A failed tool segment has no observation that could ground its frame.
                        segment_frames.clear()
                    if segment_frames:
                        yield from emit_segment_frames()
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
                yield from emit_segment_memory_controls()
                if not is_active():
                    return
                segments.append(SegmentResult(index, tuple(segment_frames), (), SegmentFinish.COMPLETE, tuple(segment_memory_controls)))
                yield from emit_segment_frames()
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


_PEER_NO_ANSWER = "i couldn't get an answer from that tomo. try again in a moment."
_PEER_APPROVAL_PENDING = "that tomo needs their owner's approval before answering. check back after they approve it."
_PEER_PENDING = "that tomo hasn't answered yet. i don't have an answer yet."
_PEER_UNAVAILABLE = "that tomo is unavailable right now. try again in a moment."
_PEER_GROUNDING_REQUIRED = "that question needs verified information this connection cannot provide, so i didn't ask that tomo to guess."
_PEER_CONNECTION_CHANGED = "the connection or its permissions changed before that tomo could answer. check tomo connections, then try again."
_PEER_INVALID_RESPONSE = "that tomo couldn't produce a safe answer. try asking another way."


def _peer_frame(index: int, frame_index: int, text: str) -> Frame:
    return Frame(index, frame_index, text, source="peer_exchange")


def _peer_is_pending(batch) -> bool:
    for call, observation in zip(batch.tool_calls, batch.observations):
        if call.name not in {"peer_ask", "peer_resume"}:
            continue
        try:
            value = json.loads(observation.content)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("ok") is True and value.get("status") == "pending":
            return True
    return False


def _observation_can_ground(observation) -> bool:
    if not observation.ok:
        return False
    if observation.name in {"peer_list", "peer_ask", "peer_resume"}:
        # Peer exchange never grants authority to author owner memory.
        return False
    try:
        value = json.loads(observation.content)
    except (TypeError, json.JSONDecodeError):
        return True
    return not (isinstance(value, dict) and value.get("ok") is False)


def _bounded_peer_text(preferred: str, max_chars: int, max_sentences: int) -> str:
    return next(
        (
            candidate
            for candidate in (preferred, "no peer answer.", "no answer.", "?")
            if len(candidate) <= max_chars and len(split_sentences(candidate)) <= max_sentences
        ),
        "?"[:max_chars],
    )


def _terminal_peer_frames(
    index: int,
    batch,
    *,
    max_frames: int,
    max_chars_per_frame: int,
    max_sentences_per_frame: int,
) -> list[Frame] | None:
    """Return runtime-owned frames for terminal peer observations, failing closed."""
    peer_observations = [
        observation
        for call, observation in zip(batch.tool_calls, batch.observations)
        if call.name in {"peer_ask", "peer_resume"}
    ]
    if not peer_observations:
        return None
    observation = peer_observations[-1]
    try:
        value = json.loads(observation.content)
    except (TypeError, json.JSONDecodeError):
        value = None
    fallback_text = _bounded_peer_text(_PEER_NO_ANSWER, max_chars_per_frame, max_sentences_per_frame)
    fallback = [_peer_frame(index, 0, fallback_text)]
    if not isinstance(value, dict):
        return fallback
    status = value.get("status")
    if status == "pending":
        return None
    if status == "completed" and value.get("ok") is True:
        raw_frames = value.get("frames")
        if (
            isinstance(raw_frames, list)
            and 0 < len(raw_frames) <= min(3, max_frames)
            and all(isinstance(frame, str) and 0 < len(frame) <= max_chars_per_frame for frame in raw_frames)
            and all(len(split_sentences(frame)) <= max_sentences_per_frame for frame in raw_frames)
        ):
            return [_peer_frame(index, ordinal, frame) for ordinal, frame in enumerate(raw_frames)]
    if status == "confirmation_pending":
        return [_peer_frame(index, 0, _bounded_peer_text(_PEER_APPROVAL_PENDING, max_chars_per_frame, max_sentences_per_frame))]
    if status == "denied":
        return [_peer_frame(index, 0, _bounded_peer_text(_PEER_CONNECTION_CHANGED, max_chars_per_frame, max_sentences_per_frame))]
    if status == "failed":
        error_code = value.get("error_code")
        if error_code == "peer_grounding_required":
            return [_peer_frame(index, 0, _bounded_peer_text(_PEER_GROUNDING_REQUIRED, max_chars_per_frame, max_sentences_per_frame))]
        if error_code in {"peer_timeout", "peer_unavailable"}:
            return [_peer_frame(index, 0, _bounded_peer_text(_PEER_UNAVAILABLE, max_chars_per_frame, max_sentences_per_frame))]
        if error_code == "peer_connection_unavailable":
            return [_peer_frame(index, 0, _bounded_peer_text(_PEER_CONNECTION_CHANGED, max_chars_per_frame, max_sentences_per_frame))]
        if error_code == "peer_invalid_response":
            return [_peer_frame(index, 0, _bounded_peer_text(_PEER_INVALID_RESPONSE, max_chars_per_frame, max_sentences_per_frame))]
    return fallback
