from transit.feeds.base import TripIndex, _dist_to_polyline_m


def make_index() -> TripIndex:
    # Route 166 at 08:00 has three trips: local (via Blvd East), Turnpike express, and a
    # short-turn that never reaches Manhattan (direction None).
    index = TripIndex.__new__(TripIndex)
    index.routes = {}
    index.trips = {"166|08:00:00": ["r166", "inbound", "166 LOCAL", "local", "t1", "166"]}
    index.alternatives = {"166|08:00:00": [
        ["r166", "inbound", "166T TURNPIKE EXP", "express", "t2", "166"],
        ["r166", None, "166 SHORT TURN", "short", "t3", "166"],
    ]}
    index.shape_samples = {
        "local": [[-74.02, 40.80], [-74.02, 40.76]],    # north-south line at lon -74.02
        "express": [[-74.06, 40.80], [-74.06, 40.76]],  # parallel line 3.4 km west
        "short": [[-74.10, 40.70], [-74.10, 40.66]],    # far south
    }
    return index


def test_pick_chooses_nearest_candidate_shape():
    index = make_index()
    assert index.pick("166|08:00:00", -74.021, 40.78)[2] == "166 LOCAL"
    assert index.pick("166|08:00:00", -74.059, 40.78)[2] == "166T TURNPIKE EXP"


def test_pick_can_choose_a_non_commuter_candidate():
    # The caller skips vehicles whose best candidate has direction None.
    assert make_index().pick("166|08:00:00", -74.10, 40.68)[1] is None


def test_pick_unambiguous_and_unknown_keys():
    index = make_index()
    index.trips["1|07:00:00"] = ["r1", "outbound", "1 X", "s", "t9", "1"]
    assert index.pick("1|07:00:00", 0, 0)[4] == "t9"
    assert index.pick("nope", 0, 0) is None


def test_dist_to_polyline():
    line = [[-74.0, 40.75], [-74.0, 40.76]]
    assert abs(_dist_to_polyline_m(-74.0, 40.755, line)) < 1
    assert 830 < _dist_to_polyline_m(-73.99, 40.755, line) < 860  # 0.01 deg lon ~ 843 m at 40.75 N
