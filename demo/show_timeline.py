"""Render the fake/local demo timeline from persisted run artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {}


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _campaign_timeline(run_dir: Path, events: list[dict[str, Any]]) -> list[str]:
    lines = ["[MODE] fake provider adapters; local evidence normalization"]
    candidate = "unknown candidate"
    call_number = 0

    for event in events:
        event_type = event.get("type")
        if event_type == "state_enter" and event.get("selected_candidate"):
            candidate = str(event["selected_candidate"])
        elif event_type == "policy_decision":
            decision = _read(run_dir / f"contacts/{candidate}/policy.json")
            allowed = decision.get("allowed")
            status = decision.get("status_code", "?")
            outcome = "allowed" if allowed else "denied"
            lines.append(f"[POLICY/FAKE] {candidate} {outcome} ({status})")
        elif event_type == "call_placed":
            call_number += 1
            summary = _read(run_dir / f"calls/call-{call_number:03d}/summary.json")
            result = _read(run_dir / f"calls/call_{call_number}_result.json")
            status = event.get("status") or summary.get("status") or result.get("status", "completed")
            code = event.get("code") or summary.get("code") or result.get("code")
            suffix = f" ({code})" if code else ""
            lines.append(f"[CALL {call_number}] {candidate}: {status}{suffix}")
        elif event_type == "diagnosis":
            missing = event.get("missing") or event.get("missing_claims") or []
            ids = event.get("evidence_ids") or []
            detail = f"missing {','.join(map(str, missing))}" if missing else "no evidence gap"
            if ids:
                detail += f"; cites {','.join(map(str, ids))}"
            lines.append(f"[EVIDENCE/LOCAL] {candidate}: {detail}")
        elif event_type == "reflection_recorded":
            number = event.get("call_number", call_number)
            who = event.get("candidate_id", candidate)
            lines.append(f"[REFLECTION {number}] {who}")
            for label, key in (
                ("went well", "went_well"),
                ("went wrong", "went_wrong"),
                ("learned", "learned"),
                ("next", "next_change"),
            ):
                values = event.get(key) or []
                if values:
                    lines.append(f"  {label}: {'; '.join(map(str, values))}")
        elif event_type == "strategy_updated":
            version = event.get("version", "?")
            tactics = event.get("tactics") or []
            detail = "; ".join(map(str, tactics)) or "no added tactics"
            lines.append(f"[STRATEGY v{version}] {detail}")
        elif event_type == "zero_search":
            capability = event.get("capability", "capability")
            count = event.get("n", "?")
            lines.append(f"[ZERO/FAKE] searched {capability}: {count} match(es)")
        elif event_type == "prospects_discovered":
            lines.append(f"[QUEUE] Zero.xyz discovered {len(event.get('candidate_ids') or [])} fictional businesses")
        elif event_type == "tool_need_evaluated":
            lines.append(f"[TOOLS] {candidate}: assessed research needs before calling")
        elif event_type == "tool_authored":
            lines.append("[CODE] generated website opportunity audit")
        elif event_type == "tool_conformance" and event.get("exit_code") == 0:
            lines.append("[CODE] generated tool passed conformance")
        elif event_type == "pr_merged" and event.get("merged") is True:
            lines.append(f"[GITHUB/FAKE] tool PR merged at {event.get('sha', '?')}")
        elif event_type == "tool_reused":
            lines.append(
                f"[TOOL] reused website opportunity audit for "
                f"{event.get('candidate_id', candidate)}"
            )
        elif event_type == "candidate_completed":
            lines.append(
                f"[CANDIDATE] {event.get('candidate_id', candidate)}: "
                f"{event.get('outcome', 'completed')}"
            )
        elif event_type == "meeting_booked":
            lines.append(f"[SUCCESS] meeting booked with {candidate}")
    return lines


def timeline(run_dir: Path) -> list[str]:
    events = _read_events(run_dir / "events.jsonl")
    if any(event.get("type") in {"reflection_recorded", "strategy_updated", "candidate_completed"} for event in events):
        return _campaign_timeline(run_dir, events)

    lines: list[str] = []
    denied = _read(run_dir / "policy/deny.json")
    allowed = _read(run_dir / "policy/allow.json")
    fact_a = _read(run_dir / "zero/fact_a_result.json")
    receipt = _read(run_dir / "zero/fact_a_receipt.json")
    call_1 = _read(run_dir / "calls/call_1_result.json")
    diagnosis = _read(run_dir / "evidence/diagnosis.json")
    search_b = _read(run_dir / "zero/search_fact_b.json")
    conformance = _read(run_dir / "tools/conformance_result.json")
    pull_request = _read(run_dir / "repo/pr.json")
    merge = _read(run_dir / "repo/merge.json")
    reload_result = _read(run_dir / "tools/reload.json")
    call_2 = _read(run_dir / "calls/call_2_result.json")

    denied_decision = denied.get("decision", denied)
    allowed_decision = allowed.get("decision", allowed)
    if denied_decision.get("allowed") is False and denied_decision.get("status_code") == 403:
        lines.append(
            f"[POLICY/FAKE] {denied.get('candidate_id', 'alex_rivera')} denied (403)"
        )
    if allowed_decision.get("allowed") is True and allowed_decision.get("status_code") == 200:
        lines.append(
            f"[POLICY/FAKE] {allowed.get('candidate_id', 'maya_chen')} allowed (200)"
        )
    fact_result = fact_a.get("result", fact_a)
    if fact_result.get("statement") and isinstance(receipt.get("amount_cents"), int):
        service = (
            fact_a.get("service_id")
            or fact_a.get("service", {}).get("name")
            or fact_result.get("source")
            or receipt.get("service_id", "service")
        )
        cents = receipt["amount_cents"]
        lines.append(f"[ZERO/FAKE] discovered {service} and paid ${cents / 100:.2f}")
    missing = call_1.get("missing_claims", [])
    if call_1.get("status") == "rejected" and "fact_b" in missing:
        lines.append(f"[CALL 1] rejected: missing {','.join(missing)}")
    if "fact_b" in diagnosis.get("missing_claims", []) and diagnosis.get("evidence_ids"):
        ids = diagnosis.get("evidence_ids") or diagnosis.get("ids") or []
        lines.append(f"[EVIDENCE/LOCAL] diagnosis cites {','.join(ids)}")
    if search_b.get("no_match") is True:
        lines.append("[ZERO/FAKE] no marketplace capability matched website opportunity audit")
    if conformance.get("passed") is True or conformance.get("exit_code") == 0:
        lines.append("[CODE] generated tool; fixed conformance passed")
    merge_result = merge.get("merge", merge)
    pr_result = pull_request.get("pull_request", pull_request)
    if merge_result.get("merged") is True:
        number = (
            pr_result.get("number")
            or merge.get("pull_request_number")
            or pull_request.get("gh_view", {}).get("number")
            or "?"
        )
        merge_sha = (
            merge_result.get("merge_sha")
            or merge.get("gh_view", {}).get("mergeCommit", {}).get("oid")
            or "?"
        )
        lines.append(
            f"[GITHUB/FAKE] PR #{number} merged at {merge_sha}"
        )
    if reload_result.get("found_fact_b") is True:
        lines.append("[TOOL] website opportunity audit acquired from generated tool")
    if call_2.get("status") == "booked":
        lines.append("[CALL 2] meeting booked")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=Path("runs/demo-001"))
    args = parser.parse_args()
    print("\n".join(timeline(args.run_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
