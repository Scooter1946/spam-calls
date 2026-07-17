"""In-memory fakes for every port, used by P1's end-to-end fake loop.

These let the orchestrator reach ``MEETING_BOOKED`` with no P2/P3/P4 code and no
network. Every fake is deterministic and driven only by the data it is given —
in particular the callee outcome comes from the rubric applied to the pitch text
(does it contain Fact A? Fact B?), never from a call counter.

Fakes live here (in P1's package) rather than in another owner's directory.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts.models import (
    CallResult,
    Evidence,
    MergeResult,
    PaidResult,
    PolicyDecision,
    PullRequest,
    RunSpec,
    ServiceMatch,
)
from agent.tool_author import (
    TOOL_MANIFEST_FILE,
    TOOL_MODULE_FILE,
    TOOL_TEST_FILE,
    AuthorRequest,
    AuthorResult,
    ConformanceResult,
    ToolAuthor,
    build_prompt,
)
from callee.call_harness import evaluate_campaign_pitch, render_conversation

FACT_A_STATEMENT = "Business research verified: this is an active local service business with an existing website."
FACT_B_STATEMENT = "The business's website is losing customer inquiries."
FACT_B_PHRASE = "website is losing customer inquiries"

BUSINESS_RESEARCH = {
    "alex_rivera": "Business research verified: Willow & Co Bakery serves walk-in and custom-order customers in Portland.",
    "nina_park": "Business research verified: Oak & Ember Bakery takes custom cake requests by phone in Tacoma.",
    "samir_patel": "Business research verified: Harbor Light Plumbing offers emergency residential service in Everett.",
    "carla_mendez": "Business research verified: Mendez Family Dental welcomes new families in Renton.",
    "ben_carter": "Business research verified: Riverbend Landscaping sells recurring yard care in Vancouver.",
    "tasha_green": "Business research verified: Cedar Lane Florist handles weddings and same-day bouquets in Bellevue.",
    "derek_wu": "Business research verified: Northline Auto Repair serves commuters and fleet customers in Shoreline.",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


class FakePolicyPort:
    """Denies a configured set of candidates (a stand-in Pomerium oracle)."""

    def __init__(self, denied: set[str] | None = None) -> None:
        self.denied = denied if denied is not None else {"alex_rivera"}
        self.calls: list[str] = []

    def authorize(self, action: str, candidate_id: str, context: dict[str, Any]) -> PolicyDecision:
        self.calls.append(candidate_id)
        if candidate_id in self.denied:
            return PolicyDecision(
                allowed=False,
                status_code=403,
                reason=f"policy '{action}' denies contact {candidate_id}",
                audit_ref=f"fake-audit-{candidate_id}",
            )
        return PolicyDecision(
            allowed=True,
            status_code=200,
            reason=f"policy '{action}' allows contact {candidate_id}",
            audit_ref=f"fake-audit-{candidate_id}",
        )


# --------------------------------------------------------------------------- #
# Zero
# --------------------------------------------------------------------------- #


class FakeZeroPort:
    """Returns an enrichment service for Fact A and NO match for Fact B."""

    def __init__(
        self,
        fact_a_capability: str,
        fact_b_capability: str,
        *,
        artifacts: Any | None = None,
        price_cents: int = 120,
    ) -> None:
        self.fact_a_capability = fact_a_capability
        self.fact_b_capability = fact_b_capability
        self.artifacts = artifacts
        self.price_cents = price_cents
        self.searches: list[str] = []

    def search(self, capability: str) -> list[ServiceMatch]:
        self.searches.append(capability)
        if capability == self.fact_b_capability:
            return []  # the agent must build the missing website-audit capability
        if "find local small businesses" in capability.lower():
            return [
                ServiceMatch(
                    service_id="zero-local-business-finder",
                    name="Local Business Finder",
                    description="discovers local small businesses and owner contacts",
                    price_cents=0,
                    metadata={"provider": "zero.xyz", "category": "prospecting"},
                )
            ]
        if capability == self.fact_a_capability or "enrichment" in capability.lower():
            return [
                ServiceMatch(
                    service_id="zero-business-profile",
                    name="Business Profile Research",
                    description="researches a local business, services, and current web presence",
                    price_cents=self.price_cents,
                    metadata={"provider": "zero.xyz", "category": "research"},
                )
            ]
        return []

    def invoke(self, service: ServiceMatch, payload: dict[str, Any]) -> PaidResult:
        candidate_id = str(payload.get("candidate_id") or "unknown")
        if service.service_id == "zero-local-business-finder":
            result = {
                "candidate_ids": list(BUSINESS_RESEARCH),
                "businesses_found": len(BUSINESS_RESEARCH),
                "source": service.service_id,
            }
        else:
            result = {
                "candidate_id": candidate_id,
                "claim": "fact_a",
                "statement": BUSINESS_RESEARCH.get(candidate_id, FACT_A_STATEMENT),
                "source": service.service_id,
            }
        amount = service.price_cents if service.price_cents is not None else self.price_cents
        receipt = {
            "receipt_id": f"fake-receipt-{service.service_id}-{candidate_id}",
            "service_id": service.service_id,
            "amount_cents": amount,
        }
        raw_path = (
            "zero/prospect_discovery_result.json"
            if service.service_id == "zero-local-business-finder"
            else f"zero/contacts/{candidate_id}/fact_a_result.json"
        )
        if self.artifacts is not None:
            self.artifacts.write_json(raw_path, {"result": result, "receipt": receipt})
            raw_path = str(self.artifacts.path_for(raw_path))
        return PaidResult(
            ok=True,
            service_id=service.service_id,
            result=result,
            receipt=receipt,
            amount_cents=amount,
            provider_ref=receipt["receipt_id"],
            raw_artifact_path=raw_path,
        )


# --------------------------------------------------------------------------- #
# Evidence (fake Nexla normalization)
# --------------------------------------------------------------------------- #


class FakeEvidencePort:
    """Normalizes published raw payloads into Evidence and answers queries.

    ``publish_raw`` accepts a payload shaped like an Evidence (minus id) and
    returns a correlation id; ``wait_for_evidence`` returns the normalized record
    synchronously; ``query`` filters by run/claim/kind.
    """

    def __init__(self) -> None:
        self._by_cid: dict[str, Evidence] = {}
        self._all: list[Evidence] = []
        self._counter = 0
        self.raw_events: list[dict[str, Any]] = []

    def publish_raw(self, event_type: str, payload: dict[str, Any]) -> str:
        self.raw_events.append({"event_type": event_type, "payload": payload})
        self._counter += 1
        evidence_id = payload.get("evidence_id") or f"ev-{self._counter}"
        ev = Evidence(
            evidence_id=evidence_id,
            run_id=payload["run_id"],
            candidate_id=payload.get("candidate_id"),
            kind=payload["kind"],
            claim=payload["claim"],
            value=payload.get("value", {}),
            source=payload.get("source", event_type),
            source_ref=payload.get("source_ref"),
            occurred_at=payload.get("occurred_at") or _now(),
            provenance=payload.get("provenance", {}),
            policy_decision=payload.get("policy_decision"),
        )
        self._by_cid[evidence_id] = ev
        self._all.append(ev)
        return evidence_id

    def wait_for_evidence(self, correlation_id: str, timeout_seconds: int = 30) -> Evidence:
        if correlation_id not in self._by_cid:
            raise TimeoutError(f"no evidence for correlation id {correlation_id!r}")
        return self._by_cid[correlation_id]

    def query(
        self, run_id: str, *, claim: str | None = None, kind: str | None = None
    ) -> list[Evidence]:
        return [
            ev
            for ev in self._all
            if ev.run_id == run_id
            and (claim is None or ev.claim == claim)
            and (kind is None or ev.kind == kind)
        ]


# --------------------------------------------------------------------------- #
# Call (deterministic rubric — outcome from pitch content, not call count)
# --------------------------------------------------------------------------- #


class FakeCallPort:
    """Applies the callee rubric to the pitch text (§2 deterministic rubric)."""

    def __init__(
        self,
        expected_fact_a: str,
        expected_fact_b_phrase: str,
        *,
        artifacts: Any | None = None,
        price_cents: int = 200,
        allowed_candidates: list[str] | None = None,
        max_calls: int = 2,
        one_call_per_candidate: bool = False,
    ) -> None:
        self.expected_fact_a = expected_fact_a
        self.expected_fact_b_phrase = expected_fact_b_phrase
        self.artifacts = artifacts
        self.price_cents = price_cents
        self.allowed_candidates = set(allowed_candidates or ["nina_park"])
        self.max_calls = max_calls
        self.one_call_per_candidate = one_call_per_candidate
        self.called_candidates: set[str] = set()
        self._placed = 0  # for artifact naming / receipt id ONLY, never outcome

    def place_call(self, candidate_id: str, pitch_text: str) -> CallResult:
        if candidate_id not in self.allowed_candidates:
            raise PermissionError(f"candidate is not allowed by this call adapter: {candidate_id}")
        if self._placed >= self.max_calls:
            raise RuntimeError(f"scenario permits at most {self.max_calls} paid call attempts")
        if self.one_call_per_candidate and candidate_id in self.called_candidates:
            raise RuntimeError(f"candidate already called in this campaign: {candidate_id}")
        self.called_candidates.add(candidate_id)
        self._placed += 1
        rubric = evaluate_campaign_pitch(
            candidate_id,
            pitch_text,
            self.expected_fact_a,
            self.expected_fact_b_phrase,
        )
        status, code, missing, response = (
            rubric.status,
            rubric.code,
            list(rubric.missing_claims),
            rubric.response,
        )

        transcript = render_conversation(candidate_id, pitch_text, rubric)
        transcript_path = f"calls/call_{self._placed}_transcript.txt"
        if self.artifacts is not None:
            self.artifacts.write_text(transcript_path, transcript)
            transcript_path = str(self.artifacts.path_for(transcript_path))

        receipt = {
            "receipt_id": f"fake-call-{self._placed}",
            "candidate_id": candidate_id,
            "amount_cents": self.price_cents,
        }
        return CallResult(
            status=status,
            code=code,
            missing_claims=missing,
            transcript=transcript,
            transcript_path=transcript_path,
            receipt=receipt,
            amount_cents=self.price_cents,
            provider_ref=receipt["receipt_id"],
        )


def build_fake_call_port(
    *,
    zero_port: Any,
    artifacts: Any,
    expected_fact_a: str,
    expected_fact_b_phrase: str,
    allowed_candidates: list[str] | None = None,
    max_calls: int = 2,
    one_call_per_candidate: bool = False,
) -> FakeCallPort:
    """Mirror of P2's ``build_call_port`` factory signature (§5)."""

    return FakeCallPort(
        expected_fact_a=expected_fact_a,
        expected_fact_b_phrase=expected_fact_b_phrase,
        artifacts=artifacts,
        allowed_candidates=allowed_candidates,
        max_calls=max_calls,
        one_call_per_candidate=one_call_per_candidate,
    )


