"""Immutable, team-authored conformance suite for the generated Fact B tool."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import threading
from typing import Any, Iterator, Literal, Mapping
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import BaseModel


_HERE = Path(__file__).resolve().parent
_EXPECTED = json.loads((_HERE / "expected_fact_b.json").read_text(encoding="utf-8"))
_EXPECTED_STATEMENT = _EXPECTED["value"]["statement"]
_EXPECTED_ENTRYPOINT = "generated_tools.fact_b_tool:run"


try:
    from contracts.models import ToolManifest as _ToolManifest
except ModuleNotFoundError as exc:
    if exc.name not in {"contracts", "contracts.models"}:
        raise

    class _ToolManifest(BaseModel):
        name: str
        capability: Literal["fact_b"]
        entrypoint: str
        input_schema: dict[str, Any]
        output_claim: Literal["fact_b"]
        version: str


def _read_manifest(generated_dir: Path) -> tuple[Path, _ToolManifest]:
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(generated_dir.glob("*.manifest.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            pytest.fail(f"manifest is not valid JSON ({path.name}): {exc}")
        if raw.get("capability") == "fact_b" or raw.get("output_claim") == "fact_b":
            candidates.append((path, raw))
    assert len(candidates) == 1, (
        "exactly one Fact B manifest is required; found "
        f"{[path.name for path, _ in candidates]}"
    )
    path, raw = candidates[0]
    try:
        manifest = _ToolManifest.model_validate(raw)
    except Exception as exc:
        pytest.fail(f"Fact B manifest does not satisfy ToolManifest: {exc}")
    return path, manifest


@contextmanager
def _import_root(generated_dir: Path) -> Iterator[None]:
    root = str(generated_dir.parent)
    sys.path.insert(0, root)
    try:
        yield
    finally:
        if root in sys.path:
            sys.path.remove(root)


def _load_run(generated_dir: Path):
    _, manifest = _read_manifest(generated_dir)
    module_name, separator, function_name = manifest.entrypoint.partition(":")
    assert separator and module_name and function_name, "entrypoint must use module:function"
    importlib.invalidate_caches()
    sys.modules.pop(module_name, None)
    sys.modules.pop(module_name.partition(".")[0], None)
    with _import_root(generated_dir):
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            pytest.fail(f"manifest entrypoint module failed to import: {exc}")
    run = getattr(module, function_name, None)
    assert callable(run), f"entrypoint function is not callable: {manifest.entrypoint}"
    return run


@dataclass
class _FixtureState:
    requests: list[str] = field(default_factory=list)


@pytest.fixture(scope="module")
def fixture_server() -> Iterator[tuple[str, _FixtureState]]:
    state = _FixtureState()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
            state.requests.append(self.path)
            parsed = urlparse(self.path)
            candidate = parse_qs(parsed.query).get("candidate_id", [""])[0]
            expected_path = "/companies/northstar_systems/migration-signal"
            if parsed.path != expected_path or candidate != _EXPECTED["candidate_id"]:
                body = json.dumps({"detail": "migration signal not found"}).encode()
                self.send_response(404)
            else:
                body = json.dumps(
                    {
                        "candidate_id": candidate,
                        "company_id": "northstar_systems",
                        "claim": "fact_b",
                        "statement": _EXPECTED_STATEMENT,
                        "source": _EXPECTED["source"],
                        "published_at": "2026-07-17T12:00:00-07:00",
                    }
                ).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _contains_fact_b(value: Any) -> bool:
    if isinstance(value, Mapping):
        if value.get("claim") == "fact_b":
            return True
        return any(_contains_fact_b(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_fact_b(item) for item in value)
    return value == _EXPECTED_STATEMENT


def test_exact_canonical_files_and_one_manifest(generated_dir: Path) -> None:
    required = {
        "fact_b_tool.py",
        "fact_b_tool.manifest.json",
        "test_fact_b_tool.py",
    }
    existing = {path.name for path in generated_dir.iterdir() if path.is_file()}
    assert required.issubset(existing), f"missing canonical generated files: {required - existing}"
    path, _ = _read_manifest(generated_dir)
    assert path.name == "fact_b_tool.manifest.json"


def test_manifest_matches_frozen_contract(generated_dir: Path) -> None:
    _, manifest = _read_manifest(generated_dir)
    assert manifest.name == "generated_fact_b_tool"
    assert manifest.capability == "fact_b"
    assert manifest.output_claim == "fact_b"
    assert manifest.entrypoint == _EXPECTED_ENTRYPOINT
    assert manifest.input_schema == {"candidate_id": "string"}
    assert manifest.version == "1.0.0"


def test_entrypoint_imports(generated_dir: Path) -> None:
    run = _load_run(generated_dir)
    assert callable(run)


def test_allowed_candidate_fetches_canonical_evidence(
    generated_dir: Path,
    fixture_server: tuple[str, _FixtureState],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url, state = fixture_server
    monkeypatch.setenv("FACT_B_FIXTURE_URL", base_url)
    before = len(state.requests)
    run = _load_run(generated_dir)
    result = run(_EXPECTED["candidate_id"])

    assert isinstance(result, dict), "run() must return a dictionary"
    assert result.get("candidate_id") == _EXPECTED["candidate_id"]
    assert result.get("claim") == "fact_b"
    assert result.get("value") == _EXPECTED["value"]
    assert result.get("source") == _EXPECTED["source"]
    provenance = result.get("provenance")
    assert isinstance(provenance, dict) and provenance.get("url")
    assert len(state.requests) == before + 1, "implementation did not fetch the fixture"
    assert state.requests[-1].startswith(
        "/companies/northstar_systems/migration-signal?"
    )


def test_unknown_candidate_does_not_receive_fact_b(
    generated_dir: Path,
    fixture_server: tuple[str, _FixtureState],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url, _ = fixture_server
    monkeypatch.setenv("FACT_B_FIXTURE_URL", base_url)
    run = _load_run(generated_dir)
    try:
        result = run("alex_rivera")
    except Exception as exc:
        assert str(exc).strip(), "candidate rejection must be explicit"
    else:
        assert not _contains_fact_b(result), "unknown candidate received Fact B"
        assert isinstance(result, dict) and result.get("error"), (
            "unknown candidate must return an explicit error or raise one"
        )


def test_implementation_uses_fixture_env_and_does_not_hardcode_statement(
    generated_dir: Path,
) -> None:
    source_path = generated_dir / "fact_b_tool.py"
    source = source_path.read_text(encoding="utf-8")
    assert _EXPECTED_STATEMENT not in source, (
        "implementation hardcodes the full Fact B sentence instead of fetching it"
    )
    assert "FACT_B_FIXTURE_URL" in source, (
        "implementation must obtain its base URL from FACT_B_FIXTURE_URL"
    )


def test_network_errors_are_explicit_and_never_return_false_evidence(
    generated_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FACT_B_FIXTURE_URL", "http://127.0.0.1:1")
    run = _load_run(generated_dir)
    try:
        result = run(_EXPECTED["candidate_id"])
    except Exception as exc:
        assert str(exc).strip(), "network exception must carry a useful message"
    else:
        assert not _contains_fact_b(result), "network failure returned false Fact B evidence"
        assert isinstance(result, dict) and result.get("error"), (
            "network failure must return an explicit error or raise one"
        )


def test_p1_registry_discovers_manifest_after_reload(generated_dir: Path) -> None:
    try:
        from agent.tool_registry import ToolRegistry
    except (ImportError, AttributeError):
        pytest.skip(
            "P1 agent.tool_registry.ToolRegistry is unavailable; enable after P1 integration"
        )

    parameters = inspect.signature(ToolRegistry).parameters
    if "generated_dir" in parameters:
        registry = ToolRegistry(generated_dir=generated_dir)
    elif "tools_dir" in parameters:
        registry = ToolRegistry(tools_dir=generated_dir)
    else:
        registry = ToolRegistry(generated_dir)
    registry.reload()
    discovered = registry.find("fact_b")
    assert discovered is not None, "registry did not discover fact_b after reload"
