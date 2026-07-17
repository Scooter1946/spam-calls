from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from integrations.policy_client import (
    FakePolicyPort,
    PomeriumPolicyPort,
    build_policy_port,
)
from infra.policy_target.app import app as policy_target_app


class RecordingClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.client_kwargs: list[dict] = []
        self.requests: list[dict] = []

    def factory(self, **kwargs):
        self.client_kwargs.append(kwargs)
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def post(self, url, *, headers, json):
        self.requests.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


def test_policy_target_accepts_any_request_that_pomerium_forwards() -> None:
    client = TestClient(policy_target_app)

    health = client.get("/health")
    reached = client.post(
        "/authorize-observation",
        json={"action": "place_sales_call", "candidate_id": "alex_rivera"},
    )

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert reached.status_code == 200
    assert reached.json()["reached_upstream"] is True
    assert reached.json()["service"] == "pitchloop-policy-target"


def test_fake_policy_returns_403_and_200_and_writes_artifacts(tmp_path: Path) -> None:
    port = FakePolicyPort(artifacts=tmp_path)

    denied = port.authorize("place_sales_call", "alex_rivera", {"run_id": "demo-001"})
    allowed = port.authorize("place_sales_call", "maya_chen", {"run_id": "demo-001"})

    assert (denied.allowed, denied.status_code, denied.reason) == (
        False,
        403,
        "candidate_not_consented",
    )
    assert (allowed.allowed, allowed.status_code, allowed.reason) == (
        True,
        200,
        "consent_verified",
    )
    assert Path(denied.raw_artifact_path) == tmp_path / "policy/deny.json"
    assert Path(allowed.raw_artifact_path) == tmp_path / "policy/allow.json"
    assert json.loads((tmp_path / "policy/deny.json").read_text())["response"][
        "upstream_reached"
    ] is False
    assert json.loads((tmp_path / "policy/allow.json").read_text())["response"][
        "upstream_reached"
    ] is True


def test_live_policy_parses_realistic_deny_and_allow_with_same_identity(tmp_path: Path) -> None:
    recorder = RecordingClient(
        [
            httpx.Response(
                403,
                json={"error": "permission denied"},
                headers={"x-request-id": "req-deny", "set-cookie": "private"},
            ),
            httpx.Response(
                200,
                json={
                    "reached_upstream": True,
                    "service": "pitchloop-policy-target",
                    "request_id": "req-allow",
                },
                headers={"x-request-id": "req-allow"},
            ),
        ]
    )
    token = "very-secret-service-account-jwt"
    port = PomeriumPolicyPort(
        denied_url="https://denied.example/authorize-observation",
        allowed_url="https://allowed.example/authorize-observation",
        service_account_token=token,
        artifacts=tmp_path,
        client_factory=recorder.factory,
    )

    denied = port.authorize(
        "place_sales_call",
        "alex_rivera",
        {"run_id": "demo-001", "nested": {"api_token": token}},
    )
    allowed = port.authorize(
        "place_sales_call",
        "maya_chen",
        {"run_id": "demo-001"},
    )

    assert (denied.allowed, denied.status_code, denied.reason, denied.audit_ref) == (
        False,
        403,
        "candidate_not_consented",
        "req-deny",
    )
    assert (allowed.allowed, allowed.status_code, allowed.reason, allowed.audit_ref) == (
        True,
        200,
        "consent_verified",
        "req-allow",
    )
    assert Path(denied.raw_artifact_path) == tmp_path / "policy/deny.raw.json"
    assert Path(allowed.raw_artifact_path) == tmp_path / "policy/allow.raw.json"
    assert recorder.requests[0]["headers"]["Authorization"] == recorder.requests[1]["headers"][
        "Authorization"
    ]
    assert recorder.requests[0]["headers"]["Authorization"] == f"Bearer Pomerium-{token}"
    assert all(kwargs["follow_redirects"] is False for kwargs in recorder.client_kwargs)

    combined_artifacts = "".join(
        (tmp_path / relative_path).read_text()
        for relative_path in (
            "policy/deny.json",
            "policy/deny.raw.json",
            "policy/allow.json",
            "policy/allow.raw.json",
        )
    )
    assert token not in combined_artifacts
    assert "Authorization" not in combined_artifacts
    assert "set-cookie" not in combined_artifacts
    assert json.loads((tmp_path / "policy/allow.json").read_text())["response"][
        "upstream_reached"
    ] is True

    # P1 normalizes the canonical path after authorize() returns. The provider
    # request/response proof must remain available at raw_artifact_path.
    (tmp_path / "policy/allow.json").write_text('{"allowed": true}\n', encoding="utf-8")
    raw_allow = json.loads(Path(allowed.raw_artifact_path).read_text())
    assert raw_allow["response"]["upstream_reached"] is True
    assert raw_allow["response"]["headers"]["x-request-id"] == "req-allow"


def test_redirect_is_not_followed_or_mistaken_for_authorization(tmp_path: Path) -> None:
    recorder = RecordingClient(
        [httpx.Response(302, headers={"location": "https://authenticate.example/login"})]
    )
    port = PomeriumPolicyPort(
        denied_url="https://denied.example/authorize-observation",
        allowed_url="https://allowed.example/authorize-observation",
        service_account_token="token",
        artifacts=tmp_path,
        client_factory=recorder.factory,
    )

    decision = port.authorize("place_sales_call", "alex_rivera", {"run_id": "demo-001"})

    assert decision.allowed is False
    assert decision.status_code == 302
    assert decision.reason == "unexpected_policy_response"
    assert len(recorder.requests) == 1
    assert recorder.client_kwargs[0]["follow_redirects"] is False


def test_missing_live_configuration_is_a_structured_failure(tmp_path: Path) -> None:
    port = PomeriumPolicyPort(
        denied_url="",
        allowed_url="",
        service_account_token="",
        artifacts=tmp_path,
    )

    decision = port.authorize("place_sales_call", "alex_rivera", {"run_id": "demo-001"})

    assert decision.allowed is False
    assert decision.status_code == 0
    assert decision.reason == "policy_config_error"
    assert Path(decision.raw_artifact_path) == tmp_path / "policy/deny.raw.json"
    payload = json.loads((tmp_path / "policy/deny.json").read_text())
    assert payload["failure"]["type"] == "policy_config_error"
    assert json.loads((tmp_path / "policy/deny.raw.json").read_text()) == payload


def test_factory_defaults_to_fake_and_rejects_unknown_mode(tmp_path: Path) -> None:
    assert isinstance(build_policy_port(mode="fake", artifacts=tmp_path), FakePolicyPort)
    with pytest.raises(ValueError, match="unsupported POLICY_MODE"):
        build_policy_port(mode="surprise", artifacts=tmp_path)
