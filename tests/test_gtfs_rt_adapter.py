from google.transit import gtfs_realtime_pb2

from transit.feeds.base import GtfsRtAdapter, TripIndex

NOW = 1_800_000_000


def make_adapter() -> GtfsRtAdapter:
    adapter = GtfsRtAdapter(agency="mnr", url="http://example.invalid")
    index = TripIndex.__new__(TripIndex)
    index.routes = {"1": {"name": "Hudson", "color": "#009B3A"}}
    index.trips = {"1234": ["1", "inbound", "Grand Central", "shape1", "static-trip-1234", "1234"]}
    adapter.__dict__["index"] = index  # bypass loading transit/data/mnr.json
    return adapter


def make_feed() -> gtfs_realtime_pb2.FeedMessage:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = NOW

    def add(entity_id, trip_id, lat=None, ts=NOW):
        e = feed.entity.add(id=entity_id)
        e.vehicle.trip.trip_id = trip_id
        e.vehicle.vehicle.id = entity_id
        e.vehicle.timestamp = ts
        if lat is not None:
            e.vehicle.position.latitude = lat
            e.vehicle.position.longitude = -73.9
        tu = e.trip_update
        tu.trip.trip_id = trip_id
        past = tu.stop_time_update.add(stop_id="9")
        past.arrival.time = NOW - 60
        nxt = tu.stop_time_update.add(stop_id="4")
        nxt.arrival.time = NOW + 120
        nxt.arrival.delay = 180

    add("a", "1234", lat=40.8)                    # running, commuter trip
    add("b", "1234", lat=None)                    # not yet departed: no position
    add("c", "9999", lat=41.0)                    # shuttle not in commuter index
    add("d", "1234", lat=40.9, ts=NOW - 3600)     # stale position
    return feed


def test_normalize_filters_and_enriches():
    vehicles, status = make_adapter().normalize(make_feed(), now=NOW)

    assert [v.id for v in vehicles] == ["mnr:a"]
    v = vehicles[0]
    assert (v.route_id, v.direction, v.headsign, v.trip_id) == ("1", "inbound", "Grand Central", "static-trip-1234")
    assert (v.number, v.vehicle_label) == ("1234", None)  # the test feed sets no vehicle label
    assert (v.next_stop_id, v.next_stop_eta, v.delay_s) == ("4", NOW + 120, 180)
    assert v.hold_until is None
    assert status.ok and status.vehicles == 1 and status.skipped_trips == 1


def test_next_stop_reports_dwell():
    from transit.feeds.base import _next_stop

    tu = gtfs_realtime_pb2.TripUpdate()
    here = tu.stop_time_update.add(stop_id="A")   # arrived 30s ago, departs in 45s
    here.arrival.time = NOW - 30
    here.departure.time = NOW + 45
    nxt = tu.stop_time_update.add(stop_id="B")
    nxt.arrival.time = NOW + 300
    assert _next_stop(tu, NOW) == ("B", NOW + 300, None, NOW + 45)
