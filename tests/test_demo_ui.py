from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from demo.ui import build_view_model, create_app, discover_runs, safe_artifact_path, select_run


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _jsonl(path: Path, values: list[object | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(value if isinstance(value, str) else json.dumps(value) for value in values),
        encoding="utf-8",
    )


def _run(root: Path, name: str = "fake-demo.test") -> Path:
    run = root / name
    _json(run / "spec.json", {"run_id": "demo-test", "budget_cents": 5000})
    _json(run / "final/artifact_index.json", {"run_id": "demo-test", "outcome": "MEETING_BOOKED"})
    _json(run / "final/meeting.json", {"candidate_id": "maya_chen", "spent_cents": 120})
    _json(run / "evidence/diagnosis.json", {"evidence_ids": ["ev-call"], "missing_claims": ["fact_b"]})
    _jsonl(
        run / "events.jsonl",
        [
            {"type": "run_started", "ts": "2026-01-01T00:00:02Z"},
            {"type": "call_placed", "ts": "2026-01-01T00:00:01Z", "evidence_id": "ev-call"},
            {
                "type": "run_finished",
                "ts": "2026-01-01T00:00:03Z",
                "outcome": "MEETING_BOOKED",
                "final_state": "FINALIZE",
                "spent_cents": 120,
            },
        ],
    )
    _jsonl(
        run / "evidence/normalized.jsonl",
        [
            {
                "evidence_id": "ev-call",
                "kind": "call",
                "claim": "call",
                "candidate_id": "maya_chen",
                "source": "call_adapter",
                "provenance": {"correlation_id": "corr-call"},
            }
        ],
    )
    _json(run / "evidence/corr-call_raw.json", {"type": "call.completed"})
    _json(run / "evidence/corr-call_normalized.json", {"evidence_id": "ev-call"})
    return run


def test_discovers_newest_run_and_selects_by_name(tmp_path: Path) -> None:
    older = _run(tmp_path, "fake-demo.older")
    newer = _run(tmp_path, "fake-demo.newer")
    ignored = tmp_path / "live-demo.nope"
    ignored.mkdir()
    outside = tmp_path.parent / "outside-run"
    outside.mkdir(exist_ok=True)
    (tmp_path / "fake-demo.link").symlink_to(outside, target_is_directory=True)
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    assert discover_runs(tmp_path) == [newer, older]
    assert select_run(None, tmp_path) == newer
    assert select_run(older.name, tmp_path) == older


def test_preserves_event_file_order_and_links_evidence_lineage(tmp_path: Path) -> None:
    model = build_view_model(_run(tmp_path))

    assert [event["type"] for event in model["events"]] == [
        "run_started",
        "call_placed",
        "run_finished",
    ]
    assert [event["sequence"] for event in model["events"]] == [1, 2, 3]
    linked = model["events"][1]["evidence"]
    assert linked["evidence_id"] == "ev-call"
    assert linked["diagnosis_cited"] is True
    assert linked["raw_artifact"] == "evidence/corr-call_raw.json"
    assert linked["normalized_artifact"] == "evidence/corr-call_normalized.json"


def test_partial_and_malformed_artifacts_do_not_crash(tmp_path: Path) -> None:
    run = tmp_path / "fake-demo.partial"
    _json(
        run / "spec.json",
        {"run_id": "partial", "api_secret": "do-not-display", "budget_cents": "bad"},
    )
    _json(run / "final/meeting.json", {"spent_cents": "bad"})
    _jsonl(
        run / "events.jsonl",
        [
            {"type": "run_started", "ts": "2026-01-01T00:00:00"},
            "{broken",
            {"type": "state_enter", "state": "PLAN", "ts": "2026-01-01T00:00:01Z"},
        ],
    )
    _jsonl(run / "evidence/normalized.jsonl", ["[]", "also broken"])
    (run / "evidence/diagnosis.json").parent.mkdir(parents=True, exist_ok=True)
    (run / "evidence/diagnosis.json").write_text("not json", encoding="utf-8")

    model = build_view_model(run)

    assert [event["type"] for event in model["events"]] == ["run_started", "state_enter"]
    assert model["evidence"] == []
    assert model["spec"]["api_secret"] == "***REDACTED***"
    assert any("events.jsonl:2" in error for error in model["errors"])
    assert any("normalized.jsonl" in error for error in model["errors"])
    assert model["summary"]["outcome"] == "IN PROGRESS"

    response = TestClient(create_app(tmp_path)).get("/?run=fake-demo.partial")
    assert response.status_code == 200
    assert "Partial readout" in response.text
    assert "do-not-display" not in response.text


def test_rejects_run_and_artifact_path_traversal(tmp_path: Path) -> None:
    run = _run(tmp_path)

    with pytest.raises(ValueError, match="unknown run"):
        select_run("../fake-demo.test", tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        safe_artifact_path(run, "../../outside.json")
    with pytest.raises(ValueError, match="relative"):
        safe_artifact_path(run, "/tmp/outside.json")
    assert TestClient(create_app(tmp_path)).get("/?run=../fake-demo.test").status_code == 400


def test_blocks_artifact_symlinks_and_redacts_text_secrets(tmp_path: Path) -> None:
    run = _run(tmp_path)
    outside = tmp_path / "outside-events.jsonl"
    outside.write_text('{"type":"run_finished"}\n', encoding="utf-8")
    (run / "events.jsonl").unlink()
    (run / "events.jsonl").symlink_to(outside)
    prompt = run / "tools/author_prompt.txt"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("Authorization: Bearer super-secret-token", encoding="utf-8")

    model = build_view_model(run)
    assert any("events.jsonl: blocked path" in error for error in model["errors"])
    response = TestClient(create_app(tmp_path)).get("/?run=fake-demo.test")
    assert response.status_code == 200
    assert "super-secret-token" not in response.text
    assert "***REDACTED***" in response.text


def test_flow_steps_require_their_own_artifact_proof(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _jsonl(
        run / "evidence/normalized.jsonl",
        [
            {
                "evidence_id": "ev-policy-allow",
                "kind": "policy",
                "claim": "policy",
                "policy_decision": "allow",
                "source": "pomerium",
            }
        ],
    )
    model = build_view_model(run)
    completed = {step["label"]: step["completed"] for step in model["flow_steps"]}

    assert completed["Policy allowed"] is True
    assert completed["Policy denied"] is False
    assert completed["Fact A purchased"] is False
    assert completed["Call 1 rejected"] is False
    assert completed["Marketplace miss"] is False
    assert completed["Call 2 booked"] is False


def test_mode_is_fake_unless_all_adapters_have_affirmative_live_proof(tmp_path: Path) -> None:
    run = _run(tmp_path)
    assert build_view_model(run)["mode"] == "FAKE / LOCAL"

    for path in ("policy/deny.json", "policy/allow.json", "repo/pr.json", "repo/merge.json"):
        _json(run / path, {"adapter_mode": "live"})
    for number in (1, 2):
        _json(run / f"calls/call_{number}_provider.json", {"adapter_mode": "live"})
    _json(run / "evidence/live_proof.json", {"adapter_mode": "live"})
    for path in (
        "zero/cli_install_proof.txt",
        "zero/wallet_claim_proof.json",
        "zero/opening_balance.json",
        "zero/closing_balance.json",
    ):
        _json(run / path, {"captured": True})
    assert build_view_model(run)["mode"] == "LIVE"

    _json(run / "evidence/live_proof.json", {"adapter_mode": "local"})
    assert build_view_model(run)["mode"] == "FAKE / LOCAL"
