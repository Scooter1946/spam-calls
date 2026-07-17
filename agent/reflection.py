"""Deterministic, evidence-backed reflection for the demo campaign."""

from __future__ import annotations

from datetime import datetime, timezone

from callee.call_harness import (
    BUSINESS_IMPACT_TACTIC,
    LOW_FRICTION_TACTIC,
    PILOT_TACTIC,
    PROOF_TACTIC,
)
from contracts.models import CallResult, ReflectionReceipt


_LESSONS = {
    "REJECTED_MISSING_FACT_A": (
        ["The opening and request were concise."],
        ["The pitch lacked business-specific evidence."],
        ["Research the business and its current web presence before contacting the next owner."],
        ["Use Zero.xyz business research before the next call."],
        "fact_a",
        None,
    ),
    "REJECTED_MISSING_FACT_B": (
        ["The business profile made the opening relevant."],
        ["The pitch had no evidence from the prospect's current website."],
        ["Business context is not enough; the agent needs a concrete website opportunity."],
        ["Search Zero.xyz for a website audit, then build one if the marketplace has no match."],
        "fact_b",
        None,
    ),
    "REJECTED_WEAK_VALUE": (
        ["The pitch used verified business and website evidence."],
        ["It did not connect those facts to more inquiries or bookings."],
        ["Translate website evidence into a concrete small-business outcome."],
        [f"Add: {BUSINESS_IMPACT_TACTIC}"],
        None,
        BUSINESS_IMPACT_TACTIC,
    ),
    "REJECTED_NO_PROOF": (
        ["The business impact was clear."],
        ["The recipient wanted proof before investing time."],
        ["A relevant before-and-after is more credible than an unsupported promise."],
        [f"Add: {PROOF_TACTIC}"],
        None,
        PROOF_TACTIC,
    ),
    "REJECTED_HIGH_FRICTION": (
        ["The evidence, impact, and proof were relevant."],
        ["The proposal sounded like a disruptive full-site rebuild."],
        ["Offer a bounded first page and contact-flow refresh."],
        [f"Add: {LOW_FRICTION_TACTIC}"],
        None,
        LOW_FRICTION_TACTIC,
    ),
    "REJECTED_TIMING": (
        ["The recipient understood the value and low-risk approach."],
        ["A broad website project did not fit the owner's current schedule."],
        ["Offer a one-page scope session before asking for a rebuild."],
        [f"Add: {PILOT_TACTIC}"],
        None,
        PILOT_TACTIC,
    ),
    "MEETING_BOOKED": (
        ["Verified business research, a website audit, business impact, proof, low friction, and a bounded first step aligned."],
        [],
        ["Keep the current strategy for similar local business owners."],
        ["Preserve the successful strategy and stop the campaign."],
        None,
        None,
    ),
}


def reflect_on_call(
    *,
    run_id: str,
    call_number: int,
    candidate_id: str,
    call: CallResult,
    call_evidence_id: str,
    strategy_version: int,
) -> tuple[ReflectionReceipt, str | None]:
    lesson = _LESSONS.get(
        call.code,
        (
            [],
            ["The call did not produce a recognized outcome."],
            ["Preserve the failure receipt and avoid assuming success."],
            ["Review provider evidence before the next candidate."],
            call.missing_claims[0] if call.missing_claims else None,
            None,
        ),
    )
    went_well, went_wrong, learned, next_change, missing_capability, tactic = lesson
    changes_strategy = call.status != "booked"
    receipt = ReflectionReceipt(
        reflection_id=f"reflection-{call_number:03d}",
        run_id=run_id,
        call_number=call_number,
        candidate_id=candidate_id,
        call_evidence_id=call_evidence_id,
        call_code=call.code,
        went_well=went_well,
        went_wrong=went_wrong,
        learned=learned,
        next_change=next_change,
        missing_capability=missing_capability,
        strategy_version_before=strategy_version,
        strategy_version_after=strategy_version + (1 if changes_strategy else 0),
        occurred_at=datetime.now(timezone.utc),
    )
    return receipt, tactic
