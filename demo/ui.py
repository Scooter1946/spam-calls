"""Desktop campaign console projected from append-only run artifacts."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agent.artifacts import redact


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = REPO_ROOT / "runs"
TEMPLATES_ROOT = Path(__file__).with_name("templates")
STATIC_ROOT = Path(__file__).with_name("static")
RUN_NAME = re.compile(r"^(?:campaign-|fake-demo\.)[A-Za-z0-9._-]+$")
CALL_NAME = re.compile(r"^call-(\d+)$")
SENSITIVE_TEXT = re.compile(
    r"(?im)\b(authorization|token|secret|password|api[_-]?key|account[_-]?id|"
    r"wallet(?:[_-]?id)?|phone(?:[_-]?number)?)\b(\s*[:=]\s*)"
    r"(?:bearer\s+)?(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s]+)"
)


def discover_runs(runs_root: str | Path = RUNS_ROOT) -> list[Path]:
    root = Path(runs_root)
    if not root.is_dir():
        return []
    resolved = root.resolve()
    return sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and RUN_NAME.fullmatch(path.name)
            and path.resolve().parent == resolved
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )


def select_run(name: str, runs_root: str | Path = RUNS_ROOT) -> Path:
    if not RUN_NAME.fullmatch(name):
        raise ValueError("invalid run name")
    runs = {path.name: path for path in discover_runs(runs_root)}
    if name not in runs:
        raise ValueError(f"unknown run: {name}")
    return runs[name]


def safe_artifact_path(run_dir: str | Path, relative_path: str | Path) -> Path:
    root = Path(run_dir).resolve()
    relative = Path(relative_path)
    if relative.is_absolute():
        raise ValueError("artifact path must be relative")
    path = (root / relative).resolve()
    if path != root and not path.is_relative_to(root):
        raise ValueError("artifact path escapes run directory")
    return path


def _read_text(run: Path, relative: str, errors: list[str]) -> str:
    try:
        path = safe_artifact_path(run, relative)
        return SENSITIVE_TEXT.sub(r"\1\2***REDACTED***", path.read_text(encoding="utf-8")) if path.is_file() else ""
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(f"{relative}: {type(exc).__name__}")
        return ""


def _read_json(run: Path, relative: str, errors: list[str]) -> dict[str, Any]:
    text = _read_text(run, relative, errors)
    if not text:
        return {}
    try:
        value = redact(json.loads(text))
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        errors.append(f"{relative}: invalid JSON")
        return {}


def _read_jsonl(run: Path, relative: str, errors: list[str]) -> list[dict[str, Any]]:
    text = _read_text(run, relative, errors)
    records: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = redact(json.loads(line))
            if isinstance(value, dict):
                records.append(value)
        except json.JSONDecodeError:
            errors.append(f"{relative}:{number}: invalid JSON")
    return records


def _pretty(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _call_dirs(run: Path) -> list[Path]:
    calls = safe_artifact_path(run, "calls")
    if not calls.is_dir():
        return []
    return sorted(
        (path for path in calls.iterdir() if path.is_dir() and CALL_NAME.fullmatch(path.name)),
        key=lambda path: int(CALL_NAME.fullmatch(path.name).group(1)),  # type: ignore[union-attr]
    )


def _build_calls(
    run: Path,
    spec: dict[str, Any],
    evidence: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    errors: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    profiles = spec.get("candidate_profiles", {})
    call_evidence = [item for item in evidence if item.get("kind") == "call"]
    for index, directory in enumerate(_call_dirs(run)):
        call_id = directory.name
        summary = _read_json(run, f"calls/{call_id}/summary.json", errors)
        candidate = summary.get("candidate_id") or (
            call_evidence[index].get("candidate_id") if index < len(call_evidence) else None
        )
        reflection = _read_json(run, f"calls/{call_id}/reflection.json", errors) or _read_json(
            run, f"reflections/{call_id}.json", errors
        )
        diagnosis = _read_json(run, f"calls/{call_id}/diagnosis.json", errors)
        provider = _read_json(run, f"calls/{call_id}/provider_receipt.json", errors)
        enrichment = _read_json(run, f"calls/{call_id}/enrichment_receipt.json", errors)
        policy = _read_json(run, f"contacts/{candidate}/policy.json", errors) if candidate else {}
        linked_evidence = [item for item in evidence if item.get("candidate_id") == candidate]
        call_actions = [
            item
            for item in actions
            if item.get("candidate_id") == candidate
            or any(call_id in str(ref) for ref in item.get("artifact_refs", []))
        ]
        calls.append(
            {
                "id": call_id,
                "number": index + 1,
                "candidate_id": candidate or "Unknown",
                "profile": profiles.get(candidate, {}),
                "status": summary.get("status", "running"),
                "code": summary.get("code", ""),
                "amount_cents": summary.get("amount_cents", 0),
                "pitch": _read_text(run, f"calls/{call_id}/pitch.md", errors),
                "transcript": _read_text(run, f"calls/{call_id}/transcript.txt", errors),
                "summary": summary,
                "provider_receipt": provider,
                "enrichment_receipt": enrichment,
                "policy_receipt": policy,
                "diagnosis": diagnosis,
                "reflection": reflection,
                "evidence": linked_evidence,
                "actions": call_actions,
            }
        )

    # Older two-call runs remain inspectable without changing their artifacts.
    if not calls:
        for index in range(1, 100):
            summary = _read_json(run, f"calls/call_{index}_result.json", [])
            if not summary:
                break
            evidence_item = call_evidence[index - 1] if index <= len(call_evidence) else {}
            candidate = evidence_item.get("candidate_id", "Unknown")
            calls.append(
                {
                    "id": f"call-{index:03d}", "number": index, "candidate_id": candidate,
                    "status": summary.get("status", "completed"), "code": summary.get("code", ""),
                    "amount_cents": summary.get("amount_cents", 0),
                    "pitch": _read_text(run, f"pitch/pitch_{index}.md", errors),
                    "transcript": _read_text(run, f"calls/call_{index}_transcript.txt", errors),
                    "summary": summary, "provider_receipt": summary.get("receipt", {}),
                    "enrichment_receipt": {}, "policy_receipt": _read_json(run, f"contacts/{candidate}/policy.json", errors),
                    "diagnosis": _read_json(run, "evidence/diagnosis.json", errors), "reflection": {},
                    "evidence": [item for item in evidence if item.get("candidate_id") == candidate],
                    "actions": [item for item in actions if item.get("candidate_id") == candidate],
                }
            )

    by_candidate = {call["candidate_id"]: call for call in calls}
    contacts: list[dict[str, Any]] = []
    for position, candidate in enumerate(spec.get("candidates", []), 1):
        call = by_candidate.get(candidate)
        policy = _read_json(run, f"contacts/{candidate}/policy.json", errors)
        if call:
            status = call["status"]
        elif policy and not policy.get("allowed", False):
            status = "denied"
        elif policy:
            status = "queued"
        else:
            status = "remaining"
        contacts.append(
            {
                "candidate_id": candidate,
                "profile": profiles.get(candidate, {}),
                "queue_position": position,
                "status": status,
                "call": call,
                "policy": policy,
                "tool_assessment": _read_json(run, f"contacts/{candidate}/tool_assessment.json", errors),
            }
        )
    return calls, contacts


def build_view_model(run_dir: str | Path, selected_call_id: str | None = None) -> dict[str, Any]:
    run = Path(run_dir).resolve()
    errors: list[str] = []
    spec = _read_json(run, "spec.json", errors)
    events = _read_jsonl(run, "events.jsonl", errors)
    evidence = _read_jsonl(run, "evidence/normalized.jsonl", errors)
    actions = _read_jsonl(run, "actions.jsonl", errors)
    meeting = _read_json(run, "final/meeting.json", errors)
    failure = _read_json(run, "final/failure.json", errors)
    strategies = [
        _read_json(run, str(path.relative_to(run)), errors)
        for path in sorted((run / "strategy").glob("v*.json"))
    ] if (run / "strategy").is_dir() else []
    reflections = [
        _read_json(run, str(path.relative_to(run)), errors)
        for path in sorted((run / "reflections").glob("call-*.json"))
    ] if (run / "reflections").is_dir() else []
    calls, contacts = _build_calls(run, spec, evidence, actions, errors)
    selected_call = next((call for call in calls if call["id"] == selected_call_id), None)
    if selected_call_id and selected_call is None:
        raise ValueError(f"unknown call: {selected_call_id}")
    finished = next((item for item in reversed(events) if item.get("type") == "run_finished"), {})
    spent = finished.get("spent_cents")
    if not isinstance(spent, int):
        spent = sum(item.get("amount_cents", 0) for item in actions if isinstance(item.get("amount_cents"), int))
    latest_state = next((item.get("state") for item in reversed(events) if item.get("state")), "STARTING")
    active_candidate = next(
        (
            item.get("candidate_id") or item.get("selected_candidate")
            for item in reversed(actions + events)
            if item.get("candidate_id") or item.get("selected_candidate")
        ),
        None,
    )
    if not (finished or failure) and active_candidate:
        for contact in contacts:
            if contact["candidate_id"] == active_candidate and not contact["call"] and contact["status"] != "denied":
                contact["status"] = "calling" if latest_state in {"CALL", "VERIFY_CALL"} else "in_progress"
    booked = sum(call["status"] == "booked" for call in calls)
    denied = sum(contact["status"] == "denied" for contact in contacts)
    remaining = sum(contact["status"] in {"remaining", "queued"} for contact in contacts)
    tool_manifest = _read_json(run, "tools/generated_manifest.json", errors)
    tool_inventory = _read_json(run, "tools/inventory.json", errors).get("tools", [])
    tool_actions = [item for item in actions if any(word in str(item.get("action", "")) for word in ("tool", "conformance", "pr_", "merge"))]
    return {
        "run_name": run.name,
        "mode": "FAKE / LOCAL",
        "running": not bool(finished or failure),
        "objective": spec.get("objective") or spec.get("goal") or "Campaign starting…",
        "state": latest_state,
        "outcome": finished.get("outcome") or ("FAILED" if failure else "IN PROGRESS"),
        "spec": spec,
        "stats": {
            "calls": len(calls), "denied": denied, "remaining": remaining, "booked": booked,
            "spent_cents": spent, "budget_cents": spec.get("budget_cents", 0),
        },
        "calls": calls,
        "contacts": contacts,
        "selected_call": selected_call,
        "events": events,
        "actions": actions,
        "evidence": evidence,
        "reflections": reflections,
        "strategies": strategies,
        "current_strategy": strategies[-1] if strategies else {},
        "tool_manifest": tool_manifest,
        "zero_tools": [tool for tool in tool_inventory if tool.get("provider") == "zero.xyz"],
        "custom_tools": [tool for tool in tool_inventory if tool.get("provider") == "custom"],
        "tool_actions": tool_actions,
        "meeting": meeting,
        "failure": failure,
        "errors": sorted(set(errors)),
        "pretty": _pretty,
    }


def _start_run(root: Path, objective: str) -> Path:
    objective = objective.strip()
    if not objective:
        raise ValueError("Enter a campaign objective.")
    if len(objective) > 2000:
        raise ValueError("Campaign objective must be 2,000 characters or fewer.")
    root.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="campaign-", dir=root)).resolve()
    if run.parent != root.resolve():
        raise ValueError("could not create a safe run directory")
    env = os.environ.copy()
    env.update(
        {
            "PITCHLOOP_RUN_DIR": str(run),
            "PITCHLOOP_OBJECTIVE": objective,
            "PITCHLOOP_STEP_DELAY_SECONDS": "0.35",
        }
    )
    log = (run / "runner.log").open("w", encoding="utf-8")
    try:
        subprocess.Popen(
            [str(REPO_ROOT / "demo" / "run_demo.sh")],
            cwd=REPO_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log.close()
    return run


def create_app(runs_root: str | Path = RUNS_ROOT, templates_root: str | Path = TEMPLATES_ROOT) -> FastAPI:
    root = Path(runs_root)
    templates = Jinja2Templates(directory=str(templates_root))
    application = FastAPI(title="PitchLoop campaign console")
    application.mount("/static", StaticFiles(directory=str(STATIC_ROOT)), name="static")

    @application.get("/", response_class=HTMLResponse)
    def landing(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request=request, name="index.html", context={"model": None, "runs": discover_runs(root)})

    @application.post("/runs")
    async def start_campaign(request: Request) -> RedirectResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        objective = parse_qs(body, keep_blank_values=True).get("objective", [""])[0]
        try:
            run = _start_run(root, objective)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/runs/{run.name}", status_code=303)

    def render_run(request: Request, run_name: str, call_id: str | None = None) -> HTMLResponse:
        try:
            run = select_run(run_name, root)
            model = build_view_model(run, call_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"model": model, "runs": discover_runs(root)},
        )

    @application.get("/runs/{run_name}", response_class=HTMLResponse)
    def dashboard(request: Request, run_name: str) -> HTMLResponse:
        return render_run(request, run_name)

    @application.get("/runs/{run_name}/calls/{call_id}", response_class=HTMLResponse)
    def call_detail(request: Request, run_name: str, call_id: str) -> HTMLResponse:
        if not CALL_NAME.fullmatch(call_id):
            raise HTTPException(status_code=404, detail="invalid call")
        return render_run(request, run_name, call_id)

    return application


app = create_app()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the PitchLoop campaign console.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--runs-dir", default=str(RUNS_ROOT), type=Path)
    args = parser.parse_args(argv)
    uvicorn.run(create_app(args.runs_dir), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
