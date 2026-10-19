import time

import httpx
import pytest
from google.transit import gtfs_realtime_pb2

from transit.feeds import njt_bus
from transit.feeds.base import TripIndex
from transit.feeds.njt_bus import NjtBusAdapter, StaleFeedError
from transit.feeds.njt_rail import NjtRailAdapter, parse_njt_time
from transit.njt import FileTokenStore, NjtAuthError, NjtClient


def test_parse_njt_time_is_new_york_local():
    # 6:17:40 PM EDT == 22:17:40 UTC
    assert parse_njt_time("18-Oct-2026 06:17:40 PM") == 1792361860
    assert parse_njt_time("garbage") is None
    assert parse_njt_time(None) is None


def make_rail_adapter() -> NjtRailAdapter:
    adapter = NjtRailAdapter(njt=NjtClient(FileTokenStore()))
    index = TripIndex.__new__(TripIndex)
    index.routes = {}
    # schedule: Ridgewood at 18:25:00 on the service day
    index.trips = {"1726": ["5", "inbound", "HOBOKEN", "461", "1880", "1726", {"131": 18 * 3600 + 25 * 60}]}
    index.stops_by_name = {"NEW YORK PENN STATION": "105", "RAMSEY ROUTE 17 STATION": "128", "RIDGEWOOD": "131"}
    adapter.__dict__["index"] = index
    return adapter


@pytest.mark.parametrize("name,stop_id", [
    ("Ridgewood", "131"),
    ("New York", "105"),        # prefix of the static name
    ("Ramsey Rt 17", "128"),    # RT -> ROUTE
    ("Cherry Hill", None),
])
def test_rail_stop_name_matching(name, stop_id):
    assert make_rail_adapter().stop_id_for(name) == stop_id


def test_rail_normalize():
    now = parse_njt_time("18-Oct-2026 06:18:00 PM")
    rows = [
        {"ID": "1726", "LATITUDE": "40.99", "LONGITUDE": "-74.11", "NEXT_STOP": "Ridgewood",
         "SEC_LATE": "105", "LAST_MODIFIED": "18-Oct-2026 06:17:40 PM"},
        {"ID": "9999", "LATITUDE": "40.5", "LONGITUDE": "-74.5", "LAST_MODIFIED": "18-Oct-2026 06:17:40 PM"},
        {"ID": "1726", "LATITUDE": "", "LONGITUDE": "", "LAST_MODIFIED": "18-Oct-2026 06:17:40 PM"},
    ]
    vehicles, status = make_rail_adapter().normalize(rows, now=now)
    assert len(vehicles) == 1 and status.skipped_trips == 1
    v = vehicles[0]
    assert (v.id, v.number, v.route_id, v.direction, v.shape_id, v.next_stop_id, v.delay_s) == (
        "njt_rail:1726", "1726", "5", "inbound", "461", "131", 105)
    assert v.observed_at == now  # LAST_MODIFIED isn't the position time
    assert v.next_stop_eta == parse_njt_time("18-Oct-2026 06:25:00 PM") + 105  # schedule + SEC_LATE


def test_schedule_to_epoch_handles_service_day_rollover():
    from transit.feeds.njt_rail import schedule_to_epoch
    late_night = parse_njt_time("19-Oct-2026 12:30:00 AM")
    # a 24:45:00 stop time on the 18th's service day is 12:45 AM on the 19th
    assert schedule_to_epoch(24 * 3600 + 45 * 60, late_night) == parse_njt_time("19-Oct-2026 12:45:00 AM")
    assert schedule_to_epoch(23 * 3600 + 50 * 60, late_night) == parse_njt_time("18-Oct-2026 11:50:00 PM")


def test_token_store_roundtrip(tmp_path):
    store = FileTokenStore(tmp_path / "tokens.json")
    assert store.get("rail") == {}
    store.set("rail", {"token": "abc"})
    assert store.get("rail") == {"token": "abc"}
    assert (tmp_path / "tokens.json").stat().st_mode & 0o777 == 0o600


def test_login_cooldown_prevents_repeated_logins(tmp_path, monkeypatch):
    store = FileTokenStore(tmp_path / "tokens.json")
    store.set("rail", {"last_login_attempt": int(time.time())})
    monkeypatch.setenv("NJT_USERNAME", "u")
    monkeypatch.setenv("NJT_PASSWORD", "p")
    with pytest.raises(NjtAuthError, match="cooldown"):
        NjtClient(store).post_sync("rail", "getVehicleData")


def _feed_bytes(ts: int) -> bytes:
    f = gtfs_realtime_pb2.FeedMessage()
    f.header.gtfs_realtime_version = "2.0"
    f.header.timestamp = ts
    return f.SerializeToString()


class FakeNjt:
    def __init__(self, timestamps):
        self.timestamps = list(timestamps)
        self.calls = 0

    async def post(self, client, api, method):
        self.calls += 1
        return httpx.Response(200, content=_feed_bytes(self.timestamps.pop(0)))


async def _download(adapter):
    async with httpx.AsyncClient() as client:
        return await adapter.download(client)


def test_bus_retries_past_stale_snapshots():
    import asyncio
    now = int(time.time())
    fake = FakeNjt([now - 10 * 86400, now - 10 * 86400, now - 5])
    content = asyncio.run(_download(NjtBusAdapter(agency="njt_bus", njt=fake)))
    assert fake.calls == 3
    f = gtfs_realtime_pb2.FeedMessage()
    f.ParseFromString(content)
    assert f.header.timestamp == now - 5


def test_bus_gives_up_when_always_stale():
    import asyncio
    stale = int(time.time()) - 10 * 86400
    fake = FakeNjt([stale] * njt_bus.MAX_ATTEMPTS)
    with pytest.raises(StaleFeedError):
        asyncio.run(_download(NjtBusAdapter(agency="njt_bus", njt=fake)))
    assert fake.calls == njt_bus.MAX_ATTEMPTS
