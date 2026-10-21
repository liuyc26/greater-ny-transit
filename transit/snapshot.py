"""Shared, rate-limited snapshots of rate-limited feeds (NJ Transit).

Visitors never cause upstream calls directly. A group of adapters is fetched together at most once
per `ttl_s` for the whole site; every request is answered from the snapshot:

  1. this instance's memory, if fresh;
  2. else the shared snapshot in Upstash Redis, if another instance refreshed it recently;
  3. else this request refreshes it (one at a time per instance) and stores it for everyone.

So upstream traffic is ~1 fetch per ttl_s regardless of visitors, CDN locations or instances (a
few instances may occasionally race; that's bounded by the instance count, not traffic). The
snapshot is stored gzipped to stay within Upstash's free-tier bandwidth.
"""

import asyncio
import base64
import gzip
import json
import time
from dataclasses import dataclass, field

import httpx

from .kv import UpstashKV
from .models import FeedStatus, Vehicle


@dataclass
class SharedSnapshot:
    key: str
    adapters: list  # FeedAdapter instances fetched together
    ttl_s: int
    kv: UpstashKV | None = None
    _memory: tuple[float, dict] | None = field(default=None, init=False, repr=False)
    _locks: dict = field(default_factory=dict, init=False, repr=False)  # event loop -> asyncio.Lock
    stats: dict = field(default_factory=lambda: {"upstream_refreshes": 0, "kv_hits": 0}, init=False)

    def status(self) -> dict:
        """For /api/health: this instance's view of the snapshot."""
        age = round(time.time() - self._memory[0]) if self._memory else None
        return {"ttl_s": self.ttl_s, "shared_store": self.kv is not None, "age_s": age, **self.stats}

    def _fresh(self, fetched_at: float) -> bool:
        return time.time() - fetched_at < self.ttl_s

    def _lock(self) -> asyncio.Lock:
        # An asyncio.Lock belongs to the loop it's first used on; keep one per loop.
        loop = asyncio.get_running_loop()
        if loop not in self._locks:
            self._locks.clear()
            self._locks[loop] = asyncio.Lock()
        return self._locks[loop]

    async def get(self, client: httpx.AsyncClient) -> dict:
        """{agency: {"vehicles": [vehicle dicts], "status": status dict}}"""
        if self._memory and self._fresh(self._memory[0]):
            return self._memory[1]
        async with self._lock():  # concurrent requests in this instance share one refresh
            if self._memory and self._fresh(self._memory[0]):
                return self._memory[1]
            shared = await self._read_kv(client)
            if shared and self._fresh(shared["fetched_at"]):
                self.stats["kv_hits"] += 1
                self._memory = (shared["fetched_at"], shared["feeds"])
                return shared["feeds"]
            fetched_at, feeds = time.time(), await self._fetch_upstream(client)
            self.stats["upstream_refreshes"] += 1
            self._memory = (fetched_at, feeds)
            await self._write_kv(client, {"fetched_at": fetched_at, "feeds": feeds})
            return feeds

    async def _fetch_upstream(self, client: httpx.AsyncClient) -> dict:
        results = await asyncio.gather(*(a.fetch(client) for a in self.adapters), return_exceptions=True)
        feeds = {}
        for adapter, result in zip(self.adapters, results):
            if isinstance(result, BaseException):
                status = FeedStatus(ok=False, error=f"{type(result).__name__}: {result}")
                feeds[adapter.agency] = {"vehicles": [], "status": status.model_dump()}
            else:
                vehicles, status = result
                feeds[adapter.agency] = {"vehicles": [v.model_dump(exclude_none=True) for v in vehicles],
                                         "status": status.model_dump()}
        return feeds

    async def _read_kv(self, client: httpx.AsyncClient) -> dict | None:
        if not self.kv:
            return None
        try:
            raw = await self.kv.get(client, self.key)
            return json.loads(gzip.decompress(base64.b64decode(raw))) if raw else None
        except Exception as e:  # Redis down or corrupt: fall back to fetching
            print(f"snapshot {self.key}: read failed: {type(e).__name__}: {e}")
            return None

    async def _write_kv(self, client: httpx.AsyncClient, snapshot: dict) -> None:
        if not self.kv:
            return
        try:
            blob = base64.b64encode(gzip.compress(json.dumps(snapshot, separators=(",", ":")).encode())).decode()
            await self.kv.set(client, self.key, blob, ex_s=3600)
        except Exception as e:
            print(f"snapshot {self.key}: write failed: {type(e).__name__}: {e}")


@dataclass
class SnapshotAdapter:
    """A FeedAdapter that serves one agency out of a SharedSnapshot."""

    snapshot: SharedSnapshot
    agency: str
    cache_s: int = 10

    async def fetch(self, client: httpx.AsyncClient) -> tuple[list[Vehicle], FeedStatus]:
        feed = (await self.snapshot.get(client))[self.agency]
        return [Vehicle.model_construct(**v) for v in feed["vehicles"]], FeedStatus(**feed["status"])
