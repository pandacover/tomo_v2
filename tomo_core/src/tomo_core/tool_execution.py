from __future__ import annotations

import copy
import json
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from .conversation.models import ToolCall, ToolObservation
from .providers import ProviderToolCallReady
from .tools import ToolRegistry, ToolRegistryError


class ToolBatchValidationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ToolBatchCancelled(RuntimeError):
    def __init__(self) -> None:
        self.code = "tool_batch_cancelled"
        super().__init__(self.code)


@dataclass(frozen=True)
class ToolBatchResult:
    tool_calls: tuple[ToolCall, ...]
    observations: tuple[ToolObservation, ...]


@dataclass(frozen=True)
class _PreparedToolCall:
    call: ToolCall
    invoke: Callable[[dict[str, object]], object]
    arguments: dict[str, object]
    serial: bool


_INVOCATION_FAILED = object()


def _has_unresolved_reference(value: object) -> bool:
    """V1 rejects any object '$ref' key or string containing '${...}'."""
    if isinstance(value, str):
        return "${" in value and "}" in value[value.index("${") + 2 :]
    if isinstance(value, Mapping):
        return "$ref" in value or any(_has_unresolved_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_unresolved_reference(item) for item in value)
    return False


def _has_non_finite_number(value: object) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, Mapping):
        return any(_has_non_finite_number(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_non_finite_number(item) for item in value)
    return False


def _parse_object(arguments_json: str) -> dict[str, object]:
    if not isinstance(arguments_json, str):
        raise ToolBatchValidationError("invalid_tool_arguments")
    def reject_non_finite_constant(_: str) -> object:
        raise ValueError

    decoder = json.JSONDecoder(parse_constant=reject_non_finite_constant)
    try:
        value, end = decoder.raw_decode(arguments_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ToolBatchValidationError("invalid_tool_arguments") from error
    if arguments_json[end:].strip() or not isinstance(value, dict):
        raise ToolBatchValidationError("invalid_tool_arguments")
    if _has_non_finite_number(value):
        raise ToolBatchValidationError("invalid_tool_arguments")
    if _has_unresolved_reference(value):
        raise ToolBatchValidationError("unresolved_tool_reference")
    return value


def _observation(call: ToolCall, value: object) -> ToolObservation:
    if isinstance(value, str):
        content = value
    else:
        try:
            content = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            return ToolObservation(call.call_id, call.name, False, "Tool returned no usable result.", "invalid_tool_result")
    if not content.strip():
        return ToolObservation(call.call_id, call.name, False, "Tool returned no usable result.", "invalid_tool_result")
    return ToolObservation(call.call_id, call.name, True, content)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        if not isinstance(registry, ToolRegistry):
            raise ValueError("tool executor requires a ToolRegistry")
        self._registry = registry

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    def prepare_batch(
        self,
        provider_calls: Sequence[ProviderToolCallReady],
        remaining_tool_calls: int,
    ) -> tuple[_PreparedToolCall, ...]:
        calls = tuple(provider_calls)
        if not calls:
            raise ToolBatchValidationError("empty_tool_batch")
        if isinstance(remaining_tool_calls, bool) or not isinstance(remaining_tool_calls, int) or remaining_tool_calls <= 0:
            raise ToolBatchValidationError("invalid_tool_budget")
        if len(calls) > remaining_tool_calls:
            raise ToolBatchValidationError("tool_budget_exceeded")
        if any(not isinstance(call, ProviderToolCallReady) for call in calls):
            raise ToolBatchValidationError("invalid_tool_call")
        if len({call.call_id for call in calls}) != len(calls):
            raise ToolBatchValidationError("duplicate_tool_call_id")

        prepared: list[_PreparedToolCall] = []
        for provider_call in calls:
            arguments = _parse_object(provider_call.arguments_json)
            try:
                tool = self._registry.resolve(provider_call.name)
            except ToolRegistryError as error:
                raise ToolBatchValidationError(error.code) from error
            prepared.append(
                _PreparedToolCall(
                    ToolCall(provider_call.call_id, provider_call.name, arguments),
                    tool.invoke,
                    copy.deepcopy(arguments),
                    not tool.spec.read_only or not tool.spec.parallel_safe,
                )
            )
        if any(call.serial for call in prepared) and len(prepared) != 1:
            raise ToolBatchValidationError("incompatible_tool_batch")
        return tuple(prepared)

    def execute_batch(
        self,
        provider_calls: Sequence[ProviderToolCallReady],
        remaining_tool_calls: int,
        is_active: Callable[[], bool],
    ) -> ToolBatchResult:
        parsed = self.prepare_batch(provider_calls, remaining_tool_calls)
        return self.execute_prepared_batch(parsed, is_active)

    def execute_prepared_batch(
        self,
        prepared_calls: Sequence[_PreparedToolCall],
        is_active: Callable[[], bool],
    ) -> ToolBatchResult:
        if not callable(is_active):
            raise ToolBatchValidationError("invalid_activity_check")
        parsed = tuple(prepared_calls)
        if not parsed:
            raise ToolBatchValidationError("empty_tool_batch")
        if any(not isinstance(call, _PreparedToolCall) for call in parsed):
            raise ToolBatchValidationError("invalid_tool_call")

        if not is_active():
            raise ToolBatchCancelled()
        if len(parsed) == 1:
            try:
                results = [parsed[0].invoke(parsed[0].arguments)]
            except Exception:
                results = [_INVOCATION_FAILED]
        else:
            with ThreadPoolExecutor(max_workers=len(parsed)) as workers:
                futures = [workers.submit(prepared_call.invoke, prepared_call.arguments) for prepared_call in parsed]
                results = []
                for future in futures:
                    try:
                        results.append(future.result())
                    except Exception:
                        results.append(_INVOCATION_FAILED)

        if not is_active():
            raise ToolBatchCancelled()
        observations = tuple(
            _observation(prepared_call.call, result) if result is not _INVOCATION_FAILED else ToolObservation(prepared_call.call.call_id, prepared_call.call.name, False, "Tool execution failed.", "tool_execution_failed")
            for prepared_call, result in zip(parsed, results)
        )
        return ToolBatchResult(tuple(prepared_call.call for prepared_call in parsed), observations)
