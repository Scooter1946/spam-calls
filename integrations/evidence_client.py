"""Local and Nexla-backed implementations of the frozen EvidencePort."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from contracts.models import Evidence
from evidence.store import EvidenceStore

_ALIASES = {
    "policy.decision": "policy",
    "policy_decision": "policy",
    "zero.paid_result": "zero",
    "enrichment_purchased": "zero",
    "call.completed": "call",
    "call_placed": "call",
    "tool.result": "tool",
    "tool_reloaded": "tool",
}
_SECRET_MARKERS = ("secret", "token", "password", "authorization", "api_key", "phone")


def _required(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if payload.get(name) is not None:
            return payload[name]
    raise ValueError(f"missing required field: {' or '.join(names)}")


def _occurred_at(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if payload.get(name):
            return payload[name]
    return datetime.now(timezone.utc)


def normalize_raw(
    event_type: str, payload: dict[str, Any], correlation_id: str
) -> Evidence:
    """Mirror the single Nexla transform for local tests and fake runs."""

    raw_type = str(payload.get("type") or event_type)
    evidence_fields = {"run_id", "kind", "claim", "value", "source", "occurred_at"}
    if evidence_fields <= payload.keys():
        provenance = dict(payload.get("provenance") or {})
        provenance.update({"correlation_id": correlation_id, "raw_type": raw_type})
        return Evidence.model_validate(
            {
                **payload,
                "evidence_id": payload.get("evidence_id")
                or f"ev_{payload['kind']}_{correlation_id}",
                "provenance": provenance,
            }
        )

    family = _ALIASES.get(raw_type) or _ALIASES.get(event_type)
    if not family:
        raise ValueError(f"unsupported evidence event type: {raw_type}")

    provenance: dict[str, Any] = {
        "correlation_id": correlation_id,
        "raw_type": raw_type,
    }
    if family == "policy":
        decision = str(_required(payload, "decision")).lower()
        if decision not in {"allow", "deny"}:
            raise ValueError("policy decision must be allow or deny")
        audit_ref = payload.get("audit_ref")
        return Evidence(
            evidence_id=f"ev_policy_{correlation_id}",
            run_id=str(_required(payload, "run", "run_id")),
            candidate_id=str(_required(payload, "candidate", "candidate_id")),
            kind="policy",
            claim="contact_allowed" if decision == "allow" else "contact_denied",
            value={
                "status_code": int(_required(payload, "http_status", "status_code")),
                "reason": str(_required(payload, "reason")),
                "audit_ref": audit_ref,
            },
            source="pomerium",
            source_ref=str(audit_ref) if audit_ref else None,
            occurred_at=_occurred_at(payload, "observed_at", "timestamp"),
            provenance=provenance,
            policy_decision=decision,
        )

    if family == "zero":
        service = dict(_required(payload, "service"))
        provenance.update(
            {
                "service": service,
                "charge_cents": int(_required(payload, "charge_cents")),
                "receipt": dict(_required(payload, "receipt")),
            }
        )
        return Evidence(
            evidence_id=f"ev_zero_{correlation_id}",
            run_id=str(_required(payload, "run_id", "run")),
            candidate_id=str(_required(payload, "candidate_id", "candidate")),
            kind="enrichment",
            claim="fact_a",
            value=dict(_required(payload, "output")),
            source=f"zero.xyz:{service.get('name', 'service')}",
            source_ref=str(_required(service, "id")),
            occurred_at=_occurred_at(payload, "timestamp", "observed_at"),
            provenance=provenance,
        )

    if family == "call":
        provider_ref = payload.get("provider_ref")
        provenance["amount_cents"] = int(_required(payload, "amount_cents"))
        return Evidence(
            evidence_id=f"ev_call_{correlation_id}",
            run_id=str(_required(payload, "run_id", "run")),
            candidate_id=str(_required(payload, "candidate_id", "candidate")),
            kind="call",
            claim="call_outcome",
            value={
                "result_code": str(_required(payload, "result_code")),
                "missing_claims": list(payload.get("missing") or []),
                "transcript": str(_required(payload, "transcript")),
            },
            source="zero.xyz:call",
            source_ref=str(provider_ref) if provider_ref else None,
            occurred_at=_occurred_at(payload, "timestamp", "observed_at"),
            provenance=provenance,
        )

    tool = str(_required(payload, "tool"))
    return Evidence(
        evidence_id=f"ev_tool_{correlation_id}",
        run_id=str(_required(payload, "run_id", "run")),
        candidate_id=str(_required(payload, "candidate_id", "candidate")),
        kind="tool",
        claim=str(payload.get("claim") or "fact_b"),
        value=dict(_required(payload, "output")),
        source=tool,
        source_ref=tool,
        occurred_at=_occurred_at(payload, "timestamp", "observed_at"),
        provenance=provenance,
    )


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if any(marker in key.lower() for marker in _SECRET_MARKERS)
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _artifact_root(artifacts: Any = None) -> Path:
    if isinstance(artifacts, (str, Path)):
        return Path(artifacts)
    for attribute in ("run_dir", "root"):
        if artifacts is not None and hasattr(artifacts, attribute):
            return Path(getattr(artifacts, attribute)) / "evidence"
    return Path(os.getenv("PITCHLOOP_RUN_DIR", "runs/demo-001")) / "evidence"


def _write(root: Path, name: str, value: Any) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(
        json.dumps(_redact(value), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


class LocalEvidencePort:
    def __init__(
        self,
        *,
        store: EvidenceStore | None = None,
        artifacts: Any = None,
        poll_interval: float = 0.05,
    ) -> None:
        self.store = store if store is not None else EvidenceStore()
        self.artifact_root = _artifact_root(artifacts)
        self.poll_interval = poll_interval

    def publish_raw(self, event_type: str, payload: dict[str, Any]) -> str:
        correlation_id = str(payload.get("correlation_id") or f"corr-{uuid4().hex}")
        raw = {"type": event_type, **payload, "correlation_id": correlation_id}
        _write(self.artifact_root, f"{correlation_id}_raw.json", raw)
        evidence = normalize_raw(event_type, raw, correlation_id)
        self.store.append(evidence)
        _write(
            self.artifact_root,
            f"{correlation_id}_normalized.json",
            evidence.model_dump(mode="json"),
        )
        return correlation_id

    def wait_for_evidence(
        self, correlation_id: str, timeout_seconds: int = 30
    ) -> Evidence:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() <= deadline:
            evidence = self.store.by_correlation(correlation_id)
            if evidence:
                return evidence
            time.sleep(self.poll_interval)
        raise TimeoutError(f"evidence timed out: {correlation_id}")

    def query(
        self, run_id: str, *, claim: str | None = None, kind: str | None = None
    ) -> list[Evidence]:
        return self.store.query(run_id, claim=claim, kind=kind)


class NexlaEvidencePort:
    def __init__(
        self,
        *,
        ingress_url: str,
        sink_url: str,
        flow_id: str,
        artifacts: Any = None,
        http_client: httpx.Client | None = None,
        flow_checker: Callable[[str], Any] | None = None,
        poll_interval: float = 0.25,
    ) -> None:
        if not ingress_url or not sink_url or not flow_id:
            raise ValueError("live evidence requires Nexla ingress, sink, and flow ID")
        self.ingress_url = ingress_url.rstrip("/")
        self.sink_url = sink_url.rstrip("/")
        self.flow_id = flow_id
        self.artifact_root = _artifact_root(artifacts)
        self.client = http_client or httpx.Client(timeout=10)
        self.flow_checker = flow_checker or self._get_flow
        self.poll_interval = poll_interval
        self._flow_validated = False

    @staticmethod
    def _get_flow(flow_id: str) -> Any:
        from nexla_sdk import NexlaClient

        return NexlaClient().flows.get(
            flow_id=int(flow_id) if flow_id.isdigit() else flow_id
        )

    def _validate_flow(self) -> None:
        if self._flow_validated:
            return
        try:
            flow = self.flow_checker(self.flow_id)
            status = flow.get("status") if isinstance(flow, dict) else getattr(flow, "status", None)
            if status is None and getattr(flow, "flows", None):
                status = flow.flows[0].status
            if flow is False or (
                status
                and str(status).lower() not in {"active", "running", "started"}
            ):
                raise RuntimeError(f"nexla flow is not active: {status or 'unknown'}")
            self._flow_validated = True
        except Exception as exc:
            failure = {"code": "nexla_flow_unavailable", "flow_id": self.flow_id, "error": str(exc)}
            _write(self.artifact_root, "flow_failure.json", failure)
            raise RuntimeError(json.dumps(failure, sort_keys=True)) from exc

    def publish_raw(self, event_type: str, payload: dict[str, Any]) -> str:
        correlation_id = str(payload.get("correlation_id") or f"corr-{uuid4().hex}")
        raw = {"type": event_type, **payload, "correlation_id": correlation_id}
        _write(self.artifact_root, f"{correlation_id}_raw.json", raw)
        self._validate_flow()
        response: httpx.Response | None = None
        for attempt in range(2):
            try:
                response = self.client.post(self.ingress_url, json=raw)
                if response.status_code < 500:
                    break
            except httpx.TransportError as exc:
                if attempt:
                    _write(
                        self.artifact_root,
                        f"{correlation_id}_failure.json",
                        {
                            "code": "nexla_ingress_failed",
                            "correlation_id": correlation_id,
                            "error": str(exc),
                        },
                    )
                    raise
        if response is None:
            raise RuntimeError("Nexla ingress did not return a response")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            _write(
                self.artifact_root,
                f"{correlation_id}_failure.json",
                {
                    "code": "nexla_ingress_failed",
                    "correlation_id": correlation_id,
                    "status_code": response.status_code,
                },
            )
            raise
        return correlation_id

    def wait_for_evidence(
        self, correlation_id: str, timeout_seconds: int = 30
    ) -> Evidence:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() <= deadline:
            response = self.client.get(f"{self.sink_url}/correlations/{correlation_id}")
            if response.status_code == 200:
                evidence = Evidence.model_validate(response.json())
                _write(
                    self.artifact_root,
                    f"{correlation_id}_normalized.json",
                    evidence.model_dump(mode="json"),
                )
                return evidence
            if response.status_code != 404:
                response.raise_for_status()
            time.sleep(self.poll_interval)
        failure = {"code": "nexla_evidence_timeout", "correlation_id": correlation_id}
        _write(self.artifact_root, f"{correlation_id}_failure.json", failure)
        raise TimeoutError(json.dumps(failure, sort_keys=True))

    def query(
        self, run_id: str, *, claim: str | None = None, kind: str | None = None
    ) -> list[Evidence]:
        params = {key: value for key, value in {"run_id": run_id, "claim": claim, "kind": kind}.items() if value is not None}
        response = self.client.get(f"{self.sink_url}/evidence", params=params)
        response.raise_for_status()
        return [Evidence.model_validate(item) for item in response.json()]


def build_evidence_port(*, mode: str | None = None, artifacts: Any = None):
    selected = (mode or os.getenv("EVIDENCE_MODE", "fake")).lower()
    if selected in {"fake", "local"}:
        return LocalEvidencePort(artifacts=artifacts)
    if selected == "live":
        return NexlaEvidencePort(
            ingress_url=os.getenv("NEXLA_INGRESS_URL", ""),
            sink_url=os.getenv("NEXLA_SINK_URL", "http://127.0.0.1:8090"),
            flow_id=os.getenv("NEXLA_FLOW_ID", ""),
            artifacts=artifacts,
        )
    raise ValueError(f"unsupported evidence mode: {selected}")
