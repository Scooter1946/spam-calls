"""The PitchLoop orchestrator: one explicit, observation-driven state machine.

Every transition is justified by a returned model or normalized evidence — never
by a call counter or demo flag. Adapters are injected as ports (:class:`Deps`) so
the same orchestrator runs against fakes (P1's end-to-end test) or the live
P2/P3/P4 adapters. Each state persists its artifacts and appends an event; all
failures become artifacts rather than silent crashes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent.artifacts import Artifacts
from agent.diagnosis import build_diagnosis
from agent.planner import build_plan
from agent.state import TERMINAL_STATES, RunState, State
from agent.tool_author import (
    CANONICAL_MANIFEST,
    AuthorPort,
    AuthorRequest,
    ConformanceResult,
    assert_only_allowed_paths,
)
from contracts.events import EventType
from contracts.models import (
    CallResult,
    Evidence,
    PaidResult,
    PolicyDecision,
    RunSpec,
    ServiceMatch,
)
from contracts.ports import CallPort, EvidencePort, PolicyPort, RepoPort, ZeroPort
from integrations.zero_client import select_within_budget

FACT_KINDS = ("enrichment", "tool")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Configuration + dependency container
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    fact_a_capability: str
    fact_b_capability: str
    expected_fact_b_phrase: str = "August 30 API v1 migration deadline"
    conformance_command: str = "pytest -q conformance/test_generated_tool.py"
    fixture_url: str = "http://127.0.0.1:8088"
    author_tool_dir: str = "generated_tools"
    reload_tools_dir: str = "generated_tools"
    allowed_tool_paths: list[str] = field(
        default_factory=lambda: [
            "generated_tools/fact_b_tool.py",
            "generated_tools/fact_b_tool.manifest.json",
            "generated_tools/test_fact_b_tool.py",
        ]
    )
    policy_action: str = "place_sales_call"
    expected_call_cents: int = 200
    max_steps: int = 100


@dataclass
class Deps:
    zero: ZeroPort
    policy: PolicyPort
    evidence: EvidencePort
    repo: RepoPort
    registry: Any  # ToolRegistryPort (reload/find)
    author: AuthorPort
    artifacts: Artifacts
    render_pitch: Callable[[RunSpec, str, list[Evidence]], str]
    build_call_port: Callable[..., CallPort]
    run_conformance: Callable[[str], ConformanceResult]
    git_status: Callable[[], list[str]]


class BudgetLedger:
    """Tracks spend from receipts (never from assumptions)."""

    def __init__(self, budget_cents: int) -> None:
        self.budget_cents = budget_cents
        self.spent_cents = 0
        self.entries: list[dict[str, Any]] = []

    def remaining(self) -> int:
        return self.budget_cents - self.spent_cents

    def can_afford(self, price_cents: int | None) -> bool:
        if price_cents is None:
            return self.remaining() > 0
        return self.remaining() >= price_cents

    def record(self, amount_cents: int, memo: str) -> None:
        self.spent_cents += amount_cents
        self.entries.append({"amount_cents": amount_cents, "memo": memo})

    def over_budget(self) -> bool:
        return self.spent_cents > self.budget_cents


class RunResult:
    def __init__(self, state: RunState, final_state: State, budget: BudgetLedger) -> None:
        self.outcome = state.outcome
        self.final_state = final_state
        self.spent_cents = budget.spent_cents
        self.budget_cents = budget.budget_cents
        self.calls_placed = state.calls_placed
        self.failure_reason = state.failure_reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "final_state": str(self.final_state),
            "spent_cents": self.spent_cents,
            "budget_cents": self.budget_cents,
            "calls_placed": self.calls_placed,
            "failure_reason": self.failure_reason,
        }


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


class Orchestrator:
    def __init__(self, spec: RunSpec, deps: Deps, config: Config) -> None:
        self.spec = spec
        self.deps = deps
        self.config = config
        self.state = RunState(spec=spec)
        self.budget = BudgetLedger(spec.budget_cents)
        self.call_port: CallPort | None = None
        self._selected_service: ServiceMatch | None = None
        self._author_request: AuthorRequest | None = None
        self._handlers: dict[State, Callable[[], State]] = {
            State.LOAD_SPEC: self._load_spec,
            State.PLAN: self._plan,
            State.SELECT_CANDIDATE: self._select_candidate,
            State.POLICY_CHECK: self._policy_check,
            State.DISCOVER_FACT_A: self._discover_fact_a,
            State.PURCHASE_FACT_A: self._purchase_fact_a,
            State.GENERATE_PITCH: self._generate_pitch,
            State.CALL: self._call,
            State.DIAGNOSE: self._diagnose,
            State.DISCOVER_FACT_B: self._discover_fact_b,
            State.AUTHOR_TOOL: self._author_tool,
            State.TEST_TOOL: self._test_tool,
            State.OPEN_PR: self._open_pr,
            State.MERGE_PR: self._merge_pr,
            State.RELOAD_TOOL: self._reload_tool,
            State.COLLECT_FACT_B: self._collect_fact_b,
            State.REGENERATE_PITCH: self._regenerate_pitch,
            State.VERIFY_CALL: self._verify_call,
        }

    # -- driver ------------------------------------------------------------ #

    def run(self) -> RunResult:
        self._emit(EventType.RUN_STARTED, {"run_id": self.spec.run_id, "goal": self.spec.goal})
        state = State.LOAD_SPEC
        steps = 0
        while state not in TERMINAL_STATES:
            steps += 1
            if steps > self.config.max_steps:
                state = self._fail(f"exceeded max steps ({self.config.max_steps})")
                break
            self._emit(EventType.STATE_ENTER, {"state": str(state)})
            try:
                next_state = self._handlers[state]()
            except Exception as exc:  # noqa: BLE001 - failures become artifacts
                next_state = self._fail(f"{state} raised {type(exc).__name__}: {exc}")
            self._emit(EventType.TRANSITION, {"from": str(state), "to": str(next_state)})
            state = next_state

        self._finish(state)
        return RunResult(self.state, state, self.budget)

    # -- states ------------------------------------------------------------ #

    def _load_spec(self) -> State:
        self.deps.artifacts.write_json("spec.json", self.spec)
        return State.PLAN

    def _plan(self) -> State:
        plan = build_plan(self.spec)
        self.state.ranked_candidates = plan.ranked_candidates
        self.deps.artifacts.write_json("plan.json", plan)
        return State.SELECT_CANDIDATE

    def _select_candidate(self) -> State:
        untried = self.state.untried_candidates()
        if not untried:
            return self._fail("no remaining candidates to try")
        self.state.current_candidate = untried[0]
        self.state.tried_candidates.append(untried[0])
        self._emit(EventType.STATE_ENTER, {"selected_candidate": untried[0]})
        return State.POLICY_CHECK

    def _policy_check(self) -> State:
        candidate = self.state.current_candidate
        assert candidate is not None
        decision: PolicyDecision = self.deps.policy.authorize(
            self.config.policy_action, candidate, {"run_id": self.spec.run_id}
        )
        rel = "policy/deny.json" if not decision.allowed else "policy/allow.json"
        self.deps.artifacts.write_json(rel, decision)
        self._publish_evidence(
            kind="policy",
            claim="policy",
            value={"status_code": decision.status_code, "reason": decision.reason},
            source="pomerium",
            candidate_id=candidate,
            policy_decision="allow" if decision.allowed else "deny",
            event_type=EventType.POLICY_DECISION,
        )
        if not decision.allowed:
            # Observation-driven: a denial routes back to try the next candidate.
            return State.SELECT_CANDIDATE
        return State.DISCOVER_FACT_A

    def _discover_fact_a(self) -> State:
        matches = self.deps.zero.search(self.config.fact_a_capability)
        self.deps.artifacts.write_json(
            "zero/search_fact_a.json",
            {"capability": self.config.fact_a_capability, "matches": [m.model_dump() for m in matches]},
        )
        self._emit(EventType.ZERO_SEARCH, {"capability": self.config.fact_a_capability, "n": len(matches)})
        if not matches:
            return self._fail("no Zero enrichment service found for Fact A")
        service = select_within_budget(matches, self.budget.remaining())
        if service is None:
            return self._fail("no affordable Zero service within budget for Fact A")
        self._selected_service = service
        return State.PURCHASE_FACT_A

    def _purchase_fact_a(self) -> State:
        service = self._selected_service
        assert service is not None
        if not self.budget.can_afford(service.price_cents):
            return self._fail(
                f"budget {self.budget.remaining()}c cannot afford service {service.price_cents}c"
            )
        result: PaidResult = self.deps.zero.invoke(
            service, {"candidate_id": self.state.current_candidate}
        )
        self.budget.record(result.amount_cents, f"zero:{service.service_id}")
        self.deps.artifacts.write_json("zero/fact_a_receipt.json", result.receipt)
        if self.budget.over_budget():
            return self._fail("budget exceeded after Fact A purchase")
        if not result.ok:
            return self._fail("Zero invocation for Fact A failed")

        statement = result.result.get("statement")
        self.state.fact_a = self._publish_evidence(
            kind="enrichment",
            claim="fact_a",
            value={"statement": statement},
            source=result.result.get("source", service.service_id),
            candidate_id=self.state.current_candidate,
            source_ref=result.provider_ref,
            provenance={"service_id": service.service_id, "raw": result.raw_artifact_path},
            event_type=EventType.ENRICHMENT_PURCHASED,
        )
        return State.GENERATE_PITCH

    def _generate_pitch(self) -> State:
        facts = self._fact_evidence()
        candidate = self.state.current_candidate
        assert candidate is not None
        pitch = self.deps.render_pitch(self.spec, candidate, facts)
        self.state.pitch_text = pitch
        self.deps.artifacts.write_text("pitch/pitch_1.md", pitch)
        # Build the call port now that Fact A is known (mirrors P2's factory).
        assert self.state.fact_a is not None
        self.call_port = self.deps.build_call_port(
            zero_port=self.deps.zero,
            artifacts=self.deps.artifacts,
            expected_fact_a=self.state.fact_a.value["statement"],
            expected_fact_b_phrase=self.config.expected_fact_b_phrase,
        )
        return State.CALL

    def _call(self) -> State:
        return self._place_call(pitch=self.state.pitch_text or "")

    def _diagnose(self) -> State:
        normalized = self.deps.evidence.query(self.spec.run_id)
        diagnosis = build_diagnosis(self.spec, normalized)
        self.deps.artifacts.write_json("evidence/diagnosis.json", diagnosis)
        self._emit(
            EventType.DIAGNOSIS,
            {
                "present": diagnosis.present_claims,
                "missing": diagnosis.missing_claims,
                "next_action": diagnosis.next_action,
                "evidence_ids": diagnosis.evidence_ids,
            },
        )
        if diagnosis.next_action == "discover_capability":
            if "fact_b" in diagnosis.missing_claims:
                return State.DISCOVER_FACT_B
            if "fact_a" in diagnosis.missing_claims:
                return State.DISCOVER_FACT_A
            return self._fail(f"unknown missing claims: {diagnosis.missing_claims}")
        if diagnosis.next_action == "retry_call":
            return State.REGENERATE_PITCH
        return State.FINALIZE

    def _discover_fact_b(self) -> State:
        matches = self.deps.zero.search(self.config.fact_b_capability)
        # Preserve the (expected) no-match result as canonical evidence.
        self.deps.artifacts.write_json(
            "zero/search_fact_b.json",
            {
                "capability": self.config.fact_b_capability,
                "matches": [m.model_dump() for m in matches],
                "no_match": not matches,
            },
        )
        self._emit(EventType.ZERO_SEARCH, {"capability": self.config.fact_b_capability, "n": len(matches)})
        # No service exists -> the loop must author its own tool.
        if not matches:
            return State.AUTHOR_TOOL
        # Unexpected: a service exists. P0 still authors the tool (no purchase
        # path for Fact B is in scope); record the anomaly.
        self._emit(EventType.ERROR, {"where": "discover_fact_b", "unexpected_matches": len(matches)})
        return State.AUTHOR_TOOL

    def _author_tool(self) -> State:
        request = self._build_author_request()
        self._author_request = request
        result = self.deps.author.author(request)
        self.state.tool_dir = request.tool_dir
        self.state.authored_files = result.files

        # Path safety: reject any working-tree change outside the allowed files.
        changed = self.deps.git_status()
        assert_only_allowed_paths(changed, request.allowed_paths)

        self.deps.artifacts.write_json(
            "tools/generated_manifest.json",
            {"manifest": request.manifest, "files": result.files, "mode": result.mode},
        )
        self.deps.artifacts.write_text("tools/author_prompt.txt", result.prompt)
        self._emit(EventType.TOOL_AUTHORED, {"files": result.files, "mode": result.mode})
        return State.TEST_TOOL

    def _test_tool(self) -> State:
        assert self.state.tool_dir is not None
        request = self._author_request
        assert request is not None

        conformance = self.deps.run_conformance(self.state.tool_dir)
        if conformance.exit_code != 0 and self.state.repair_attempts < 1:
            # Exactly one automated repair attempt, fed the exact test output.
            self.state.repair_attempts += 1
            self._emit(EventType.TOOL_CONFORMANCE, {"attempt": "initial", "exit_code": conformance.exit_code})
            self.deps.author.repair(request, conformance.output)
            changed = self.deps.git_status()
            assert_only_allowed_paths(changed, request.allowed_paths)
            conformance = self.deps.run_conformance(self.state.tool_dir)

        self.deps.artifacts.write_json("tools/conformance_result.json", conformance)
        self._emit(
            EventType.TOOL_CONFORMANCE,
            {"exit_code": conformance.exit_code, "repairs": self.state.repair_attempts},
        )
        if conformance.exit_code != 0:
            return self._fail("generated tool failed conformance after one repair attempt")
        return State.OPEN_PR

    def _open_pr(self) -> State:
        pr = self.deps.repo.create_agent_pr(
            files=self.state.authored_files,
            title="agent: add fact_b retrieval tool",
            body="Auto-authored tool for capability fact_b (PitchLoop self-improvement loop).",
        )
        self.state.pr = pr
        self.deps.artifacts.write_json("repo/pr.json", pr)
        self._emit(EventType.PR_OPENED, {"number": pr.number, "url": pr.url})
        return State.MERGE_PR

    def _merge_pr(self) -> State:
        assert self.state.pr is not None
        merge = self.deps.repo.merge(self.state.pr)
        self.deps.artifacts.write_json("repo/merge.json", merge)
        self._emit(EventType.PR_MERGED, {"merged": merge.merged, "sha": merge.merge_sha})
        if not merge.merged:
            return self._fail("agent PR did not merge")
        return State.RELOAD_TOOL

    def _reload_tool(self) -> State:
        self.deps.registry.reload()
        handle = self.deps.registry.find("fact_b")
        self.deps.artifacts.write_json(
            "tools/reload.json",
            {"found_fact_b": handle is not None},
        )
        self._emit(EventType.TOOL_RELOADED, {"found_fact_b": handle is not None})
        if handle is None:
            return self._fail("fact_b capability not found after reload")
        return State.COLLECT_FACT_B

    def _collect_fact_b(self) -> State:
        handle = self.deps.registry.find("fact_b")
        if handle is None:
            return self._fail("fact_b tool disappeared before collection")
        candidate = self.state.current_candidate
        assert candidate is not None
        payload = handle.run(candidate) if hasattr(handle, "run") else handle(candidate)
        statement = (payload or {}).get("value", {}).get("statement")
        if not statement:
            return self._fail("fact_b tool returned no statement for candidate")
        self.state.fact_b = self._publish_evidence(
            kind="tool",
            claim="fact_b",
            value={"statement": statement},
            source=payload.get("source", "generated_fact_b_tool"),
            candidate_id=candidate,
            provenance=payload.get("provenance", {}),
            event_type=EventType.RECEIPT_RECORDED,
        )
        return State.REGENERATE_PITCH

    def _regenerate_pitch(self) -> State:
        facts = self._fact_evidence()
        candidate = self.state.current_candidate
        assert candidate is not None
        pitch = self.deps.render_pitch(self.spec, candidate, facts)
        self.state.pitch_text = pitch
        self.deps.artifacts.write_text("pitch/pitch_2.md", pitch)
        return State.VERIFY_CALL

    def _verify_call(self) -> State:
        return self._place_call(pitch=self.state.pitch_text or "")

    # -- shared call logic (identical for both calls — no count branching) - #

    def _place_call(self, *, pitch: str) -> State:
        if self.state.calls_placed >= self.spec.max_paid_calls:
            return self._fail(f"paid call limit reached ({self.spec.max_paid_calls})")
        if not self.budget.can_afford(self.config.expected_call_cents):
            return self._fail("budget cannot afford another paid call")
        assert self.call_port is not None
        candidate = self.state.current_candidate
        assert candidate is not None

        result: CallResult = self.call_port.place_call(candidate, pitch)
        self.state.calls_placed += 1
        self.state.last_call = result
        self.budget.record(result.amount_cents, f"call:{candidate}")

        n = self.state.calls_placed
        self.deps.artifacts.write_json(f"calls/call_{n}_result.json", result)
        self._publish_evidence(
            kind="call",
            claim="call",
            value={"status": result.status, "code": result.code, "missing": result.missing_claims},
            source="call_adapter",
            candidate_id=candidate,
            source_ref=result.provider_ref,
            event_type=EventType.CALL_PLACED,
        )
        if self.budget.over_budget():
            return self._fail("budget exceeded after paid call")

        # Outcome is driven by the rubric result, not by which call this was.
        if result.status == "booked":
            return State.FINALIZE
        return State.DIAGNOSE

    # -- terminal ---------------------------------------------------------- #

    def _finalize_meeting(self) -> None:
        call = self.state.last_call
        assert call is not None and call.status == "booked"
        meeting_ev = self._publish_evidence(
            kind="meeting",
            claim="meeting",
            value={"booked": True, "code": call.code},
            source="call_adapter",
            candidate_id=self.state.current_candidate,
            source_ref=call.provider_ref,
            event_type=EventType.MEETING_BOOKED,
        )
        self.deps.artifacts.write_json(
            "final/meeting.json",
            {
                "candidate_id": self.state.current_candidate,
                "code": call.code,
                "evidence_id": meeting_ev.evidence_id,
                "transcript_path": call.transcript_path,
                "spent_cents": self.budget.spent_cents,
            },
        )
        self.state.outcome = "MEETING_BOOKED"

    def _finish(self, final_state: State) -> None:
        if final_state == State.FINALIZE:
            self._finalize_meeting()
        self._write_artifact_index()
        self._emit(
            EventType.RUN_FINISHED,
            {
                "final_state": str(final_state),
                "outcome": self.state.outcome,
                "spent_cents": self.budget.spent_cents,
                "failure_reason": self.state.failure_reason,
            },
        )

    def _fail(self, reason: str) -> State:
        self.state.failure_reason = reason
        self.state.outcome = "FAILED"
        self.deps.artifacts.write_json("final/failure.json", {"reason": reason, "state": self.state.outcome})
        self._emit(EventType.ERROR, {"reason": reason})
        return State.FAILED

    def _write_artifact_index(self) -> None:
        run_dir = self.deps.artifacts.run_dir
        paths: list[str] = []
        if run_dir.is_dir():
            for p in sorted(run_dir.rglob("*")):
                if p.is_file():
                    paths.append(str(p.relative_to(run_dir)))
        self.deps.artifacts.write_json(
            "final/artifact_index.json",
            {"run_id": self.spec.run_id, "outcome": self.state.outcome, "artifacts": paths},
        )

    # -- helpers ----------------------------------------------------------- #

    def _fact_evidence(self) -> list[Evidence]:
        out: list[Evidence] = []
        for ev in self.deps.evidence.query(self.spec.run_id):
            if ev.claim in self.spec.required_claims and ev.kind in FACT_KINDS:
                out.append(ev)
        return out

    def _build_author_request(self) -> AuthorRequest:
        manifest = dict(CANONICAL_MANIFEST)
        failed_search = self.deps.artifacts.path_for("zero/search_fact_b.json")
        return AuthorRequest(
            run_id=self.spec.run_id,
            tool_dir=self.config.author_tool_dir,
            allowed_paths=self.config.allowed_tool_paths,
            function_contract=(
                "def run(candidate_id: str) -> dict:  # return canonical fact_b evidence payload"
            ),
            manifest=manifest,
            fixture_url=self.config.fixture_url,
            response_schema={"statement": "string", "source": "string", "url": "string"},
            failed_zero_search={"path": str(failed_search), "capability": self.config.fact_b_capability},
            conformance_command=self.config.conformance_command,
            canonical_value={
                "candidate_id": self.state.current_candidate,
                "claim": "fact_b",
                "value": {"statement": f"Northstar Systems has an {self.config.expected_fact_b_phrase}."},
                "source": "northstar_public_migration_signal",
                "provenance": {"url": self.config.fixture_url},
            },
        )

    def _publish_evidence(
        self,
        *,
        kind: str,
        claim: str,
        value: dict[str, Any],
        source: str,
        event_type: EventType,
        candidate_id: str | None = None,
        source_ref: str | None = None,
        provenance: dict[str, Any] | None = None,
        policy_decision: str | None = None,
    ) -> Evidence:
        payload = {
            "run_id": self.spec.run_id,
            "candidate_id": candidate_id,
            "kind": kind,
            "claim": claim,
            "value": value,
            "source": source,
            "source_ref": source_ref,
            "provenance": provenance or {},
            "policy_decision": policy_decision,
            "occurred_at": _now_iso(),
        }
        cid = self.deps.evidence.publish_raw(str(event_type), payload)
        evidence = self.deps.evidence.wait_for_evidence(cid)
        self._emit(event_type, {"evidence_id": evidence.evidence_id, "claim": claim, "kind": kind})
        return evidence

    def _emit(self, event_type: EventType, payload: dict[str, Any]) -> None:
        self.deps.artifacts.append_event({"type": str(event_type), **payload})