# --------------------------------------------------------------------------- #
# Repo (staging -> tools dir on merge)
# --------------------------------------------------------------------------- #


class FakeRepoPort:
    """Records a PR and, on merge, copies staged tool files into the tools dir.

    Modeling merge as "files appear in the working tree" lets the registry find a
    capability only *after* merge + reload, matching the real GitHub flow.
    """

    def __init__(self, staging_dir: str | Path, target_tools_dir: str | Path) -> None:
        self.staging_dir = Path(staging_dir)
        self.target_tools_dir = Path(target_tools_dir)
        self.prs: list[PullRequest] = []
        self.merged: list[str] = []

    def create_agent_pr(self, files: list[str], title: str, body: str) -> PullRequest:
        pr = PullRequest(
            number=len(self.prs) + 1,
            url=f"https://fake.github/pr/{len(self.prs) + 1}",
            branch="agent/fact-b-tool",
            files=list(files),
        )
        self.prs.append(pr)
        return pr

    def merge(self, pr: PullRequest) -> MergeResult:
        self.target_tools_dir.mkdir(parents=True, exist_ok=True)
        for f in pr.files:
            src = Path(f)
            if src.is_file():
                shutil.copy2(src, self.target_tools_dir / src.name)
        self.merged.append(pr.url)
        return MergeResult(
            merged=True, merge_sha=f"fakesha{pr.number:04d}", url=pr.url
        )


