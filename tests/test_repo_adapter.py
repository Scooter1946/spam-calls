from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from integrations.repo_client import (
    AGENT_COMMIT_SUBJECT,
    AGENT_PR_TITLE,
    ALLOWED_GENERATED_PATHS,
    FakeRepoPort,
    GitHubRepoPort,
    RepoValidationError,
)


class RecordingRunner:
    def __init__(self, *, extra_status_path: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.extra_status_path = extra_status_path

    def __call__(self, command, **kwargs):
        del kwargs
        command = list(command)
        self.calls.append(command)
        stdout = ""
        if command[:3] == ["git", "status", "--porcelain=v1"]:
            paths = sorted(ALLOWED_GENERATED_PATHS)
            if self.extra_status_path:
                paths.append(self.extra_status_path)
            stdout = "\n".join(f"?? {path}" for path in paths)
        elif command[:3] == ["git", "config", "--get"]:
            stdout = "https://github.com/acme/pitchloop.git"
        elif command[:4] == ["git", "diff", "--cached", "--name-only"]:
            stdout = "\n".join(sorted(ALLOWED_GENERATED_PATHS))
        elif command[:3] == ["gh", "pr", "create"]:
            stdout = "https://github.com/acme/pitchloop/pull/42"
        elif command[:3] == ["gh", "pr", "view"]:
            fields = command[-1]
            if fields == "number,url,headRefName,files":
                stdout = json.dumps(
                    {
                        "number": 42,
                        "url": "https://github.com/acme/pitchloop/pull/42",
                        "headRefName": "agent/fact-b-demo-001",
                        "files": [{"path": path} for path in sorted(ALLOWED_GENERATED_PATHS)],
                    }
                )
            else:
                stdout = json.dumps(
                    {
                        "number": 42,
                        "url": "https://github.com/acme/pitchloop/pull/42",
                        "state": "MERGED",
                        "mergedAt": "2026-07-17T18:00:00Z",
                        "mergeCommit": {"oid": "a" * 40},
                    }
                )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fixture(tmp_path: Path, *, conformance_passed: bool = True):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    for relative_path in ALLOWED_GENERATED_PATHS:
        path = repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture for {relative_path}\n", encoding="utf-8")

    run_dir = tmp_path / "runs/demo-001"
    _write_json(run_dir / "tools/conformance_result.json", {"passed": conformance_passed})
    _write_json(
        run_dir / "evidence/diagnosis.json",
        {
            "missing_claims": ["fact_b"],
            "failed_call_evidence_id": "evidence-call-001",
        },
    )
    _write_json(run_dir / "zero/search_fact_b.json", {"matches": []})
    return repo_root, run_dir


def _live_port(tmp_path: Path, runner: RecordingRunner, *, conformance_passed: bool = True):
    repo_root, run_dir = _fixture(tmp_path, conformance_passed=conformance_passed)
    return GitHubRepoPort(
        repo_root=repo_root,
        run_id="demo-001",
        run_dir=run_dir,
        runner=runner,
    ), repo_root, run_dir


@pytest.mark.parametrize(
    "bad_path",
    [
        "README.md",
        "generated_tools/extra.py",
        "../generated_tools/fact_b_tool.py",
        "/tmp/fact_b_tool.py",
        "generated_tools\\fact_b_tool.py",
    ],
)
def test_rejects_extra_absolute_and_traversal_paths(tmp_path: Path, bad_path: str) -> None:
    port, _, _ = _live_port(tmp_path, RecordingRunner())
    files = sorted(ALLOWED_GENERATED_PATHS) + [bad_path]

    with pytest.raises(RepoValidationError):
        port.create_agent_pr(files, "ignored", "ignored")


def test_requires_all_three_generated_files(tmp_path: Path) -> None:
    port, _, _ = _live_port(tmp_path, RecordingRunner())

    with pytest.raises(RepoValidationError, match="exactly all three"):
        port.create_agent_pr(sorted(ALLOWED_GENERATED_PATHS)[:-1], "ignored", "ignored")


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    runner = RecordingRunner()
    port, repo_root, _ = _live_port(tmp_path, runner)
    escaped = tmp_path / "outside.py"
    escaped.write_text("outside\n", encoding="utf-8")
    tool_path = repo_root / "generated_tools/fact_b_tool.py"
    tool_path.unlink()
    tool_path.symlink_to(escaped)

    with pytest.raises(RepoValidationError, match="symlink escapes"):
        port.create_agent_pr(sorted(ALLOWED_GENERATED_PATHS), "ignored", "ignored")
    assert runner.calls == []


def test_rejects_failed_conformance_before_any_command(tmp_path: Path) -> None:
    runner = RecordingRunner()
    port, _, run_dir = _live_port(tmp_path, runner, conformance_passed=False)

    with pytest.raises(RepoValidationError, match="conformance did not pass"):
        port.create_agent_pr(sorted(ALLOWED_GENERATED_PATHS), "ignored", "ignored")
    assert runner.calls == []
    failure = json.loads((run_dir / "repo/pr.json").read_text())
    assert failure["status"] == "failed"
    assert failure["error_type"] == "RepoValidationError"


def test_accepts_p1_conformance_and_diagnosis_artifact_shapes(tmp_path: Path) -> None:
    runner = RecordingRunner()
    port, _, run_dir = _live_port(tmp_path, runner)
    _write_json(
        run_dir / "tools/conformance_result.json",
        {"exit_code": 0, "output": "1 passed", "command": "pytest -q conformance"},
    )
    _write_json(
        run_dir / "evidence/diagnosis.json",
        {
            "present_claims": ["fact_a"],
            "missing_claims": ["fact_b"],
            "evidence_ids": ["ev-call-001", "ev-fact-a-001"],
            "next_action": "discover_capability",
        },
    )

    pr = port.create_agent_pr(sorted(ALLOWED_GENERATED_PATHS), "ignored", "ignored")

    assert pr.number == 42
    create = next(call for call in runner.calls if call[:3] == ["gh", "pr", "create"])
    body = create[create.index("--body") + 1]
    assert "Failed-call evidence ID: ev-call-001" in body
    artifact = json.loads((run_dir / "repo/pr.json").read_text())
    assert artifact["inputs"]["conformance"]["exit_code"] == 0
    assert artifact["inputs"]["failed_call_evidence_id"] == "ev-call-001"


def test_rejects_conflicting_conformance_signals(tmp_path: Path) -> None:
    runner = RecordingRunner()
    port, _, run_dir = _live_port(tmp_path, runner)
    _write_json(run_dir / "tools/conformance_result.json", {"passed": True, "exit_code": 1})

    with pytest.raises(RepoValidationError, match="conformance did not pass"):
        port.create_agent_pr(sorted(ALLOWED_GENERATED_PATHS), "ignored", "ignored")
    assert runner.calls == []


def test_rejects_extra_modified_worktree_file(tmp_path: Path) -> None:
    runner = RecordingRunner(extra_status_path="README.md")
    port, _, _ = _live_port(tmp_path, runner)

    with pytest.raises(RepoValidationError, match=r"extra=\['README.md'\]"):
        port.create_agent_pr(sorted(ALLOWED_GENERATED_PATHS), "ignored", "ignored")


def test_rejects_github_repo_that_does_not_match_origin(tmp_path: Path) -> None:
    runner = RecordingRunner()
    repo_root, run_dir = _fixture(tmp_path)
    port = GitHubRepoPort(
        repo_root=repo_root,
        run_id="demo-001",
        run_dir=run_dir,
        github_repo="different/repository",
        runner=runner,
    )

    with pytest.raises(RepoValidationError, match="does not match remote.origin.url"):
        port.create_agent_pr(sorted(ALLOWED_GENERATED_PATHS), "ignored", "ignored")
    assert not any(call[:2] == ["git", "switch"] for call in runner.calls)


def test_builds_exact_branch_commit_and_pr_metadata_and_parses_gh_json(tmp_path: Path) -> None:
    runner = RecordingRunner()
    port, _, run_dir = _live_port(tmp_path, runner)

    pr = port.create_agent_pr(
        sorted(ALLOWED_GENERATED_PATHS),
        "caller title is deliberately ignored",
        "caller body is deliberately ignored",
    )

    assert pr.number == 42
    assert pr.branch == "agent/fact-b-demo-001"
    assert set(pr.files) == ALLOWED_GENERATED_PATHS

    switch = next(call for call in runner.calls if call[:2] == ["git", "switch"])
    assert switch == ["git", "switch", "-c", "agent/fact-b-demo-001"]
    commit = next(call for call in runner.calls if "commit" in call)
    assert "user.name=PitchLoop Agent" in commit
    assert f"user.email=pitchloop-agent@users.noreply.github.com" in commit
    assert commit[-2:] == ["-m", AGENT_COMMIT_SUBJECT]
    create = next(call for call in runner.calls if call[:3] == ["gh", "pr", "create"])
    assert create[create.index("--title") + 1] == AGENT_PR_TITLE
    body = create[create.index("--body") + 1]
    assert "Run ID: demo-001" in body
    assert "Missing claim: fact_b" in body
    assert "Failed-call evidence ID: evidence-call-001" in body
    assert "zero/search_fact_b.json" in body
    assert "tools/conformance_result.json" in body

    artifact = json.loads((run_dir / "repo/pr.json").read_text())
    assert artifact["gh_view"]["number"] == 42
    assert artifact["metadata"]["commit_subject"] == AGENT_COMMIT_SUBJECT


def test_parses_merge_json_and_writes_artifact(tmp_path: Path) -> None:
    runner = RecordingRunner()
    port, _, run_dir = _live_port(tmp_path, runner)
    pr = port.create_agent_pr(sorted(ALLOWED_GENERATED_PATHS), "ignored", "ignored")

    result = port.merge(pr)

    assert result.merged is True
    assert result.merge_sha == "a" * 40
    artifact = json.loads((run_dir / "repo/merge.json").read_text())
    assert artifact["gh_view"]["state"] == "MERGED"
    assert set(artifact["local_sync_outputs"]) == {
        "fetch",
        "switch",
        "fast_forward",
        "verify_merge",
    }
    assert ["git", "fetch", "origin", "main"] in runner.calls
    assert ["git", "switch", "main"] in runner.calls
    assert ["git", "merge", "--ff-only", "origin/main"] in runner.calls
    assert ["git", "merge-base", "--is-ancestor", "a" * 40, "HEAD"] in runner.calls


def test_fake_create_and_merge_are_deterministic(tmp_path: Path) -> None:
    port = FakeRepoPort(run_id="demo-001", run_dir=tmp_path)

    pr = port.create_agent_pr(sorted(ALLOWED_GENERATED_PATHS), "ignored", "ignored")
    result = port.merge(pr)

    assert pr.number == 101
    assert pr.branch == "agent/fact-b-demo-001"
    assert result.merged is True
    assert result.merge_sha == "f" * 40
    assert (tmp_path / "repo/pr.json").exists()
    assert (tmp_path / "repo/merge.json").exists()
