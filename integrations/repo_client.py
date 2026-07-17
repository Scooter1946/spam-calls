"""Constrained GitHub change-control implementation for generated Fact B code."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from contracts.models import MergeResult, PullRequest


ALLOWED_GENERATED_PATHS = frozenset(
    {
        "generated_tools/fact_b_tool.py",
        "generated_tools/fact_b_tool.manifest.json",
        "generated_tools/test_fact_b_tool.py",
    }
)
AGENT_COMMIT_AUTHOR = "PitchLoop Agent"
AGENT_COMMIT_EMAIL = "pitchloop-agent@users.noreply.github.com"
AGENT_COMMIT_SUBJECT = "agent: add fact-b capability"
AGENT_PR_TITLE = "Agent-authored Fact B capability"
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class RepoValidationError(ValueError):
    """The proposed change is outside the generated-tool security boundary."""


class RepoOperationError(RuntimeError):
    """A finite git or GitHub CLI operation failed."""


def _default_run_dir(run_id: str) -> Path:
    return Path(os.getenv("PITCHLOOP_RUN_DIR", str(Path("runs") / run_id)))


def _model_payload(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return dict(vars(model))


def _artifact_path(artifacts: Any, run_dir: Path, relative_path: str) -> Path:
    if artifacts is None:
        return run_dir / relative_path
    if isinstance(artifacts, (str, os.PathLike)):
        return Path(artifacts) / relative_path
    for attribute in ("root", "run_dir", "base_dir"):
        root = getattr(artifacts, attribute, None)
        if root is not None:
            return Path(root) / relative_path
    return Path(relative_path)


def _write_json_artifact(
    artifacts: Any,
    run_dir: Path,
    relative_path: str,
    payload: dict[str, Any],
) -> str:
    writer = getattr(artifacts, "write_json", None) if artifacts is not None else None
    if callable(writer):
        result = writer(relative_path, payload)
        return str(result) if result is not None else str(
            _artifact_path(artifacts, run_dir, relative_path)
        )

    path = _artifact_path(artifacts, run_dir, relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return str(path)


def _validate_requested_paths(files: Sequence[str], *, require_complete: bool = True) -> list[str]:
    normalized: list[str] = []
    for raw_path in files:
        if not isinstance(raw_path, str) or not raw_path:
            raise RepoValidationError("generated-tool paths must be non-empty strings")
        if "\\" in raw_path:
            raise RepoValidationError(f"backslash path syntax is not permitted: {raw_path!r}")
        path = PurePosixPath(raw_path)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise RepoValidationError(f"absolute or traversal path is not permitted: {raw_path!r}")
        canonical = path.as_posix()
        if canonical not in ALLOWED_GENERATED_PATHS:
            raise RepoValidationError(f"path is outside the generated-tool allowlist: {raw_path!r}")
        normalized.append(canonical)

    if len(set(normalized)) != len(normalized):
        raise RepoValidationError("duplicate generated-tool paths are not permitted")
    if require_complete and set(normalized) != ALLOWED_GENERATED_PATHS:
        missing = sorted(ALLOWED_GENERATED_PATHS.difference(normalized))
        raise RepoValidationError(f"the Fact B change must contain exactly all three files; missing={missing}")
    return sorted(normalized)


def _read_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RepoValidationError(f"missing {description}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RepoValidationError(f"invalid JSON in {description}: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RepoValidationError(f"{description} must contain a JSON object: {path}")
    return payload


def _find_evidence_id(value: Any) -> str | None:
    if isinstance(value, Mapping):
        preferred = (
            "failed_call_evidence_id",
            "call_evidence_id",
            "source_evidence_id",
            "evidence_id",
        )
        for key in preferred:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        # P1's Diagnosis model serializes call evidence first, followed by fact
        # evidence, under ``evidence_ids``. The first non-empty ID is therefore
        # the failed-call evidence that must be cited in the PR body.
        evidence_ids = value.get("evidence_ids")
        if isinstance(evidence_ids, list):
            for candidate in evidence_ids:
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        for nested in value.values():
            found = _find_evidence_id(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_evidence_id(nested)
            if found:
                return found
    return None


def _status_paths(porcelain: str) -> set[str]:
    paths: set[str] = set()
    for line in porcelain.splitlines():
        if not line:
            continue
        if len(line) < 4:
            raise RepoValidationError(f"could not parse git status entry: {line!r}")
        raw_path = line[3:]
        if " -> " in raw_path:
            old_path, new_path = raw_path.split(" -> ", 1)
            paths.update((old_path, new_path))
        else:
            paths.add(raw_path)
    return paths


def _repo_slug(remote: str) -> str:
    value = remote.strip()
    if not value:
        raise RepoValidationError("GITHUB_REPO and remote.origin.url are both empty")

    if value.startswith("git@") and ":" in value:
        host, path = value[4:].split(":", 1)
        value = f"{host}/{path}"
    elif "://" in value:
        parts = urlsplit(value)
        host = parts.hostname or ""
        value = f"{host}/{parts.path.lstrip('/')}"
    value = value.removesuffix(".git").strip("/")

    components = value.split("/")
    if len(components) == 2:
        return value
    if len(components) >= 3:
        return "/".join(components[-3:]) if components[-3] != "github.com" else "/".join(components[-2:])
    raise RepoValidationError(f"could not derive a GitHub repository from {remote!r}")


def _redact_command_text(value: str) -> str:
    redacted = value
    for environment_name in ("GH_TOKEN", "GITHUB_TOKEN"):
        secret = os.getenv(environment_name, "")
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return re.sub(r"(https://)[^/@\s]+@", r"\1[REDACTED]@", redacted)


class FakeRepoPort:
    """Deterministic RepoPort for orchestration tests and offline demos."""

    def __init__(
        self,
        *,
        run_id: str | None = None,
        run_dir: str | os.PathLike[str] | None = None,
        artifacts: Any = None,
    ) -> None:
        self._run_id = run_id or os.getenv("PITCHLOOP_RUN_ID", "demo-001")
        self._run_dir = Path(run_dir) if run_dir is not None else _default_run_dir(self._run_id)
        self._artifacts = artifacts

    def create_agent_pr(self, files: list[str], title: str, body: str) -> PullRequest:
        del title, body
        validated = _validate_requested_paths(files)
        pull_request = PullRequest(
            number=101,
            url="https://github.example/pitchloop/pull/101",
            branch=f"agent/fact-b-{self._run_id}",
            files=validated,
        )
        _write_json_artifact(
            self._artifacts,
            self._run_dir,
            "repo/pr.json",
            {
                "adapter_mode": "fake",
                "metadata": {
                    "commit_author": AGENT_COMMIT_AUTHOR,
                    "commit_subject": AGENT_COMMIT_SUBJECT,
                    "pr_title": AGENT_PR_TITLE,
                },
                "pull_request": _model_payload(pull_request),
            },
        )
        return pull_request

    def merge(self, pr: PullRequest) -> MergeResult:
        result = MergeResult(
            merged=True,
            merge_sha="f" * 40,
            url=pr.url,
        )
        _write_json_artifact(
            self._artifacts,
            self._run_dir,
            "repo/merge.json",
            {
                "adapter_mode": "fake",
                "pull_request_number": pr.number,
                "merge": _model_payload(result),
            },
        )
        return result


class GitHubRepoPort:
    """Create and merge one tightly scoped agent-authored GitHub pull request."""

    def __init__(
        self,
        *,
        repo_root: str | os.PathLike[str] | None = None,
        run_id: str | None = None,
        run_dir: str | os.PathLike[str] | None = None,
        github_repo: str | None = None,
        timeout_seconds: float = 60.0,
        artifacts: Any = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._repo_root = Path(repo_root or Path.cwd()).resolve()
        self._run_id = run_id or os.getenv("PITCHLOOP_RUN_ID", "demo-001")
        if not _RUN_ID_PATTERN.fullmatch(self._run_id):
            raise RepoValidationError(f"unsafe run ID for branch name: {self._run_id!r}")
        self._run_dir = Path(run_dir) if run_dir is not None else _default_run_dir(self._run_id)
        self._github_repo = github_repo if github_repo is not None else os.getenv("GITHUB_REPO", "")
        self._timeout_seconds = timeout_seconds
        self._artifacts = artifacts
        self._runner = runner

    @property
    def branch_name(self) -> str:
        return f"agent/fact-b-{self._run_id}"

    def create_agent_pr(self, files: list[str], title: str, body: str) -> PullRequest:
        del title, body
        git_outputs: dict[str, str] = {}
        try:
            validated = _validate_requested_paths(files)
            conformance_path, conformance = self._require_conformance()
            diagnosis_path, failed_call_evidence_id = self._require_diagnosis()
            no_match_path = self._run_dir / "zero/search_fact_b.json"
            _read_json_object(no_match_path, "Zero Fact B no-match artifact")
            self._validate_physical_files(validated)

            status = self._run(["git", "status", "--porcelain=v1", "--untracked-files=all"])
            changed_paths = _status_paths(status)
            if changed_paths != set(validated):
                extra = sorted(changed_paths.difference(validated))
                missing = sorted(set(validated).difference(changed_paths))
                raise RepoValidationError(
                    f"worktree must contain only the three generated files; extra={extra}, missing={missing}"
                )

            repository = self._repository()
            pr_body = self._build_pr_body(
                failed_call_evidence_id=failed_call_evidence_id,
                diagnosis_path=diagnosis_path,
                no_match_path=no_match_path,
                conformance_path=conformance_path,
            )

            git_outputs["switch"] = self._run(["git", "switch", "-c", self.branch_name])
            git_outputs["add"] = self._run(["git", "add", "--", *validated])
            staged = self._run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRTUXB"])
            staged_paths = {line for line in staged.splitlines() if line}
            if staged_paths != set(validated):
                raise RepoValidationError(
                    f"staged paths differ from the generated-tool allowlist: {sorted(staged_paths)}"
                )

            git_outputs["commit"] = self._run(
                [
                    "git",
                    "-c",
                    f"user.name={AGENT_COMMIT_AUTHOR}",
                    "-c",
                    f"user.email={AGENT_COMMIT_EMAIL}",
                    "commit",
                    "-m",
                    AGENT_COMMIT_SUBJECT,
                ]
            )
            git_outputs["push"] = self._run(
                ["git", "push", "--set-upstream", "origin", self.branch_name]
            )
            gh_create_stdout = self._run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    repository,
                    "--base",
                    "main",
                    "--head",
                    self.branch_name,
                    "--title",
                    AGENT_PR_TITLE,
                    "--body",
                    pr_body,
                ]
            )
            gh_view_text = self._run(
                [
                    "gh",
                    "pr",
                    "view",
                    gh_create_stdout.strip(),
                    "--repo",
                    repository,
                    "--json",
                    "number,url,headRefName,files",
                ]
            )
            gh_view = json.loads(gh_view_text)
            returned_files = sorted(
                item["path"] for item in gh_view.get("files", []) if isinstance(item, dict) and "path" in item
            )
            if set(returned_files) != ALLOWED_GENERATED_PATHS:
                raise RepoValidationError(f"GitHub PR contains unexpected files: {returned_files}")
            if gh_view.get("headRefName") != self.branch_name:
                raise RepoValidationError("GitHub PR head branch does not match the constrained branch")

            pull_request = PullRequest(
                number=int(gh_view["number"]),
                url=str(gh_view["url"]),
                branch=self.branch_name,
                files=returned_files,
            )
            _write_json_artifact(
                self._artifacts,
                self._run_dir,
                "repo/pr.json",
                {
                    "adapter_mode": "live",
                    "repository": repository,
                    "metadata": {
                        "branch": self.branch_name,
                        "commit_author": AGENT_COMMIT_AUTHOR,
                        "commit_email": AGENT_COMMIT_EMAIL,
                        "commit_subject": AGENT_COMMIT_SUBJECT,
                        "pr_title": AGENT_PR_TITLE,
                        "pr_body": pr_body,
                    },
                    "inputs": {
                        "conformance": conformance,
                        "conformance_path": str(conformance_path),
                        "diagnosis_path": str(diagnosis_path),
                        "failed_call_evidence_id": failed_call_evidence_id,
                        "zero_no_match_path": str(no_match_path),
                    },
                    "git_outputs": {key: _redact_command_text(value) for key, value in git_outputs.items()},
                    "gh_create_stdout": _redact_command_text(gh_create_stdout),
                    "gh_view": gh_view,
                    "pull_request": _model_payload(pull_request),
                },
            )
            return pull_request
        except (RepoValidationError, RepoOperationError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            _write_json_artifact(
                self._artifacts,
                self._run_dir,
                "repo/pr.json",
                {
                    "adapter_mode": "live",
                    "status": "failed",
                    "branch": self.branch_name,
                    "error_type": type(exc).__name__,
                    "error": _redact_command_text(str(exc)),
                    "git_outputs": {key: _redact_command_text(value) for key, value in git_outputs.items()},
                },
            )
            raise

    def merge(self, pr: PullRequest) -> MergeResult:
        repository = ""
        merge_stdout = ""
        gh_view: dict[str, Any] = {}
        sync_outputs: dict[str, str] = {}
        try:
            if pr.branch != self.branch_name:
                raise RepoValidationError("refusing to merge a PR from an unconstrained branch")
            if set(pr.files) != ALLOWED_GENERATED_PATHS:
                raise RepoValidationError("refusing to merge a PR with files outside the allowlist")
            repository = self._repository()
            merge_stdout = self._run(
                [
                    "gh",
                    "pr",
                    "merge",
                    str(pr.number),
                    "--repo",
                    repository,
                    "--merge",
                    "--delete-branch",
                ]
            )
            gh_view_text = self._run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr.number),
                    "--repo",
                    repository,
                    "--json",
                    "number,url,state,mergedAt,mergeCommit",
                ]
            )
            gh_view = json.loads(gh_view_text)
            merge_commit = gh_view.get("mergeCommit") or {}
            merge_sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
            merged = gh_view.get("state") == "MERGED" and bool(gh_view.get("mergedAt"))
            if merged:
                if not isinstance(merge_sha, str) or not merge_sha:
                    raise RepoValidationError("merged PR response did not include a merge commit SHA")
                # ``gh pr merge --delete-branch`` deletes the agent branch. Make
                # the merged base branch explicit before ToolRegistry.reload(),
                # and prove that the reported merge commit is in the checkout.
                sync_outputs["fetch"] = self._run(["git", "fetch", "origin", "main"])
                sync_outputs["switch"] = self._run(["git", "switch", "main"])
                sync_outputs["fast_forward"] = self._run(
                    ["git", "merge", "--ff-only", "origin/main"]
                )
                sync_outputs["verify_merge"] = self._run(
                    ["git", "merge-base", "--is-ancestor", merge_sha, "HEAD"]
                )
                self._validate_physical_files(sorted(ALLOWED_GENERATED_PATHS))
            result = MergeResult(
                merged=merged,
                merge_sha=merge_sha,
                url=str(gh_view.get("url") or pr.url),
            )
            _write_json_artifact(
                self._artifacts,
                self._run_dir,
                "repo/merge.json",
                {
                    "adapter_mode": "live",
                    "repository": repository,
                    "merge_stdout": _redact_command_text(merge_stdout),
                    "local_sync_outputs": {
                        key: _redact_command_text(value) for key, value in sync_outputs.items()
                    },
                    "gh_view": gh_view,
                    "merge": _model_payload(result),
                },
            )
            return result
        except (RepoValidationError, RepoOperationError, json.JSONDecodeError, TypeError, ValueError) as exc:
            _write_json_artifact(
                self._artifacts,
                self._run_dir,
                "repo/merge.json",
                {
                    "adapter_mode": "live",
                    "status": "failed",
                    "pull_request_number": pr.number,
                    "repository": repository,
                    "merge_stdout": _redact_command_text(merge_stdout),
                    "local_sync_outputs": {
                        key: _redact_command_text(value) for key, value in sync_outputs.items()
                    },
                    "gh_view": gh_view,
                    "error_type": type(exc).__name__,
                    "error": _redact_command_text(str(exc)),
                },
            )
            raise

    def _repository(self) -> str:
        origin = self._run(["git", "config", "--get", "remote.origin.url"])
        origin_repository = _repo_slug(origin)
        if not self._github_repo:
            return origin_repository
        configured_repository = _repo_slug(self._github_repo)
        if configured_repository != origin_repository:
            raise RepoValidationError(
                "GITHUB_REPO does not match remote.origin.url; refusing to push to an ambiguous target"
            )
        return configured_repository

    def _run(self, command: list[str]) -> str:
        try:
            completed = self._runner(
                command,
                cwd=str(self._repo_root),
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RepoOperationError(f"command failed to start or timed out: {command[:3]}: {exc}") from exc
        if completed.returncode != 0:
            stderr = _redact_command_text(completed.stderr.strip())
            raise RepoOperationError(
                f"command returned {completed.returncode}: {command[:3]}: {stderr or 'no stderr'}"
            )
        return completed.stdout.strip()

    def _require_conformance(self) -> tuple[Path, dict[str, Any]]:
        path = self._run_dir / "tools/conformance_result.json"
        payload = _read_json_object(path, "generated-tool conformance result")
        signals: list[bool] = []
        if "passed" in payload:
            passed = payload["passed"]
            if not isinstance(passed, bool):
                raise RepoValidationError(f"conformance passed field must be boolean: {path}")
            signals.append(passed)
        if "exit_code" in payload:
            exit_code = payload["exit_code"]
            if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                raise RepoValidationError(f"conformance exit_code field must be an integer: {path}")
            signals.append(exit_code == 0)
        if not signals or not all(signals):
            raise RepoValidationError(f"generated-tool conformance did not pass: {path}")
        return path, payload

    def _require_diagnosis(self) -> tuple[Path, str]:
        path = self._run_dir / "evidence/diagnosis.json"
        payload = _read_json_object(path, "failed-call diagnosis")
        evidence_id = _find_evidence_id(payload)
        if not evidence_id:
            raise RepoValidationError(f"diagnosis does not identify failed-call evidence: {path}")
        missing_claims = payload.get("missing_claims")
        missing_claim = payload.get("missing_claim")
        if missing_claims is not None and "fact_b" not in missing_claims:
            raise RepoValidationError("diagnosis does not identify fact_b as missing")
        if missing_claim is not None and missing_claim != "fact_b":
            raise RepoValidationError("diagnosis missing_claim is not fact_b")
        return path, evidence_id

    def _validate_physical_files(self, files: Iterable[str]) -> None:
        root = self._repo_root.resolve(strict=True)
        for relative_path in files:
            candidate = root.joinpath(*PurePosixPath(relative_path).parts)
            try:
                resolved = candidate.resolve(strict=True)
            except FileNotFoundError as exc:
                raise RepoValidationError(f"generated-tool file does not exist: {relative_path}") from exc
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise RepoValidationError(f"symlink escapes repository: {relative_path}") from exc
            if not resolved.is_file():
                raise RepoValidationError(f"generated-tool path is not a regular file: {relative_path}")

    def _build_pr_body(
        self,
        *,
        failed_call_evidence_id: str,
        diagnosis_path: Path,
        no_match_path: Path,
        conformance_path: Path,
    ) -> str:
        return "\n".join(
            (
                "PitchLoop autonomous capability change.",
                "",
                f"Run ID: {self._run_id}",
                "Missing claim: fact_b",
                f"Failed-call evidence ID: {failed_call_evidence_id}",
                f"Diagnosis artifact: {diagnosis_path}",
                f"Zero no-match artifact: {no_match_path}",
                f"Conformance result: {conformance_path}",
            )
        )


def build_repo_port(*, mode: str | None = None, artifacts: Any = None):
    """Build the fake or live RepoPort selected by configuration."""

    selected = (mode or os.getenv("REPO_MODE", "fake")).strip().casefold()
    if selected == "fake":
        return FakeRepoPort(artifacts=artifacts)
    if selected == "live":
        return GitHubRepoPort(artifacts=artifacts)
    raise ValueError(f"unsupported REPO_MODE: {selected!r}")
