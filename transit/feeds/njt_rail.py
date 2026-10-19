"""NJ Transit Rail: RailData API getVehicleData (JSON, not GTFS-RT).

Each row is one train: ID (train number = static block_id), LATITUDE/LONGITUDE, NEXT_STOP
(station name), SEC_LATE, LAST_MODIFIED (local time, e.g. "18-Oct-2026 06:17:40 PM").
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cached_property
from zoneinfo import ZoneInfo

import httpx

from ..filtering import normalize_stop_name
from ..models import FeedStatus, Vehicle
from ..njt import NjtClient
from .base import MAX_POSITION_AGE_S, TripIndex

NY = ZoneInfo("America/New_York")


def parse_njt_time(s: str | None) -> int | None:
    try:
        return int(datetime.strptime(s, "%d-%b-%Y %I:%M:%S %p").replace(tzinfo=NY).timestamp())
    except (TypeError, ValueError):
        return None


def schedule_to_epoch(secs: int, now: int) -> int:
    """Seconds after a service day's midnight (may exceed 24h) -> epoch, picking the service day
    that puts the time closest to now."""
    midnight = datetime.fromtimestamp(now, NY).replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = [int((midnight + timedelta(days=d)).timestamp()) + secs for d in (-1, 0, 1)]
    return min(candidates, key=lambda t: abs(t - now))


def _float(s) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


@dataclass
class NjtRailAdapter:
    agency: str = "njt_rail"
    njt: NjtClient = field(default_factory=NjtClient)
    cache_s: int = 10

    @cached_property
    def index(self) -> TripIndex:
        return TripIndex(self.agency)

    @cached_property
    def _stop_names(self) -> list[str]:
        return sorted(self.index.stops_by_name)

    def stop_id_for(self, name: str) -> str | None:
        """Match a RailData station name ("New York", "Ramsey Rt 17") to a static stop_id
        ("NEW YORK PENN STATION", "RAMSEY ROUTE 17 STATION"): exact, else unique-ish prefix."""
        key = " ".join("ROUTE" if w == "RT" else w for w in normalize_stop_name(name).split())
        if key in self.index.stops_by_name:
            return self.index.stops_by_name[key]
        prefixed = next((n for n in self._stop_names if n.startswith(key + " ")), None)
        return self.index.stops_by_name[prefixed] if prefixed else None

    async def fetch(self, client: httpx.AsyncClient) -> tuple[list[Vehicle], FeedStatus]:
        resp = await self.njt.post(client, "rail", "getVehicleData")
        return self.normalize(resp.json(), now=int(time.time()))

    def normalize(self, rows: list[dict], now: int) -> tuple[list[Vehicle], FeedStatus]:
        vehicles, skipped = [], 0
        for r in rows:
            lat, lon = _float(r.get("LATITUDE")), _float(r.get("LONGITUDE"))
            if not lat or not lon:
                continue
            # LAST_MODIFIED isn't the position time (positions jump km while it stays the same),
            # so it's only used to drop dead records; positions are stamped with fetch time.
            modified = parse_njt_time(r.get("LAST_MODIFIED")) or now
            if now - modified > MAX_POSITION_AGE_S:
                continue
            train = str(r.get("ID", "")).strip()
            match = self.index.get(train)
            if match is None:
                skipped += 1
                continue
            route_id, direction, headsign, shape_id, static_trip_id = match[:5]
            schedule = match[6] if len(match) > 6 else {}
            next_stop = r.get("NEXT_STOP")
            next_stop_id = self.stop_id_for(next_stop) if next_stop else None
            sec_late = r.get("SEC_LATE")
            delay = int(sec_late) if str(sec_late).lstrip("-").isdigit() else None
            eta = None
            if next_stop_id in schedule:
                eta = schedule_to_epoch(schedule[next_stop_id], now) + (delay or 0)
            vehicles.append(Vehicle(
                id=f"{self.agency}:{train}",
                agency=self.agency,
                route_id=route_id,
                trip_id=static_trip_id,
                number=train,
                headsign=headsign,
                lat=lat,
                lon=lon,
                direction=direction,
                shape_id=shape_id,
                next_stop_id=next_stop_id,
                next_stop_eta=eta,
                delay_s=delay,
                observed_at=now,
            ))
        return vehicles, FeedStatus(ok=True, vehicles=len(vehicles), skipped_trips=skipped)
