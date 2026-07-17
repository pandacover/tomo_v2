import unittest
import math

from tomo_core.tools import BoundTool, ToolRegistry, ToolRegistryError, ToolSpec


def _parameters() -> dict[str, object]:
    return {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}


class ToolContractTests(unittest.TestCase):
    def test_spec_normalizes_safe_name_and_defensively_copies_parameters(self):
        parameters = _parameters()
        spec = ToolSpec(" lookup ", " Finds a record. ", parameters)
        parameters["properties"]["query"]["type"] = "number"  # type: ignore[index]

        self.assertEqual(spec.name, "lookup")
        self.assertEqual(spec.description, "Finds a record.")
        self.assertEqual(spec.parameters["properties"]["query"]["type"], "string")  # type: ignore[index]
        with self.assertRaises(TypeError):
            spec.parameters["type"] = "array"  # type: ignore[index]

    def test_spec_rejects_invalid_names_schemas_and_boolean_flags(self):
        for kwargs in (
            {"name": "bad name"},
            {"name": "bad/name"},
            {"description": " "},
            {"parameters": {"type": "array"}},
            {"parameters": {"type": "object", "properties": []}},
            {"parameters": {"type": "object", "properties": {}, "required": "query"}},
            {"parameters": {"type": "object", "properties": {}, "required": ["missing"]}},
            {"read_only": 1},
            {"parallel_safe": "yes"},
            {"internal_context": None},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ToolSpec(**{"name": "lookup", "description": "Finds a record.", "parameters": _parameters(), **kwargs})

    def test_spec_rejects_non_finite_values_and_non_object_property_schemas(self):
        for parameters in (
            {"type": "object", "properties": {"query": {"default": math.nan}}},
            {"type": "object", "properties": {"query": {"examples": [math.inf]}}},
            {"type": "object", "properties": {"query": {"minimum": -math.inf}}},
            {"type": "object", "properties": {"query": "string"}},
            {"type": "object", "properties": {"query": []}},
        ):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                ToolSpec("lookup", "Finds a record.", parameters)

    def test_registry_rejects_duplicates_but_accepts_mutating_and_serial_tools(self):
        lookup = BoundTool(ToolSpec("lookup", "Finds a record.", _parameters()), lambda arguments: arguments)
        with self.assertRaises(ValueError):
            ToolRegistry((lookup, lookup))
        registry = ToolRegistry((
            BoundTool(ToolSpec("write", "Writes a record.", _parameters(), read_only=False), lambda _: None),
            BoundTool(ToolSpec("serial", "Serial access.", _parameters(), parallel_safe=False), lambda _: None),
        ))
        self.assertEqual(tuple(schema["function"]["name"] for schema in registry.schemas()), ("write", "serial"))

    def test_registry_exposes_deterministic_defensive_openai_schemas_and_exact_resolution(self):
        registry = ToolRegistry((BoundTool(ToolSpec("lookup", "Finds a record.", _parameters()), lambda _: "ok"),))
        schemas = registry.schemas()

        self.assertEqual(schemas, ({"type": "function", "function": {"name": "lookup", "description": "Finds a record.", "parameters": _parameters()}},))
        schemas[0]["function"]["parameters"]["properties"]["query"]["type"] = "number"  # type: ignore[index]
        self.assertEqual(registry.schemas()[0]["function"]["parameters"]["properties"]["query"]["type"], "string")
        self.assertEqual(registry.resolve("lookup").spec.name, "lookup")
        with self.assertRaises(ToolRegistryError) as raised:
            registry.resolve("LOOKUP")
        self.assertEqual(raised.exception.code, "unknown_tool")
        self.assertEqual(ToolRegistry().schemas(), ())

    def test_unattended_view_exposes_only_explicitly_safe_tools_and_marks_blocked_calls(self):
        registry = ToolRegistry((
            BoundTool(ToolSpec("lookup", "Finds a record.", _parameters(), unattended_safe=True), lambda _: "ok"),
            BoundTool(ToolSpec("write", "Writes a record.", _parameters(), read_only=False), lambda _: "ok"),
        ))

        unattended = registry.unattended()

        self.assertEqual(tuple(schema["function"]["name"] for schema in unattended.schemas()), ("lookup",))
        with self.assertRaises(ToolRegistryError) as raised:
            unattended.resolve("write")
        self.assertEqual(raised.exception.code, "approval_needed")
