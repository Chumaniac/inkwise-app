"""Production LangGraph grading flow with evidence validation and bounded debate."""

import asyncio
import re
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from inkwise.contracts import DimensionScore, Rubric
from inkwise.core import DIMENSIONS
from inkwise.errors import DomainError
from inkwise.providers import CallBudget, ModelGateway, reflect_scores, score_dimension
from inkwise.retrieval import HybridRetriever, Scope


class State(TypedDict, total=False):
    text: str
    prompt: str
    examples: list[dict]
    provenance: list[dict]
    warnings: list[str]
    initial: list[dict]
    scores: list[dict]
    reflections: list[dict]
    challenges: list[dict]
    round: int
    report: dict


def observable_checks(text: str,scores: list[dict]) -> dict:
    paragraphs=len([p for p in re.split(r"\n\s*\n",text.strip()) if p.strip()])
    flagged=[]
    if paragraphs==1 and "\n" not in text.strip():
        for score in scores:
            if re.search(r"each paragraph|every paragraph|multiple paragraphs|各个段落|多个段落|每个段落",score["reason"],re.I):
                flagged.append(score["dimension"])
    return {"paragraphsByBlankLines":paragraphs,"characters":len(text),"paragraphReasoningToReview":flagged}


def build_grading_graph(gateway: ModelGateway, retriever: HybridRetriever, scope: Scope,
                        rubric: Rubric, budget: CallBudget, notify, local_findings=None, feedback_pool=None):
    """Four-role coordination starts after OCR's human confirmation boundary."""
    async def retrieve(state):
        await notify("retrieval.started")
        bundle = await retriever.retrieve(scope, rubric, state["prompt"] + "\n" + state["text"][:1500])
        return {"examples": bundle.examples, "provenance": bundle.provenance,
                "warnings": bundle.warnings, "round": 0, "reflections": []}

    async def score(state):
        await notify("scoring.started")
        results = await asyncio.gather(*[score_dimension(gateway, text=state["text"], prompt=state["prompt"],
                rubric=rubric, dimension=dimension, examples=state["examples"], budget=budget,local_findings=local_findings)
                for dimension in DIMENSIONS], return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        payload = [result.model_dump(mode="json", by_alias=True) for result in results]
        return {"scores": payload, "initial": payload}

    async def reflect(state):
        await notify("reflection.started")
        try:
            reflection = await reflect_scores(gateway, text=state["text"], prompt=state["prompt"], rubric=rubric,
                            scores=[DimensionScore.model_validate({k:s[k] for k in ("dimension","band","reason","suggestion","evidence")}) for s in state["scores"]], budget=budget,feedback_pool=feedback_pool)
        except DomainError as exc:
            # Five evidence-validated draft scores can still be reviewed by the
            # teacher. A failed reflection is not presented as model agreement.
            return {"challenges":[],"round":2,
                    "warnings":[*state["warnings"],"REFLECTION_UNAVAILABLE_TEACHER_REVIEW"],
                    "reflections":state["reflections"]+[{"status":"unavailable","errorCode":exc.code}]}
        return {"challenges": [c.model_dump(by_alias=True) for c in reflection.challenges],
                "reflections": state["reflections"] + [reflection.model_dump(by_alias=True)],
                "round": state["round"] + 1}

    async def revise(state):
        await notify("revision.started")
        scores = {s["dimension"]: s for s in state["scores"]}
        # Only challenged dimensions are repeated; successful independent scores are reused.
        for challenge in state["challenges"]:
            try:
                replacement = await score_dimension(gateway, text=state["text"], prompt=state["prompt"], rubric=rubric,
                               dimension=challenge["dimension"], examples=state["examples"], budget=budget, challenge=challenge,local_findings=local_findings)
            except DomainError:
                return {"scores":[scores[d] for d in DIMENSIONS],"round":2,
                        "warnings":[*state["warnings"],"REVISION_UNAVAILABLE_TEACHER_REVIEW"]}
            scores[replacement.dimension] = replacement.model_dump(by_alias=True)
        return {"scores": [scores[d] for d in DIMENSIONS]}

    async def arbitrate(state):
        await notify("arbitration.completed")
        initial = {s["dimension"]: s["band"] for s in state["initial"]}
        disputed = any(abs(s["band"] - initial[s["dimension"]]) >= 1 for s in state["scores"])
        warnings = list(state["warnings"])
        if state["challenges"] or disputed:
            warnings.append("TEACHER_ARBITRATION_REQUIRED")
        if rubric.synthetic:
            warnings.append("SYNTHETIC_RUBRIC_NOT_SCHOOL_STANDARD")
        from inkwise.orchestration import duplicate_feedback_groups
        duplicates=duplicate_feedback_groups(state['scores'])
        if duplicates:warnings.append('POSSIBLE_DUPLICATE_FEEDBACK_TEACHER_REVIEW')
        initial_total=sum(rubric.weights[key]*value/4 for key,value in initial.items())
        final_total=sum(rubric.weights[row['dimension']]*row['band']/4 for row in state['scores'])
        if abs(final_total-initial_total)>8:warnings.append('TEACHER_ARBITRATION_REQUIRED')
        observations=observable_checks(state["text"],state["scores"])
        if observations["paragraphReasoningToReview"]:
            warnings.append("PARAGRAPH_REASONING_REQUIRES_REVIEW")
        total = round(sum(rubric.weights[s["dimension"]] * s["band"] / 4 for s in state["scores"]), 2)
        return {"report": {"scores": state["scores"], "initialScores": state["initial"], "total": total,
                "reflections": state["reflections"], "warnings": sorted(set(warnings)),
                "duplicateFeedbackGroups":duplicates,"teacherFeedbackPool":feedback_pool or [],
                "evidenceProvenance": state["provenance"], "rubricId": rubric.id, "release": rubric.release,
                "model": gateway.settings.model_name, "textHash": scope.essay_hash,
                "requiresTeacherReview": True, "confidence": None, "modelCallsThisRun": budget.used,
                "observations":observations}}

    graph = StateGraph(State)
    for name, node in [("retrieve", retrieve), ("score", score), ("reflect", reflect),
                        ("revise", revise), ("arbitrate", arbitrate)]:
        graph.add_node(name, node)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "score")
    graph.add_edge("score", "reflect")
    graph.add_conditional_edges("reflect", lambda s: "revise" if s["challenges"] and s["round"] < 2 else "arbitrate")
    graph.add_conditional_edges("revise",lambda s:"arbitrate" if s["round"]>=2 else "reflect")
    graph.add_edge("arbitrate", END)
    return graph.compile()
