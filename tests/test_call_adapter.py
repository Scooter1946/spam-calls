from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from callee.call_harness import MEETING_BOOKED_RESPONSE
from integrations.call_client import build_call_port, build_provider_payload


FACT_A = "Northstar is expanding its developer platform"
FACT_B_PHRASE = "August 30 API v1 migration deadline"
PITCH_A = (
    "Hi Maya, I'm calling from MigrationGuard. I noticed Northstar is expanding its "
    "developer platform. We reduce migration risk. Would Tuesday be useful?"
)
PITCH_BOTH = (
    f"{PITCH_A} I also saw your {FACT_B_PHRASE}."
)


@dataclass
class Service:
    service_id: str = "call-service"
    name: str = "Documented Call Service"
    description: str = "Outbound call with transcript"
    price_cents: int | None = 17
    metadata: dict[str, Any] = field(
        default_factory=lambda: {
            "input_schema": {
                "type": "object",
                "properties": {
                    "to_phone_number": {"type": "string"},
                    "script": {"type": "string"},
                    "record": {"type": "boolean", "default": True},
                },
                "required": ["to_phone_number", "script", "record"],
            }
        }
    )


@dataclass
class Paid:
    ok: bool
    service_id: str
    result: dict[str, Any]
    receipt: dict[str, Any]
    amount_cents: int
    provider_ref: str | None
    raw_artifact_path: str


class ZeroSpy:
    def __init__(self, paid: Paid, services: list[Service] | None = None) -> None:
        self.paid = paid
        self.services = services if services is not None else [Service()]
        self.searches: list[str] = []
        self.invocations: list[tuple[Service, dict[str, Any]]] = []

    def search(self, capability: str) -> list[Service]:
        self.searches.append(capability)
        return self.services

    def invoke(self, service: Service, payload: dict[str, Any]) -> Paid:
        self.invocations.append((service, payload))
        return self.paid


def fake_port(tmp_path: Path):
    return build_call_port(
        zero_port=ZeroSpy(paid_result(MEETING_BOOKED_RESPONSE)),
        artifacts=tmp_path,
        expected_fact_a=FACT_A,
        expected_fact_b_phrase=FACT_B_PHRASE,
        mode="fake",
    )


def paid_result(transcript: str, *, ok: bool = True, amount: int = 23) -> Paid:
    return Paid(
        ok=ok,
        service_id="call-service",
        result={"output": {"transcript": transcript}},
        receipt={"transaction_id": "txn-123", "amount_cents": amount},
        amount_cents=amount,
        provider_ref="provider-call-123",
        raw_artifact_path="runs/demo-001/zero/raw-call.json",
    )


def test_fake_call_uses_rubric_and_persists_required_artifacts(tmp_path: Path) -> None:
    port = fake_port(tmp_path)
    first = port.place_call("maya_chen", PITCH_A)
    second = port.place_call("maya_chen", PITCH_BOTH)

    assert first.code == "REJECTED_MISSING_FACT_B"
    assert first.status == "rejected"
    assert first.amount_cents == 0
    assert first.receipt["charged"] is False
    assert second.code == "MEETING_BOOKED"
    assert second.status == "booked"
    for relative in (
        "pitch/pitch_1.md",
        "calls/call_1_provider.json",
        "calls/call_1_transcript.txt",
        "calls/call_1_result.json",
        "zero/call_1_receipt.json",
        "pitch/pitch_2.md",
        "calls/call_2_result.json",
    ):
        assert (tmp_path / relative).is_file(), relative


def test_fake_call_rejects_denied_contact_before_action_and_caps_calls(tmp_path: Path) -> None:
    port = fake_port(tmp_path)
    with pytest.raises(PermissionError, match="only maya_chen"):
        port.place_call("alex_rivera", PITCH_A)
    assert list(tmp_path.rglob("*")) == []

    port.place_call("maya_chen", PITCH_A)
    port.place_call("maya_chen", PITCH_BOTH)
    with pytest.raises(RuntimeError, match="at most two"):
        port.place_call("maya_chen", PITCH_BOTH)


