"""Feed adapters: each one turns an agency's realtime source into normalized Vehicles."""

import json
import time
from dataclasses import dataclass, field
from functools import cached_property
from typing import Protocol

import httpx
from google.transit import gtfs_realtime_pb2

from .. import config
from ..models import FeedStatus, Vehicle

# Positions older than this are treated as vehicles that have left service.
MAX_POSITION_AGE_S = 15 * 60


class FeedAdapter(Protocol):
    agency: str
    cache_s: int  # CDN cache lifetime for this agency's endpoint: matches feed cadence and API budget

    async def fetch(self, client: httpx.AsyncClient) -> tuple[list[Vehicle], FeedStatus]: ...


class TripIndex:
    """Realtime trip key -> [route_id, direction, headsign, shape_id, static trip_id, number,
    (schedule)], built by scripts/build_static.py. number is the public train/route number;
    schedule (optional) is {stop_id: arrival seconds after service-day midnight}."""

    def __init__(self, agency: str):
        data = json.loads((config.BACKEND_DATA_DIR / f"{agency}.json").read_text())
        self.routes: dict[str, dict] = data["routes"]
        self.trips: dict[str, list] = data["trips"]
        self.stops_by_name: dict[str, str] = data.get("stops_by_name", {})

    def get(self, key: str) -> list | None:
        return self.trips.get(key)


@dataclass
class GtfsRtAdapter:
    """Generic adapter for a GTFS-realtime feed that includes VehiclePositions."""

    agency: str
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    cache_s: int = 5

    @cached_property
    def index(self) -> TripIndex:
        return TripIndex(self.agency)

    def index_key(self, trip) -> str:
        """Key into the trip index for a realtime TripDescriptor (see config.Agency.rt_trip_key)."""
        return trip.trip_id

    async def download(self, client: httpx.AsyncClient) -> bytes:
        resp = await client.get(self.url, headers=self.headers)
        resp.raise_for_status()
        return resp.content

    async def fetch(self, client: httpx.AsyncClient) -> tuple[list[Vehicle], FeedStatus]:
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(await self.download(client))
        return self.normalize(feed, now=int(time.time()))

    def normalize(self, feed: gtfs_realtime_pb2.FeedMessage, now: int) -> tuple[list[Vehicle], FeedStatus]:
        # Trip updates by trip_id (LIRR: separate entities). MNR instead puts the vehicle and its
        # trip update in one entity, with a different trip_id on each, so same-entity wins below.
        next_stops = {
            e.trip_update.trip.trip_id: _next_stop(e.trip_update, now)
            for e in feed.entity if e.HasField("trip_update")
        }
        vehicles, skipped = [], 0
        for e in feed.entity:
            if not e.HasField("vehicle") or not e.vehicle.HasField("position"):
                continue
            vp = e.vehicle
            observed_at = vp.timestamp or feed.header.timestamp or now
            if now - observed_at > MAX_POSITION_AGE_S:
                continue
            trip_key = vp.trip.trip_id
            match = self.index.get(self.index_key(vp.trip))
            if match is None:
                skipped += 1
                continue
            route_id, direction, headsign, shape_id, static_trip_id, number = match[:6]
            if e.HasField("trip_update"):
                next_stop_id, next_eta, delay, hold_until = _next_stop(e.trip_update, now)
            else:
                next_stop_id, next_eta, delay, hold_until = next_stops.get(trip_key, (None, None, None, None))
            vehicle_id = vp.vehicle.id or vp.vehicle.label or e.id
            label = vp.vehicle.label or None
            vehicles.append(Vehicle(
                id=f"{self.agency}:{vehicle_id}",
                agency=self.agency,
                route_id=route_id,
                trip_id=static_trip_id,
                number=number,
                vehicle_label=label if label != number else None,
                headsign=headsign,
                lat=vp.position.latitude,
                lon=vp.position.longitude,
                bearing=vp.position.bearing if vp.position.HasField("bearing") else None,
                speed_mps=vp.position.speed if vp.position.HasField("speed") else None,
                direction=direction,
                shape_id=shape_id,
                next_stop_id=next_stop_id or (vp.stop_id or None),
                next_stop_eta=next_eta,
                delay_s=delay,
                hold_until=hold_until,
                observed_at=observed_at,
            ))
        status = FeedStatus(ok=True, vehicles=len(vehicles), skipped_trips=skipped,
                            feed_timestamp=feed.header.timestamp or None)
        return vehicles, status


def _next_stop(tu, now: int) -> tuple[str | None, int | None, int | None, int | None]:
    """First upcoming stop of a TripUpdate.

    Returns (stop_id, eta, delay seconds if the feed provides it, hold_until), where hold_until is
    the departure time of a stop the vehicle is currently dwelling at (arrived, not yet departed).
    """
    hold_until = None
    for stu in tu.stop_time_update:
        arr = stu.arrival.time or stu.departure.time
        dep = stu.departure.time or arr
        if not arr:
            continue
        if arr >= now:
            event = stu.arrival if stu.arrival.time else stu.departure
            delay = event.delay if event.HasField("delay") else None
            return stu.stop_id, arr, delay, hold_until
        if dep >= now:
            hold_until = dep
    return None, None, None, hold_until
