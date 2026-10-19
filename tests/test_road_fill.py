import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from road_fill import RoadFiller, dist_m, shape_dist_unit  # noqa: E402

MILE = 1609.34
A, B = (-74.0421, 40.77644), (-74.22069, 40.69817)  # Lincoln Tunnel exit -> Union: ~17.4 km straight


def make_filler(route_length_m):
    f = RoadFiller.__new__(RoadFiller)
    f.stats = {"gaps": 0, "filled": 0, "rejected": 0, "failed": 0, "requests": 0}
    f._route = lambda a, b: ([list(a), [-74.1, 40.75], [-74.15, 40.72], list(b)], route_length_m)
    return f


def test_fill_splices_road_when_length_matches_traveled_distance():
    pts = [(*A, 4.107), (*B, 16.68)]  # shape says 12.57 mi = 20.2 km actually traveled
    out = make_filler(20_200).fill(pts, MILE)
    assert out == [A, (-74.1, 40.75), (-74.15, 40.72), B]


def test_fill_keeps_original_when_router_disagrees():
    pts = [(*A, 4.107), (*B, 16.68)]
    f = make_filler(35_000)  # router took a big detour
    assert f.fill(pts, MILE) == [A, B]
    assert f.stats["rejected"] == 1


def test_fill_without_traveled_distance_uses_detour_limit():
    pts = [(*A, None), (*B, None)]
    assert len(make_filler(20_000).fill(pts, None)) == 4      # 20 km vs 17.4 km straight: ok
    assert make_filler(40_000).fill(pts, None) == [A, B]      # > 1.6x straight: rejected


def test_short_segments_untouched():
    pts = [(-74.0, 40.75, 0.0), (-74.001, 40.751, 0.1)]
    f = make_filler(1)
    assert f.fill(pts, MILE) == [(-74.0, 40.75), (-74.001, 40.751)]
    assert f.stats["gaps"] == 0


def test_shape_dist_unit_detects_miles():
    # 20 points 100 m apart along a meridian, traveled distance in miles
    step_deg = 100 / 111_320
    pts = [(-74.0, 40.7 + i * step_deg, i * 100 / MILE) for i in range(20)]
    assert abs(shape_dist_unit(pts) - MILE) / MILE < 0.01
    assert abs(dist_m(pts[0][:2], pts[1][:2]) - 100) < 0.5
