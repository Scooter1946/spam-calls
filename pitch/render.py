"""Render short sales pitches exclusively from normalized evidence."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from contracts.models import Evidence, RunSpec


_FORBIDDEN_INTERNAL_TERMS = ("zero.xyz", "pomerium", "nexla", "hackathon")
_MAX_WORDS = 120


def _field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _timestamp(item: Any) -> datetime:
    occurred_at = _field(item, "occurred_at")
    if isinstance(occurred_at, datetime):
        if occurred_at.tzinfo is None:
            return occurred_at.replace(tzinfo=timezone.utc)
        return occurred_at
    if isinstance(occurred_at, str):
        parsed = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    return datetime.min.replace(tzinfo=timezone.utc)


def _clean_statement(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    statement = value.get("statement")
    if not isinstance(statement, str):
        return None
    statement = re.sub(r"\s+", " ", statement).strip()
    if not statement:
        return None
    return statement.rstrip(" .!?;:")


def _latest_statement(
    evidence: Iterable["Evidence"],
    *,
    run_id: str,
    candidate_id: str,
    claim: str,
) -> str | None:
    matching = [
        item
        for item in evidence
        if _field(item, "run_id") == run_id
        and _field(item, "candidate_id") == candidate_id
        and _field(item, "claim") == claim
        and _field(item, "policy_decision") != "deny"
        and _clean_statement(_field(item, "value")) is not None
    ]
    if not matching:
        return None
    latest = max(matching, key=_timestamp)
    return _clean_statement(_field(latest, "value"))


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text, flags=re.UNICODE))


def render_pitch(
    spec: "RunSpec",
    candidate_id: str,
    evidence: list["Evidence"],
    strategy_tactics: list[str] | None = None,
) -> str:
    """Render one concise pitch without inventing facts absent from evidence."""

    candidates = _field(spec, "candidates", [])
    if candidate_id not in candidates:
        raise ValueError(f"candidate is not in the locked scenario: {candidate_id}")

    run_id = _field(spec, "run_id")
    product = _field(spec, "product")
    if not isinstance(run_id, str) or not isinstance(product, str):
        raise ValueError("spec must provide string run_id and product fields")

    fact_a = _latest_statement(
        evidence,
        run_id=run_id,
        candidate_id=candidate_id,
        claim="fact_a",
    )
    fact_b = _latest_statement(
        evidence,
        run_id=run_id,
        candidate_id=candidate_id,
        claim="fact_b",
    )

    profiles = _field(spec, "candidate_profiles", {})
    profile = profiles.get(candidate_id, {}) if isinstance(profiles, dict) else {}
    first_name = str(profile.get("name") or candidate_id).split()[0].replace("-", " ").title()
    sentences = [f"Hi {first_name}, I'm calling from {product}. I'll be brief."]
    if fact_a:
        sentences.append(f"I noticed {fact_a}.")
    if fact_b:
        sentences.append(f"I also saw that {fact_b}.")
    sentences.extend(strategy_tactics or [])
    sentences.extend(
        [
            "We build practical websites that turn more local visitors into calls and bookings.",
            "Would it be unreasonable to review the research together for 20 minutes Tuesday?",
        ]
    )
    pitch = " ".join(sentences)

    if _word_count(pitch) > _MAX_WORDS:
        raise ValueError(
            f"rendered pitch exceeds the {_MAX_WORDS}-word spoken limit; "
            "select shorter canonical evidence"
        )
    normalized_pitch = pitch.casefold()
    forbidden = [term for term in _FORBIDDEN_INTERNAL_TERMS if term in normalized_pitch]
    if forbidden:
        raise ValueError(f"evidence contains forbidden internal terms: {forbidden}")
    return pitch