def test_payload_mapping_uses_only_documented_schema_fields() -> None:
    payload = build_provider_payload(Service(), "+14155550123", PITCH_A)
    assert payload == {
        "to_phone_number": "+14155550123",
        "script": PITCH_A,
        "record": True,
    }

    undocumented = Service(metadata={})
    with pytest.raises(ValueError, match="document an input schema"):
        build_provider_payload(undocumented, "+14155550123", PITCH_A)


def test_live_adapter_invokes_injected_zero_and_uses_paid_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CALLEE_PHONE_E164", "+14155550123")
    zero = ZeroSpy(paid_result(MEETING_BOOKED_RESPONSE, amount=37))
    port = build_call_port(
        zero_port=zero,
        artifacts=tmp_path,
        expected_fact_a=FACT_A,
        expected_fact_b_phrase=FACT_B_PHRASE,
        mode="live",
    )
    result = port.place_call("maya_chen", PITCH_BOTH)

    assert zero.searches == ["paid outbound phone call with transcript"]
    assert len(zero.invocations) == 1
    assert zero.invocations[0][1]["to_phone_number"] == "+14155550123"
    assert result.code == "MEETING_BOOKED"
    assert result.amount_cents == 37
    assert result.provider_ref == "provider-call-123"
    assert json.loads((tmp_path / "zero/call_1_receipt.json").read_text())["amount_cents"] == 37

    all_artifacts = "\n".join(
        path.read_text(errors="ignore") for path in tmp_path.rglob("*") if path.is_file()
    )
    assert "+14155550123" not in all_artifacts


def test_live_adapter_rejects_transcript_only_false_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CALLEE_PHONE_E164", "+14155550123")
    zero = ZeroSpy(paid_result(MEETING_BOOKED_RESPONSE))
    port = build_call_port(
        zero_port=zero,
        artifacts=tmp_path,
        expected_fact_a=FACT_A,
        expected_fact_b_phrase=FACT_B_PHRASE,
        mode="live",
    )
    result = port.place_call("maya_chen", PITCH_A)
    assert result.code == "CALL_FAILED_UNPARSEABLE"
    assert result.status == "failed"


def test_paid_receipt_survives_missing_transcript_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CALLEE_PHONE_E164", "+14155550123")
    paid = paid_result("", amount=41)
    paid.result = {"status": "completed_without_transcript"}
    zero = ZeroSpy(paid)
    port = build_call_port(
        zero_port=zero,
        artifacts=tmp_path,
        expected_fact_a=FACT_A,
        expected_fact_b_phrase=FACT_B_PHRASE,
        mode="live",
    )
    result = port.place_call("maya_chen", PITCH_BOTH)
    assert result.code == "CALL_FAILED_PROVIDER"
    assert result.amount_cents == 41
    assert result.receipt["transaction_id"] == "txn-123"
    provider = json.loads((tmp_path / "calls/call_1_provider.json").read_text())
    assert provider["result"]["status"] == "completed_without_transcript"


def test_live_adapter_converts_provider_failures_to_explicit_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CALLEE_PHONE_E164", raising=False)
    zero = ZeroSpy(paid_result("", ok=False))
    port = build_call_port(
        zero_port=zero,
        artifacts=tmp_path,
        expected_fact_a=FACT_A,
        expected_fact_b_phrase=FACT_B_PHRASE,
        mode="live",
    )
    result = port.place_call("maya_chen", PITCH_A)
    assert result.code == "CALL_FAILED_PROVIDER"
    provider = json.loads((tmp_path / "calls/call_1_provider.json").read_text())
    assert provider["ok"] is False
    assert provider["error"]["type"] == "ValueError"
    assert zero.invocations == []


