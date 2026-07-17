"""Read-only FastAPI projection of persisted PitchLoop demo artifacts.

Launch from the repository root with ``python -m demo.ui`` and open
http://127.0.0.1:8000. Only direct ``runs/fake-demo.*`` children are selectable;
paths stored inside artifacts are display data and are never followed.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agent.artifacts import redact


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = REPO_ROOT / "runs"
TEMPLATES_ROOT = Path(__file__).with_name("templates")
STATIC_ROOT = Path(__file__).with_name("static")
CORRELATION_ID = re.compile(r"^[A-Za-z0-9_-]+$")
SENSITIVE_TEXT = re.compile(
    r"(?im)\b(authorization|token|secret|password|api[_-]?key|account[_-]?id|"
    r"wallet(?:[_-]?id)?|phone(?:[_-]?number)?)\b(\s*[:=]\s*)"
    r"(?:bearer\s+)?(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s]+)"
)

DETAIL_ARTIFACTS = (
    ("policy-deny", "Policy denial", "policy/deny.json"),
    ("policy-allow", "Policy allowance", "policy/allow.json"),
    ("pitch-1", "Pitch 1", "pitch/pitch_1.md"),
    ("call-1", "Call 1 result", "calls/call_1_result.json"),
    ("transcript-1", "Call 1 transcript", "calls/call_1_transcript.txt"),
    ("diagnosis", "Diagnosis", "evidence/diagnosis.json"),
    ("marketplace-search", "Fact B marketplace search", "zero/search_fact_b.json"),
    ("author-prompt", "Tool author prompt", "tools/author_prompt.txt"),
    ("tool-manifest", "Generated tool manifest", "tools/generated_manifest.json"),
    ("conformance", "Tool conformance", "tools/conformance_result.json"),
    ("pull-request", "Pull request", "repo/pr.json"),
    ("merge", "Merge", "repo/merge.json"),
    ("reload", "Tool reload", "tools/reload.json"),
    ("fact-a-receipt", "Fact A receipt", "zero/fact_a_receipt.json"),
    ("call-1-receipt", "Call 1 receipt", "zero/call_1_receipt.json"),
    ("pitch-2", "Pitch 2", "pitch/pitch_2.md"),
    ("call-2", "Call 2 result", "calls/call_2_result.json"),
    ("transcript-2", "Call 2 transcript", "calls/call_2_transcript.txt"),
    ("call-2-receipt", "Call 2 receipt", "zero/call_2_receipt.json"),
    ("meeting", "Final meeting", "final/meeting.json"),
)


def discover_runs(runs_root: str | Path = RUNS_ROOT) -> list[Path]:
    """Return direct fake-demo run directories, newest mtime first."""

    root = Path(runs_root)
    if not root.is_dir():
        return []
    resolved_root = root.resolve()
    return sorted(
        (
            path
            for path in root.glob("fake-demo.*")
            if path.is_dir()
            and not path.is_symlink()
            and path.resolve().parent == resolved_root
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )


def select_run(name: str | None, runs_root: str | Path = RUNS_ROOT) -> Path | None:
    """Select by discovered basename only; arbitrary paths are rejected."""

    runs = discover_runs(runs_root)
    if name is None:
        return runs[0] if runs else None
    matches = {path.name: path for path in runs}
    try:
        return matches[name]
    except KeyError as exc:
        raise ValueError(f"unknown run: {name}") from exc


def safe_artifact_path(run_dir: str | Path, relative_path: str | Path) -> Path:
    """Resolve an allowlisted relative artifact path without following escapes."""

    root = Path(run_dir).resolve()
    relative = Path(relative_path)
    if relative.is_absolute():
        raise ValueError("artifact path must be relative")
    path = (root / relative).resolve()
    if path != root and not path.is_relative_to(root):
        raise ValueError("artifact path escapes run directory")
    return path


def _artifact_path(run_dir: Path, relative_path: str, errors: list[str]) -> Path | None:
    try:
        return safe_artifact_path(run_dir, relative_path)
    except ValueError:
        errors.append(f"{relative_path}: blocked path")
        return None


def _redact_text(value: str) -> str:
    return SENSITIVE_TEXT.sub(r"\1\2***REDACTED***", value)


def _read_json(run_dir: Path, relative_path: str, errors: list[str]) -> Any:
    path = _artifact_path(run_dir, relative_path, errors)
    if path is None or not path.is_file():
        return {}
    try:
        return redact(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"{relative_path}: {type(exc).__name__}")
        return {}


def _read_jsonl(run_dir: Path, relative_path: str, errors: list[str]) -> list[dict[str, Any]]:
    path = _artifact_path(run_dir, relative_path, errors)
    if path is None:
        return []
    if not path.is_file():
        errors.append(f"{relative_path}: missing")
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        errors.append(f"{relative_path}: {type(exc).__name__}")
        return []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise TypeError("record is not an object")
            records.append(redact(record))
        except (json.JSONDecodeError, TypeError) as exc:
            errors.append(f"{relative_path}:{line_number}: {type(exc).__name__}")
    return records


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _summary(
    spec: dict[str, Any],
    index: dict[str, Any],
    meeting: dict[str, Any],
    events: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    finished = next((event for event in reversed(events) if event.get("type") == "run_finished"), {})
    timestamps = [parsed for event in events if (parsed := _parse_time(event.get("ts")))]
    elapsed_seconds = (
        max(0.0, (timestamps[-1] - timestamps[0]).total_seconds())
        if len(timestamps) > 1
        else None
    )
    calls = [item for item in evidence if item.get("kind") == "call"]
    call_events = [event for event in events if event.get("type") == "call_placed"]
    allowed = next(
        (item for item in evidence if item.get("kind") == "policy" and item.get("policy_decision") == "allow"),
        {},
    )
    spent = finished.get("spent_cents", meeting.get("spent_cents", 0))
    budget = spec.get("budget_cents", 0)
    run_id = index.get("run_id", spec.get("run_id", "Unknown"))
    outcome = finished.get("outcome", index.get("outcome", "IN PROGRESS"))
    final_state = finished.get(
        "final_state",
        next((event.get("state") for event in reversed(events) if event.get("state")), "Unknown"),
    )
    candidate = meeting.get("candidate_id", allowed.get("candidate_id", "Unknown"))
    return {
        "run_id": run_id if isinstance(run_id, str) else "Unknown",
        "outcome": outcome if isinstance(outcome, str) else "IN PROGRESS",
        "final_state": final_state if isinstance(final_state, str) else "Unknown",
        "budget_cents": budget,
        "spent_cents": spent,
        "budget_remaining_cents": budget - spent if isinstance(budget, int) and isinstance(spent, int) else None,
        "calls_placed": len(calls) or len(call_events),
        "candidate": candidate if isinstance(candidate, str) else "Unknown",
        "elapsed_seconds": elapsed_seconds,
        "elapsed": f"{elapsed_seconds:.2f}s" if elapsed_seconds is not None else "Unknown",
    }


def _infer_mode(run_dir: Path, _index: dict[str, Any]) -> str:
    """Require affirmative proof for every external adapter; ambiguity is local."""

    policy = [_quiet_json(run_dir, f"policy/{decision}.json") for decision in ("deny", "allow")]
    repo = [_quiet_json(run_dir, f"repo/{action}.json") for action in ("pr", "merge")]
    calls = [_quiet_json(run_dir, f"calls/call_{number}_provider.json") for number in (1, 2)]
    zero_proof = all(
        (path := _artifact_path(run_dir, relative, [])) is not None and path.is_file()
        for relative in (
            "zero/cli_install_proof.txt",
            "zero/wallet_claim_proof.json",
            "zero/opening_balance.json",
            "zero/closing_balance.json",
        )
    )
    evidence_proof = _quiet_json(run_dir, "evidence/live_proof.json")
    if (
        all(item.get("adapter_mode") == "live" for item in policy + repo)
        and all(item.get("adapter_mode") == "live" for item in calls)
        and evidence_proof.get("adapter_mode") == "live"
        and zero_proof
    ):
        return "LIVE"
    return "FAKE / LOCAL"


def _quiet_json(run_dir: Path, relative_path: str) -> dict[str, Any]:
    value = _read_json(run_dir, relative_path, [])
    return value if isinstance(value, dict) else {}


def _details(run_dir: Path, errors: list[str]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for key, label, relative_path in DETAIL_ARTIFACTS:
        path = _artifact_path(run_dir, relative_path, errors)
        if path is None or not path.is_file():
            continue
        try:
            if path.suffix == ".json":
                content = _read_json(run_dir, relative_path, errors)
                formatted = json.dumps(content, indent=2, ensure_ascii=False)
                kind = "json"
            else:
                formatted = path.read_text(encoding="utf-8")
                content = _redact_text(formatted)
                formatted = str(content)
                kind = "text"
        except (OSError, UnicodeError) as exc:
            errors.append(f"{relative_path}: {type(exc).__name__}")
            continue
        details.append(
            {"key": key, "label": label, "path": relative_path, "kind": kind, "content": content, "formatted": formatted}
        )
    return details


def _flow_steps(
    events: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    diagnosis: dict[str, Any],
    details: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project the presentation checkpoints from canonical events and evidence."""

    event_types = {
        event.get("type") for event in events if isinstance(event.get("type"), str)
    }
    detail = {item["key"]: item.get("content") for item in details}
    calls = [item for item in evidence if item.get("kind") == "call"]

    def has_evidence(**expected: str) -> bool:
        return any(all(item.get(key) == value for key, value in expected.items()) for item in evidence)

    def call_status(status: str) -> bool:
        return any(
            isinstance(item.get("value"), dict) and item["value"].get("status") == status
            for item in calls
        )

    missing_claims = diagnosis.get("missing_claims", [])
    search = detail.get("marketplace-search")
    conformance = detail.get("conformance")
    return [
        {"label": "Policy denied", "detail": "Non-consenting candidate held at gate", "completed": has_evidence(kind="policy", policy_decision="deny")},
        {"label": "Policy allowed", "detail": "Consented candidate advanced", "completed": has_evidence(kind="policy", policy_decision="allow")},
        {"label": "Fact A purchased", "detail": "Hiring signal acquired", "completed": has_evidence(claim="fact_a")},
        {"label": "Call 1 rejected", "detail": "Pitch lacked fact_b", "completed": call_status("rejected")},
        {"label": "Gap diagnosed", "detail": "Deadline evidence identified", "completed": "diagnosis" in event_types and isinstance(missing_claims, list) and "fact_b" in missing_claims},
        {"label": "Marketplace miss", "detail": "No matching Fact B service", "completed": isinstance(search, dict) and search.get("no_match") is True},
        {"label": "Tool authored", "detail": "One constrained capability generated", "completed": "tool_authored" in event_types},
        {"label": "Conformance passed", "detail": "Fixed suite returned exit 0", "completed": isinstance(conformance, dict) and conformance.get("exit_code") == 0},
        {"label": "PR opened + merged", "detail": "Generated capability accepted", "completed": {"pr_opened", "pr_merged"} <= event_types},
        {"label": "Fact B collected", "detail": "Deadline signal normalized", "completed": has_evidence(claim="fact_b")},
        {"label": "Call 2 booked", "detail": "Revised pitch used both facts", "completed": call_status("booked")},
        {"label": "Meeting booked", "detail": "The loop closed", "completed": "meeting_booked" in event_types},
    ]


