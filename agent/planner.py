"""Deterministic P0 planner and candidate ranking.

The plan order is fixed (policy check -> Fact A enrichment -> pitch -> call).
Candidate ranking uses *scenario priority* — the order in which the scenario
lists candidates — rather than any hardcoded ``if candidate == "alex_rivera"``
transition logic. The scenario places the to-be-denied contact first so the loop
provably exercises a real policy denial before selecting an allowed contact.
"""

from __future__ import annotations

from pydantic import BaseModel

from contracts.models import RunSpec


class PlanStep(BaseModel):
    name: str
    description: str


class Plan(BaseModel):
    run_id: str
    goal: str
    ranked_candidates: list[str]
    steps: list[PlanStep]


#: Fixed P0 plan skeleton. The dynamic diagnose/author/retry loop is not part of
#: the static plan; it is driven at runtime by observations.
_P0_STEPS: list[PlanStep] = [
    PlanStep(name="policy_check", description="Authorize the top-ranked candidate via PolicyPort (expect a real denial first)."),
    PlanStep(name="discover_fact_a", description="Search Zero for a company-enrichment service."),
    PlanStep(name="purchase_fact_a", description="Invoke the selected Zero service within budget; publish Fact A."),
    PlanStep(name="generate_pitch", description="Render the first pitch from normalized evidence."),
    PlanStep(name="call", description="Place the first paid call and observe the rubric outcome."),
]


def rank_candidates(spec: RunSpec) -> list[str]:
    """Rank candidates by scenario priority (the order given in the spec).

    De-duplicates while preserving order. This is intentionally *data-driven*:
    the scenario decides who is tried first.
    """

    seen: set[str] = set()
    ranked: list[str] = []
    for candidate in spec.candidates:
        if candidate not in seen:
            seen.add(candidate)
            ranked.append(candidate)
    return ranked


def build_plan(spec: RunSpec) -> Plan:
    """Produce the deterministic P0 plan for a run."""

    return Plan(
        run_id=spec.run_id,
        goal=spec.goal,
        ranked_candidates=rank_candidates(spec),
        steps=list(_P0_STEPS),
    )
