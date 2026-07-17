"""Public fictional small-business website fixture used by generated tools.

Run locally with::

    uvicorn fixtures.public_signal_server:app --host 127.0.0.1 --port 8088
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query


_DATA_PATH = Path(__file__).with_name("public_company_data.json")


@lru_cache(maxsize=1)
def _load_fixture() -> dict[str, Any]:
    """Load and minimally validate the committed fictional public dataset."""

    with _DATA_PATH.open(encoding="utf-8") as handle:
        data = json.load(handle)
    businesses = data.get("small_businesses")
    if not isinstance(businesses, dict) or not businesses:
        raise RuntimeError("fixture is missing small_businesses")
    required = {"claim", "statement", "source"}
    for candidate_id, business in businesses.items():
        signal = business.get("website_opportunity", {}) if isinstance(business, dict) else {}
        if not required.issubset(signal):
            missing = sorted(required.difference(signal))
            raise RuntimeError(f"fixture for {candidate_id!r} is missing required fields: {missing}")
    return data


app = FastAPI(
    title="PitchLoop Website Opportunity Fixture",
    description="Fictional public small-business website data; contains no private personal data.",
    version="1.0.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    _load_fixture()
    return {"status": "ok"}


@app.get("/businesses/website-opportunity")
def website_opportunity(
    candidate_id: str = Query(..., min_length=1),
) -> dict[str, str]:
    signal = (
        _load_fixture()["small_businesses"]
        .get(candidate_id, {})
        .get("website_opportunity")
    )
    if not signal:
        raise HTTPException(status_code=404, detail="website opportunity not found")
    return {
        "candidate_id": candidate_id,
        "claim": signal["claim"],
        "statement": signal["statement"],
        "source": signal["source"],
    }
