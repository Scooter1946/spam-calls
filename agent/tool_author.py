"""Tool-authoring adapter: asks a coding model to write the missing Fact B tool.

Modes
-----
* ``fake``    — writes a valid, self-contained example tool into the target dir
                (used by P1's tests; the target is always a temp directory).
* ``cli``     — runs a coding-agent command from ``CODE_AGENT_COMMAND`` (or the
                request) and feeds it the production prompt on stdin.
* ``bedrock`` — optional; invokes a Bedrock model via boto3 and writes the three
                files from a JSON response. Only used when already configured.

The running agent creates exactly three files (global context §9)::

    generated_tools/fact_b_tool.py
    generated_tools/fact_b_tool.manifest.json
    generated_tools/test_fact_b_tool.py

Path safety (§7) is enforced by :func:`assert_only_allowed_paths`, which the
orchestrator calls against ``git status --porcelain`` after generation.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

# The three canonical tool files (basenames), relative to the tools directory.
TOOL_MODULE_FILE = "fact_b_tool.py"
TOOL_MANIFEST_FILE = "fact_b_tool.manifest.json"
TOOL_TEST_FILE = "test_fact_b_tool.py"

# Canonical generated-tool manifest (§9). ``version`` may be bumped on repair.
CANONICAL_MANIFEST: dict[str, Any] = {
    "name": "generated_fact_b_tool",
    "capability": "fact_b",
    "entrypoint": "generated_tools.fact_b_tool:run",
    "input_schema": {"candidate_id": "string"},
    "output_claim": "fact_b",
    "version": "1.0.0",
}

DEFAULT_SUBPROCESS_TIMEOUT_S = 180


class AuthoringError(RuntimeError):
    """Raised when tool authoring fails in a way the loop must observe."""


class PathViolation(AuthoringError):
    """Raised when generation touched a path outside the allowed set."""


class AuthorRequest(BaseModel):
    """Everything the author needs to produce the Fact B tool."""

    run_id: str
    tool_dir: str
    allowed_paths: list[str]
    function_contract: str
    manifest: dict[str, Any]
    fixture_url: str
    response_schema: dict[str, Any]
    failed_zero_search: Any = None
    conformance_command: str
    # Canonical expected payload — used ONLY by fake mode to synthesize a working
    # example. Live modes must derive the value from the fixture, not from this.
    canonical_value: dict[str, Any] | None = None


class AuthorResult(BaseModel):
    files: list[str]
    prompt: str
    mode: str
    notes: str = ""


class ConformanceResult(BaseModel):
    """Outcome of running the team-authored conformance suite."""

    exit_code: int
    output: str
    command: str | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


def run_conformance_command(
    command: str, *, cwd: str | None = None, timeout_s: int = DEFAULT_SUBPROCESS_TIMEOUT_S
) -> ConformanceResult:
    """Run P2's fixed conformance command as a subprocess with a finite timeout.

    The exact test output is preserved so it can be fed back to the author for a
    single repair attempt.
    """

    try:
        proc = subprocess.run(
            shlex.split(command),
            text=True,
            capture_output=True,
            timeout=timeout_s,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return ConformanceResult(
            exit_code=124, output=f"conformance timed out after {timeout_s}s", command=command
        )
    return ConformanceResult(
        exit_code=proc.returncode,
        output=(proc.stdout + proc.stderr)[-8000:],
        command=command,
    )


@runtime_checkable
class AuthorPort(Protocol):
    def author(self, request: AuthorRequest) -> AuthorResult: ...

    def repair(self, request: AuthorRequest, test_output: str) -> AuthorResult: ...


# --------------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------------- #


def assert_only_allowed_paths(changed_paths: list[str], allowed_paths: list[str]) -> None:
    """Raise :class:`PathViolation` if any changed path is not allowed.

    ``changed_paths`` are repo-relative paths (e.g. from ``git status --porcelain``).
    """

    allowed = {p.replace("\\", "/").lstrip("./") for p in allowed_paths}
    violations = [
        p for p in changed_paths if p and p.replace("\\", "/").lstrip("./") not in allowed
    ]
    if violations:
        raise PathViolation(
            f"generation changed paths outside the allowed set: {sorted(violations)}"
        )


def parse_git_status_porcelain(output: str) -> list[str]:
    """Extract changed repo-relative paths from ``git status --porcelain`` output."""

    paths: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        # Format: 'XY <path>' or 'XY <old> -> <new>' for renames.
        rest = line[3:] if len(line) > 3 else line.strip()
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        paths.append(rest.strip().strip('"'))
    return paths


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #


def build_prompt(request: AuthorRequest, *, repair_output: str | None = None) -> str:
    """Construct the production authoring prompt with every required element (§7)."""

    parts = [
        "You are generating exactly one tool for the PitchLoop agent.",
        "",
        "GOAL: implement a tool that retrieves the Fact B public-company signal for a",
        "candidate and returns it as a canonical evidence payload for claim `fact_b`.",
        "",
        "YOU MAY CREATE OR MODIFY ONLY THESE THREE FILES (relative paths):",
        *(f"  - {p}" for p in request.allowed_paths),
        "Editing ANY other file is forbidden and will cause automatic rejection.",
        "",
        "FUNCTION CONTRACT:",
        request.function_contract,
        "",
        "MANIFEST (write this JSON verbatim to the .manifest.json file):",
        json.dumps(request.manifest, indent=2, sort_keys=True),
        "",
        f"FIXTURE URL (retrieve the signal here): {request.fixture_url}",
        "FIXTURE RESPONSE SCHEMA:",
        json.dumps(request.response_schema, indent=2, sort_keys=True),
        "",
        "The prior Zero capability search for this signal returned NO match:",
        json.dumps(request.failed_zero_search, indent=2, sort_keys=True, default=str),
        "",
        "Unknown candidates MUST NOT receive Fact B (return an empty payload).",
        "Use a finite HTTP timeout. Validate the response before using it.",
        "",
        f"CONFORMANCE COMMAND (your tool must make this pass): {request.conformance_command}",
    ]
    if repair_output is not None:
        parts += [
            "",
            "THE PREVIOUS ATTEMPT FAILED CONFORMANCE. Exact test output follows;",
            "fix the tool so the same command passes. Do not touch other files.",
            "----- BEGIN TEST OUTPUT -----",
            repair_output.strip(),
            "----- END TEST OUTPUT -----",
        ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Author
# --------------------------------------------------------------------------- #


class ToolAuthor:
    """Authoring adapter with fake / cli / bedrock backends."""

    def __init__(
        self,
        mode: str = "fake",
        *,
        code_agent_command: str | None = None,
        timeout_s: int = DEFAULT_SUBPROCESS_TIMEOUT_S,
    ) -> None:
        if mode not in {"fake", "cli", "bedrock"}:
            raise ValueError(f"unknown AUTHOR_MODE {mode!r}")
        self.mode = mode
        self.code_agent_command = code_agent_command or os.environ.get("CODE_AGENT_COMMAND")
        self.timeout_s = timeout_s

    # -- AuthorPort -------------------------------------------------------- #

    def author(self, request: AuthorRequest) -> AuthorResult:
        return self._run(request, repair_output=None)

    def repair(self, request: AuthorRequest, test_output: str) -> AuthorResult:
        return self._run(request, repair_output=test_output)

    # -- dispatch ---------------------------------------------------------- #

    def _run(self, request: AuthorRequest, *, repair_output: str | None) -> AuthorResult:
        prompt = build_prompt(request, repair_output=repair_output)
        if self.mode == "fake":
            files = self._write_fake_tool(request)
        elif self.mode == "cli":
            files = self._run_cli(request, prompt)
        else:  # bedrock
            files = self._run_bedrock(request, prompt)
        return AuthorResult(files=files, prompt=prompt, mode=self.mode)

    # -- fake -------------------------------------------------------------- #

    def _write_fake_tool(self, request: AuthorRequest) -> list[str]:
        tool_dir = Path(request.tool_dir)
        tool_dir.mkdir(parents=True, exist_ok=True)

        value = request.canonical_value or {
            "candidate_id": "maya_chen",
            "claim": "fact_b",
            "value": {
                "statement": "Northstar Systems has an August 30 API v1 migration deadline."
            },
            "source": "northstar_public_migration_signal",
            "provenance": {"url": request.fixture_url},
        }
        canonical_candidate = value.get("candidate_id", "maya_chen")

        module_src = (
            '"""Generated Fact B tool (fake author example — self-contained)."""\n'
            "from __future__ import annotations\n\n"
            f"_CANONICAL_CANDIDATE = {canonical_candidate!r}\n"
            f"_PAYLOAD = {json.dumps(value, indent=4, sort_keys=True)}\n\n\n"
            "def run(candidate_id: str) -> dict:\n"
            '    """Return the canonical fact_b evidence payload for a known candidate."""\n'
            "    if candidate_id != _CANONICAL_CANDIDATE:\n"
            "        return {}\n"
            "    return dict(_PAYLOAD)\n"
        )

        test_src = (
            '"""Generated self-test for the fake Fact B tool."""\n'
            "from fact_b_tool import run\n\n\n"
            "def test_known_candidate_gets_fact_b():\n"
            f"    out = run({canonical_candidate!r})\n"
            '    assert out["claim"] == "fact_b"\n'
            '    assert "August 30" in out["value"]["statement"]\n\n\n'
            "def test_unknown_candidate_gets_nothing():\n"
            '    assert run("nobody") == {}\n'
        )

        (tool_dir / TOOL_MODULE_FILE).write_text(module_src, encoding="utf-8")
        (tool_dir / TOOL_MANIFEST_FILE).write_text(
            json.dumps(request.manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (tool_dir / TOOL_TEST_FILE).write_text(test_src, encoding="utf-8")

        return [
            str(tool_dir / TOOL_MODULE_FILE),
            str(tool_dir / TOOL_MANIFEST_FILE),
            str(tool_dir / TOOL_TEST_FILE),
        ]

    # -- cli --------------------------------------------------------------- #

    def _run_cli(self, request: AuthorRequest, prompt: str) -> list[str]:
        if not self.code_agent_command:
            raise AuthoringError("CODE_AGENT_COMMAND is not set for cli author mode")
        argv = shlex.split(self.code_agent_command)
        try:
            proc = subprocess.run(
                argv,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.timeout_s,
                cwd=None,
            )
        except subprocess.TimeoutExpired as exc:
            raise AuthoringError(f"coding-agent command timed out after {self.timeout_s}s") from exc
        if proc.returncode != 0:
            raise AuthoringError(
                f"coding-agent command exited {proc.returncode}: {proc.stderr[-2000:]}"
            )
        return self._verify_outputs(request)

    # -- bedrock (optional) ------------------------------------------------ #

    def _run_bedrock(self, request: AuthorRequest, prompt: str) -> list[str]:
        try:
            import boto3  # noqa: PLC0415 - optional dependency, imported lazily
        except Exception as exc:  # pragma: no cover - env dependent
            raise AuthoringError("boto3 unavailable for bedrock author mode") from exc

        model_id = os.environ.get("BEDROCK_MODEL_ID")
        if not model_id:
            raise AuthoringError("BEDROCK_MODEL_ID is not set for bedrock author mode")

        client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION"))
        instruction = (
            prompt
            + "\n\nRespond with ONLY a JSON object mapping each of the three relative "
            "file paths to its full file contents, no prose."
        )
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": instruction}],
            }
        )
        resp = client.invoke_model(modelId=model_id, body=body)  # pragma: no cover
        payload = json.loads(resp["body"].read())
        text = "".join(block.get("text", "") for block in payload.get("content", []))
        try:
            files_map = json.loads(text)
        except json.JSONDecodeError as exc:  # pragma: no cover
            raise AuthoringError("bedrock response was not valid JSON file map") from exc

        tool_dir = Path(request.tool_dir)
        tool_dir.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for rel, contents in files_map.items():
            dest = tool_dir / Path(rel).name
            dest.write_text(contents, encoding="utf-8")
            written.append(str(dest))
        return written

    # -- shared ------------------------------------------------------------ #

    def _verify_outputs(self, request: AuthorRequest) -> list[str]:
        tool_dir = Path(request.tool_dir)
        expected = [TOOL_MODULE_FILE, TOOL_MANIFEST_FILE, TOOL_TEST_FILE]
        written: list[str] = []
        for name in expected:
            path = tool_dir / name
            if not path.is_file():
                raise AuthoringError(f"author did not produce required file: {path}")
            written.append(str(path))
        return written
