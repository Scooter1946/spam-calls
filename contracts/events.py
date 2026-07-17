"""Canonical event-type vocabulary for PitchLoop.

Both the P1 orchestrator (which appends to ``events.jsonl``) and the P4 evidence
pipeline (``EvidencePort.publish_raw(event_type, ...)``) reference these names,
so they live in the shared contract. Values are stable strings; add new members
additively rather than renaming existing ones.
"""

from __future__ import annotations

from enum import Enum


class EventType(str, Enum):
    """Stable event-type names written to the run event log and evidence bus."""

    # --- run lifecycle / state machine ---
    RUN_STARTED = "run_started"
    STATE_ENTER = "state_enter"
    TRANSITION = "transition"
    RUN_FINISHED = "run_finished"
    ERROR = "error"

    # --- evidence-producing actions (mirror Evidence.kind where applicable) ---
    POLICY_DECISION = "policy_decision"
    ENRICHMENT_PURCHASED = "enrichment_purchased"
    RECEIPT_RECORDED = "receipt_recorded"
    CALL_PLACED = "call_placed"
    DIAGNOSIS = "diagnosis"
    ZERO_SEARCH = "zero_search"
    PROSPECTS_DISCOVERED = "prospects_discovered"
    TOOL_NEED_EVALUATED = "tool_need_evaluated"
    TOOL_AUTHORED = "tool_authored"
    TOOL_CONFORMANCE = "tool_conformance"
    PR_OPENED = "pr_opened"
    PR_MERGED = "pr_merged"
    TOOL_RELOADED = "tool_reloaded"
    TOOL_REUSED = "tool_reused"
    REFLECTION_RECORDED = "reflection_recorded"
    STRATEGY_UPDATED = "strategy_updated"
    CANDIDATE_COMPLETED = "candidate_completed"
    MEETING_BOOKED = "meeting_booked"

    def __str__(self) -> str:  # so f-strings / json emit the plain value
        return self.value
