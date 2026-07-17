"""Deterministic fake and Zero-backed paid-call adapters.

This module intentionally consumes an injected ``ZeroPort``. It never invokes the
Zero CLI, interprets wallet state, or manufactures a live receipt.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from callee.call_harness import evaluate_pitch
from pitch.transcript_parser import parse_transcript

if TYPE_CHECKING:
    from contracts.models import CallResult, ServiceMatch
    from contracts.ports import CallPort, ZeroPort


_CALL_CAPABILITY = "paid outbound phone call with transcript"
_PHONE_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")
_PHONE_FIELD_ALIASES = (
    "phone",
    "phone_number",
    "to",
    "to_phone_number",
    "callee_phone_e164",
)
_PITCH_FIELD_ALIASES = ("pitch", "message", "script", "prompt", "instructions")
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "credential",
    "password",
    "phone",
    "secret",
    "token",
    "wallet",
)


@dataclass(slots=True)
class _FallbackCallResult:
    """Used only until P1's frozen contracts package lands in this worktree."""

    status: str
    code: str
    missing_claims: list[str]
    transcript: str
    transcript_path: str
    receipt: dict[str, Any]
    amount_cents: int
    provider_ref: str | None = None


def _make_call_result(**values: Any) -> "CallResult":
    try:
        from contracts.models import CallResult
    except ModuleNotFoundError as exc:
        if exc.name not in {"contracts", "contracts.models"}:
            raise
        return _FallbackCallResult(**values)  # type: ignore[return-value]
    return CallResult(**values)


