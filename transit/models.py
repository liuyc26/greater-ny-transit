from typing import Literal

from pydantic import BaseModel

Direction = Literal["inbound", "outbound", "through", "unknown"]


class Vehicle(BaseModel):
    """One vehicle, normalized from any agency feed."""

    id: str                         # "{agency}:{vehicle_id}"
    agency: str
    route_id: str | None = None
    trip_id: str | None = None
    number: str | None = None        # public train number (rail) or route number (bus)
    vehicle_label: str | None = None  # fleet number (bus) or lead car (LIRR), when it differs from `number`
    headsign: str | None = None
    lat: float
    lon: float
    bearing: float | None = None
    speed_mps: float | None = None
    direction: Direction = "unknown"
    shape_id: str | None = None
    dist_along_shape_m: float | None = None
    next_stop_id: str | None = None
    next_stop_eta: int | None = None
    hold_until: int | None = None   # dwelling at a station until this time (epoch s)
    delay_s: int | None = None
    position_source: Literal["live", "estimated"] = "live"
    observed_at: int                # epoch seconds of the source data


class FeedStatus(BaseModel):
    ok: bool
    vehicles: int = 0
    skipped_trips: int = 0          # realtime trips not in the Manhattan-commute index (feeder shuttles, unknown trips)
    feed_timestamp: int | None = None
    error: str | None = None


class VehiclesResponse(BaseModel):
    generated_at: int
    vehicles: list[Vehicle]
    feeds: dict[str, FeedStatus]