def build_view_model(run_dir: str | Path) -> dict[str, Any]:
    """Build the template-facing view model without mutating any artifact."""

    run = Path(run_dir).resolve()
    errors: list[str] = []
    spec = _read_json(run, "spec.json", errors)
    index = _read_json(run, "final/artifact_index.json", errors)
    meeting = _read_json(run, "final/meeting.json", errors)
    diagnosis = _read_json(run, "evidence/diagnosis.json", errors)
    events = _read_jsonl(run, "events.jsonl", errors)
    evidence = _read_jsonl(run, "evidence/normalized.jsonl", errors)
    spec = spec if isinstance(spec, dict) else {}
    index = index if isinstance(index, dict) else {}
    meeting = meeting if isinstance(meeting, dict) else {}
    diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
    for field in ("evidence_ids", "missing_claims", "present_claims"):
        value = diagnosis.get(field)
        diagnosis[field] = (
            [item for item in value if isinstance(item, str)]
            if isinstance(value, list)
            else []
        )
    if not isinstance(diagnosis.get("next_action"), str):
        diagnosis["next_action"] = ""
    cited = diagnosis.get("evidence_ids", [])
    cited_ids = set(cited) if isinstance(cited, list) else set()
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for item in evidence:
        for field in ("evidence_id", "claim", "source", "source_ref", "occurred_at", "policy_decision"):
            if item.get(field) is not None and not isinstance(item.get(field), str):
                item[field] = str(item[field])
        evidence_id = item.get("evidence_id")
        provenance = item.get("provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        item["provenance"] = provenance
        item["value"] = item.get("value") if isinstance(item.get("value"), dict) else {}
        correlation = provenance.get("correlation_id")
        item["diagnosis_cited"] = evidence_id in cited_ids
        item["raw_artifact"] = None
        item["normalized_artifact"] = None
        if isinstance(correlation, str) and CORRELATION_ID.fullmatch(correlation):
            for field, suffix in (("raw_artifact", "raw"), ("normalized_artifact", "normalized")):
                relative = f"evidence/{correlation}_{suffix}.json"
                path = _artifact_path(run, relative, errors)
                if path is not None and path.is_file():
                    item[field] = relative
        if isinstance(evidence_id, str):
            evidence_by_id[evidence_id] = item
    for sequence, event in enumerate(events, 1):
        for field in (
            "type",
            "ts",
            "state",
            "claim",
            "evidence_id",
            "next_action",
            "selected_candidate",
            "from",
            "to",
        ):
            if not isinstance(event.get(field), str):
                event[field] = "unknown" if field == "type" else ""
        missing = event.get("missing")
        event["missing"] = (
            [item for item in missing if isinstance(item, str)]
            if isinstance(missing, list)
            else []
        )
        event["sequence"] = sequence
        event["evidence"] = evidence_by_id.get(event.get("evidence_id"))
    details = _details(run, errors)
    return {
        "run_name": run.name,
        "mode": _infer_mode(run, index),
        "summary": _summary(spec, index, meeting, events, evidence),
        "spec": spec,
        "events": events,
        "event_types": {
            event.get("type") for event in events if isinstance(event.get("type"), str)
        },
        "evidence": evidence,
        "diagnosis": diagnosis,
        "details": details,
        "flow_steps": _flow_steps(events, evidence, diagnosis, details),
        "errors": errors,
    }


def create_app(
    runs_root: str | Path = RUNS_ROOT,
    templates_root: str | Path = TEMPLATES_ROOT,
) -> FastAPI:
    """Create the read-only UI app; injectable roots keep focused tests small."""

    root = Path(runs_root)
    templates = Jinja2Templates(directory=str(templates_root))
    application = FastAPI(title="PitchLoop visual demo")
    if STATIC_ROOT.is_dir():
        application.mount("/static", StaticFiles(directory=str(STATIC_ROOT)), name="static")

    @application.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, run: str | None = None) -> HTMLResponse:
        try:
            selected = select_run(run, root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        runs = discover_runs(root)
        model = (
            build_view_model(selected)
            if selected
            else {
                "run_name": None,
                "mode": "FAKE / LOCAL",
                "summary": {},
                "spec": {},
                "events": [],
                "event_types": set(),
                "evidence": [],
                "diagnosis": {},
                "details": [],
                "flow_steps": [],
                "errors": ["No runs/fake-demo.* directories found."],
            }
        )
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"model": model, "runs": [path.name for path in runs]},
        )

    return application


app = create_app()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the read-only PitchLoop demo UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--runs-dir", default=str(RUNS_ROOT), type=Path)
    args = parser.parse_args(argv)
    uvicorn.run(create_app(args.runs_dir), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
