from __future__ import annotations

import sys
import unittest
from typing import Any
from unittest.mock import patch

from tomo_core.graph import build_langgraph_or_linear, linear_turn_graph


def _nodes() -> list[tuple[str, Any]]:
    def load_context(state: dict[str, Any]) -> dict[str, Any]:
        return {"memory_data": "remember this"}

    def run_conversation(state: dict[str, Any]) -> dict[str, Any]:
        assert state["memory_data"] == "remember this"
        return {"conversation_result": "answer"}

    return [("load_context", load_context), ("run_conversation", run_conversation)]


class GraphTests(unittest.TestCase):
    def test_langgraph_preserves_state_between_sequential_nodes(self) -> None:
        result = build_langgraph_or_linear(_nodes())({"envelope": "input"})

        self.assertEqual(result["memory_data"], "remember this")
        self.assertEqual(result["conversation_result"], "answer")

    def test_langgraph_allows_final_node_to_return_none_and_preserves_updates(self) -> None:
        def first(state: dict[str, Any]) -> dict[str, Any]:
            return {"conversation_result": "answer"}

        def final(state: dict[str, Any]) -> None:
            return None

        result = build_langgraph_or_linear([("first", first), ("final", final)])({"envelope": "input"})

        self.assertEqual(result, {"envelope": "input", "conversation_result": "answer"})

    def test_missing_langgraph_uses_equivalent_linear_fallback(self) -> None:
        with patch.dict(sys.modules, {"langgraph.graph": None}):
            result = build_langgraph_or_linear(_nodes())({"envelope": "input"})

        self.assertEqual(result, {"envelope": "input", "memory_data": "remember this", "conversation_result": "answer"})

    def test_linear_graph_ignores_none_updates_and_preserves_state(self) -> None:
        def first(state: dict[str, Any]) -> dict[str, Any]:
            return {"memory_data": "remember this"}

        def final(state: dict[str, Any]) -> None:
            return None

        result = linear_turn_graph([first, final])({"envelope": "input"})

        self.assertEqual(result, {"envelope": "input", "memory_data": "remember this"})

    def test_empty_graph_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one node"):
            linear_turn_graph([])

        with self.assertRaisesRegex(ValueError, "at least one node"):
            build_langgraph_or_linear([])


if __name__ == "__main__":
    unittest.main()
