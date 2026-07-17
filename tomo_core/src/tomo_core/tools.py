from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Mapping


_FUNCTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


def _freeze_json(value: object) -> object:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("tool parameters must not contain non-finite numbers")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("tool parameters must have string keys")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError("tool parameters must be JSON-compatible")


def _copy_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _copy_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_copy_json(item) for item in value]
    return copy.deepcopy(value)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, object]
    read_only: bool = True
    parallel_safe: bool = True
    internal_context: bool = False
    unattended_safe: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise ValueError("tool name must be a string")
        name = self.name.strip()
        if not _FUNCTION_NAME.fullmatch(name):
            raise ValueError("tool name must be a safe function name")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("tool description is required")
        if not isinstance(self.parameters, Mapping):
            raise ValueError("tool parameters must be a mapping")
        for flag in (self.read_only, self.parallel_safe, self.internal_context, self.unattended_safe):
            if not isinstance(flag, bool):
                raise ValueError("tool flags must be booleans")
        parameters = _freeze_json(self.parameters)
        if not isinstance(parameters, Mapping) or parameters.get("type") != "object":
            raise ValueError("tool parameters must be an object JSON Schema")
        properties = parameters.get("properties", MappingProxyType({}))
        if not isinstance(properties, Mapping):
            raise ValueError("tool schema properties must be an object")
        if any(not isinstance(schema, Mapping) for schema in properties.values()):
            raise ValueError("tool schema properties must contain object schemas")
        required = parameters.get("required", ())
        if not isinstance(required, tuple) or any(not isinstance(item, str) for item in required):
            raise ValueError("tool schema required must be an array of property names")
        if len(set(required)) != len(required) or any(item not in properties for item in required):
            raise ValueError("tool schema required entries must name properties")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", self.description.strip())
        object.__setattr__(self, "parameters", parameters)


@dataclass(frozen=True)
class BoundTool:
    spec: ToolSpec
    invoke: Callable[[dict[str, object]], object]

    def __post_init__(self) -> None:
        if not isinstance(self.spec, ToolSpec):
            raise ValueError("bound tool requires a ToolSpec")
        if not callable(self.invoke):
            raise ValueError("bound tool invoke must be callable")


class ToolRegistryError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ToolRegistry:
    def __init__(self, tools: tuple[BoundTool, ...] = (), *, blocked: frozenset[str] = frozenset()) -> None:
        tools = tuple(tools)
        if any(not isinstance(tool, BoundTool) for tool in tools):
            raise ValueError("registry tools must be BoundTools")
        names = [tool.spec.name for tool in tools]
        if len(set(names)) != len(names):
            raise ValueError("tool names must be unique")
        if not isinstance(blocked, frozenset) or any(not isinstance(name, str) or not name for name in blocked):
            raise ValueError("blocked tool names must be non-empty strings")
        if blocked & set(names):
            raise ValueError("blocked tool cannot be exposed")
        self._tools = tools
        self._by_name = {tool.spec.name: tool for tool in tools}
        self._blocked = blocked

    def schemas(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "type": "function",
                "function": {
                    "name": tool.spec.name,
                    "description": tool.spec.description,
                    "parameters": _copy_json(tool.spec.parameters),
                },
            }
            for tool in self._tools
        )

    def resolve(self, name: str) -> BoundTool:
        tool = self._by_name.get(name)
        if tool is None:
            raise ToolRegistryError("approval_needed" if name in self._blocked else "unknown_tool")
        return tool

    def is_blocked(self, name: str) -> bool:
        return name in self._blocked

    def extend(self, other: "ToolRegistry") -> "ToolRegistry":
        if not isinstance(other, ToolRegistry):
            raise ValueError("other must be a ToolRegistry")
        return ToolRegistry((*self._tools, *other._tools), blocked=self._blocked | other._blocked)

    def unattended(self) -> "ToolRegistry":
        return ToolRegistry(
            tuple(tool for tool in self._tools if tool.spec.unattended_safe),
            blocked=self._blocked | frozenset(tool.spec.name for tool in self._tools if not tool.spec.unattended_safe),
        )
