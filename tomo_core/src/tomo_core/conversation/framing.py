from __future__ import annotations

import json

from ..personal_data import MemoryControl
from .models import Frame, MovePlan, TurnBudget
from .parsing import ConversationOutputError, _parse_memory_control_payload, _parse_strict_move_plan_payload, _validate_strict_frame_text


class SegmentFrameParser:
    """Incrementally parses one model segment encoded as strict JSON Lines."""

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
        self._plan_seen = False
        self._frame_count = 0
        self._memory_control_count = 0

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
        if self._first_segment and not self._plan_seen:
            raise ConversationOutputError("missing_plan")
        return records

    def _parse_line(self, line: str) -> list[MovePlan | MemoryControl | Frame]:
        if not line.strip():
            return []
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            raise ConversationOutputError("invalid_json") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            raise ConversationOutputError("invalid_record")
        record_type = payload["type"]
        if record_type == "turn_plan":
            return [self._parse_plan(payload)]
        if record_type == "frame":
            return [self._parse_frame(payload)]
        if record_type == "memory_control":
            return [self._parse_memory_control(payload)]
        raise ConversationOutputError("invalid_record")

    def _parse_plan(self, payload: dict[str, object]) -> MovePlan:
        if not self._first_segment:
            raise ConversationOutputError("unexpected_plan")
        if self._plan_seen:
            raise ConversationOutputError("duplicate_plan")
        if self._frame_count:
            raise ConversationOutputError("late_plan")
        plan_keys = set(payload) - {"type"}
        if plan_keys not in ({"primary_move", "supporting_moves", "response_goal", "confidence"}, {"primary_move", "supporting_moves", "move_sequence", "response_goal", "confidence"}, {"primary_move", "supporting_moves", "response_goal", "confidence", "reaction"}, {"primary_move", "supporting_moves", "move_sequence", "response_goal", "confidence", "reaction"}):
            raise ConversationOutputError("invalid_plan")
        plan_payload = dict(payload)
        del plan_payload["type"]
        try:
            plan = _parse_strict_move_plan_payload(plan_payload)
        except (TypeError, ValueError, KeyError):
            raise ConversationOutputError("invalid_plan") from None
        self._plan_seen = True
        return plan

    def _parse_frame(self, payload: dict[str, object]) -> Frame:
        if self._first_segment and not self._plan_seen:
            raise ConversationOutputError("missing_plan")
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
        if self._first_segment and not self._plan_seen:
            raise ConversationOutputError("missing_plan")
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
