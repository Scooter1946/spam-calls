"""Frozen shared Pydantic models for PitchLoop.

These are copied verbatim (names and semantics) from the global integration
contract (§7). Keep them small and stable. Do not change a field after freeze
without explicit team approval; any change must be additive.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class RunSpec(BaseModel):
    """The specification the agent receives and plans against."""

    run_id: str
    goal: Literal["book_one_qualified_meeting"]
    product: str
    persona: str
    candidates: list[str]
    budget_cents: int
    policy_ref: str
    required_claims: list[Literal["fact_a", "fact_b"]]
    max_paid_calls: int = 2
    objective: str | None = None
    candidate_profiles: dict[str, dict[str, str]] = Field(default_factory=dict)


class Evidence(BaseModel):
    """A single normalized piece of evidence produced during a run.

    ``value`` holds the canonical claim payload; ``provenance`` and the raw
    provider output are preserved separately (see the runtime artifact contract).
    """

    evidence_id: str
    run_id: str
    candidate_id: str | None = None
    kind: Literal["policy", "enrichment", "receipt", "call", "tool", "repo", "meeting"]
    claim: str
    value: dict[str, Any]
    source: str
    source_ref: str | None = None
    occurred_at: datetime
    provenance: dict[str, Any] = Field(default_factory=dict)
    policy_decision: Literal["allow", "deny"] | None = None


class PolicyDecision(BaseModel):
    """Result of a real Pomerium authorization check."""

    allowed: bool
    status_code: int
    reason: str
    audit_ref: str | None = None
    raw_artifact_path: str | None = None


class ServiceMatch(BaseModel):
    """A candidate Zero.xyz service returned by capability search."""

    service_id: str
    name: str
    description: str
    price_cents: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PaidResult(BaseModel):
    """Outcome of a paid Zero.xyz invocation, with receipt and raw artifact."""

    ok: bool
    service_id: str
    result: dict[str, Any]
    receipt: dict[str, Any]
    amount_cents: int
    provider_ref: str | None = None
    raw_artifact_path: str


class CallResult(BaseModel):
    """Outcome of a paid call, driven by the deterministic callee rubric."""

    status: Literal["rejected", "booked", "failed"]
    code: str
    missing_claims: list[str] = Field(default_factory=list)
    transcript: str
    transcript_path: str
    receipt: dict[str, Any]
    amount_cents: int
    provider_ref: str | None = None


class ReflectionReceipt(BaseModel):
    """Evidence-backed learning recorded after one candidate call."""

    reflection_id: str
    run_id: str
    call_number: int
    candidate_id: str
    call_evidence_id: str
    call_code: str
    went_well: list[str]
    went_wrong: list[str]
    learned: list[str]
    next_change: list[str]
    missing_capability: str | None = None
    strategy_version_before: int
    strategy_version_after: int
    occurred_at: datetime


class StrategyReceipt(BaseModel):
    """One immutable campaign strategy version used by later candidates."""

    run_id: str
    version: int
    tactics: list[str]
    based_on_reflection_ids: list[str]
    occurred_at: datetime


class ActionReceipt(BaseModel):
    """Append-only history record for one agent observation or action."""

    action_id: str
    run_id: str
    action: str
    status: Literal["completed", "failed"]
    candidate_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    provider_ref: str | None = None
    amount_cents: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime


class PullRequest(BaseModel):
    """An agent-authored GitHub pull request (created via RepoPort)."""

    number: int
    url: str
    branch: str
    files: list[str]


class MergeResult(BaseModel):
    """Result of merging an agent-authored pull request."""

    merged: bool
    merge_sha: str | None = None
    url: str | None = None


class ToolManifest(BaseModel):
    """Manifest describing a generated tool the registry can load by capability."""

    name: str
    capability: Literal["fact_b"]
    entrypoint: str
    input_schema: dict[str, Any]
    output_claim: Literal["fact_b"]
    version: str
