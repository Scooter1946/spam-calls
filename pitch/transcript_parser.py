"""Deterministically map scripted call transcripts to canonical outcomes."""

from __future__ import annotations

from dataclasses import dataclass

from callee.call_harness import (
    MEETING_BOOKED_RESPONSE,
    REJECTED_MISSING_FACT_A_RESPONSE,
    REJECTED_MISSING_FACT_B_RESPONSE,
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

    parsed = _PHRASE_TO_RESULT.get(normalize_for_match(transcript))
    if parsed is None:
        return _FAILED

    has_a = _pitch_contains(pitch_text, expected_fact_a)
    has_b = _pitch_contains(pitch_text, expected_fact_b_phrase)
    pitch_is_consistent = {
        "REJECTED_MISSING_FACT_A": not has_a,
        "REJECTED_MISSING_FACT_B": has_a and not has_b,
        "MEETING_BOOKED": has_a and has_b,
    }[parsed.code]
    return parsed if pitch_is_consistent else _FAILED
