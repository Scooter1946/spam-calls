from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from contracts.models import Evidence
from demo.show_timeline import timeline
from evidence.sink import create_app
from evidence.store import EvidenceStore
from integrations.evidence_client import (
    LocalEvidencePort,
    NexlaEvidencePort,
    normalize_raw,
)

NOW = "2026-07-17T12:00:00Z"


def policy_event(**extra):
    return {
        "type": "policy.decision",
        "run": "demo-001",
        "candidate": "alex_rivera",
        "http_status": 403,
        "decision": "deny",
        "reason": "candidate_not_consented",
        "audit_ref": "audit-1",
        "observed_at": NOW,
        **extra,
    }


def zero_event(**extra):
    return {
        "type": "zero.paid_result",
        "run_id": "demo-001",
        "candidate_id": "maya_chen",
        "service": {"id": "svc-1", "name": "Company signal"},
        "charge_cents": 12,
        "receipt": {"id": "receipt-1"},
        "output": {"statement": "Northstar is migrating APIs."},
        "timestamp": NOW,
        **extra,
    }


def call_event(**extra):
    return {
        "type": "call.completed",
        "run_id": "demo-001",
        "candidate_id": "maya_chen",
        "result_code": "REJECTED_MISSING_FACT_B",
        "missing": ["fact_b"],
        "transcript": "The deadline is missing.",
        "provider_ref": "call-1",
        "amount_cents": 54,
        "timestamp": NOW,
        **extra,
    }


def tool_event(**extra):
    return {
        "type": "tool.result",
        "run_id": "demo-001",
        "candidate_id": "maya_chen",
        "tool": "generated_fact_b_tool",
        "claim": "fact_b",
        "output": {
            "statement": "Northstar Systems has an August 30 API v1 migration deadline."
        },
        "timestamp": NOW,
        **extra,
    }


@pytest.mark.parametrize(
    ("event_type", "payload", "kind", "claim", "prefix"),
    [
        ("policy.decision", policy_event(), "policy", "contact_denied", "ev_policy_"),
        ("zero.paid_result", zero_event(), "enrichment", "fact_a", "ev_zero_"),
        ("call.completed", call_event(), "call", "call_outcome", "ev_call_"),
        ("tool.result", tool_event(), "tool", "fact_b", "ev_tool_"),
    ],
)
def test_normalization_semantics(event_type, payload, kind, claim, prefix):
    evidence = normalize_raw(event_type, payload, "corr-1")
    assert evidence.kind == kind
    assert evidence.claim == claim
    assert evidence.evidence_id == f"{prefix}corr-1"
    assert evidence.provenance["correlation_id"] == "corr-1"


def test_store_deduplicates_persists_and_filters(tmp_path):
    store = EvidenceStore(tmp_path / "runs")
    later = normalize_raw("tool.result", tool_event(timestamp="2026-07-18T12:00:00Z"), "corr-2")
    earlier = normalize_raw("zero.paid_result", zero_event(), "corr-1")
    assert store.append(later)[1]
    assert store.append(earlier)[1]
    assert not store.append(earlier)[1]
    assert [item.claim for item in store.query("demo-001")] == ["fact_a", "fact_b"]
    assert store.query("demo-001", claim="fact_b") == [later]
    assert EvidenceStore(tmp_path / "runs").get(earlier.evidence_id) == earlier

    changed = earlier.model_copy(update={"value": {"statement": "changed"}})
    with pytest.raises(ValueError, match="conflicting evidence_id"):
        store.append(changed)


def test_sink_validates_correlation_and_handles_replays(tmp_path):
    client = TestClient(create_app(EvidenceStore(tmp_path / "runs")))
    evidence = normalize_raw("zero.paid_result", zero_event(), "corr-1")
    body = evidence.model_dump(mode="json")
    body["correlation_id"] = "corr-1"
    assert client.post("/ingest/evidence", json=body).json()["created"] is True
    assert client.post("/ingest/evidence", json=body).json()["created"] is False
    assert client.get("/correlations/corr-1").json()["claim"] == "fact_a"
    assert client.get("/evidence", params={"run_id": "demo-001", "claim": "fact_a"}).status_code == 200
    body.pop("correlation_id")
    body["provenance"] = {}
    assert client.post("/ingest/evidence", json=body).status_code == 422


