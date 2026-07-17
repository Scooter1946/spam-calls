"""Deterministically map scripted call transcripts to canonical outcomes."""

from __future__ import annotations

from dataclasses import dataclass

from callee.call_harness import (
    BUSINESS_IMPACT_TACTIC,
    LOW_FRICTION_TACTIC,
    MEETING_BOOKED_RESPONSE,
    PILOT_TACTIC,
    PROOF_TACTIC,
    REJECTED_HIGH_FRICTION_RESPONSE,
    REJECTED_MISSING_FACT_A_RESPONSE,
    REJECTED_MISSING_FACT_B_RESPONSE,
    REJECTED_NO_PROOF_RESPONSE,
    REJECTED_TIMING_RESPONSE,
    REJECTED_WEAK_VALUE_RESPONSE,
    normalize_for_match,
)


@dataclass(frozen=True, slots=True)
class ParsedTranscript:
    status: str
    code: str
    missing_claims: tuple[str, ...]


_PHRASE_TO_RESULT = {
    normalize_for_match(REJECTED_MISSING_FACT_A_RESPONSE): ParsedTranscript(
        status="rejected",
        code="REJECTED_MISSING_FACT_A",
        missing_claims=("fact_a",),
    ),
    normalize_for_match(REJECTED_MISSING_FACT_B_RESPONSE): ParsedTranscript(
        status="rejected",
        code="REJECTED_MISSING_FACT_B",
        missing_claims=("fact_b",),
    ),
    normalize_for_match(REJECTED_WEAK_VALUE_RESPONSE): ParsedTranscript(
        status="rejected", code="REJECTED_WEAK_VALUE", missing_claims=()
    ),
    normalize_for_match(REJECTED_NO_PROOF_RESPONSE): ParsedTranscript(
        status="rejected", code="REJECTED_NO_PROOF", missing_claims=()
    ),
    normalize_for_match(REJECTED_HIGH_FRICTION_RESPONSE): ParsedTranscript(
        status="rejected", code="REJECTED_HIGH_FRICTION", missing_claims=()
    ),
    normalize_for_match(REJECTED_TIMING_RESPONSE): ParsedTranscript(
        status="rejected", code="REJECTED_TIMING", missing_claims=()
    ),
    normalize_for_match(MEETING_BOOKED_RESPONSE): ParsedTranscript(
        status="booked",
        code="MEETING_BOOKED",
        missing_claims=(),
    ),
}

_FAILED = ParsedTranscript(
    status="failed",
    code="CALL_FAILED_UNPARSEABLE",
    missing_claims=(),
)


def _pitch_contains(pitch_text: str, expected: str) -> bool:
    normalized_expected = normalize_for_match(expected)
    return bool(normalized_expected) and normalized_expected in normalize_for_match(pitch_text)


def parse_transcript(
    transcript: str,
    *,
    pitch_text: str,
    expected_fact_a: str,
    expected_fact_b_phrase: str,
) -> ParsedTranscript:
    """Parse only exact scripted responses and cross-check the triggering pitch."""

    if not all(
        isinstance(value, str)
        for value in (transcript, pitch_text, expected_fact_a, expected_fact_b_phrase)
    ):
        return _FAILED

    normalized_transcript = normalize_for_match(transcript)
    parsed = _PHRASE_TO_RESULT.get(normalized_transcript)
    responses = []
    for line in transcript.splitlines():
        speaker, separator, response = line.partition(":")
        if separator and speaker.strip().casefold() != "agent":
            responses.append(normalize_for_match(response))
    if parsed is None:
        parsed = next((_PHRASE_TO_RESULT[item] for item in reversed(responses) if item in _PHRASE_TO_RESULT), None)
    if parsed is None:
        return _FAILED

    has_a = _pitch_contains(pitch_text, expected_fact_a)
    has_b = _pitch_contains(pitch_text, expected_fact_b_phrase)
    pitch_is_consistent = {
        "REJECTED_MISSING_FACT_A": not has_a,
        "REJECTED_MISSING_FACT_B": has_a and not has_b,
        "REJECTED_WEAK_VALUE": has_a and has_b and not _pitch_contains(pitch_text, BUSINESS_IMPACT_TACTIC),
        "REJECTED_NO_PROOF": has_a and has_b and not _pitch_contains(pitch_text, PROOF_TACTIC),
        "REJECTED_HIGH_FRICTION": has_a and has_b and not _pitch_contains(pitch_text, LOW_FRICTION_TACTIC),
        "REJECTED_TIMING": has_a and has_b and not _pitch_contains(pitch_text, PILOT_TACTIC),
        "MEETING_BOOKED": has_a and has_b,
    }[parsed.code]
    return parsed if pitch_is_consistent else _FAILED
