"""Pure, deterministic pitch evaluator used as the fake callee oracle."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


REJECTED_MISSING_FACT_A_RESPONSE = (
    "Uh, honestly, this still feels pretty generic. I don't think you know enough "
    "about our business yet, so I'm going to pass."
)
REJECTED_MISSING_FACT_B_RESPONSE = (
    "Mm, okay, but you haven't actually looked at our website, have you? "
    "I think I'm going to pass for now."
)
MEETING_BOOKED_RESPONSE = (
    "Yeah, okay, that actually sounds useful. Send me a 20-minute invite for "
    "Tuesday at 2 PM."
)
REJECTED_WEAK_VALUE_RESPONSE = (
    "I hear you, but I'm still not sure what that changes for the business. "
    "Let me think about it and, uh, don't book anything yet."
)
REJECTED_NO_PROOF_RESPONSE = (
    "Maybe, but can you show me something you've actually done for a business like ours? "
    "Without that, I'm not ready to take a meeting."
)
REJECTED_HIGH_FRICTION_RESPONSE = (
    "Oof, that sounds like a whole project, and we're already stretched thin. "
    "I can't take that on right now."
)
REJECTED_TIMING_RESPONSE = (
    "Yeah, the idea makes sense, but the timing is rough. "
    "I can't justify a big website project this month."
)

BUSINESS_IMPACT_TACTIC = (
    "Turn more local searches into calls and bookings."
)
PROOF_TACTIC = (
    "I can show a before-and-after from a similar local business."
)
LOW_FRICTION_TACTIC = (
    "Start with a fixed-price homepage and contact-flow refresh."
)
PILOT_TACTIC = (
    "Let's scope one page together before you commit to a rebuild."
)


@dataclass(frozen=True, slots=True)
class RubricResult:
    status: str
    code: str
    missing_claims: tuple[str, ...]
    response: str


def normalize_for_match(value: str) -> str:
    """Normalize Unicode, punctuation, whitespace, and case for matching."""

    if not isinstance(value, str):
        raise TypeError("value must be a string")
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    without_marks = "".join(
        char for char in decomposed if not unicodedata.category(char).startswith("M")
    )
    punctuation_as_space = "".join(
        " " if unicodedata.category(char)[0] in {"P", "S"} else char
        for char in without_marks
    )
    return re.sub(r"\s+", " ", punctuation_as_space).strip()


def _contains_normalized(text: str, expected: str) -> bool:
    normalized_expected = normalize_for_match(expected)
    return bool(normalized_expected) and normalized_expected in normalize_for_match(text)


def evaluate_pitch(
    pitch_text: str,
    fact_a_statement: str,
    fact_b_phrase: str,
) -> RubricResult:
    """Apply the frozen rubric in precedence order without semantic scoring."""

    if not _contains_normalized(pitch_text, fact_a_statement):
        return RubricResult(
            status="rejected",
            code="REJECTED_MISSING_FACT_A",
            missing_claims=("fact_a",),
            response=REJECTED_MISSING_FACT_A_RESPONSE,
        )
    if not _contains_normalized(pitch_text, fact_b_phrase):
        return RubricResult(
            status="rejected",
            code="REJECTED_MISSING_FACT_B",
            missing_claims=("fact_b",),
            response=REJECTED_MISSING_FACT_B_RESPONSE,
        )
    return RubricResult(
        status="booked",
        code="MEETING_BOOKED",
        missing_claims=(),
        response=MEETING_BOOKED_RESPONSE,
    )


def evaluate_campaign_pitch(
    candidate_id: str,
    pitch_text: str,
    fact_a_statement: str,
    fact_b_phrase: str,
) -> RubricResult:
    """Apply the base evidence rubric, then one deterministic cohort objection."""

    base = evaluate_pitch(pitch_text, fact_a_statement, fact_b_phrase)
    if base.status != "booked":
        return base

    requirements = {
        "samir_patel": (
            BUSINESS_IMPACT_TACTIC,
            "REJECTED_WEAK_VALUE",
            REJECTED_WEAK_VALUE_RESPONSE,
        ),
        "carla_mendez": (PROOF_TACTIC, "REJECTED_NO_PROOF", REJECTED_NO_PROOF_RESPONSE),
        "ben_carter": (
            LOW_FRICTION_TACTIC,
            "REJECTED_HIGH_FRICTION",
            REJECTED_HIGH_FRICTION_RESPONSE,
        ),
        "tasha_green": (PILOT_TACTIC, "REJECTED_TIMING", REJECTED_TIMING_RESPONSE),
    }
    requirement = requirements.get(candidate_id)
    if requirement and not _contains_normalized(pitch_text, requirement[0]):
        return RubricResult(
            status="rejected",
            code=requirement[1],
            missing_claims=(),
            response=requirement[2],
        )
    return base


def render_conversation(candidate_id: str, pitch_text: str, result: RubricResult) -> str:
    """Render a deterministic but human-sounding, multi-turn demo transcript."""

    name = candidate_id.split("_", 1)[0].title()
    exchanges = {
        "REJECTED_MISSING_FACT_B": [
            f"{name.upper()}: Hi—yeah, I've got a minute. What's this about?",
            f"AGENT: {pitch_text}",
            f"{name.upper()}: Okay… so what, specifically, would you change on our site?",
            "AGENT: I'd start by looking at where visitors drop off and which contact path is hardest to use.",
        ],
        "REJECTED_WEAK_VALUE": [
            f"{name.upper()}: Hey. I've got, uh, maybe two minutes.",
            f"AGENT: {pitch_text}",
            f"{name.upper()}: Right, but is this mostly a design thing?",
            "AGENT: The design supports it, but the goal is getting more qualified customers to contact you.",
        ],
        "REJECTED_NO_PROOF": [
            f"{name.upper()}: Hi, this is {name}.",
            f"AGENT: {pitch_text}",
            f"{name.upper()}: Hmm. We get calls like this a lot. Do you have relevant work I can see?",
            "AGENT: I can walk you through the opportunity report and the changes we'd prioritize.",
        ],
        "REJECTED_HIGH_FRICTION": [
            f"{name.upper()}: Yeah, go ahead.",
            f"AGENT: {pitch_text}",
            f"{name.upper()}: Wait—are you talking about replacing the whole website?",
            "AGENT: Not necessarily. We'd keep what works and focus on the path that produces inquiries.",
        ],
        "REJECTED_TIMING": [
            f"{name.upper()}: Hi. Sorry, it's a little noisy here—what can I do for you?",
            f"AGENT: {pitch_text}",
            f"{name.upper()}: I like the idea. I'm just worried this turns into a six-week thing.",
            "AGENT: We can keep the first step small and decide on a rebuild only after you see the scope.",
        ],
        "MEETING_BOOKED": [
            f"{name.upper()}: Hello?",
            f"AGENT: {pitch_text}",
            f"{name.upper()}: Okay, that's… surprisingly specific. What's the commitment for the first conversation?",
            "AGENT: Twenty minutes. I'll show the research, the highest-impact page, and a fixed first step—no prep needed.",
            f"{name.upper()}: And you'll send the examples beforehand?",
            "AGENT: Absolutely. I'll include the audit and the comparable before-and-after.",
        ],
    }
    lines = exchanges.get(result.code, [f"AGENT: {pitch_text}"])
    lines.append(f"{name.upper()}: {result.response}")
    return "\n".join(lines) + "\n"
