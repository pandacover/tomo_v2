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

    def execute_batch(
        self,
        provider_calls: Sequence[ProviderToolCallReady],
        remaining_tool_calls: int,
        is_active: Callable[[], bool],
    ) -> ToolBatchResult:
        calls = tuple(provider_calls)
        if not calls:
            raise ToolBatchValidationError("empty_tool_batch")
        if isinstance(remaining_tool_calls, bool) or not isinstance(remaining_tool_calls, int) or remaining_tool_calls <= 0:
            raise ToolBatchValidationError("invalid_tool_budget")
        if len(calls) > remaining_tool_calls:
            raise ToolBatchValidationError("tool_budget_exceeded")
        if not callable(is_active):
            raise ToolBatchValidationError("invalid_activity_check")
        if any(not isinstance(call, ProviderToolCallReady) for call in calls):
            raise ToolBatchValidationError("invalid_tool_call")
        if len({call.call_id for call in calls}) != len(calls):
            raise ToolBatchValidationError("duplicate_tool_call_id")

        parsed: list[tuple[ToolCall, Callable[[dict[str, object]], object], dict[str, object]]] = []
        for provider_call in calls:
            arguments = _parse_object(provider_call.arguments_json)
            try:
                tool = self._registry.resolve(provider_call.name)
            except ToolRegistryError as error:
                raise ToolBatchValidationError(error.code) from error
            if not tool.spec.read_only or not tool.spec.parallel_safe:
                raise ToolBatchValidationError("unsupported_tool")
            parsed.append((ToolCall(provider_call.call_id, provider_call.name, arguments), tool.invoke, copy.deepcopy(arguments)))

        if not is_active():
            raise ToolBatchCancelled()
        with ThreadPoolExecutor(max_workers=min(len(parsed), remaining_tool_calls)) as workers:
            futures = [workers.submit(invoke, arguments) for _, invoke, arguments in parsed]
            results = []
            for future in futures:
                try:
                    results.append(future.result())
                except Exception:
                    results.append(_INVOCATION_FAILED)

        if not is_active():
            raise ToolBatchCancelled()
        observations = tuple(
            _observation(call, result) if result is not _INVOCATION_FAILED else ToolObservation(call.call_id, call.name, False, "Tool execution failed.", "tool_execution_failed")
            for (call, _, _), result in zip(parsed, results)
        )
        return ToolBatchResult(tuple(call for call, _, _ in parsed), observations)
