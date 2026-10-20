"""Build step: download static GTFS, keep only Manhattan-commute trips, and write

  public/data/routes.geojson   route lines for the map
  public/data/stops.geojson    stations served by those routes
  public/data/shapes.json      per-shape track geometry, for moving vehicles along it
  public/data/meta.json        build info
  transit/data/<agency>.json   realtime trip index used by the API

Usage: python scripts/build_static.py [--refresh] [agency ...]
"""

import argparse
import csv
import datetime
import io
import json
import math
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform
from shapely.prepared import prep

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transit import config  # noqa: E402
from transit.filtering import classify_direction, normalize_stop_name  # noqa: E402
from transit.njt import NjtClient  # noqa: E402

from road_fill import RoadFiller, shape_dist_unit  # noqa: E402  (scripts/ is on sys.path when run directly)

NY = ZoneInfo("America/New_York")
MAX_ZIP_AGE_S = 20 * 3600
SHAPE_SIMPLIFY_M = 8
SAMPLE_POINTS = 40  # points per coarse shape outline used to disambiguate candidate trips
COORD_DIGITS = 5

# Local equirectangular projection around NYC: good to well under 1% over the metro area.
_LAT0 = 40.75
_M_PER_DEG_LAT = 111_320.0
_M_PER_DEG_LON = _M_PER_DEG_LAT * math.cos(math.radians(_LAT0))


def to_m(lon, lat, z=None):
    return (lon * _M_PER_DEG_LON, lat * _M_PER_DEG_LAT)


def to_deg(x, y, z=None):
    return (x / _M_PER_DEG_LON, y / _M_PER_DEG_LAT)


def download(url: str, dest: Path, refresh: bool) -> Path:
    if dest.exists() and not refresh and time.time() - dest.stat().st_mtime < MAX_ZIP_AGE_S:
        return dest
    print(f"  downloading {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    if url == "njt:bus":
        tmp.write_bytes(NjtClient().post_sync("bus", "getGTFS").content)
    else:
        with httpx.stream("GET", url, follow_redirects=True, timeout=300) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
    if tmp.read_bytes()[:2] != b"PK":
        raise RuntimeError(f"{url} did not return a zip file")
    tmp.replace(dest)
    return dest


def load_manhattan(refresh: bool):
    """Manhattan polygon (projected to meters), buffered so shoreline piers count."""
    path = config.MANHATTAN_GEOJSON
    if refresh or not path.exists():
        print("  downloading borough boundaries")
        boroughs = httpx.get(config.BOROUGH_BOUNDARIES_URL, timeout=60).raise_for_status().json()
        feat = next(f for f in boroughs["features"] if f["properties"]["boroname"] == "Manhattan")
        geom = transform(to_deg, transform(to_m, shape(feat["geometry"])).simplify(5))
        path.write_text(json.dumps({"type": "Feature", "properties": {"name": "Manhattan"},
                                    "geometry": geom.__geo_interface__}))
    geom = shape(json.loads(path.read_text())["geometry"])
    return prep(transform(to_m, geom).buffer(config.MANHATTAN_BUFFER_M))


def hub_points():
    hubs = list(config.HUBS)
    if config.INCLUDE_ATLANTIC_TERMINAL:
        hubs.append(config.ATLANTIC_TERMINAL)
    return [Point(to_m(lon, lat)) for _, lat, lon in hubs]


def read_csv(zf: zipfile.ZipFile, name: str):
    if name not in zf.namelist():
        return
    with zf.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))


def read_cols(zf: zipfile.ZipFile, name: str, *cols: str, optional: tuple[str, ...] = ()):
    """Fast path for big files: yield tuples of just the requested columns. Columns in `optional`
    may be absent from the file and yield None."""
    with zf.open(name) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
        header = [h.strip() for h in next(reader)]
        idx = [header.index(c) if c in header or c not in optional else None for c in cols]
        for row in reader:
            if row:
                yield tuple(row[i] if i is not None and i < len(row) else None for i in idx)


def active_service_ids(zf: zipfile.ZipFile, dates: list[datetime.date]) -> set[str]:
    """service_ids running on any of the given dates (calendar.txt + calendar_dates.txt)."""
    active = set()
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    ymd = {d.strftime("%Y%m%d"): d for d in dates}
    for c in read_csv(zf, "calendar.txt") or []:
        for s, d in ymd.items():
            if c["start_date"] <= s <= c["end_date"] and c[weekdays[d.weekday()]] == "1":
                active.add(c["service_id"])
    removed = set()
    for c in read_csv(zf, "calendar_dates.txt") or []:
        if c["date"] in ymd:
            (active if c["exception_type"] == "1" else removed).add(c["service_id"])
    return active - (removed - active)