def _field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _serialize(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _redact(value: Any, *, phone: str | None = None) -> Any:
    serialized = _serialize(value)
    if isinstance(serialized, dict):
        redacted: dict[str, Any] = {}
        for key, item in serialized.items():
            if any(part in key.casefold() for part in _SENSITIVE_KEY_PARTS):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact(item, phone=phone)
        return redacted
    if isinstance(serialized, list):
        return [_redact(item, phone=phone) for item in serialized]
    if isinstance(serialized, str):
        result = serialized.replace(phone, "[REDACTED_PHONE]") if phone else serialized
        return re.sub(r"\+[1-9]\d{7,14}", "[REDACTED_PHONE]", result)
    return serialized


class _ArtifactWriter:
    def __init__(self, artifacts: Any) -> None:
        self._artifacts = artifacts
        self._is_path_input = isinstance(artifacts, (str, os.PathLike))
        self._root = self._find_root(artifacts)
        has_methods = callable(getattr(artifacts, "write_text", None)) and callable(
            getattr(artifacts, "write_json", None)
        )
        if self._root is None and not has_methods:
            raise TypeError(
                "artifacts must be a path/run_dir or expose write_text and write_json"
            )

    @staticmethod
    def _find_root(artifacts: Any) -> Path | None:
        if isinstance(artifacts, (str, os.PathLike)):
            return Path(artifacts).resolve()
        for attribute in ("run_dir", "root"):
            value = getattr(artifacts, attribute, None)
            if isinstance(value, (str, os.PathLike)):
                return Path(value).resolve()
        return None

    @staticmethod
    def _validate_relative(relative_path: str) -> Path:
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe artifact path: {relative_path}")
        return path

    def write_text(self, relative_path: str, content: str) -> str:
        relative = self._validate_relative(relative_path)
        method = getattr(self._artifacts, "write_text", None)
        if callable(method) and not self._is_path_input:
            written = method(relative.as_posix(), content)
            return str(written or relative.as_posix())
        assert self._root is not None
        destination = self._root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        return str(destination)

    def write_json(self, relative_path: str, payload: Any) -> str:
        relative = self._validate_relative(relative_path)
        safe_payload = _serialize(payload)
        method = getattr(self._artifacts, "write_json", None)
        if callable(method) and not self._is_path_input:
            written = method(relative.as_posix(), safe_payload)
            return str(written or relative.as_posix())
        assert self._root is not None
        destination = self._root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(safe_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return str(destination)


def _input_schema(service: "ServiceMatch") -> dict[str, Any]:
    metadata = _field(service, "metadata", {})
    if not isinstance(metadata, Mapping):
        return {}
    for key in ("input_schema", "inputSchema", "request_schema"):
        schema = metadata.get(key)
        if isinstance(schema, Mapping):
            return dict(schema)
    return {}


def _first_declared(properties: Mapping[str, Any], aliases: Iterable[str]) -> str | None:
    casefolded = {key.casefold(): key for key in properties}
    for alias in aliases:
        if alias in casefolded:
            return casefolded[alias]
    return None


def build_provider_payload(
    service: "ServiceMatch",
    phone: str,
    pitch: str,
) -> dict[str, Any]:
    """Map canonical call fields only to input names documented in metadata."""

    if not _PHONE_PATTERN.fullmatch(phone):
        raise ValueError("CALLEE_PHONE_E164 must be a valid E.164 phone number")
    if not isinstance(pitch, str) or not pitch.strip():
        raise ValueError("pitch must be a non-empty string")

    metadata = _field(service, "metadata", {})
    metadata = metadata if isinstance(metadata, Mapping) else {}
    explicit = metadata.get("pitchloop_payload_mapping")
    static_values: dict[str, Any] = {}
    if isinstance(explicit, Mapping):
        phone_field = explicit.get("phone")
        pitch_field = explicit.get("pitch")
        configured_static = explicit.get("static", {})
        if isinstance(configured_static, Mapping):
            static_values = dict(configured_static)
    else:
        schema = _input_schema(service)
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping) or not properties:
            raise ValueError(
                "call service metadata must document an input schema or "
                "pitchloop_payload_mapping"
            )
        phone_field = _first_declared(properties, _PHONE_FIELD_ALIASES)
        pitch_field = _first_declared(properties, _PITCH_FIELD_ALIASES)
        for key, declaration in properties.items():
            if isinstance(declaration, Mapping) and "default" in declaration:
                static_values.setdefault(key, declaration["default"])

    if not isinstance(phone_field, str) or not isinstance(pitch_field, str):
        raise ValueError("call service metadata does not declare phone and pitch fields")

    payload = {**static_values, phone_field: phone, pitch_field: pitch}
    schema = _input_schema(service)
    required = schema.get("required", []) if schema else []
    if isinstance(required, list):
        unresolved = [field for field in required if field not in payload]
        if unresolved:
            raise ValueError(f"call service has unmapped required fields: {unresolved}")
    return payload


def _select_service(
    matches: list["ServiceMatch"], configured_service_id: str | None
) -> "ServiceMatch":
    if configured_service_id:
        for service in matches:
            if _field(service, "service_id") == configured_service_id:
                return service
        raise LookupError(
            f"configured ZERO_CALL_SERVICE_ID was not returned by search: "
            f"{configured_service_id}"
        )
    if not matches:
        raise LookupError(f"Zero returned no service for capability: {_CALL_CAPABILITY}")
    return matches[0]


def _nested_value(payload: Any, dotted_path: str) -> Any:
    current = payload
    for part in dotted_path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        else:
            return None
    return current


def _extract_transcript(service: "ServiceMatch", result: Mapping[str, Any]) -> str | None:
    metadata = _field(service, "metadata", {})
    if isinstance(metadata, Mapping):
        mapping = metadata.get("pitchloop_result_mapping")
        if isinstance(mapping, Mapping) and isinstance(mapping.get("transcript_path"), str):
            mapped = _nested_value(result, mapping["transcript_path"])
            if isinstance(mapped, str) and mapped.strip():
                return mapped.strip()

    def find_known(value: Any, depth: int = 0) -> str | None:
        if depth > 3 or not isinstance(value, Mapping):
            return None
        for key in ("transcript", "transcript_text"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
            if isinstance(candidate, Mapping):
                text = candidate.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
        for key in ("result", "output", "call"):
            nested = find_known(value.get(key), depth + 1)
            if nested:
                return nested
        messages = value.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                if isinstance(message, Mapping):
                    for key in ("text", "content"):
                        text = message.get(key)
                        if isinstance(text, str) and text.strip():
                            return text.strip()
        return None

    return find_known(result)


class _BaseCallPort:
    def __init__(
        self,
        *,
        artifacts: Any,
        expected_fact_a: str,
        expected_fact_b_phrase: str,
    ) -> None:
        if not expected_fact_a.strip() or not expected_fact_b_phrase.strip():
            raise ValueError("expected Fact A and Fact B phrase must be non-empty")
        self._artifacts = _ArtifactWriter(artifacts)
        self._expected_fact_a = expected_fact_a
        self._expected_fact_b_phrase = expected_fact_b_phrase
        self._call_count = 0

    def _begin(self, candidate_id: str, pitch_text: str) -> int:
        if candidate_id != "maya_chen":
            raise PermissionError(
                f"paid calls are forbidden for candidate {candidate_id}; only maya_chen is callable"
            )
        if not isinstance(pitch_text, str) or not pitch_text.strip():
            raise ValueError("pitch_text must be non-empty")
        if self._call_count >= 2:
            raise RuntimeError("scenario permits at most two paid call attempts")
        self._call_count += 1
        index = self._call_count
        self._artifacts.write_text(f"pitch/pitch_{index}.md", pitch_text.strip() + "\n")
        return index

    def _persist(
        self,
        *,
        index: int,
        pitch_text: str,
        transcript: str,
        receipt: dict[str, Any],
        amount_cents: int,
        provider_ref: str | None,
        raw_provider_result: Any,
        phone: str | None = None,
        provider_ok: bool = True,
    ) -> "CallResult":
        safe_raw = _redact(raw_provider_result, phone=phone)
        safe_receipt = _redact(receipt, phone=phone)
        self._artifacts.write_json(f"calls/call_{index}_provider.json", safe_raw)
        transcript_path = self._artifacts.write_text(
            f"calls/call_{index}_transcript.txt", transcript.strip() + ("\n" if transcript else "")
        )
        self._artifacts.write_json(f"zero/call_{index}_receipt.json", safe_receipt)

        parsed = parse_transcript(
            transcript,
            pitch_text=pitch_text,
            expected_fact_a=self._expected_fact_a,
            expected_fact_b_phrase=self._expected_fact_b_phrase,
        )
        if not provider_ok:
            status, code, missing_claims = "failed", "CALL_FAILED_PROVIDER", []
        else:
            status, code, missing_claims = (
                parsed.status,
                parsed.code,
                list(parsed.missing_claims),
            )
        canonical = _make_call_result(
            status=status,
            code=code,
            missing_claims=missing_claims,
            transcript=transcript,
            transcript_path=transcript_path,
            receipt=safe_receipt,
            amount_cents=amount_cents,
            provider_ref=provider_ref,
        )
        self._artifacts.write_json(f"calls/call_{index}_result.json", _serialize(canonical))
        return canonical


class FakeCallPort(_BaseCallPort):
    """Deterministic, explicitly unpaid fake used by tests and rehearsals."""

    def place_call(self, candidate_id: str, pitch_text: str) -> "CallResult":
        index = self._begin(candidate_id, pitch_text)
        rubric = evaluate_pitch(
            pitch_text,
            self._expected_fact_a,
            self._expected_fact_b_phrase,
        )
        receipt = {
            "mode": "fake",
            "charged": False,
            "amount_cents": 0,
            "provider_ref": f"fake-call-{index}",
        }
        return self._persist(
            index=index,
            pitch_text=pitch_text,
            transcript=rubric.response,
            receipt=receipt,
            amount_cents=0,
            provider_ref=f"fake-call-{index}",
            raw_provider_result={"mode": "fake", "rubric_code": rubric.code},
        )


class ZeroBackedCallPort(_BaseCallPort):
    """Live paid adapter built exclusively on P1's injected canonical ZeroPort."""

    def __init__(self, *, zero_port: "ZeroPort", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._zero_port = zero_port
        self._configured_service_id = os.getenv("ZERO_CALL_SERVICE_ID") or None

    def place_call(self, candidate_id: str, pitch_text: str) -> "CallResult":
        index = self._begin(candidate_id, pitch_text)
        phone = os.getenv("CALLEE_PHONE_E164", "")
        receipt: dict[str, Any] = {}
        amount_cents = 0
        provider_ref: str | None = None
        raw_observation: dict[str, Any] = {}
        try:
            if not _PHONE_PATTERN.fullmatch(phone):
                raise ValueError("CALLEE_PHONE_E164 must be set to a valid E.164 number")
            matches = self._zero_port.search(_CALL_CAPABILITY)
            service = _select_service(matches, self._configured_service_id)
            payload = build_provider_payload(service, phone, pitch_text)
            paid = self._zero_port.invoke(service, payload)
            raw_result = _field(paid, "result", {})
            if not isinstance(raw_result, Mapping):
                raise TypeError("Zero paid result.result must be a dictionary")
            raw_result = dict(raw_result)
            receipt = _field(paid, "receipt", {})
            if not isinstance(receipt, Mapping):
                raise TypeError("Zero paid result.receipt must be a dictionary")
            receipt = dict(receipt)
            candidate_amount = _field(paid, "amount_cents")
            if (
                not isinstance(candidate_amount, int)
                or isinstance(candidate_amount, bool)
                or candidate_amount < 0
            ):
                raise ValueError("Zero paid result has an invalid amount_cents")
            amount_cents = candidate_amount
            candidate_ref = _field(paid, "provider_ref")
            if candidate_ref is not None and not isinstance(candidate_ref, str):
                raise TypeError("Zero paid result provider_ref must be a string or null")
            provider_ref = candidate_ref
            raw_observation = {
                "service_id": _field(service, "service_id"),
                "result": raw_result,
                "raw_artifact_path": _field(paid, "raw_artifact_path"),
            }
            if _field(paid, "service_id") != _field(service, "service_id"):
                raise ValueError("Zero paid result service_id does not match invoked service")
            paid_ok = _field(paid, "ok", False)
            if not isinstance(paid_ok, bool):
                raise TypeError("Zero paid result ok must be a boolean")
            if not paid_ok:
                return self._persist(
                    index=index,
                    pitch_text=pitch_text,
                    transcript="",
                    receipt=receipt,
                    amount_cents=amount_cents,
                    provider_ref=provider_ref,
                    raw_provider_result=raw_observation,
                    phone=phone,
                    provider_ok=False,
                )
            transcript = _extract_transcript(service, raw_result)
            if transcript is None:
                raise ValueError("paid call result did not contain a documented transcript")
            return self._persist(
                index=index,
                pitch_text=pitch_text,
                transcript=transcript,
                receipt=receipt,
                amount_cents=amount_cents,
                provider_ref=provider_ref,
                raw_provider_result=raw_observation,
                phone=phone,
            )
        except Exception as exc:
            error_observation = {
                **raw_observation,
                "ok": False,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
            return self._persist(
                index=index,
                pitch_text=pitch_text,
                transcript="",
                receipt=receipt,
                amount_cents=amount_cents,
                provider_ref=provider_ref,
                raw_provider_result=error_observation,
                phone=phone or None,
                provider_ok=False,
            )


def build_call_port(
    *,
    zero_port: "ZeroPort",
    artifacts: Any,
    expected_fact_a: str,
    expected_fact_b_phrase: str,
    mode: str | None = None,
) -> "CallPort":
    """Build the configured call port; defaults to safe, unpaid fake mode."""

    selected_mode = (mode or os.getenv("CALL_MODE", "fake")).strip().casefold()
    common = {
        "artifacts": artifacts,
        "expected_fact_a": expected_fact_a,
        "expected_fact_b_phrase": expected_fact_b_phrase,
    }
    if selected_mode == "fake":
        return FakeCallPort(**common)  # type: ignore[return-value]
    if selected_mode == "live":
        if zero_port is None:
            raise ValueError("live call mode requires an injected ZeroPort")
        return ZeroBackedCallPort(zero_port=zero_port, **common)  # type: ignore[return-value]
    raise ValueError(f"unsupported CALL_MODE: {selected_mode!r}; expected fake or live")