def test_local_port_polls_queries_and_redacts_artifacts(tmp_path):
    artifacts = tmp_path / "artifacts"
    port = LocalEvidencePort(
        store=EvidenceStore(tmp_path / "runs"), artifacts=artifacts, poll_interval=0
    )
    event = zero_event(receipt={"api_key": "never-write-me"})
    correlation_id = port.publish_raw("zero.paid_result", event)
    evidence = port.wait_for_evidence(correlation_id, timeout_seconds=1)
    assert evidence.claim == "fact_a"
    assert port.query("demo-001", kind="enrichment") == [evidence]
    assert "never-write-me" not in (artifacts / f"{correlation_id}_raw.json").read_text()
    with pytest.raises(TimeoutError):
        port.wait_for_evidence("missing", timeout_seconds=0)


def test_live_port_polls_sink_and_queries(tmp_path):
    evidence = normalize_raw("call.completed", call_event(), "corr-live")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"accepted": True})
        if request.url.path == "/correlations/corr-live":
            return httpx.Response(200, json=evidence.model_dump(mode="json"))
        if request.url.path == "/evidence":
            return httpx.Response(200, json=[evidence.model_dump(mode="json")])
        return httpx.Response(404)

    port = NexlaEvidencePort(
        ingress_url="https://nexla.test/ingress",
        sink_url="https://sink.test",
        flow_id="flow-1",
        artifacts=tmp_path,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        flow_checker=lambda _: {"status": "active"},
        poll_interval=0,
    )
    assert port.publish_raw("call.completed", call_event(correlation_id="corr-live")) == "corr-live"
    assert port.wait_for_evidence("corr-live", 1) == evidence
    assert port.query("demo-001", kind="call") == [evidence]


def test_live_mode_fails_closed_for_inactive_flow(tmp_path):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    port = NexlaEvidencePort(
        ingress_url="https://nexla.test/ingress",
        sink_url="https://sink.test",
        flow_id="flow-1",
        artifacts=tmp_path,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        flow_checker=lambda _: {"status": "paused"},
    )
    with pytest.raises(RuntimeError, match="nexla_flow_unavailable"):
        port.publish_raw("policy.decision", policy_event())
    assert not requests
    assert json.loads((tmp_path / "flow_failure.json").read_text())["code"] == "nexla_flow_unavailable"


def test_live_mode_never_falls_back_on_ingress_failure(tmp_path):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, request=request)

    port = NexlaEvidencePort(
        ingress_url="https://nexla.test/ingress",
        sink_url="https://sink.test",
        flow_id="flow-1",
        artifacts=tmp_path,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        flow_checker=lambda _: {"status": "active"},
    )
    with pytest.raises(httpx.HTTPStatusError):
        port.publish_raw("policy.decision", policy_event())
    assert attempts == 2
    assert not list(tmp_path.glob("*_normalized.json"))
    failure = json.loads(next(tmp_path.glob("*_failure.json")).read_text())
    assert failure["code"] == "nexla_ingress_failed"


def test_live_poll_timeout_is_structured(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    port = NexlaEvidencePort(
        ingress_url="https://nexla.test/ingress",
        sink_url="https://sink.test",
        flow_id="flow-1",
        artifacts=tmp_path,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        flow_checker=lambda _: {"status": "active"},
        poll_interval=0,
    )
    with pytest.raises(TimeoutError, match="nexla_evidence_timeout"):
        port.wait_for_evidence("corr-timeout", timeout_seconds=0)


def test_timeline_reads_artifacts_without_becoming_state_machine(tmp_path):
    (tmp_path / "policy").mkdir()
    (tmp_path / "calls").mkdir()
    (tmp_path / "policy/deny.json").write_text(
        json.dumps({"candidate_id": "alex_rivera", "status_code": 403})
    )
    (tmp_path / "calls/call_1_result.json").write_text(
        json.dumps({"missing_claims": ["fact_b"]})
    )
    assert timeline(tmp_path) == [
        "[POLICY] alex_rivera denied by Pomerium (403)",
        "[CALL 1] rejected: missing fact_b",
    ]
