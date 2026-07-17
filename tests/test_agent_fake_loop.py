"""P1 end-to-end fake loop + supporting unit tests.

The headline test drives the whole state machine against fakes and proves it
reaches MEETING_BOOKED with no human input. The remaining tests cover the parts
the loop relies on (artifacts/redaction, registry reload semantics, planner
ranking, diagnosis, path-restricted authoring, budget enforcement, and the
one-repair rule). All P1 agent tests live here because the assignment restricts
P1 to two test files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import fakes
from agent.artifacts import Artifacts, redact
from agent.diagnosis import build_diagnosis
from agent.orchestrator import BudgetLedger, Config, Deps, Orchestrator
from agent.planner import build_plan, rank_candidates
from agent.state import State
from agent.tool_author import (
    TOOL_MANIFEST_FILE,
    TOOL_MODULE_FILE,
    PathViolation,
    ToolAuthor,
    assert_only_allowed_paths,
    parse_git_status_porcelain,
)
from agent.tool_registry import ToolRegistry
from contracts.models import Evidence, RunSpec, ServiceMatch
from datetime import datetime, timezone
from integrations.zero_client import (
    REQUIRED_LIVE_ZERO_ARTIFACTS,
    ZeroClient,
    ZeroCliError,
    select_within_budget,
    validate_live_proof,
)
import os
import stat


# --------------------------------------------------------------------------- #
# Fixtures / harness
# --------------------------------------------------------------------------- #


def _spec(**overrides) -> RunSpec:
    base = dict(
        run_id="demo-001",
        goal="book_one_qualified_meeting",
        product="MigrationGuard",
        persona="maya_chen",
        candidates=["alex_rivera", "maya_chen"],
        budget_cents=5000,
        policy_ref="northstar/pitch",
        required_claims=["fact_a", "fact_b"],
        max_paid_calls=2,
    )
    base.update(overrides)
    return RunSpec(**base)


def _harness(tmp_path: Path, *, author=None, run_conformance=None, spec: RunSpec | None = None):
    """Build an Orchestrator wired entirely to fakes, plus handles for asserting."""

    spec = spec or _spec()
    run_dir = tmp_path / "run"
    staging = tmp_path / "staging"
    target = tmp_path / "tools"

    artifacts = Artifacts(run_dir=run_dir)
    config = Config(
        fact_a_capability="company enrichment for sales personalization",
        fact_b_capability="northstar api v1 migration deadline",
        author_tool_dir=str(staging),
        reload_tools_dir=str(target),
    )
    evidence = fakes.FakeEvidencePort()
    policy = fakes.FakePolicyPort()
    zero = fakes.FakeZeroPort(config.fact_a_capability, config.fact_b_capability, artifacts=artifacts)
    repo = fakes.FakeRepoPort(staging_dir=staging, target_tools_dir=target)
    registry = ToolRegistry(tools_dir=target, artifacts=artifacts)

    deps = Deps(
        zero=zero,
        policy=policy,
        evidence=evidence,
        repo=repo,
        registry=registry,
        author=author or ToolAuthor(mode="fake"),
        artifacts=artifacts,
        render_pitch=fakes.fake_render_pitch,
        build_call_port=fakes.build_fake_call_port,
        run_conformance=run_conformance or fakes.fake_conformance,
        git_status=lambda: [],
    )
    orch = Orchestrator(spec, deps, config)
    return orch, deps, {"policy": policy, "zero": zero, "evidence": evidence, "repo": repo, "registry": registry, "run_dir": run_dir}


def _events(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _read_json(run_dir: Path, rel: str) -> dict:
    return json.loads((run_dir / rel).read_text())


# --------------------------------------------------------------------------- #
# Headline: full fake loop reaches MEETING_BOOKED (the 14 required proofs)
# --------------------------------------------------------------------------- #


def test_fake_loop_books_meeting(tmp_path):
    orch, deps, h = _harness(tmp_path)
    result = orch.run()
    run_dir = h["run_dir"]

    # Reaches the goal with no input.
    assert result.outcome == "MEETING_BOOKED"
    assert result.final_state == State.FINALIZE

    # (1) Denied candidate returns a real 403 (published + persisted).
    assert "alex_rivera" in h["policy"].calls
    deny = _read_json(run_dir, "policy/deny.json")
    assert deny["allowed"] is False and deny["status_code"] == 403

    # (2) Orchestrator then selects the allowed candidate.
    assert orch.state.tried_candidates == ["alex_rivera", "maya_chen"]
    assert orch.state.current_candidate == "maya_chen"
    allow = _read_json(run_dir, "policy/allow.json")
    assert allow["allowed"] is True and allow["status_code"] == 200

    # (3) Fake Zero search/invoke publishes Fact A.
    fact_a = deps.evidence.query("demo-001", claim="fact_a")
    assert len(fact_a) == 1 and "August 30" not in fact_a[0].value["statement"]

    # (4) Call #1 rejects with only Fact B missing.
    call1 = _read_json(run_dir, "calls/call_1_result.json")
    assert call1["code"] == "REJECTED_MISSING_FACT_B"
    assert call1["missing_claims"] == ["fact_b"]

    # (5) Diagnosis (from normalized evidence) reports Fact B missing.
    diag = _read_json(run_dir, "evidence/diagnosis.json")
    assert diag["missing_claims"] == ["fact_b"]
    assert diag["present_claims"] == ["fact_a"]
    assert diag["next_action"] == "discover_capability"
    # cites the normalized call evidence + Fact A evidence id
    assert fact_a[0].evidence_id in diag["evidence_ids"]

    # (6) Fake Zero Fact B search returns no match (preserved).
    search_b = _read_json(run_dir, "zero/search_fact_b.json")
    assert search_b["no_match"] is True and search_b["matches"] == []
    assert "northstar api v1 migration deadline" in h["zero"].searches

    # (7) Author created a valid tool in the temp staging dir.
    assert (tmp_path / "staging" / TOOL_MODULE_FILE).is_file()
    assert (tmp_path / "staging" / TOOL_MANIFEST_FILE).is_file()

    # (8) Conformance passes.
    conf = _read_json(run_dir, "tools/conformance_result.json")
    assert conf["exit_code"] == 0

    # (9) Fake RepoPort merges.
    merge = _read_json(run_dir, "repo/merge.json")
    assert merge["merged"] is True

    # (10) Registry reload finds Fact B.
    assert _read_json(run_dir, "tools/reload.json")["found_fact_b"] is True
    assert h["registry"].find("fact_b") is not None

    # (11) Fact B is published.
    fact_b = deps.evidence.query("demo-001", claim="fact_b")
    assert len(fact_b) == 1 and "August 30 API v1 migration deadline" in fact_b[0].value["statement"]

    # (12) Call #2 books the meeting.
    call2 = _read_json(run_dir, "calls/call_2_result.json")
    assert call2["code"] == "MEETING_BOOKED" and call2["status"] == "booked"
    meeting = _read_json(run_dir, "final/meeting.json")
    assert meeting["candidate_id"] == "maya_chen"

    # (13) Outcome tracks pitch CONTENT, not call count: pitch 1 lacks the Fact B
    #      phrase and was rejected; pitch 2 contains it and booked.
    pitch1 = (run_dir / "pitch/pitch_1.md").read_text()
    pitch2 = (run_dir / "pitch/pitch_2.md").read_text()
    assert "August 30 API v1 migration deadline" not in pitch1
    assert "August 30 API v1 migration deadline" in pitch2
    assert orch.state.calls_placed == 2

    # (14) Total spending stays within budget (120 + 2*200 = 520).
    assert result.spent_cents == 520
    assert result.spent_cents <= result.budget_cents

    # artifact index enumerates the run's evidence
    index = _read_json(run_dir, "final/artifact_index.json")
    assert "policy/deny.json" in index["artifacts"]
    assert "final/meeting.json" in index["artifacts"]


def test_no_branch_on_call_count_source():
    """Guard against reintroducing `if call_number == N` outcome logic."""

    src = Path("agent/orchestrator.py").read_text() + Path("agent/fakes.py").read_text()
    assert "call_number" not in src
    # the call fake must not switch behavior on its internal counter
    assert "_placed == 1" not in src and "_placed == 2" not in src


# --------------------------------------------------------------------------- #
# One-repair rule
# --------------------------------------------------------------------------- #


def test_one_repair_attempt_then_success(tmp_path):
    orch, deps, h = _harness(tmp_path, author=fakes.FakeAuthor(fail_first=1))
    result = orch.run()
    assert result.outcome == "MEETING_BOOKED"
    assert orch.state.repair_attempts == 1
    conf = _read_json(h["run_dir"], "tools/conformance_result.json")
    assert conf["exit_code"] == 0


def test_second_failure_is_rejected(tmp_path):
    # Broken on both the initial attempt and the single repair -> FAILED.
    orch, deps, h = _harness(tmp_path, author=fakes.FakeAuthor(fail_first=2))
    result = orch.run()
    assert result.outcome == "FAILED"
    assert orch.state.repair_attempts == 1
    assert "conformance" in (result.failure_reason or "")


# --------------------------------------------------------------------------- #
# Budget enforcement
# --------------------------------------------------------------------------- #


def test_budget_stops_before_unaffordable_purchase(tmp_path):
    # Budget below the enrichment price (120c) must stop the run, not overspend.
    orch, deps, h = _harness(tmp_path, spec=_spec(budget_cents=50))
    result = orch.run()
    assert result.outcome == "FAILED"
    assert result.spent_cents == 0
    assert result.final_state == State.FAILED


def test_budget_ledger_tracks_from_receipts():
    ledger = BudgetLedger(1000)
    assert ledger.can_afford(400)
    ledger.record(400, "zero")
    ledger.record(200, "call")
    assert ledger.spent_cents == 600 and ledger.remaining() == 400
    assert not ledger.over_budget()


# --------------------------------------------------------------------------- #
# Registry reload semantics: no handle before merge, callable after
# --------------------------------------------------------------------------- #


def test_registry_finds_capability_only_after_files_present(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    target = tmp_path / "tools"
    target.mkdir()

    author = ToolAuthor(mode="fake")
    from agent.tool_author import AuthorRequest, CANONICAL_MANIFEST

    req = AuthorRequest(
        run_id="demo-001",
        tool_dir=str(staging),
        allowed_paths=["generated_tools/fact_b_tool.py"],
        function_contract="def run(candidate_id): ...",
        manifest=dict(CANONICAL_MANIFEST),
        fixture_url="http://127.0.0.1:8088",
        response_schema={},
        conformance_command="pytest",
    )
    author.author(req)

    registry = ToolRegistry(tools_dir=target)
    registry.reload()
    assert registry.find("fact_b") is None  # nothing merged yet

    # "merge": copy staged files into the tools dir, then reload.
    import shutil

    for f in staging.iterdir():
        shutil.copy2(f, target / f.name)
    registry.reload()
    handle = registry.find("fact_b")
    assert handle is not None
    response_payload = {
        "candidate_id": "maya_chen",
        "claim": "fact_b",
        "statement": "Northstar Systems has an August 30 API v1 migration deadline.",
        "source": "northstar_public_migration_signal",
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(response_payload).encode()

    monkeypatch.setenv("FACT_B_FIXTURE_URL", "http://fixture.test")
    handle.run.__globals__["urlopen"] = lambda *_args, **_kwargs: Response()
    out = handle.run("maya_chen")  # callable handle
    assert "August 30" in out["value"]["statement"]
    assert handle("nobody")["error"]  # __call__ also works; unknown gets no fact


def test_registry_invalid_manifest_becomes_failure(tmp_path):
    target = tmp_path / "tools"
    target.mkdir()
    (target / "bad.manifest.json").write_text('{"capability": "fact_b"}')  # incomplete
    registry = ToolRegistry(tools_dir=target)
    registry.reload()
    assert registry.find("fact_b") is None
    assert len(registry.failures) == 1


# --------------------------------------------------------------------------- #
# Planner + diagnosis + path safety units
# --------------------------------------------------------------------------- #


def test_planner_ranks_by_scenario_order():
    spec = _spec(candidates=["alex_rivera", "maya_chen", "alex_rivera"])
    assert rank_candidates(spec) == ["alex_rivera", "maya_chen"]
    plan = build_plan(spec)
    assert plan.ranked_candidates[0] == "alex_rivera"
    assert [s.name for s in plan.steps][:2] == ["policy_check", "discover_fact_a"]


def _ev(claim, kind, eid):
    return Evidence(
        evidence_id=eid,
        run_id="demo-001",
        kind=kind,
        claim=claim,
        value={"statement": "x"},
        source="s",
        occurred_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
    )


def test_diagnosis_reports_missing_fact_b():
    spec = _spec()
    evidence = [_ev("fact_a", "enrichment", "ev-a"), _ev("call", "call", "ev-call")]
    diag = build_diagnosis(spec, evidence)
    assert diag.present_claims == ["fact_a"]
    assert diag.missing_claims == ["fact_b"]
    assert diag.next_action == "discover_capability"
    assert diag.evidence_ids == ["ev-call", "ev-a"]


def test_diagnosis_all_present_wants_retry_then_finalize():
    spec = _spec()
    have_all = [_ev("fact_a", "enrichment", "a"), _ev("fact_b", "tool", "b")]
    assert build_diagnosis(spec, have_all).next_action == "retry_call"
    with_meeting = have_all + [_ev("meeting", "meeting", "m")]
    assert build_diagnosis(spec, with_meeting).next_action == "finalize"


def test_path_restriction_rejects_out_of_scope_changes():
    allowed = [
        "generated_tools/fact_b_tool.py",
        "generated_tools/fact_b_tool.manifest.json",
        "generated_tools/test_fact_b_tool.py",
    ]
    assert_only_allowed_paths(["generated_tools/fact_b_tool.py"], allowed)  # ok
    with pytest.raises(PathViolation):
        assert_only_allowed_paths(["agent/orchestrator.py"], allowed)


def test_parse_git_status_porcelain_handles_rename():
    out = " M agent/x.py\n?? generated_tools/fact_b_tool.py\nR  a.py -> b.py\n"
    assert parse_git_status_porcelain(out) == [
        "agent/x.py",
        "generated_tools/fact_b_tool.py",
        "b.py",
    ]


# --------------------------------------------------------------------------- #
# Redaction (central)
# --------------------------------------------------------------------------- #


def test_redact_masks_sensitive_keys_at_any_depth():
    out = redact(
        {
            "wallet_key": "x",
            "ok": 1,
            "nested": {"authorization": "Bearer y", "keep": 2},
            "list": [{"phone": "555", "note": "hi"}],
        }
    )
    assert out["wallet_key"] == "***REDACTED***"
    assert out["nested"]["authorization"] == "***REDACTED***"
    assert out["nested"]["keep"] == 2
    assert out["list"][0]["phone"] == "***REDACTED***"
    assert out["list"][0]["note"] == "hi"


# --------------------------------------------------------------------------- #
# Live Zero adapter (pure selection + real subprocess via a stub CLI)
# --------------------------------------------------------------------------- #


def test_select_within_budget_prefers_cheapest_affordable():
    matches = [
        ServiceMatch(service_id="a", name="A", description="", price_cents=300),
        ServiceMatch(service_id="b", name="B", description="", price_cents=120),
        ServiceMatch(service_id="c", name="C", description="", price_cents=9000),
    ]
    assert select_within_budget(matches, 5000).service_id == "b"
    assert select_within_budget(matches, 100) is None
    # unpriced fallback when nothing is priced
    unpriced = [ServiceMatch(service_id="u", name="U", description="")]
    assert select_within_budget(unpriced, 5000).service_id == "u"


def _write_stub_zero_cli(tmp_path: Path) -> str:
    script = tmp_path / "zero"
    script.write_text(
        "#!/bin/bash\n"
        'case "$2" in\n'
        '  search) echo \'{"services":[{"id":"svc-1","name":"Enrich","description":"company enrichment","price_cents":120}]}\';;\n'
        '  invoke) echo \'{"ok":true,"result":{"statement":"Northstar is hiring"},"receipt":{"receipt_id":"r1","amount_cents":120}}\';;\n'
        "  *) echo '{}';;\n"
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def test_zero_client_search_and_invoke_against_stub_cli(tmp_path):
    cli = _write_stub_zero_cli(tmp_path)
    artifacts = Artifacts(run_dir=tmp_path / "run")
    client = ZeroClient(artifacts=artifacts, cli=cli)

    matches = client.search("company enrichment for sales personalization")
    assert len(matches) == 1
    assert matches[0].service_id == "svc-1" and matches[0].price_cents == 120

    result = client.invoke(matches[0], {"candidate_id": "maya_chen"})
    assert result.ok is True
    assert result.amount_cents == 120  # taken from the receipt, not assumed
    assert result.result["statement"] == "Northstar is hiring"

    # raw artifacts preserved under the run dir
    run = tmp_path / "run"
    assert (run / "zero/search_fact_a.json").is_file()
    assert (run / "zero/search_stdio.json").is_file()
    assert (run / "zero/fact_a_result.json").is_file()
    assert (run / "zero/fact_a_receipt.json").is_file()


def test_zero_client_missing_cli_raises(tmp_path):
    artifacts = Artifacts(run_dir=tmp_path / "run")
    client = ZeroClient(artifacts=artifacts, cli="definitely-not-a-real-zero-binary-xyz")
    with pytest.raises(ZeroCliError):
        client.search("anything")


def test_zero_client_nonzero_exit_raises(tmp_path):
    script = tmp_path / "zero"
    script.write_text("#!/bin/bash\necho 'boom' >&2\nexit 3\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    artifacts = Artifacts(run_dir=tmp_path / "run")
    client = ZeroClient(artifacts=artifacts, cli=str(script))
    with pytest.raises(ZeroCliError):
        client.search("anything")
    # stdio is still preserved even on failure
    assert (tmp_path / "run/zero/search_stdio.json").is_file()


def test_validate_live_proof_reports_missing_then_complete(tmp_path):
    run = tmp_path / "run"
    (run / "zero").mkdir(parents=True)
    assert set(validate_live_proof(run)) == set(REQUIRED_LIVE_ZERO_ARTIFACTS)
    for rel in REQUIRED_LIVE_ZERO_ARTIFACTS:
        (run / rel).write_text("proof")
    assert validate_live_proof(run) == []
