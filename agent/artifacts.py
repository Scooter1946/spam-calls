"""Atomic artifact writer + central redactor for a single PitchLoop run.

Every action writes into one run directory (see the runtime artifact contract,
global context §11). All paths are relative to ``PITCHLOOP_RUN_DIR``. Writes are
atomic (temp file + ``os.replace``), parents are created automatically, and JSON
is stable and human-readable via :mod:`contracts.serde`.

A central redactor masks any dict key containing ``token``, ``secret``,
``password``, ``authorization``, ``wallet``, or ``phone`` (case-insensitive, at
any nesting depth) so structured artifacts never persist secrets.

Usage
-----
The orchestrator and adapters share one injected :class:`Artifacts` instance::

    artifacts = Artifacts(run_dir="runs/demo-001")   # or Artifacts() from env
    artifacts.write_json("policy/deny.json", decision)
    artifacts.append_event({"type": "policy_decision", "allowed": False})

Module-level ``write_json`` / ``write_text`` / ``append_event`` delegate to a
default instance built from the environment, matching the signatures in §2 of
the P1 assignment.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from contracts import serde

# --------------------------------------------------------------------------- #
# Central redaction
# --------------------------------------------------------------------------- #

SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "authorization",
    "wallet",
    "phone",
)
REDACTED = "***REDACTED***"


def _is_sensitive_key(key: Any) -> bool:
    k = str(key).lower()
    return any(part in k for part in SENSITIVE_KEY_PARTS)


def redact(value: Any) -> Any:
    """Recursively mask values under sensitive keys; structure is preserved."""

    if isinstance(value, dict):
        return {
            k: (REDACTED if _is_sensitive_key(k) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def _json_default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _compact_json(value: Any) -> str:
    """Single-line, deterministic JSON for one JSONL event record."""

    return json.dumps(
        value,
        default=_json_default,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


# --------------------------------------------------------------------------- #
# Run-directory resolution
# --------------------------------------------------------------------------- #


def resolve_run_dir(run_dir: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the run directory from an explicit value or the environment.

    Precedence: explicit ``run_dir`` > ``PITCHLOOP_RUN_DIR`` > ``runs/<PITCHLOOP_RUN_ID>``.
    """

    if run_dir is not None:
        return Path(run_dir).resolve()
    env_dir = os.environ.get("PITCHLOOP_RUN_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    run_id = os.environ.get("PITCHLOOP_RUN_ID")
    if run_id:
        return (Path("runs") / run_id).resolve()
    raise RuntimeError(
        "No run directory: pass run_dir= or set PITCHLOOP_RUN_DIR / PITCHLOOP_RUN_ID."
    )


# --------------------------------------------------------------------------- #
# Artifacts
# --------------------------------------------------------------------------- #

EVENTS_FILE = "events.jsonl"


class Artifacts:
    """Atomic, redacting writer rooted at a single run directory."""

    def __init__(self, run_dir: str | os.PathLike[str] | None = None) -> None:
        self.run_dir: Path = resolve_run_dir(run_dir)

    # -- path handling ----------------------------------------------------- #

    def path_for(self, relative_path: str | os.PathLike[str]) -> Path:
        """Resolve ``relative_path`` inside the run dir, rejecting escapes."""

        rel = Path(relative_path)
        if rel.is_absolute():
            raise ValueError(f"artifact path must be relative, got {relative_path!r}")
        dest = (self.run_dir / rel).resolve()
        if dest != self.run_dir and not dest.is_relative_to(self.run_dir):
            raise ValueError(f"artifact path escapes run dir: {relative_path!r}")
        return dest

    # -- writers ----------------------------------------------------------- #

    def write_json(
        self, relative_path: str | os.PathLike[str], value: BaseModel | dict[str, Any]
    ) -> Path:
        """Write a Pydantic model or dict as redacted, stable, pretty JSON."""

        jsonable = serde.to_jsonable(value)
        text = serde.dumps(redact(jsonable))
        return self._atomic_write(relative_path, text)

    def write_text(self, relative_path: str | os.PathLike[str], value: str) -> Path:
        """Write freeform text atomically (e.g. transcripts, raw stdout)."""

        return self._atomic_write(relative_path, value)

    def append_event(self, event: dict[str, Any]) -> None:
        """Append one redacted JSON line to ``events.jsonl`` (stamped with ts)."""

        record: dict[str, Any] = dict(event)
        record.setdefault("ts", datetime.now(timezone.utc).isoformat())
        line = _compact_json(redact(serde.to_jsonable(record)))
        dest = self.path_for(EVENTS_FILE)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    # -- internals --------------------------------------------------------- #

    def _atomic_write(self, relative_path: str | os.PathLike[str], text: str) -> Path:
        dest = self.path_for(relative_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-", suffix=dest.suffix)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                if not text.endswith("\n"):
                    fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return dest


# --------------------------------------------------------------------------- #
# Module-level convenience API (default instance from the environment)
# --------------------------------------------------------------------------- #


def write_json(relative_path: str, value: BaseModel | dict[str, Any]) -> Path:
    return Artifacts().write_json(relative_path, value)


def write_text(relative_path: str, value: str) -> Path:
    return Artifacts().write_text(relative_path, value)


def append_event(event: dict[str, Any]) -> None:
    Artifacts().append_event(event)
