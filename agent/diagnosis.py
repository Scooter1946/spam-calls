"""Diagnosis computed from normalized Evidence (never from raw transcripts).

After a call, the orchestrator asks ``EvidencePort.query(run_id)`` for the
normalized evidence and passes it here. Diagnosis reports which required claims
are present/missing and what to do next. In live mode this is the ONLY basis for
the next state — raw transcript files must not drive the decision.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from contracts.models import Evidence, RunSpec

#: Evidence kinds that represent an *obtained* fact/claim.
FACT_KINDS: frozenset[str] = frozenset({"enrichment", "tool"})


class Diagnosis(BaseModel):
    present_claims: list[str]
    missing_claims: list[str]
    evidence_ids: list[str]
    next_action: Literal["discover_capability", "retry_call", "finalize"]


def build_diagnosis(spec: RunSpec, evidence: list[Evidence]) -> Diagnosis:
    """Diagnose required-claim coverage from normalized evidence.

    ``evidence_ids`` cites the normalized call evidence together with the
    fact evidence that established each present claim (so a Call #1 diagnosis
    references both the call record and the Fact A evidence).
    """

    required = list(spec.required_claims)

    present: list[str] = []
    fact_ids: list[str] = []
    for ev in evidence:
        if ev.claim in required and ev.kind in FACT_KINDS:
            fact_ids.append(ev.evidence_id)
            if ev.claim not in present:
                present.append(ev.claim)

    missing = [claim for claim in required if claim not in present]
    call_ids = [ev.evidence_id for ev in evidence if ev.kind == "call"]
    meeting_booked = any(ev.kind == "meeting" for ev in evidence)

    if missing:
        # A required claim is still absent; the loop must acquire a capability
        # to produce it (for fact_b, this means authoring a tool).
        next_action: Literal["discover_capability", "retry_call", "finalize"] = (
            "discover_capability"
        )
    elif meeting_booked:
        next_action = "finalize"
    else:
        # All required claims are present but no meeting yet -> try the call.
        next_action = "retry_call"

    # De-duplicate evidence ids while preserving order (call first, then facts).
    ordered_ids: list[str] = []
    for eid in call_ids + fact_ids:
        if eid not in ordered_ids:
            ordered_ids.append(eid)

    return Diagnosis(
        present_claims=present,
        missing_claims=missing,
        evidence_ids=ordered_ids,
        next_action=next_action,
    )
