"""NJ Transit Bus: standard GTFS-RT, but fetched by authenticated POST."""

import time
from dataclasses import dataclass, field

import httpx
from google.transit import gtfs_realtime_pb2

from ..njt import NjtClient
from .base import GtfsRtAdapter

# Realtime feed endpoints, tried in order (same login token for both):
# - GTFSG2: what we use. Fresh data, no stale snapshots seen (checked 2026-10-21).
# - GTFS: the original. About half its responses were a days-old snapshot, and on 2026-10-21 it
#   returned 404 "Feed file not found" all afternoon. Kept as a fallback.
FEED_BASES = (
    "https://pcsdata.njtransit.com/api/GTFSG2/",
    "https://pcsdata.njtransit.com/api/GTFS/",
)
# Stale snapshots: retry on a new connection each time (a kept-alive connection tends to stick to
# the stale server).
MAX_ATTEMPTS = 4
MAX_HEADER_AGE_S = 300


class StaleFeedError(RuntimeError):
    pass


@dataclass
class NjtBusAdapter(GtfsRtAdapter):
    njt: NjtClient = field(default_factory=NjtClient)
    cache_s: int = 15

    def index_key(self, trip) -> str:
        # Realtime trip_ids don't match the static feed; route number + start time does.
        return f"{trip.route_id}|{trip.start_time}"

    async def download(self, client: httpx.AsyncClient) -> bytes:
        # Vehicle positions only. Trip updates are large (~2.5 MB on GTFSG2) and are for phase 2 (delays).
        errors = []
        for base in FEED_BASES:
            try:
                return await self._download_fresh(client, base)
            except (httpx.HTTPStatusError, StaleFeedError) as e:
                errors.append(f"{base.rsplit('/', 2)[-2]}: {type(e).__name__}: {str(e).splitlines()[0]}")
        raise StaleFeedError("no usable NJT bus feed; " + " | ".join(errors))

    async def _download_fresh(self, client: httpx.AsyncClient, base: str) -> bytes:
        for _ in range(MAX_ATTEMPTS):
            async with httpx.AsyncClient(timeout=client.timeout) as fresh_client:
                content = (await self.njt.post(fresh_client, "bus", "getVehiclePositions", base=base)).content
            header = gtfs_realtime_pb2.FeedMessage()
            header.ParseFromString(content)
            if time.time() - header.header.timestamp < MAX_HEADER_AGE_S:
                return content
        raise StaleFeedError(f"stale after {MAX_ATTEMPTS} attempts")