def build_agency(agency: config.Agency, manhattan, hubs, refresh: bool):
    zip_path = download(agency.static_url, config.RAW_DIR / f"{agency.id}.zip", refresh)
    zf = zipfile.ZipFile(zip_path)
    today = datetime.datetime.now(NY).date()
    if agency.current_service_only and not active_service_ids(zf, [today]):
        # The NJT bus schedule only covers dates through the day it was published, so a cached
        # copy from yesterday has no service today. Fetch a fresh one; never write an empty index.
        if not refresh:
            print("  cached schedule has no service today; re-downloading")
            zf = zipfile.ZipFile(download(agency.static_url, zip_path, refresh=True))
        if not active_service_ids(zf, [today]):
            raise RuntimeError(f"{agency.id}: schedule has no service on {today}; keeping the previous index")

    stops = {}
    for s in read_csv(zf, "stops.txt"):
        lat, lon = float(s["stop_lat"]), float(s["stop_lon"])
        p = Point(to_m(lon, lat))
        core = manhattan.contains(p) or (agency.use_hubs and any(p.distance(h) <= config.HUB_RADIUS_M for h in hubs))
        stops[s["stop_id"]] = {"name": s["stop_name"], "lat": lat, "lon": lon, "core": core}

    routes = {r["route_id"]: r for r in read_csv(zf, "routes.txt")}
    if agency.route_types is not None:
        routes = {rid: r for rid, r in routes.items() if r.get("route_type") in agency.route_types}
    trips = {t["trip_id"]: t for t in read_csv(zf, "trips.txt") if t["route_id"] in routes}
    route_start = agency.rt_trip_key == config.ROUTE_START_KEY
    if agency.current_service_only:
        active = active_service_ids(zf, [today, today + datetime.timedelta(days=1)])
        trips = {tid: t for tid, t in trips.items() if t["service_id"] in active}

    # Pass 1: per trip, sequence range overall and over core stops (+ first departure time).
    agg = {}  # trip_id -> [first, last, min_core, max_core, first_departure]
    for trip_id, stop_id, seq_s, dep in read_cols(zf, "stop_times.txt", "trip_id", "stop_id", "stop_sequence",
                                                   "departure_time"):
        if trip_id not in trips:
            continue
        seq = int(seq_s)
        a = agg.get(trip_id)
        if a is None:
            a = agg[trip_id] = [seq, seq, None, None, dep]
        elif seq < a[0]:
            a[0] = seq
            a[4] = dep
        elif seq > a[1]:
            a[1] = seq
        stop = stops.get(stop_id)
        if stop and stop["core"]:
            a[2] = seq if a[2] is None else min(a[2], seq)
            a[3] = seq if a[3] is None else max(a[3], seq)

    qualifying = {}
    for trip_id, (first, last, min_core, max_core, _) in agg.items():
        direction = classify_direction(first, last, min_core, max_core)
        if direction:
            qualifying[trip_id] = direction

    # Pass 2: stops served by qualifying trips (+ their schedules, if indexed).
    served = set()
    schedules = defaultdict(list)  # trip_id -> [(seq, stop_id, arrival seconds after service-day midnight)]
    for trip_id, stop_id, seq, arr in read_cols(zf, "stop_times.txt", "trip_id", "stop_id", "stop_sequence",
                                                "arrival_time"):
        if trip_id in qualifying:
            served.add(stop_id)
            if agency.index_schedule and arr:
                h, m, s = (int(x) for x in arr.split(":"))
                schedules[trip_id].append((int(seq), stop_id, h * 3600 + m * 60 + s))

    # Realtime trip index, keyed by whatever identifier the realtime feed uses.
    def rt_key(trip_id):
        t = trips[trip_id]
        if route_start:
            return f"{routes[t['route_id']].get('route_short_name')}|{agg[trip_id][4]}"
        return t.get(agency.rt_trip_key) or trip_id

    def entry_for(trip_id):
        t = trips[trip_id]
        number = t.get(agency.number_field) or routes[t["route_id"]].get(agency.number_field) or None
        # direction None marks a candidate that doesn't reach Manhattan: the API skips such vehicles.
        entry = [t["route_id"], qualifying.get(trip_id), t.get("trip_headsign") or None,
                 t.get("shape_id") or None, trip_id, number]
        if agency.index_schedule:
            entry.append({stop_id: secs for _, stop_id, secs in sorted(schedules[trip_id])})
        return entry

    by_key = defaultdict(list)  # realtime key -> all trips with it, commuter or not
    for trip_id in agg:
        by_key[rt_key(trip_id)].append(trip_id)
    # A key can match several trips (NJT bus: same route and start time, different variants). Keep one
    # trip per distinct shape as alternatives; the API picks the one whose shape is nearest the vehicle.
    index, alternatives = {}, {}
    for key, tids in by_key.items():
        primary = next((t for t in tids if t in qualifying), None)
        if primary is None:
            continue
        index[key] = entry_for(primary)
        per_shape = {trips[primary].get("shape_id"): primary}
        for t in tids:
            per_shape.setdefault(trips[t].get("shape_id"), t)
        if len(per_shape) > 1:
            alternatives[key] = [entry_for(t) for t in per_shape.values() if t != primary]
    conflicts = len(alternatives)
    sample_shapes = {e[3] for key in alternatives for e in [index[key], *alternatives[key]] if e[3]}

    route_info = {}
    for rid in {trips[t]["route_id"] for t in qualifying}:
        r = routes.get(rid, {})
        name = r.get("route_long_name") or r.get("route_short_name") or rid
        if r.get("route_short_name") and r.get("route_long_name") and agency.id == "njt_bus":
            name = f"{r['route_short_name']} {r['route_long_name']}"
        color = r.get("route_color") or ""
        route_info[rid] = {
            "name": name,
            # Agencies often publish black/white placeholders; let the frontend use the agency color.
            "color": "#" + color if color and color.upper() not in ("000000", "FFFFFF") else None,
        }

    # Route geometry: distinct shapes used by qualifying trips, grouped by route.
    shape_routes = defaultdict(set)
    for trip_id in qualifying:
        if sid := trips[trip_id].get("shape_id"):
            shape_routes[sid].add(trips[trip_id]["route_id"])
    points = defaultdict(list)
    for sid, lat, lon, seq, dist in read_cols(zf, "shapes.txt", "shape_id", "shape_pt_lat", "shape_pt_lon",
                                              "shape_pt_sequence", "shape_dist_traveled",
                                              optional=("shape_dist_traveled",)):
        if sid in shape_routes or sid in sample_shapes:
            points[sid].append((int(seq), float(lon), float(lat), float(dist) if dist else None))
    for pts in points.values():
        pts.sort()
    # Coarse outlines of ambiguous candidates' shapes, for the API to pick the nearest candidate.
    shape_samples = {}
    for sid in sample_shapes:
        pts = points.get(sid, [])
        step = max(1, len(pts) // SAMPLE_POINTS)
        picked = pts[::step] + ([pts[-1]] if pts and (len(pts) - 1) % step else [])
        shape_samples[sid] = [[round(x, 4), round(y, 4)] for _, x, y, _ in picked]
    route_lines = defaultdict(dict)  # route_id -> {geometry key: coords}, deduplicating repeated shapes
    shape_coords = {}  # "agency:shape_id" -> coords, for moving vehicles along the track
    filler = RoadFiller() if agency.road_fill else None
    for sid, pts in points.items():
        if sid not in shape_routes or len(pts) < 2:
            continue
        xy = [(x, y, d) for _, x, y, d in pts]
        raw = filler.fill(xy, shape_dist_unit(xy)) if filler else [(x, y) for x, y, _ in xy]
        line = transform(to_deg, transform(to_m, LineString(raw)).simplify(SHAPE_SIMPLIFY_M))
        coords = [[round(x, COORD_DIGITS), round(y, COORD_DIGITS)] for x, y in line.coords]
        shape_coords[f"{agency.id}:{sid}"] = coords
        for rid in shape_routes[sid]:
            route_lines[rid][json.dumps(coords)] = coords
    missing_shapes = sum(1 for t in qualifying if not trips[t].get("shape_id"))
    if filler:
        filler.save()
        print(f"  road fill: {json.dumps(filler.stats)}")

    route_features = [
        {"type": "Feature",
         "properties": {"agency": agency.id, "route_id": rid, **route_info[rid]},
         "geometry": {"type": "MultiLineString", "coordinates": list(lines.values())}}
        for rid, lines in sorted(route_lines.items())
    ]
    stop_features = [] if not agency.publish_stops else [
        {"type": "Feature",
         "properties": {"agency": agency.id, "stop_id": sid, "name": stops[sid]["name"], "core": stops[sid]["core"]},
         "geometry": {"type": "Point", "coordinates": [round(stops[sid]["lon"], COORD_DIGITS),
                                                       round(stops[sid]["lat"], COORD_DIGITS)]}}
        for sid in sorted(served) if sid in stops
    ]

    backend_index = {"routes": route_info, "trips": index}
    if alternatives:
        backend_index["alternatives"] = alternatives
        backend_index["shape_samples"] = shape_samples
    if agency.index_stop_names:
        backend_index["stops_by_name"] = {normalize_stop_name(stops[sid]["name"]): sid
                                          for sid in sorted(served) if sid in stops}
    config.BACKEND_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.BACKEND_DATA_DIR / f"{agency.id}.json").write_text(json.dumps(backend_index, separators=(",", ":")))

    dirs = defaultdict(int)
    for d in qualifying.values():
        dirs[d] += 1
    stats = {"name": agency.name, "trips_total": len(trips), "trips_qualifying": len(qualifying),
             "directions": dict(dirs), "routes": len(route_info), "stops": len(served),
             "shapes": len(shape_coords), "distinct_shapes": len({json.dumps(c) for c in shape_coords.values()}),
             "ambiguous_keys": conflicts, "trips_missing_shape": missing_shapes}
    print(f"  {json.dumps(stats)}")
    return route_features, stop_features, shape_coords, stats


