"""Small append-only store used by the Nexla REST destination."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock

from contracts.models import Evidence
from evidence.redact import redact


class EvidenceStore:
    def __init__(self, runs_root: str | Path | None = None) -> None:
        run_dir = Path(os.getenv("PITCHLOOP_RUN_DIR", "runs/demo-001"))
        self.run_dir = run_dir if runs_root is None else None
        self.runs_root = Path(runs_root) if runs_root else run_dir.parent
        self._lock = Lock()
        self._records: dict[str, Evidence] = {}
        self._correlations: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        paths = (
            [self.run_dir / "evidence" / "normalized.jsonl"]
            if self.run_dir
            else self.runs_root.glob("*/evidence/normalized.jsonl")
        )
        for path in paths:
            if not path.exists():
                continue
            for line in path.read_text().splitlines():
                if line.strip():
                    self._remember(Evidence.model_validate_json(line))

    def _remember(self, evidence: Evidence) -> None:
        self._records[evidence.evidence_id] = evidence
        correlation_id = evidence.provenance.get("correlation_id")
        if correlation_id:
            self._correlations[str(correlation_id)] = evidence.evidence_id

    def append(self, evidence: Evidence) -> tuple[Evidence, bool]:
        evidence = Evidence.model_validate(redact(evidence.model_dump(mode="json")))
        with self._lock:
            existing = self._records.get(evidence.evidence_id)
            if existing:
                if existing != evidence:
                    raise ValueError(f"conflicting evidence_id: {evidence.evidence_id}")
                return existing, False

            run_dir = self.run_dir or self.runs_root / evidence.run_id
            path = run_dir / "evidence" / "normalized.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(evidence.model_dump(mode="json"), sort_keys=True))
                stream.write("\n")
            self._remember(evidence)
            return evidence, True

    def get(self, evidence_id: str) -> Evidence | None:
        with self._lock:
            return self._records.get(evidence_id)

    def by_correlation(self, correlation_id: str) -> Evidence | None:
        with self._lock:
            evidence_id = self._correlations.get(correlation_id)
            return self._records.get(evidence_id) if evidence_id else None

    def query(
        self, run_id: str, *, claim: str | None = None, kind: str | None = None
    ) -> list[Evidence]:
        with self._lock:
            records = [
                evidence
                for evidence in self._records.values()
                if evidence.run_id == run_id
                and (claim is None or evidence.claim == claim)
                and (kind is None or evidence.kind == kind)
            ]
        return sorted(
            records,
            key=lambda evidence: (
                evidence.occurred_at.isoformat(),
                evidence.evidence_id,
            ),
        )

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)
