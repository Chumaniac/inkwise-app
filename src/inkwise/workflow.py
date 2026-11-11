"""Real LangGraph orchestration over injected providers; no pretend AI fallback.

Providers own OCR, retrieval, scoring and reflection. Missing rules stop scoring.
This reference graph ends at teacher review; approval/publication require an
authenticated application and are intentionally not exposed as public actions.
"""

from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from inkwise.core import MAX_CHARACTERS, require_review


class State(TypedDict, total=False):
    """Minimal job state; raw student images stay outside checkpoint state."""

    text: str
    ocr_confirmed: bool
    rules_available: bool
    evidence_valid: bool
    initial: dict[str, float]
    revised: dict[str, float]
    disputed: bool
    status: str
    round: int


Node = Callable[[State], dict[str, Any]]


def build_workflow(*, ocr: Node, retrieve: Node, score: Node, reflect: Node):
    """Compile a four-role workflow with two-round maximum and fail-closed gates."""

    def validate(state: State) -> dict[str, Any]:
        text = state.get("text", "")
        if not text.strip() or len(text) > MAX_CHARACTERS:
            raise ValueError("Invalid text length")
        return {"round": 0, "status": "validated"}

    def reflect_bounded(state: State) -> dict[str, Any]:
        result = reflect(state)
        result["round"] = state["round"] + 1
        return result

    def decide(state: State) -> str:
        needs_review = require_review(
            state["initial"], state["revised"],
            rules_available=state.get("rules_available", False),
            ocr_confirmed=state.get("ocr_confirmed", False),
            evidence_valid=state.get("evidence_valid", False),
        )
        if needs_review and state["round"] < 2 and state.get("evidence_valid", False):
            return "score"
        return "teacher_review"

    graph = StateGraph(State)
    graph.add_node("validate", validate)
    graph.add_node("ocr", ocr)
    graph.add_node("retrieve", retrieve)
    graph.add_node("score", score)
    graph.add_node("reflect", reflect_bounded)
    graph.add_node("text_review", lambda s: {"status": "needs_text_review"})
    graph.add_node("rules_review", lambda s: {"status": "rules_unavailable"})
    graph.add_node("teacher_review", lambda s: {"status": "teacher_review"})
    graph.add_edge(START, "validate")
    graph.add_edge("validate", "ocr")
    graph.add_conditional_edges("ocr", lambda s: "retrieve" if s.get("ocr_confirmed") else "text_review")
    graph.add_conditional_edges("retrieve", lambda s: "score" if s.get("rules_available") else "rules_review")
    graph.add_edge("score", "reflect")
    graph.add_conditional_edges("reflect", decide)
    for name in ("text_review", "rules_review", "teacher_review"):
        graph.add_edge(name, END)
    return graph.compile()
