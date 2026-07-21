from __future__ import annotations

from typing import Any, Callable, TypedDict, cast


class GraphState(TypedDict, total=False):
    envelope: Any
    session: Any
    soul: Any
    memory_data: Any
    conversation_result: Any
    bubbles: Any


NodeResult = dict[str, Any] | None
Node = Callable[[dict[str, Any]], NodeResult]


def linear_turn_graph(nodes: list[Node]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    if not nodes:
        raise ValueError("graph requires at least one node")

    def invoke(state: dict[str, Any]) -> dict[str, Any]:
        current = dict(state)
        for node in nodes:
            updates = node(current)
            if updates is not None:
                current.update(updates)
        return current

    return invoke


def build_langgraph_or_linear(nodes: list[tuple[str, Node]]):
    """build a langgraph state graph when langgraph is installed, otherwise use the same nodes linearly.

    milestone 1 keeps tests stdlib-only, but the production seam is already langgraph-shaped.
    """
    if not nodes:
        raise ValueError("graph requires at least one node")

    try:
        from langgraph.graph import END, StateGraph
    except ImportError:
        return linear_turn_graph([node for _, node in nodes])

    graph = StateGraph(GraphState)
    for name, node in nodes:
        # LangGraph's stubs do not accept callbacks that may return None.
        graph.add_node(name, cast(Any, node))
    graph.set_entry_point(nodes[0][0])
    for (name, _), (next_name, _) in zip(nodes, nodes[1:]):
        graph.add_edge(name, next_name)
    graph.add_edge(nodes[-1][0], END)
    return graph.compile().invoke
