from __future__ import annotations

from typing import Any, Callable


def linear_turn_graph(nodes: list[Callable[[dict[str, Any]], dict[str, Any]]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def invoke(state: dict[str, Any]) -> dict[str, Any]:
        current = dict(state)
        for node in nodes:
            current.update(node(current))
        return current

    return invoke


def build_langgraph_or_linear(nodes: list[tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]]):
    """build a langgraph state graph when langgraph is installed, otherwise use the same nodes linearly.

    milestone 1 keeps tests stdlib-only, but the production seam is already langgraph-shaped.
    """
    try:
        from langgraph.graph import END, StateGraph  # type: ignore
        from typing import TypedDict

        class GraphState(TypedDict, total=False):
            envelope: Any
            session: Any
            soul: Any
            conversation_result: Any
            bubbles: Any

        state_schema = GraphState
    except Exception:
        return linear_turn_graph([node for _, node in nodes])

    graph = StateGraph(state_schema)
    for name, node in nodes:
        graph.add_node(name, node)
    graph.set_entry_point(nodes[0][0])
    for (name, _), (next_name, _) in zip(nodes, nodes[1:]):
        graph.add_edge(name, next_name)
    graph.add_edge(nodes[-1][0], END)
    return graph.compile().invoke
