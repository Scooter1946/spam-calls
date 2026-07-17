"""Minimal upstream used to prove Pomerium enforced a policy decision.

This service contains no consent logic. A request that reaches
``/authorize-observation`` is intentionally accepted; the deny decision must be
made by Pomerium before the request can reach this process.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request


app = FastAPI(title="PitchLoop policy target", version="1.0.0")


@app.get("/health")
def health() -> dict[str, str]:
    """Return a container health signal without implementing authorization."""

    return {"status": "ok", "service": "pitchloop-policy-target"}


@app.post("/authorize-observation")
async def authorize_observation(request: Request) -> dict[str, Any]:
    """Confirm that Pomerium forwarded an authorized request upstream."""

    request_id = request.headers.get("x-request-id") or str(uuid4())
    return {
        "reached_upstream": True,
        "service": "pitchloop-policy-target",
        "request_id": request_id,
        "observed_at": datetime.now(UTC).isoformat(),
    }
