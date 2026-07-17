"""Zero.xyz CLI adapter (implements ZeroPort) — the ONLY place that shells out
to the Zero CLI.

This module has two parts:

* :func:`select_within_budget` — a pure, provider-agnostic selection helper the
  orchestrator uses so it never hardcodes a provider name.
* :class:`ZeroClient` — the live adapter that invokes the installed Zero CLI via
  ``subprocess`` for capability search and paid invocation, preserving raw
  stdout/stderr and parsed JSON as run artifacts, enforcing budget from receipts,
  and never logging wallet credentials.

The live adapter is intentionally defensive about CLI syntax: inspect
``zero --help`` and https://zero.xyz/docs rather than guessing flags. Flag names
are read from environment overrides where practical so the adapter can be aligned
to the installed CLI without code changes.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from contracts.models import PaidResult, ServiceMatch

DEFAULT_TIMEOUT_S = 60


# --------------------------------------------------------------------------- #
# Pure selection helper (safe to import anywhere)
# --------------------------------------------------------------------------- #


def select_within_budget(
    matches: list[ServiceMatch], remaining_cents: int, *, prefer_cheapest: bool = True
) -> ServiceMatch | None:
    """Pick a service the run can afford.

    Prefers the cheapest priced service that fits ``remaining_cents``. If no
    service is priced, returns the first unpriced match (the caller must still
    enforce the budget from the receipt after invocation). Returns ``None`` when
    nothing is affordable.
    """

    priced = [m for m in matches if m.price_cents is not None and m.price_cents <= remaining_cents]
    if priced:
        return min(priced, key=lambda m: m.price_cents or 0) if prefer_cheapest else priced[0]
    unpriced = [m for m in matches if m.price_cents is None]
    if unpriced:
        return unpriced[0]
    return None


class ZeroCliError(RuntimeError):
    """Raised when the Zero CLI exits non-zero or returns unparseable output."""


# --------------------------------------------------------------------------- #
# Live adapter
# --------------------------------------------------------------------------- #


class ZeroClient:
    """Live ZeroPort backed by the installed Zero CLI.

    Parameters
    ----------
    artifacts:
        An :class:`agent.artifacts.Artifacts` instance; raw stdout/stderr and
        parsed JSON are preserved under the run directory (redaction is applied
        by the artifact writer, so wallet credentials are never persisted).
    cli:
        Executable name/path, defaulting to ``$ZERO_CLI`` or ``"zero"``.
    """

    def __init__(
        self,
        artifacts: Any,
        *,
        cli: str | None = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.artifacts = artifacts
        self.cli = cli or os.environ.get("ZERO_CLI", "zero")
        self.timeout_s = timeout_s
        self._raw_counter = 0

    # -- ZeroPort ---------------------------------------------------------- #

    def search(self, capability: str) -> list[ServiceMatch]:
        """Search the live Zero catalog for services matching ``capability``."""

        argv = self._search_argv(capability)
        proc = self._run(argv, context="search")
        data = self._parse_json(proc.stdout, context="search")
        self.artifacts.write_json("zero/search_fact_a.json", {"capability": capability, "raw": data})
        return self._parse_matches(data)

    def invoke(self, service: ServiceMatch, payload: dict[str, Any]) -> PaidResult:
        """Invoke a paid Zero service and preserve the raw result + receipt."""

        argv = self._invoke_argv(service, payload)
        proc = self._run(argv, context="invoke")
        data = self._parse_json(proc.stdout, context="invoke")

        result = data.get("result", data) if isinstance(data, dict) else {"raw": data}
        receipt = data.get("receipt", {}) if isinstance(data, dict) else {}
        amount = int(receipt.get("amount_cents", service.price_cents or 0))

        raw_path = self._preserve_raw("zero/fact_a_result.json", data)
        self.artifacts.write_json("zero/fact_a_receipt.json", receipt)

        return PaidResult(
            ok=bool(data.get("ok", True)) if isinstance(data, dict) else True,
            service_id=service.service_id,
            result=result,
            receipt=receipt,
            amount_cents=amount,
            provider_ref=str(receipt.get("receipt_id")) if receipt.get("receipt_id") else None,
            raw_artifact_path=raw_path,
        )

    # -- argv builders (aligned to the installed CLI via env overrides) ---- #

    def _search_argv(self, capability: str) -> list[str]:
        template = os.environ.get("ZERO_SEARCH_CMD")
        if template:
            return shlex.split(template) + [capability]
        # Reasonable default; verify against `zero --help` before live use.
        return [self.cli, "services", "search", "--json", capability]

    def _invoke_argv(self, service: ServiceMatch, payload: dict[str, Any]) -> list[str]:
        template = os.environ.get("ZERO_INVOKE_CMD")
        base = shlex.split(template) if template else [self.cli, "services", "invoke", "--json"]
        return base + [service.service_id, "--payload", json.dumps(payload)]

    # -- subprocess + parsing --------------------------------------------- #

    def _run(self, argv: list[str], *, context: str) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                argv, text=True, capture_output=True, timeout=self.timeout_s
            )
        except FileNotFoundError as exc:
            raise ZeroCliError(f"Zero CLI not found: {self.cli!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ZeroCliError(f"zero {context} timed out after {self.timeout_s}s") from exc

        # Preserve raw stdio for every call (redaction handled by the writer).
        self._preserve_raw(
            f"zero/{context}_stdio.json",
            {"argv": argv, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr},
        )
        if proc.returncode != 0:
            raise ZeroCliError(
                f"zero {context} exited {proc.returncode}: {proc.stderr[-2000:]}"
            )
        return proc

    def _parse_json(self, text: str, *, context: str) -> Any:
        text = (text or "").strip()
        if not text:
            raise ZeroCliError(f"zero {context} returned empty output")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ZeroCliError(f"zero {context} returned non-JSON output") from exc

    def _parse_matches(self, data: Any) -> list[ServiceMatch]:
        items = data.get("services", data) if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        matches: list[ServiceMatch] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            matches.append(
                ServiceMatch(
                    service_id=str(item.get("id") or item.get("service_id") or item.get("name", "")),
                    name=str(item.get("name", item.get("id", "unknown"))),
                    description=str(item.get("description", "")),
                    price_cents=item.get("price_cents"),
                    metadata={k: v for k, v in item.items() if k not in {"id", "name", "description", "price_cents"}},
                )
            )
        return matches

    def _preserve_raw(self, relative_path: str, value: Any) -> str:
        path = self.artifacts.write_json(relative_path, value)
        return str(path)


# --------------------------------------------------------------------------- #
# Live sponsor-proof capture + validation (no fabrication)
# --------------------------------------------------------------------------- #

#: Zero-owned artifacts that must exist for a run to count as a live Zero proof.
REQUIRED_LIVE_ZERO_ARTIFACTS: tuple[str, ...] = (
    "zero/cli_install_proof.txt",
    "zero/wallet_claim_proof.json",
    "zero/opening_balance.json",
    "zero/search_fact_a.json",
    "zero/fact_a_result.json",
    "zero/fact_a_receipt.json",
    "zero/closing_balance.json",
)


def validate_live_proof(run_dir: str | Path) -> list[str]:
    """Return the list of required live-Zero artifacts that are still missing.

    An empty list means the run directory contains a complete live Zero proof.
    This lets the team verify sponsor proof without ever fabricating it.
    """

    root = Path(run_dir)
    return [rel for rel in REQUIRED_LIVE_ZERO_ARTIFACTS if not (root / rel).is_file()]


def capture_cli_install_proof(
    artifacts: Any, *, cli: str | None = None, timeout_s: int = DEFAULT_TIMEOUT_S
) -> str:
    """Capture ``zero --version``/``--help`` output as installation proof.

    Raises :class:`ZeroCliError` if the CLI is absent — it never invents proof.
    """

    exe = cli or os.environ.get("ZERO_CLI", "zero")
    for flags in (["--version"], ["--help"]):
        try:
            proc = subprocess.run([exe, *flags], text=True, capture_output=True, timeout=timeout_s)
        except FileNotFoundError as exc:
            raise ZeroCliError(f"Zero CLI not found: {exe!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ZeroCliError("zero install-proof capture timed out") from exc
        if proc.returncode == 0:
            text = f"$ {exe} {' '.join(flags)}\n{proc.stdout}\n{proc.stderr}"
            return str(artifacts.write_text("zero/cli_install_proof.txt", text))
    raise ZeroCliError("could not capture Zero CLI install proof")
