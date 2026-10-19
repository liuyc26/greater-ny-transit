"""Agency definitions and filter settings shared by the build script and the API."""

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"              # downloaded GTFS zips (gitignored)
MANHATTAN_GEOJSON = ROOT / "data" / "manhattan.geojson"
BACKEND_DATA_DIR = ROOT / "transit" / "data"  # trip indexes bundled with the API
PUBLIC_DATA_DIR = ROOT / "public" / "data"    # route/stop GeoJSON served to the browser

BOROUGH_BOUNDARIES_URL = "https://data.cityofnewyork.us/resource/gthc-hcne.geojson"

# Stops within this distance of the Manhattan shoreline count as Manhattan (ferry piers sit on it).
MANHATTAN_BUFFER_M = 150

# Direct-transfer terminals outside Manhattan that still count as "commuting to Manhattan".
# (name, lat, lon); a stop within HUB_RADIUS_M of one of these qualifies.
HUB_RADIUS_M = 300
HUBS = [
    ("Hoboken Terminal", 40.7350, -74.0275),
    ("Long Island City", 40.7413, -73.9586),
    ("Weehawken Port Imperial", 40.7769, -74.0107),
    ("Exchange Place", 40.7163, -74.0329),
    ("Newport", 40.7268, -74.0340),
]
INCLUDE_ATLANTIC_TERMINAL = False
ATLANTIC_TERMINAL = ("Atlantic Terminal", 40.6845, -73.9770)


ROUTE_START_KEY = "route_start"


@dataclass(frozen=True)
class Agency:
    id: str
    name: str
    # Static GTFS source: an http(s) URL, or "njt:bus" for the authenticated NJT bus API.
    static_url: str
    realtime_url: str | None
    # Which static trips.txt column the realtime feed uses as its trip identifier, or
    # ROUTE_START_KEY: "<route_short_name>|<first departure HH:MM:SS>", for feeds whose trip_ids
    # don't match the static feed (NJT bus); use with current_service_only.
    rt_trip_key: str = "trip_id"
    # Keep only these GTFS route_types (e.g. {"2"} = rail, dropping light rail). None = all.
    route_types: frozenset[str] | None = None
    # Also index stops by name (for feeds that report the next stop by name).
    index_stop_names: bool = False
    # Static field holding the public train/route number: a trips.txt or routes.txt column.
    number_field: str = "trip_short_name"
    # Include stops in public/data/stops.geojson (off for buses: thousands of curbside stops).
    publish_stops: bool = True
    # Index only trips running today/tomorrow (needed when realtime keys repeat across days).
    current_service_only: bool = False
    # Ship each trip's stop schedule in the index, for feeds that give delays but no ETAs.
    index_schedule: bool = False
    # Fill long straight gaps in shapes with OSRM road geometry (sparse express-bus shapes).
    road_fill: bool = False


AGENCIES = {
    "lirr": Agency(
        id="lirr",
        name="Long Island Rail Road",
        static_url="https://rrgtfsfeeds.s3.amazonaws.com/gtfslirr.zip",
        realtime_url="https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/lirr%2Fgtfs-lirr",
    ),
    "mnr": Agency(
        id="mnr",
        name="Metro-North Railroad",
        static_url="https://rrgtfsfeeds.s3.amazonaws.com/gtfsmnr.zip",
        realtime_url="https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/mnr%2Fgtfs-mnr",
        # MNR realtime trip_id is the train number, i.e. static trip_short_name.
        rt_trip_key="trip_short_name",
    ),
    "njt_rail": Agency(
        id="njt_rail",
        name="NJ Transit Rail",
        static_url="https://www.njtransit.com/rail_data.zip",
        realtime_url=None,  # RailData API getVehicleData (JSON), see transit/feeds/njt_rail.py
        # RailData reports trains by train number, which is static block_id.
        rt_trip_key="block_id",
        number_field="block_id",
        route_types=frozenset({"2"}),  # the zip also has HBLR / Newark LR / RiverLINE
        index_stop_names=True,
        current_service_only=True,  # train numbers repeat across weekday/weekend schedules
        index_schedule=True,        # RailData gives SEC_LATE but no ETAs
    ),
    "njt_bus": Agency(
        id="njt_bus",
        name="NJ Transit Bus",
        # From the API: its route numbers and service match the realtime feed. (Neither zip's
        # trip_ids match realtime trip_ids, so trips are matched by route + start time.)
        static_url="njt:bus",
        realtime_url=None,  # GTFS-RT via authenticated POST, see transit/feeds/njt_bus.py
        rt_trip_key=ROUTE_START_KEY,
        number_field="route_short_name",
        current_service_only=True,
        publish_stops=False,
        road_fill=True,  # express shapes skip highway stretches (e.g. Lincoln Tunnel -> Union in one segment)
    ),
}
