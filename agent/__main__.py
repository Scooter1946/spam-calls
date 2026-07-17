"""CLI entrypoint: ``python -m agent --spec scenario/run_spec.json``.

Loads mode-specific adapters from the environment, then runs the orchestrator to
completion with no human input. Every port has a ``*_MODE`` env var
(``fake``/``live``); ``AUTHOR_MODE`` is ``fake``/``cli``/``bedrock``. When every
mode is ``fake`` (the default), the full loop runs standalone against P1's fakes
so the vertical loop is demonstrable before P2-P4 land.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from agent.artifacts import Artifacts
from agent.orchestrator import Config, Deps, Orchestrator
from agent.tool_author import ToolAuthor, parse_git_status_porcelain, run_conformance_command
from agent.tool_registry import ToolRegistry
from contracts import serde
from contracts.models import RunSpec

PORT_MODES = ("ZERO", "POLICY", "CALL", "REPO", "EVIDENCE")


def load_spec(path: str) -> RunSpec:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return RunSpec.model_validate(data)


def build_config() -> Config:
    return Config(
        fact_a_capability=os.environ.get(
            "ZERO_FACT_A_CAPABILITY", "company enrichment for sales personalization"
        ),
        fact_b_capability=os.environ.get(
            "ZERO_FACT_B_CAPABILITY", "northstar api v1 migration deadline"
        ),
        fixture_url=os.environ.get("FACT_B_FIXTURE_URL", "http://127.0.0.1:8088"),
        conformance_command=os.environ.get(
            "CONFORMANCE_COMMAND", "pytest -q conformance/test_generated_tool.py"
        ),
    )


def _real_git_status() -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"], text=True, capture_output=True, timeout=15
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return parse_git_status_porcelain(proc.stdout)


def build_fake_deps(spec: RunSpec, config: Config, artifacts: Artifacts) -> Deps:
    from agent import fakes

    staging = Path(tempfile.mkdtemp(prefix="pitchloop-staging-"))
    target = Path(tempfile.mkdtemp(prefix="pitchloop-tools-"))
    config.author_tool_dir = str(staging)
    config.reload_tools_dir = str(target)

    return Deps(
        zero=fakes.FakeZeroPort(config.fact_a_capability, config.fact_b_capability, artifacts=artifacts),
        policy=fakes.FakePolicyPort(),
        evidence=fakes.FakeEvidencePort(),
        repo=fakes.FakeRepoPort(staging_dir=staging, target_tools_dir=target),
        registry=ToolRegistry(tools_dir=target, artifacts=artifacts),
        author=ToolAuthor(mode="fake"),
        artifacts=artifacts,
        render_pitch=fakes.fake_render_pitch,
        build_call_port=fakes.build_fake_call_port,
        run_conformance=fakes.fake_conformance,
        git_status=lambda: [],
    )


def build_live_deps(spec: RunSpec, config: Config, artifacts: Artifacts, author_mode: str) -> Deps:
    """Wire the live P2/P3/P4 adapters. Raises a clear error if any is absent."""

    missing: list[str] = []

    def _try(import_path: str, attr: str):
        module_name, _, _ = import_path.partition(":")
        try:
            module = __import__(module_name, fromlist=[attr])
            return getattr(module, attr)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{import_path} ({type(exc).__name__}: {exc})")
            return None

    from integrations.zero_client import ZeroClient

    build_policy_port = _try("integrations.policy_client", "build_policy_port")
    build_call_port = _try("integrations.call_client", "build_call_port")
    build_repo_port = _try("integrations.repo_client", "build_repo_port")
    build_evidence_port = _try("integrations.evidence_client", "build_evidence_port")
    render_pitch = _try("pitch.render", "render_pitch")

    if missing:
        raise SystemExit(
            "live mode requires P2/P3/P4 modules that are not available yet:\n  - "
            + "\n  - ".join(missing)
            + "\nRun with all *_MODE=fake to exercise the P1 loop standalone."
        )

    return Deps(
        zero=ZeroClient(artifacts=artifacts),
        policy=build_policy_port(artifacts=artifacts),
        evidence=build_evidence_port(artifacts=artifacts),
        repo=build_repo_port(artifacts=artifacts),
        registry=ToolRegistry(tools_dir=config.reload_tools_dir, artifacts=artifacts),
        author=ToolAuthor(mode=author_mode),
        artifacts=artifacts,
        render_pitch=render_pitch,
        build_call_port=build_call_port,
        run_conformance=lambda _tool_dir: run_conformance_command(config.conformance_command),
        git_status=_real_git_status,
    )


def build_deps(spec: RunSpec, config: Config, artifacts: Artifacts) -> Deps:
    modes = {name: os.environ.get(f"{name}_MODE", "fake") for name in PORT_MODES}
    author_mode = os.environ.get("AUTHOR_MODE", "fake")
    all_fake = all(m == "fake" for m in modes.values()) and author_mode == "fake"
    if all_fake:
        return build_fake_deps(spec, config, artifacts)
    return build_live_deps(spec, config, artifacts, author_mode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent", description="Run the PitchLoop agent.")
    parser.add_argument("--spec", required=True, help="Path to run_spec.json")
    parser.add_argument("--run-dir", default=None, help="Override PITCHLOOP_RUN_DIR")
    args = parser.parse_args(argv)

    spec = load_spec(args.spec)
    config = build_config()
    artifacts = Artifacts(run_dir=args.run_dir)
    deps = build_deps(spec, config, artifacts)

    result = Orchestrator(spec, deps, config).run()
    print(serde.dumps(result.to_dict()))
    return 0 if result.outcome == "MEETING_BOOKED" else 1


if __name__ == "__main__":
    sys.exit(main())
