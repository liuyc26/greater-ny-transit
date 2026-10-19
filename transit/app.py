"""Stateless API: each request fetches one agency's feed and returns normalized vehicles.

Endpoints are per agency so each gets its own CDN cache lifetime (adapter.cache_s): feeds hit
at most once per cache period regardless of how many people are viewing, which keeps rate-
limited APIs (NJ Transit) within budget.
"""

import json
import time

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.gzip import GZipMiddleware

from . import config
from .feeds import ADAPTERS
from .models import FeedStatus, VehiclesResponse

load_dotenv(config.ROOT / ".env")

FEED_TIMEOUT_S = 8
ADAPTERS_BY_ID = {a.agency: a for a in ADAPTERS}

app = FastAPI(title="Greater NY Live Transit")
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.get("/api/agencies")
async def agencies(response: Response) -> list[dict]:
    response.headers["Cache-Control"] = "public, s-maxage=3600"
    return [{"id": a.agency, "name": config.AGENCIES[a.agency].name, "poll_s": a.cache_s} for a in ADAPTERS]


@app.get("/api/vehicles/{agency}", response_model=VehiclesResponse, response_model_exclude_none=True)
async def vehicles(agency: str, response: Response) -> VehiclesResponse:
    adapter = ADAPTERS_BY_ID.get(agency)
    if adapter is None:
        raise HTTPException(404, f"unknown agency {agency!r}")
    try:
        async with httpx.AsyncClient(timeout=FEED_TIMEOUT_S, follow_redirects=True) as client:
            vs, status = await adapter.fetch(client)
        cache_s = adapter.cache_s
    except Exception as e:  # report feed failures in-band; the map keeps showing other agencies
        vs, status = [], FeedStatus(ok=False, error=f"{type(e).__name__}: {e}")
        cache_s = min(adapter.cache_s, 5)
    response.headers["Cache-Control"] = f"public, s-maxage={cache_s}, stale-while-revalidate={cache_s * 2}"
    return VehiclesResponse(generated_at=int(time.time()), vehicles=vs, feeds={agency: status})


@app.get("/api/health")
async def health() -> dict:
    meta_path = config.PUBLIC_DATA_DIR / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else None
    return {"ok": True, "agencies": list(ADAPTERS_BY_ID), "static_built_at": meta and meta["built_at"]}