def main():
    load_dotenv(config.ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("agencies", nargs="*", default=list(config.AGENCIES))
    parser.add_argument("--refresh", action="store_true", help="re-download even if cached")
    args = parser.parse_args()

    manhattan = load_manhattan(args.refresh)
    hubs = hub_points()

    # Start from the existing public data so building a subset of agencies (or an agency failing)
    # leaves the others' data in place.
    routes, stops, shapes, meta = load_public_data()
    failed = []
    for agency_id in args.agencies:
        print(f"[{agency_id}]")
        t0 = time.time()
        try:
            r, s, sh, stats = build_agency(config.AGENCIES[agency_id], manhattan, hubs, args.refresh)
        except Exception as e:  # keep building the others; this agency keeps its previous data
            print(f"  FAILED: {type(e).__name__}: {e}")
            failed.append(agency_id)
            continue
        print(f"  {time.time() - t0:.1f}s")
        routes = [f for f in routes if f["properties"]["agency"] != agency_id] + r
        stops = [f for f in stops if f["properties"]["agency"] != agency_id] + s
        shapes = {k: v for k, v in shapes.items() if not k.startswith(f"{agency_id}:")} | sh
        meta["agencies"][agency_id] = {**stats, "built_at": int(time.time())}
    meta["built_at"] = int(time.time())

    write_public_data(routes, stops, shapes, meta)
    if failed:
        sys.exit(f"build failed for: {', '.join(failed)} (their previous data was kept)")
    print("done")


def load_public_data():
    """Existing routes/stops features, shapes ("agency:shape_id" -> coords) and meta, or empty."""
    d = config.PUBLIC_DATA_DIR
    try:
        routes = json.loads((d / "routes.geojson").read_text())["features"]
        stops = json.loads((d / "stops.geojson").read_text())["features"]
        sh = json.loads((d / "shapes.json").read_text())
        shapes = {key: sh["geoms"][gi] for key, gi in sh["ids"].items()}
        meta = json.loads((d / "meta.json").read_text())
        return routes, stops, shapes, meta
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return [], [], {}, {"agencies": {}}


def write_public_data(routes, stops, shapes, meta):
    config.PUBLIC_DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name, feats in (("routes", routes), ("stops", stops)):
        (config.PUBLIC_DATA_DIR / f"{name}.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")))
    # shapes.json: distinct geometries once, plus "agency:shape_id" -> geometry index.
    geoms, geom_index, ids = [], {}, {}
    for key, coords in shapes.items():
        k = json.dumps(coords)
        if k not in geom_index:
            geom_index[k] = len(geoms)
            geoms.append(coords)
        ids[key] = geom_index[k]
    (config.PUBLIC_DATA_DIR / "shapes.json").write_text(
        json.dumps({"geoms": geoms, "ids": ids}, separators=(",", ":")))
    (config.PUBLIC_DATA_DIR / "meta.json").write_text(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