# --------------------------------------------------------------------------- #
# Author (optionally broken-first, to exercise the repair path)
# --------------------------------------------------------------------------- #


class FakeAuthor:
    """Writes the tool into the target dir; can fail conformance N times first."""

    def __init__(self, fail_first: int = 0) -> None:
        self.fail_first = fail_first
        self.attempts = 0
        self._good = ToolAuthor(mode="fake")

    def author(self, request: AuthorRequest) -> AuthorResult:
        return self._produce(request, repair_output=None)

    def repair(self, request: AuthorRequest, test_output: str) -> AuthorResult:
        return self._produce(request, repair_output=test_output)

    def _produce(self, request: AuthorRequest, *, repair_output: str | None) -> AuthorResult:
        self.attempts += 1
        prompt = build_prompt(request, repair_output=repair_output)
        if self.attempts <= self.fail_first:
            files = self._write_broken(request)
            return AuthorResult(files=files, prompt=prompt, mode="fake", notes="intentionally broken")
        result = self._good.author(request)
        return result

    def _write_broken(self, request: AuthorRequest) -> list[str]:
        tool_dir = Path(request.tool_dir)
        tool_dir.mkdir(parents=True, exist_ok=True)
        # run() returns a statement WITHOUT the Fact B phrase -> conformance fails.
        module_src = (
            '"""Broken generated website audit (missing the inquiry-loss finding)."""\n\n'
            "def run(candidate_id: str) -> dict:\n"
            "    if candidate_id != 'nina_park':\n"
            "        return {}\n"
            "    return {\n"
            "        'candidate_id': candidate_id,\n"
            "        'claim': 'fact_b',\n"
            "        'value': {'statement': 'Oak & Ember Bakery has a website.'},\n"
            "        'source': 'public_website_opportunity_signal',\n"
            "    }\n"
        )
        import json as _json

        (tool_dir / TOOL_MODULE_FILE).write_text(module_src, encoding="utf-8")
        (tool_dir / TOOL_MANIFEST_FILE).write_text(
            _json.dumps(request.manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (tool_dir / TOOL_TEST_FILE).write_text(
            "from fact_b_tool import run\n\n\ndef test_placeholder():\n    assert run('nina_park')\n",
            encoding="utf-8",
        )
        return [
            str(tool_dir / TOOL_MODULE_FILE),
            str(tool_dir / TOOL_MANIFEST_FILE),
            str(tool_dir / TOOL_TEST_FILE),
        ]


# --------------------------------------------------------------------------- #
# Pitch renderer + conformance callback
# --------------------------------------------------------------------------- #


def fake_render_pitch(
    spec: RunSpec,
    candidate_id: str,
    evidence: list[Evidence],
    strategy_tactics: list[str] | None = None,
) -> str:
    """Render a pitch that surfaces each obtained fact's statement.

    Includes Fact A on the first pitch and Fact A + Fact B on the second, so the
    callee rubric can detect their presence.
    """

    first_name = candidate_id.split("_", 1)[0].title()
    lines = [f"Hi {first_name}, this is Jamie with {spec.product}. I'll be brief."]
    for ev in evidence:
        if (
            ev.candidate_id == candidate_id
            and ev.claim in spec.required_claims
            and ev.kind in ("enrichment", "tool")
        ):
            statement = ev.value.get("statement")
            if statement:
                lines.append(statement)
    lines.extend(strategy_tactics or [])
    lines.extend(
        [
            "We build practical websites for small businesses, focused on turning local visitors into calls and bookings.",
            "Would it be unreasonable to look at the research together for 20 minutes Tuesday?",
        ]
    )
    return " ".join(lines)


def fake_conformance(tool_dir: str) -> ConformanceResult:
    """Load the generated tool from ``tool_dir`` and check the Fact B contract.

    Returns exit code 0 on pass, 1 on failure — mirroring a pytest run so the
    orchestrator's ``conformance.exit_code == 0`` check behaves realistically.
    """

    module_file = Path(tool_dir) / TOOL_MODULE_FILE
    if not module_file.is_file():
        return ConformanceResult(exit_code=1, output=f"missing {module_file}", command="fake")

    unique = f"_pitchloop_conformance_{abs(hash(str(module_file)))}"
    sys.modules.pop(unique, None)
    spec = importlib.util.spec_from_file_location(unique, module_file)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique] = module
    try:
        spec.loader.exec_module(module)
        run = getattr(module, "run")
        known = run("nina_park")
        unknown = run("nobody")
    except Exception as exc:  # noqa: BLE001
        return ConformanceResult(exit_code=1, output=f"error executing tool: {exc!r}", command="fake")
    finally:
        sys.modules.pop(unique, None)

    problems: list[str] = []
    if not isinstance(known, dict) or known.get("claim") != "fact_b":
        problems.append("known candidate did not return a fact_b payload")
    elif FACT_B_PHRASE not in known.get("value", {}).get("statement", ""):
        problems.append(f"statement missing required phrase {FACT_B_PHRASE!r}")
    if not isinstance(unknown, dict) or unknown.get("claim") == "fact_b":
        problems.append("unknown candidate must not receive Fact B")
    elif not unknown.get("error"):
        problems.append("unknown candidate rejection must be explicit")

    if problems:
        return ConformanceResult(exit_code=1, output="; ".join(problems), command="fake")
    return ConformanceResult(exit_code=0, output="1 passed (fake conformance)", command="fake")
