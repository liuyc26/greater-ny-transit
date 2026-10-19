"""NJ Transit Bus: standard GTFS-RT, but fetched by authenticated POST."""

import time
from dataclasses import dataclass, field

import httpx
from google.transit import gtfs_realtime_pb2

from ..njt import NjtClient
from .base import GtfsRtAdapter

# The API intermittently serves a snapshot that's days old (about half of calls, seen 2026-10-18),
# probably from a stale server behind its load balancer. Retry until the header is fresh, on a
# new connection each time (a kept-alive connection tends to stick to the stale server).
MAX_ATTEMPTS = 4
MAX_HEADER_AGE_S = 300


class StaleFeedError(RuntimeError):
    pass


@dataclass
class NjtBusAdapter(GtfsRtAdapter):
    njt: NjtClient = field(default_factory=NjtClient)
    cache_s: int = 15  # NJT allows ~40k bus calls/day; retries make this ~1.9 calls per poll

    def index_key(self, trip) -> str:
        # Realtime trip_ids don't match the static feed; route number + start time does.
        return f"{trip.route_id}|{trip.start_time}"

    async def download(self, client: httpx.AsyncClient) -> bytes:
        # Vehicle positions only. Trip updates are ~900 KB per call and carry delays but no
        # times; they're for phase 2 (delays).
        for _ in range(MAX_ATTEMPTS):
            async with httpx.AsyncClient(timeout=client.timeout) as fresh_client:
                content = (await self.njt.post(fresh_client, "bus", "getVehiclePositions")).content
            header = gtfs_realtime_pb2.FeedMessage()
            header.ParseFromString(content)
            if time.time() - header.header.timestamp < MAX_HEADER_AGE_S:
                return content
        raise StaleFeedError(f"NJT bus feed stale after {MAX_ATTEMPTS} attempts")
