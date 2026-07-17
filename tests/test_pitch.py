from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from callee.call_harness import (
    MEETING_BOOKED_RESPONSE,
    REJECTED_MISSING_FACT_B_RESPONSE,
    evaluate_pitch,
)
from pitch.render import render_pitch
from pitch.transcript_parser import parse_transcript


FACT_A = "Northstar is expanding its developer platform"
FACT_B = "Northstar Systems has an August 30 API v1 migration deadline."
FACT_B_PHRASE = "August 30 API v1 migration deadline"


@dataclass
class Spec:
    run_id: str = "demo-001"
    product: str = "MigrationGuard"
    candidates: tuple[str, ...] = ("alex_rivera", "maya_chen")


@dataclass
class Evidence:
    run_id: str
    candidate_id: str
    claim: str
    value: dict[str, str]
    occurred_at: datetime
    policy_decision: str | None = "allow"


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def evidence(
    claim: str,
    statement: str,
    *,
    candidate_id: str = "maya_chen",
    run_id: str = "demo-001",
    minutes: int = 0,
    policy_decision: str | None = "allow",
) -> Evidence:
    return Evidence(
        run_id=run_id,
        candidate_id=candidate_id,
        claim=claim,
        value={"statement": statement},
        occurred_at=NOW + timedelta(minutes=minutes),
        policy_decision=policy_decision,
    )


def test_pitch_with_no_facts_contains_neither() -> None:
    pitch = render_pitch(Spec(), "maya_chen", [])
    assert FACT_A not in pitch
    assert FACT_B_PHRASE not in pitch
    assert "Would a 20-minute conversation Tuesday be useful?" in pitch


def test_pitch_with_a_only_omits_b_and_rubric_rejects_b() -> None:
    pitch = render_pitch(Spec(), "maya_chen", [evidence("fact_a", FACT_A)])
    assert FACT_A in pitch
    assert FACT_B_PHRASE not in pitch
    assert evaluate_pitch(pitch, FACT_A, FACT_B_PHRASE).code == "REJECTED_MISSING_FACT_B"


def test_pitch_with_a_and_b_books() -> None:
    pitch = render_pitch(
        Spec(),
        "maya_chen",
        [evidence("fact_a", FACT_A), evidence("fact_b", FACT_B)],
    )
    assert FACT_A in pitch
    assert FACT_B_PHRASE in pitch
    assert evaluate_pitch(pitch, FACT_A, FACT_B_PHRASE).code == "MEETING_BOOKED"
    assert len(pitch.split()) < 70


def test_renderer_uses_latest_candidate_scoped_allowed_evidence() -> None:
    items = [
        evidence("fact_a", "older allowed signal", minutes=0),
        evidence("fact_a", FACT_A, minutes=1),
        evidence("fact_a", "denied newer signal", minutes=2, policy_decision="deny"),
        evidence("fact_b", "wrong candidate secret", candidate_id="alex_rivera"),
        evidence("fact_b", "wrong run secret", run_id="other-run"),
    ]
    pitch = render_pitch(Spec(), "maya_chen", items)
    assert FACT_A in pitch
    assert "older allowed signal" not in pitch
    assert "denied newer signal" not in pitch
    assert "secret" not in pitch


def test_renderer_has_no_hallucinated_placeholders_or_internal_tools() -> None:
    pitch = render_pitch(Spec(), "maya_chen", [])
    for forbidden in ("[name]", "{", "}", "fact_a", "fact_b", "Zero", "Pomerium", "Nexla"):
        assert forbidden not in pitch


def test_renderer_rejects_unknown_candidate_and_overlong_evidence() -> None:
    with pytest.raises(ValueError, match="locked scenario"):
        render_pitch(Spec(), "unknown", [])
    long_fact = " ".join(["signal"] * 60)
    with pytest.raises(ValueError, match="spoken limit"):
        render_pitch(Spec(), "maya_chen", [evidence("fact_a", long_fact)])


def test_transcript_parser_requires_exact_response_and_consistent_pitch() -> None:
    pitch_a = render_pitch(Spec(), "maya_chen", [evidence("fact_a", FACT_A)])
    rejected = parse_transcript(
        REJECTED_MISSING_FACT_B_RESPONSE,
        pitch_text=pitch_a,
        expected_fact_a=FACT_A,
        expected_fact_b_phrase=FACT_B_PHRASE,
    )
    assert rejected.code == "REJECTED_MISSING_FACT_B"

    pitch_both = render_pitch(
        Spec(),
        "maya_chen",
        [evidence("fact_a", FACT_A), evidence("fact_b", FACT_B)],
    )
    booked = parse_transcript(
        MEETING_BOOKED_RESPONSE.upper(),
        pitch_text=pitch_both,
        expected_fact_a=FACT_A,
        expected_fact_b_phrase=FACT_B_PHRASE,
    )
    assert booked.code == "MEETING_BOOKED"

    false_pass = parse_transcript(
        MEETING_BOOKED_RESPONSE,
        pitch_text=pitch_a,
        expected_fact_a=FACT_A,
        expected_fact_b_phrase=FACT_B_PHRASE,
    )
    assert false_pass.code == "CALL_FAILED_UNPARSEABLE"

    improvised = parse_transcript(
        "Sure, let us meet Tuesday.",
        pitch_text=pitch_both,
        expected_fact_a=FACT_A,
        expected_fact_b_phrase=FACT_B_PHRASE,
    )
    assert improvised.code == "CALL_FAILED_UNPARSEABLE"


def test_public_fixture_is_candidate_gated() -> None:
    pytest.importorskip("fastapi", reason="P1 dependency baseline has not landed")
    from fastapi.testclient import TestClient
    from fixtures.public_signal_server import app

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        allowed = client.get(
            "/companies/northstar_systems/migration-signal",
            params={"candidate_id": "maya_chen"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["statement"] == FACT_B

        for candidate_id in ("alex_rivera", "unknown"):
            denied = client.get(
                "/companies/northstar_systems/migration-signal",
                params={"candidate_id": candidate_id},
            )
            assert denied.status_code == 404
            assert "statement" not in denied.json()
