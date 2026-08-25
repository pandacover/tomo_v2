from __future__ import annotations

import json

from ..personal_data import MemoryControl
from .contract import PlanSource, resolve_advisory_plan, synthesized_direct_plan
from .models import Frame, MovePlan, TurnBudget
from .parsing import ConversationOutputError, _parse_memory_control_payload, _validate_strict_frame_text


class SegmentFrameParser:
    """Incrementally parses one model segment encoded as strict JSON Lines."""

    _MAX_PREAMBLE_LINES = 16
    _MAX_PREAMBLE_CHARS = 4096

    def __init__(self, segment_index: int, first_segment: bool, budget: TurnBudget) -> None:
        if not isinstance(segment_index, int) or isinstance(segment_index, bool) or segment_index < 0:
            raise ValueError("segment_index must be a non-negative integer")
        if not isinstance(first_segment, bool):
            raise ValueError("first_segment must be a boolean")
        if not isinstance(budget, TurnBudget):
            raise ValueError("budget must be a TurnBudget")
        self._segment_index = segment_index
        self._first_segment = first_segment
        self._budget = budget
        self._buffer = ""
        self._record_seen = False
        self._preamble_lines = 0
        self._preamble_chars = 0
        self._plan_seen = False
        self._plan_source: PlanSource | None = None
        self._frame_count = 0
        self._memory_control_count = 0

    @property
    def plan_source(self) -> PlanSource | None:
        return self._plan_source

    def feed(self, delta: str) -> list[MovePlan | MemoryControl | Frame]:
        if not isinstance(delta, str):
            raise ConversationOutputError("invalid_delta")
        self._buffer += delta
        records: list[MovePlan | MemoryControl | Frame] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            records.extend(self._parse_line(line))
        return records

    def finish(self) -> list[MovePlan | MemoryControl | Frame]:
        records = self._parse_line(self._buffer)
        self._buffer = ""
        return records

    def _parse_line(self, line: str) -> list[MovePlan | MemoryControl | Frame]:
        stripped = line.strip()
        if not stripped:
            return []
        if stripped in {"```", "```json", "```jsonl"}:
            return []
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            if stripped.startswith("{"):
                raise ConversationOutputError("invalid_json_object") from None
            if not self._record_seen:
                next_lines = self._preamble_lines + 1
                next_chars = self._preamble_chars + len(stripped)
                if next_lines <= self._MAX_PREAMBLE_LINES and next_chars <= self._MAX_PREAMBLE_CHARS:
                    self._preamble_lines = next_lines
                    self._preamble_chars = next_chars
                    return []
            raise ConversationOutputError("invalid_json_non_record") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            raise ConversationOutputError("invalid_record")
        record_type = payload["type"]
        if record_type == "turn_plan":
            self._record_seen = True
            return [self._parse_plan(payload)]
        if record_type == "frame":
            self._record_seen = True
            frame = self._parse_frame(payload)
            return self._synthesized_plan_if_needed() + [frame]
        if record_type == "memory_control":
            self._record_seen = True
            control = self._parse_memory_control(payload)
            return self._synthesized_plan_if_needed() + [control]
        raise ConversationOutputError("invalid_record")

    def _parse_plan(self, payload: dict[str, object]) -> MovePlan:
        if not self._first_segment:
            raise ConversationOutputError("unexpected_plan")
        if self._frame_count:
            raise ConversationOutputError("late_plan")
        if self._plan_seen:
            raise ConversationOutputError("duplicate_plan")
        plan_payload = dict(payload)
        del plan_payload["type"]
        resolution = resolve_advisory_plan(plan_payload)
        self._plan_seen = True
        self._plan_source = resolution.source
        return resolution.plan

    def _parse_frame(self, payload: dict[str, object]) -> Frame:
        if set(payload) != {"type", "text"}:
            raise ConversationOutputError("invalid_frame")
        if self._frame_count >= self._budget.max_frames_per_segment:
            raise ConversationOutputError("frame_limit")
        try:
            text = _validate_strict_frame_text(
                payload["text"],
                max_chars=self._budget.max_chars_per_frame,
                max_sentences=self._budget.max_sentences_per_frame,
            )
        except ValueError as error:
            raise ConversationOutputError(str(error)) from None
        frame = Frame(self._segment_index, self._frame_count, text)
        self._frame_count += 1
        return frame

    def _parse_memory_control(self, payload: dict[str, object]) -> MemoryControl:
        if self._frame_count:
            raise ConversationOutputError("late_memory_control")
        if self._memory_control_count >= 5:
            raise ConversationOutputError("memory_control_limit")
        control_payload = dict(payload)
        del control_payload["type"]
        try:
            control = _parse_memory_control_payload(control_payload)
        except (TypeError, ValueError, KeyError):
            raise ConversationOutputError("invalid_memory_control") from None
        self._memory_control_count += 1
        return control

    def _synthesized_plan_if_needed(self) -> list[MovePlan]:
        if not self._first_segment or self._plan_seen:
            return []
        resolution = synthesized_direct_plan()
        self._plan_seen = True
        self._plan_source = resolution.source
        return [resolution.plan]
