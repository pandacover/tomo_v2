import threading
import time
import unittest

from tomo_core.providers import ProviderToolCallReady
from tomo_core.tool_execution import ToolBatchCancelled, ToolBatchValidationError, ToolExecutor
from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec


def _registry(*tools: BoundTool) -> ToolRegistry:
    return ToolRegistry(tools)


def _tool(name: str, invoke, **flags: object) -> BoundTool:
    return BoundTool(ToolSpec(name, f"Runs {name}.", {"type": "object", "properties": {}}, **flags), invoke)


class ToolExecutionTests(unittest.TestCase):
    def test_executor_exposes_its_exact_read_only_registry(self):
        registry = _registry(_tool("known", lambda _: "ok"))

        self.assertIs(ToolExecutor(registry).registry, registry)

    def test_calls_overlap_and_observations_keep_provider_order(self):
        started = threading.Barrier(2)
        release = threading.Event()

        def first(_: dict[str, object]) -> str:
            started.wait(timeout=1)
            release.wait(timeout=1)
            return "first"

        def second(_: dict[str, object]) -> dict[str, int]:
            started.wait(timeout=1)
            release.set()
            return {"position": 2}

        result = ToolExecutor(_registry(_tool("first", first), _tool("second", second))).execute_batch(
            (ProviderToolCallReady("call-1", "first", "{}"), ProviderToolCallReady("call-2", "second", "{}")), 2, lambda: True
        )

        self.assertEqual([call.call_id for call in result.tool_calls], ["call-1", "call-2"])
        self.assertEqual([observation.content for observation in result.observations], ["first", '{"position":2}'])

    def test_success_and_failure_are_isolated_without_secret_leakage(self):
        def fail(_: dict[str, object]) -> object:
            raise RuntimeError("secret-token at C:\\private")

        result = ToolExecutor(_registry(_tool("good", lambda _: 7), _tool("bad", fail))).execute_batch(
            (ProviderToolCallReady("good-id", "good", "{}"), ProviderToolCallReady("bad-id", "bad", "{}")), 2, lambda: True
        )

        self.assertEqual(result.observations[0].content, "7")
        self.assertTrue(result.observations[0].ok)
        self.assertFalse(result.observations[1].ok)
        self.assertEqual(result.observations[1].error_code, "tool_execution_failed")
        self.assertNotIn("secret", result.observations[1].content.lower())
        self.assertNotIn("private", result.observations[1].content.lower())

    def test_preflight_failures_invoke_zero_tools(self):
        invoked = []
        executor = ToolExecutor(_registry(_tool("known", lambda _: invoked.append(True))))
        invalid_batches = (
            (ProviderToolCallReady("id", "unknown", "{}"),),
            (ProviderToolCallReady("id", "known", "{"),),
            (ProviderToolCallReady("id", "known", "[]"),),
            (ProviderToolCallReady("id", "known", "{} trailing"),),
            (ProviderToolCallReady("id", "known", '{"value":"${later}"}'),),
            (ProviderToolCallReady("id", "known", '{"$ref":"later"}'),),
            (ProviderToolCallReady("id", "known", '{"value":NaN}'),),
            (ProviderToolCallReady("id", "known", '{"value":Infinity}'),),
            (ProviderToolCallReady("id", "known", '{"value":-Infinity}'),),
            (ProviderToolCallReady("same", "known", "{}"), ProviderToolCallReady("same", "known", "{}")),
        )
        for calls in invalid_batches:
            with self.subTest(calls=calls), self.assertRaises(ToolBatchValidationError):
                executor.execute_batch(calls, 2, lambda: True)
            self.assertEqual(invoked, [])
        with self.assertRaises(ToolBatchValidationError):
            executor.execute_batch((ProviderToolCallReady("id", "known", "{}"),), 0, lambda: True)
        self.assertEqual(invoked, [])

    def test_prepare_batch_validates_without_invoking_tools(self):
        invoked = []
        executor = ToolExecutor(_registry(_tool("known", lambda _: invoked.append(True))))

        with self.assertRaisesRegex(ToolBatchValidationError, "invalid_tool_arguments"):
            executor.prepare_batch((ProviderToolCallReady("id", "known", "not-json"),), 1)

        self.assertEqual(invoked, [])

    def test_execute_prepared_batch_uses_the_exact_preflight_result(self):
        resolved = []

        class CountingRegistry(ToolRegistry):
            def resolve(self, name):
                resolved.append(name)
                return super().resolve(name)

        executor = ToolExecutor(CountingRegistry((_tool("known", lambda _: "ok"),)))
        prepared = executor.prepare_batch((ProviderToolCallReady("id", "known", "{}"),), 1)

        result = executor.execute_prepared_batch(prepared, lambda: True)

        self.assertEqual(resolved, ["known"])
        self.assertEqual(result.observations[0].content, "ok")

    def test_batch_exceeding_remaining_budget_invokes_zero_tools(self):
        invoked = []
        executor = ToolExecutor(_registry(_tool("known", lambda _: invoked.append(True))))
        calls = (ProviderToolCallReady("one", "known", "{}"), ProviderToolCallReady("two", "known", "{}"))

        with self.assertRaises(ToolBatchValidationError) as raised:
            executor.execute_batch(calls, 1, lambda: True)

        self.assertEqual(raised.exception.code, "tool_budget_exceeded")
        self.assertEqual(invoked, [])

    def test_mutating_or_serial_tool_must_be_the_only_call_in_a_batch(self):
        invoked = []
        executor = ToolExecutor(_registry(
            _tool("read", lambda _: invoked.append("read")),
            _tool("write", lambda _: invoked.append("write"), read_only=False),
            _tool("serial", lambda _: invoked.append("serial"), parallel_safe=False),
        ))
        for name in ("write", "serial"):
            with self.subTest(name=name), self.assertRaises(ToolBatchValidationError) as raised:
                executor.execute_batch(
                    (ProviderToolCallReady("one", "read", "{}"), ProviderToolCallReady("two", name, "{}")), 2, lambda: True
                )
            self.assertEqual(raised.exception.code, "incompatible_tool_batch")
            self.assertEqual(invoked, [])

        executor.execute_batch((ProviderToolCallReady("one", "write", "{}"),), 1, lambda: True)
        executor.execute_batch((ProviderToolCallReady("two", "serial", "{}"),), 1, lambda: True)
        self.assertEqual(invoked, ["write", "serial"])

    def test_tool_argument_mutation_does_not_change_retained_tool_call(self):
        def mutate(arguments: dict[str, object]) -> str:
            arguments["nested"]["items"].append("mutated")  # type: ignore[index]
            return "ok"

        result = ToolExecutor(_registry(_tool("known", mutate))).execute_batch(
            (ProviderToolCallReady("id", "known", '{"nested":{"items":["original"]}}'),), 1, lambda: True
        )

        self.assertEqual(result.tool_calls[0].arguments, {"nested": {"items": ["original"]}})

    def test_peer_ask_uses_the_provider_call_id_without_exposing_it_to_the_model(self):
        received = []
        result = ToolExecutor(_registry(_tool("peer_ask", lambda arguments: received.append(arguments) or "ok"))).execute_batch(
            (ProviderToolCallReady("provider-call-1", "peer_ask", '{"message":"hello"}'),), 1, lambda: True
        )

        self.assertEqual(received, [{"message": "hello", "call_id": "provider-call-1"}])
        self.assertEqual(result.tool_calls[0].arguments, {"message": "hello"})

    def test_cancellation_before_and_after_a_serial_side_effect_returns_nothing(self):
        executor = ToolExecutor(_registry(_tool("known", lambda _: "finished", read_only=False)))
        calls = (ProviderToolCallReady("id", "known", "{}"),)
        with self.assertRaises(ToolBatchCancelled) as before:
            executor.execute_batch(calls, 1, lambda: False)
        self.assertEqual(before.exception.code, "tool_batch_cancelled")

        active = [True]
        def finish_then_cancel(_: dict[str, object]) -> str:
            active[0] = False
            return "finished"

        executor = ToolExecutor(_registry(_tool("known", finish_then_cancel, read_only=False)))
        with self.assertRaises(ToolBatchCancelled):
            executor.execute_batch(calls, 1, lambda: active[0])