def test_configured_service_must_be_present_and_mode_is_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CALLEE_PHONE_E164", "+14155550123")
    monkeypatch.setenv("ZERO_CALL_SERVICE_ID", "missing-service")
    zero = ZeroSpy(paid_result(MEETING_BOOKED_RESPONSE))
    port = build_call_port(
        zero_port=zero,
        artifacts=tmp_path,
        expected_fact_a=FACT_A,
        expected_fact_b_phrase=FACT_B_PHRASE,
        mode="live",
    )
    result = port.place_call("maya_chen", PITCH_BOTH)
    assert result.code == "CALL_FAILED_PROVIDER"
    assert zero.invocations == []

    with pytest.raises(ValueError, match="unsupported CALL_MODE"):
        build_call_port(
            zero_port=zero,
            artifacts=tmp_path / "other",
            expected_fact_a=FACT_A,
            expected_fact_b_phrase=FACT_B_PHRASE,
            mode="mystery",
        )


_MANIFEST = {
    "name": "generated_fact_b_tool",
    "capability": "fact_b",
    "entrypoint": "generated_tools.fact_b_tool:run",
    "input_schema": {"candidate_id": "string"},
    "output_claim": "fact_b",
    "version": "1.0.0",
}


def _write_generated_fixture(directory: Path, implementation: str) -> None:
    directory.mkdir(parents=True)
    (directory / "fact_b_tool.manifest.json").write_text(
        json.dumps(_MANIFEST), encoding="utf-8"
    )
    (directory / "fact_b_tool.py").write_text(implementation, encoding="utf-8")
    (directory / "test_fact_b_tool.py").write_text(
        "def test_generated_smoke():\n    assert True\n", encoding="utf-8"
    )


def _run_conformance(generated_dir: Path) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "conformance/test_generated_tool.py",
            "--generated-dir",
            str(generated_dir),
        ],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )


def test_conformance_rejects_deliberately_hardcoded_bad_tool(tmp_path: Path) -> None:
    generated_dir = tmp_path / "bad" / "generated_tools"
    _write_generated_fixture(
        generated_dir,
        '''def run(candidate_id: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "claim": "fact_b",
        "value": {"statement": "Northstar Systems has an August 30 API v1 migration deadline."},
        "source": "northstar_public_migration_signal",
        "provenance": {"url": "hardcoded"},
    }
''',
    )
    completed = _run_conformance(generated_dir)
    assert completed.returncode != 0, completed.stdout
    assert "hardcode" in completed.stdout.casefold() or "unknown candidate" in completed.stdout.casefold()


def test_conformance_accepts_correct_fetching_tool(tmp_path: Path) -> None:
    generated_dir = tmp_path / "good" / "generated_tools"
    _write_generated_fixture(
        generated_dir,
        '''from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


def run(candidate_id: str) -> dict:
    if candidate_id != "maya_chen":
        return {"candidate_id": candidate_id, "error": "candidate is not allowed"}
    base_url = os.environ.get("FACT_B_FIXTURE_URL", "").rstrip("/")
    if not base_url:
        return {"candidate_id": candidate_id, "error": "FACT_B_FIXTURE_URL is not set"}
    query = urlencode({"candidate_id": candidate_id})
    url = f"{base_url}/companies/northstar_systems/migration-signal?{query}"
    try:
        with urlopen(url, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return {"candidate_id": candidate_id, "error": f"fixture fetch failed: {type(exc).__name__}"}
    required = ("candidate_id", "claim", "statement", "source")
    if any(not isinstance(payload.get(key), str) for key in required):
        return {"candidate_id": candidate_id, "error": "fixture response is invalid"}
    if payload["candidate_id"] != candidate_id or payload["claim"] != "fact_b":
        return {"candidate_id": candidate_id, "error": "fixture response did not match request"}
    return {
        "candidate_id": candidate_id,
        "claim": payload["claim"],
        "value": {"statement": payload["statement"]},
        "source": payload["source"],
        "provenance": {"url": url},
    }
''',
    )
    completed = _run_conformance(generated_dir)
    assert completed.returncode == 0, completed.stdout
