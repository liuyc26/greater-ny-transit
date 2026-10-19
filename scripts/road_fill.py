"""Fill sparse gaps in GTFS shapes with road geometry from OSRM.

Some agencies (NJT bus express routes) publish shapes with only the endpoints of long highway
stretches, e.g. Lincoln Tunnel -> Union as one 17 km straight segment. For each gap longer than
GAP_M we ask OSRM for the driving route and splice it in, but only if its length agrees with the
shape's own shape_dist_traveled (when present), so a router detour never replaces real data.

Results (including rejections) are cached in data/road_fill_cache.json; commit it so routine
builds only query new gaps. Uses the public OSRM demo server: max 1 request/second, light use.
"""

import json
import math
import time

import httpx

from transit import config

OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{a};{b}?overview=full&geometries=geojson"
CACHE_PATH = config.ROOT / "data" / "road_fill_cache.json"
GAP_M = 1500
MAX_LENGTH_ERROR = 0.2   # routed length must be within 20% of the shape's own traveled distance
MAX_DETOUR = 1.6         # without traveled distance: routed length <= 1.6x straight-line distance
REQUEST_INTERVAL_S = 1.1

_M_PER_DEG_LAT = 111_320.0
_M_PER_DEG_LON = _M_PER_DEG_LAT * math.cos(math.radians(40.75))


def dist_m(a, b) -> float:
    return math.hypot((a[0] - b[0]) * _M_PER_DEG_LON, (a[1] - b[1]) * _M_PER_DEG_LAT)


class RoadFiller:
    def __init__(self):
        self.cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
        self.client = httpx.Client(timeout=30, headers={"User-Agent": "greater-ny-transit/0.1 (static build)"})
        self.last_request = 0.0
        self.stats = {"gaps": 0, "filled": 0, "rejected": 0, "failed": 0, "requests": 0}

    def save(self):
        CACHE_PATH.write_text(json.dumps(self.cache, separators=(",", ":"), sort_keys=True))

    def _route(self, a, b) -> tuple[list, float] | None:
        key = f"{a[0]:.5f},{a[1]:.5f};{b[0]:.5f},{b[1]:.5f}"
        if key not in self.cache:
            wait = REQUEST_INTERVAL_S - (time.time() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.time()
            self.stats["requests"] += 1
            try:
                resp = self.client.get(OSRM_URL.format(a=key.split(";")[0], b=key.split(";")[1]))
                resp.raise_for_status()
                route = resp.json()["routes"][0]
                coords = [[round(x, 5), round(y, 5)] for x, y in route["geometry"]["coordinates"]]
                self.cache[key] = {"coords": coords, "length_m": round(route["distance"])}
            except (httpx.HTTPError, KeyError, IndexError, ValueError):
                self.stats["failed"] += 1
                return None  # not cached: retry on the next build
        hit = self.cache[key]
        return hit["coords"], hit["length_m"]

    def fill(self, pts: list[tuple[float, float, float | None]], dist_to_m: float | None) -> list[tuple[float, float]]:
        """pts: [(lon, lat, shape_dist_traveled or None)] in sequence order. Returns [(lon, lat)]."""
        out = [pts[0][:2]]
        for (x0, y0, d0), (x1, y1, d1) in zip(pts, pts[1:]):
            straight = dist_m((x0, y0), (x1, y1))
            if straight > GAP_M:
                self.stats["gaps"] += 1
                routed = self._route((x0, y0), (x1, y1))
                if routed:
                    coords, length = routed
                    if d0 is not None and d1 is not None and dist_to_m:
                        expected = (d1 - d0) * dist_to_m
                        ok = expected > 0 and abs(length - expected) / expected <= MAX_LENGTH_ERROR
                    else:
                        ok = length <= straight * MAX_DETOUR
                    if ok:
                        self.stats["filled"] += 1
                        out.extend(tuple(c) for c in coords[1:-1])
                    else:
                        self.stats["rejected"] += 1
            out.append((x1, y1))
        return out


def shape_dist_unit(pts: list[tuple[float, float, float | None]]) -> float | None:
    """Meters per shape_dist_traveled unit, estimated from short segments where road ~ straight line.
    (Agencies use miles, km, feet or meters without saying which.)"""
    straight = traveled = 0.0
    for (x0, y0, d0), (x1, y1, d1) in zip(pts, pts[1:]):
        if d0 is None or d1 is None or d1 <= d0:
            continue
        s = dist_m((x0, y0), (x1, y1))
        if s < 500:
            straight += s
            traveled += d1 - d0
    return straight / traveled if traveled > 0 and straight > 1000 else None
