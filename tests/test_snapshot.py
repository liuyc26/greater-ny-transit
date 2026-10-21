import asyncio

import httpx

from transit import snapshot as snap_mod
from transit.models import FeedStatus, Vehicle
from transit.snapshot import SharedSnapshot, SnapshotAdapter


class FakeAdapter:
    def __init__(self, agency, fail=False):
        self.agency, self.fail, self.calls = agency, fail, 0

    async def fetch(self, client):
        self.calls += 1
        await asyncio.sleep(0.01)
        if self.fail:
            raise RuntimeError("upstream down")
        v = Vehicle(id=f"{self.agency}:1", agency=self.agency, lat=40.7, lon=-74.0, observed_at=1)
        return [v], FeedStatus(ok=True, vehicles=1)


class FakeKV:
    def __init__(self, broken=False):
        self.data, self.broken, self.ops = {}, broken, []

    async def get(self, client, key):
        self.ops.append("GET")
        if self.broken:
            raise httpx.ConnectError("redis down")
        return self.data.get(key)

    async def set(self, client, key, value, ex_s=None):
        self.ops.append("SET")
        if self.broken:
            raise httpx.ConnectError("redis down")
        self.data[key] = value


def run(coro):
    return asyncio.run(coro)


async def _fetch(adapter):
    async with httpx.AsyncClient() as client:
        return await adapter.fetch(client)


def test_visitors_share_one_upstream_fetch_per_ttl(monkeypatch):
    rail, bus = FakeAdapter("njt_rail"), FakeAdapter("njt_bus")
    snap = SharedSnapshot("k", [rail, bus], ttl_s=30, kv=FakeKV())
    now = [1000.0]
    monkeypatch.setattr(snap_mod.time, "time", lambda: now[0])

    async def many_visitors():
        async with httpx.AsyncClient() as client:
            adapters = [SnapshotAdapter(snap, a) for a in ("njt_rail", "njt_bus") * 25]
            return await asyncio.gather(*(a.fetch(client) for a in adapters))

    results = run(many_visitors())  # 50 concurrent requests
    assert (rail.calls, bus.calls) == (1, 1)
    assert all(st.ok and len(vs) == 1 for vs, st in results)
    now[0] += 29
    run(_fetch(SnapshotAdapter(snap, "njt_bus")))
    assert bus.calls == 1  # still within the TTL
    now[0] += 2
    run(_fetch(SnapshotAdapter(snap, "njt_bus")))
    assert bus.calls == 2  # expired: one refresh


def test_instance_uses_snapshot_another_instance_stored(monkeypatch):
    kv = FakeKV()
    a_rail = FakeAdapter("njt_rail")
    instance_a = SharedSnapshot("k", [a_rail], ttl_s=30, kv=kv)
    run(_fetch(SnapshotAdapter(instance_a, "njt_rail")))
    b_rail = FakeAdapter("njt_rail")
    instance_b = SharedSnapshot("k", [b_rail], ttl_s=30, kv=kv)
    vs, st = run(_fetch(SnapshotAdapter(instance_b, "njt_rail")))
    assert b_rail.calls == 0 and st.ok and vs[0].id == "njt_rail:1"
    assert instance_b.stats["kv_hits"] == 1


def test_upstream_failure_is_reported_not_raised():
    snap = SharedSnapshot("k", [FakeAdapter("njt_rail", fail=True)], ttl_s=30, kv=FakeKV())
    vs, st = run(_fetch(SnapshotAdapter(snap, "njt_rail")))
    assert vs == [] and not st.ok and "upstream down" in st.error


def test_redis_outage_falls_back_to_memory_only():
    rail = FakeAdapter("njt_rail")
    snap = SharedSnapshot("k", [rail], ttl_s=30, kv=FakeKV(broken=True))
    vs, st = run(_fetch(SnapshotAdapter(snap, "njt_rail")))
    assert st.ok and rail.calls == 1
    run(_fetch(SnapshotAdapter(snap, "njt_rail")))
    assert rail.calls == 1  # served from memory
