"""The explicit PitchLoop state machine and mutable run state.

One state machine drives the whole loop. States are entered only via transitions
returned by handlers, and every transition must be justified by a returned model
or normalized evidence — never by a call counter or demo flag (global context
§"Required architecture").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from contracts.models import CallResult, Evidence, PullRequest, RunSpec


class State(str, Enum):
    LOAD_SPEC = "LOAD_SPEC"
    PLAN = "PLAN"
    SELECT_CANDIDATE = "SELECT_CANDIDATE"
    POLICY_CHECK = "POLICY_CHECK"
    EVALUATE_TOOLS = "EVALUATE_TOOLS"
    DISCOVER_FACT_A = "DISCOVER_FACT_A"
    PURCHASE_FACT_A = "PURCHASE_FACT_A"
    GENERATE_PITCH = "GENERATE_PITCH"
    CALL = "CALL"
    DIAGNOSE = "DIAGNOSE"
    REFLECT = "REFLECT"
    DISCOVER_FACT_B = "DISCOVER_FACT_B"
    AUTHOR_TOOL = "AUTHOR_TOOL"
    TEST_TOOL = "TEST_TOOL"
    OPEN_PR = "OPEN_PR"
    MERGE_PR = "MERGE_PR"
    RELOAD_TOOL = "RELOAD_TOOL"
    COLLECT_FACT_B = "COLLECT_FACT_B"
    REGENERATE_PITCH = "REGENERATE_PITCH"
    VERIFY_CALL = "VERIFY_CALL"
    FINALIZE = "FINALIZE"
    FAILED = "FAILED"

    def __str__(self) -> str:
        return self.value


TERMINAL_STATES: frozenset[State] = frozenset({State.FINALIZE, State.FAILED})


@dataclass
class RunState:
    """Mutable context carried across state transitions for one run."""

    spec: RunSpec
    ranked_candidates: list[str] = field(default_factory=list)
    tried_candidates: list[str] = field(default_factory=list)
    current_candidate: str | None = None
    called_candidates: list[str] = field(default_factory=list)

    fact_a: Evidence | None = None
    fact_b: Evidence | None = None
    pitch_text: str | None = None

    last_call: CallResult | None = None
    last_call_evidence: Evidence | None = None
    calls_placed: int = 0

    strategy_version: int = 1
    strategy_tactics: list[str] = field(default_factory=list)
    reflection_ids: list[str] = field(default_factory=list)

    tool_dir: str | None = None
    authored_files: list[str] = field(default_factory=list)
    repair_attempts: int = 0
    pr: PullRequest | None = None

    outcome: str | None = None
    failure_reason: str | None = None

    def untried_candidates(self) -> list[str]:
        return [c for c in self.ranked_candidates if c not in self.tried_candidates]
