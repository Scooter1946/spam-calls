"""Pomerium-backed implementation of PitchLoop's frozen PolicyPort contract."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from contracts.models import PolicyDecision


_SAFE_RESPONSE_HEADERS = {
    "content-type",
    "date",
    "server",
    "x-cloud-trace-context",
    "x-envoy-upstream-service-time",
    "x-pomerium-request-id",
    "x-request-id",
}
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)
_MAX_RESPONSE_BODY_CHARS = 65_536


class PolicyConfigurationError(RuntimeError):
    """Raised internally when live policy configuration is incomplete."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _run_root() -> Path:
    configured = os.getenv("PITCHLOOP_RUN_DIR")
    if configured:
        return Path(configured)
    return Path("runs") / os.getenv("PITCHLOOP_RUN_ID", "demo-001")


def _safe_url(value: str) -> str:
    """Keep the route location while stripping query parameters and fragments."""

    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).casefold().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Recursively remove secret-shaped fields and known secret values."""

    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _is_sensitive_key(key) else _redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted
    return value


def _artifact_path(artifacts: Any, relative_path: str) -> Path:
    if artifacts is None:
        return _run_root() / relative_path
    if isinstance(artifacts, (str, os.PathLike)):
        return Path(artifacts) / relative_path
    for attribute in ("root", "run_dir", "base_dir"):
        root = getattr(artifacts, attribute, None)
        if root is not None:
            return Path(root) / relative_path
    return Path(relative_path)


def _write_json_artifact(artifacts: Any, relative_path: str, payload: dict[str, Any]) -> str:
    """Write through the orchestrator artifact store or a filesystem run root."""

    writer = getattr(artifacts, "write_json", None) if artifacts is not None else None
    if callable(writer):
        result = writer(relative_path, payload)
        return str(result) if result is not None else str(_artifact_path(artifacts, relative_path))

    path = _artifact_path(artifacts, relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return str(path)


def _write_live_policy_artifacts(
    artifacts: Any,
    relative_path: str,
    payload: dict[str, Any],
) -> str:
    """Preserve provider proof even if the orchestrator normalizes its path.

    P1 writes the canonical ``policy/deny.json`` or ``policy/allow.json`` after
    this adapter returns. Keep the full request/response observation in a
    sibling ``*.raw.json`` file and return that immutable proof path, while also
    writing the canonical path for standalone adapter use.
    """

    path = Path(relative_path)
    raw_name = f"{path.stem}.raw{path.suffix}" if path.suffix else f"{path.name}.raw.json"
    raw_relative_path = str(path.with_name(raw_name))
    raw_artifact_path = _write_json_artifact(artifacts, raw_relative_path, payload)
    _write_json_artifact(artifacts, relative_path, payload)
    return raw_artifact_path


def _authorization_value(token: str) -> str:
    token = token.strip()
    if not token:
        raise PolicyConfigurationError("POMERIUM_SERVICE_ACCOUNT_TOKEN is not configured")
    if token.startswith("Bearer ") or token.startswith("Pomerium "):
        return token
    if token.startswith("Pomerium-"):
        return f"Bearer {token}"
    return f"Bearer Pomerium-{token}"


def _body_for_artifact(response: httpx.Response) -> tuple[Any, bool]:
    text = response.text
    truncated = len(text) > _MAX_RESPONSE_BODY_CHARS
    if truncated:
        text = text[:_MAX_RESPONSE_BODY_CHARS]
    try:
        return json.loads(text), truncated
    except (json.JSONDecodeError, TypeError):
        return text, truncated


class FakePolicyPort:
    """Deterministic policy implementation for the complete fake loop."""

    def __init__(self, *, artifacts: Any = None) -> None:
        self._artifacts = artifacts

    def authorize(
        self,
        action: str,
        candidate_id: str,
        context: dict[str, Any],
    ) -> PolicyDecision:
        if candidate_id == "alex_rivera":
            allowed, status_code, reason, filename = (
                False,
                403,
                "candidate_not_consented",
                "deny.json",
            )
        elif candidate_id == "maya_chen":
            allowed, status_code, reason, filename = (
                True,
                200,
                "consent_verified",
                "allow.json",
            )
        else:
            allowed, status_code, reason, filename = (
                False,
                0,
                "unknown_candidate",
                "error.json",
            )

        relative_path = f"policy/{filename}"
        payload = {
            "adapter_mode": "fake",
            "action": action,
            "candidate_id": candidate_id,
            "context": _redact(context),
            "observed_at": _utc_now(),
            "decision": {
                "allowed": allowed,
                "status_code": status_code,
                "reason": reason,
            },
            "response": {
                "status_code": status_code,
                "upstream_reached": allowed,
            },
        }
        artifact_path = _write_json_artifact(self._artifacts, relative_path, payload)
        return PolicyDecision(
            allowed=allowed,
            status_code=status_code,
            reason=reason,
            audit_ref=f"fake-policy-{candidate_id}",
            raw_artifact_path=artifact_path,
        )


class PomeriumPolicyPort:
    """Ask one of two Pomerium routes using the same service-account identity."""

    def __init__(
        self,
        *,
        denied_url: str | None = None,
        allowed_url: str | None = None,
        service_account_token: str | None = None,
        timeout_seconds: float = 10.0,
        artifacts: Any = None,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        self._denied_url = denied_url if denied_url is not None else os.getenv("POMERIUM_DENIED_URL", "")
        self._allowed_url = allowed_url if allowed_url is not None else os.getenv("POMERIUM_ALLOWED_URL", "")
        self._token = (
            service_account_token
            if service_account_token is not None
            else os.getenv("POMERIUM_SERVICE_ACCOUNT_TOKEN", "")
        )
        self._timeout_seconds = timeout_seconds
        self._artifacts = artifacts
        self._client_factory = client_factory

    def authorize(
        self,
        action: str,
        candidate_id: str,
        context: dict[str, Any],
    ) -> PolicyDecision:
        observed_at = _utc_now()
        if candidate_id == "alex_rivera":
            url, url_label, filename = self._denied_url, "denied", "deny.json"
        elif candidate_id == "maya_chen":
            url, url_label, filename = self._allowed_url, "allowed", "allow.json"
        else:
            return self._failure_decision(
                action=action,
                candidate_id=candidate_id,
                context=context,
                observed_at=observed_at,
                url="",
                url_label="unmapped",
                reason="unknown_candidate",
                error="candidate has no configured policy route",
            )

        if action != "place_sales_call":
            return self._failure_decision(
                action=action,
                candidate_id=candidate_id,
                context=context,
                observed_at=observed_at,
                url=url,
                url_label=url_label,
                reason="unsupported_policy_action",
                error="only place_sales_call can be authorized by this adapter",
            )

        relative_path = f"policy/{filename}"
        raw_token = self._token.strip()
        secrets = (raw_token, _authorization_value(raw_token)) if raw_token else ()

        try:
            if not url:
                raise PolicyConfigurationError(f"POMERIUM_{url_label.upper()}_URL is not configured")
            authorization = _authorization_value(raw_token)
            headers = {
                "Accept": "application/json",
                "Authorization": authorization,
                "Content-Type": "application/json",
            }
            request_body = {
                "action": action,
                "candidate_id": candidate_id,
                "context": context,
            }
            with self._client_factory(
                follow_redirects=False,
                timeout=httpx.Timeout(self._timeout_seconds),
            ) as client:
                response = client.post(url, headers=headers, json=request_body)
        except (PolicyConfigurationError, httpx.HTTPError, OSError) as exc:
            reason = "policy_config_error" if isinstance(exc, PolicyConfigurationError) else "policy_network_error"
            return self._failure_decision(
                action=action,
                candidate_id=candidate_id,
                context=context,
                observed_at=observed_at,
                url=url,
                url_label=url_label,
                reason=reason,
                error=f"{type(exc).__name__}: {exc}",
                secrets=secrets,
                relative_path=relative_path,
            )

        response_body, body_truncated = _body_for_artifact(response)
        safe_headers = {
            key.casefold(): value
            for key, value in response.headers.items()
            if key.casefold() in _SAFE_RESPONSE_HEADERS
        }
        request_id = safe_headers.get("x-request-id") or safe_headers.get("x-pomerium-request-id")
        upstream_reached = bool(
            isinstance(response_body, Mapping) and response_body.get("reached_upstream") is True
        )

        if candidate_id == "alex_rivera" and response.status_code == 403:
            allowed, reason = False, "candidate_not_consented"
        elif candidate_id == "maya_chen" and response.status_code == 200 and upstream_reached:
            allowed, reason = True, "consent_verified"
        else:
            allowed, reason = False, "unexpected_policy_response"

        artifact = {
            "adapter_mode": "live",
            "action": action,
            "candidate_id": candidate_id,
            "context": _redact(context, secrets),
            "observed_at": observed_at,
            "request": {
                "method": "POST",
                "url": _safe_url(url),
                "url_label": url_label,
                "authenticated": True,
                "authentication_scheme": "pomerium_service_account",
                "follow_redirects": False,
            },
            "response": {
                "status_code": response.status_code,
                "headers": safe_headers,
                "body": _redact(response_body, secrets),
                "body_truncated": body_truncated,
                "upstream_reached": upstream_reached,
            },
            "decision": {
                "allowed": allowed,
                "status_code": response.status_code,
                "reason": reason,
            },
        }
        artifact_path = _write_live_policy_artifacts(
            self._artifacts,
            relative_path,
            _redact(artifact, secrets),
        )
        return PolicyDecision(
            allowed=allowed,
            status_code=response.status_code,
            reason=reason,
            audit_ref=request_id,
            raw_artifact_path=artifact_path,
        )

    def _failure_decision(
        self,
        *,
        action: str,
        candidate_id: str,
        context: dict[str, Any],
        observed_at: str,
        url: str,
        url_label: str,
        reason: str,
        error: str,
        secrets: tuple[str, ...] = (),
        relative_path: str = "policy/error.json",
    ) -> PolicyDecision:
        artifact = {
            "adapter_mode": "live",
            "action": action,
            "candidate_id": candidate_id,
            "context": _redact(context, secrets),
            "observed_at": observed_at,
            "request": {
                "method": "POST",
                "url": _safe_url(url) if url else "",
                "url_label": url_label,
                "authenticated": bool(self._token),
                "follow_redirects": False,
            },
            "failure": {
                "type": reason,
                "message": _redact(error, secrets),
            },
            "decision": {
                "allowed": False,
                "status_code": 0,
                "reason": reason,
            },
        }
        artifact_path = _write_live_policy_artifacts(
            self._artifacts,
            relative_path,
            _redact(artifact, secrets),
        )
        return PolicyDecision(
            allowed=False,
            status_code=0,
            reason=reason,
            raw_artifact_path=artifact_path,
        )


def build_policy_port(*, mode: str | None = None, artifacts: Any = None):
    """Build the fake or live PolicyPort selected by configuration."""

    selected = (mode or os.getenv("POLICY_MODE", "fake")).strip().casefold()
    if selected == "fake":
        return FakePolicyPort(artifacts=artifacts)
    if selected == "live":
        return PomeriumPolicyPort(artifacts=artifacts)
    raise ValueError(f"unsupported POLICY_MODE: {selected!r}")
