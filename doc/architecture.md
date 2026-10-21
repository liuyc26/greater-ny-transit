# Architecture

## Overview

```
                     daily build (GitHub Actions, planned)
 agency static GTFS ─────────► scripts/build_static.py ──► public/data/*.geojson, shapes.json   (map)
 + Manhattan boundary                                   └► transit/data/<agency>.json         (trip index)

 Browser ──poll per agency──► CDN cache ──miss──► FastAPI function  GET /api/vehicles/{agency}
   ▲         LIRR, MNR, NJT rail 10s               one adapter fetches its agency feed,
   │                                               matches trips via the index,
   │         NJT bus 15s                           returns normalized Vehicles (JSON)
   │
   └─ animates every vehicle along its track every frame (public/track.js)
```

The design is **stateless and serverless** so it can run free:

- No long-running server, queue, or database. Each request fetches one agency's feed, normalizes it and
  returns it. The CDN cache (`Cache-Control: s-maxage=<adapter.cache_s>`) means each feed is hit at most
  once per cache period, however many people are watching.
- **One endpoint per agency** so each feed gets its own cache lifetime (rate-limited NJ Transit is
  polled least) and fails independently. A failed feed shows a ⚠ in the panel; the others keep working.
- **No WebSockets and no 1-second polling.** Feeds don't change that fast (measured: LIRR publishes every
  ~3s but positions are often 2–4 min old; Metro-North every ~11s; NJT bus GPS every few seconds).
  Motion comes from client-side animation instead.
- **Heavy work happens at build time.** Agency GTFS files are large (NJT bus `stop_times.txt` is 237 MB).
  The build reduces them to ~1 MB of map files plus small per-agency trip indexes the API loads.

## Backend (`transit/`)

| File | Role |
|---|---|
| `app.py` | FastAPI app: `/api/agencies`, `/api/vehicles/{agency}`, `/api/health`. Sets cache headers; reports feed errors in-band. |
| `dev.py` | Local dev server: the API plus `public/` as static files. |
| `config.py` | Agency definitions (URLs, how realtime trips map to static trips, filters), Manhattan hubs, paths. |
| `models.py` | `Vehicle`, `FeedStatus`, `VehiclesResponse` (pydantic). |
| `filtering.py` | Trip direction classification; stop-name normalization. |
| `njt.py` | NJ Transit login, token store and authenticated calls (sync and async). |
| `feeds/` | Feed adapters and the registry (`feeds/__init__.py`). |
| `data/` | Generated trip indexes (committed; shipped with the function). |

### Adapter pattern

Every source has an adapter with `agency`, `cache_s` and `async fetch(client) -> (vehicles, status)`.
All adapters return the same `Vehicle` model, so the API and frontend never know which agency a vehicle
came from.

- `GtfsRtAdapter` (`feeds/base.py`) handles any standard GTFS-realtime feed (LIRR, Metro-North).
  Subclasses override `download()` (how to fetch) and `index_key()` (how a realtime trip maps to the index).
- `NjtBusAdapter` (`feeds/njt_bus.py`): GTFS-RT fetched by authenticated POST, with retries for NJT's
  stale snapshots and route+start-time trip matching.
- `NjtRailAdapter` (`feeds/njt_rail.py`): NJT's own JSON format; computes arrival times from the schedule
  plus reported lateness.

### NJ Transit snapshot (`transit/snapshot.py`)

NJ Transit has daily request limits, so visitors never trigger NJT calls directly. Rail and bus are fetched
together into one `SharedSnapshot`, at most once every 30 s for the whole site. Each request is answered
from this instance's memory if fresh, else from the shared copy in Upstash Redis (another instance may have
refreshed it), else this request refreshes it, one refresh at a time per instance. NJT traffic is then
~1 fetch per 30 s regardless of visitors, CDN locations or instances. `SnapshotAdapter` serves each agency
out of the snapshot. MTA feeds have no stated limit and use the CDN cache only.

### Trip index

`transit/data/<agency>.json` maps the identifier the realtime feed uses to what the map needs:

```
"trips": { "<realtime key>": [route_id, direction, headsign, shape_id, static trip_id, number, (schedule)] }
```

`number` is the public train or route number. `schedule` (NJT rail only) is `{stop_id: seconds after
service-day midnight}`.

Some keys are ambiguous: an NJT bus route + start time can match a local and an express variant, or a
trip that never reaches Manhattan. For those, the index also stores `alternatives` (one candidate per
distinct shape; direction `null` = not a Manhattan trip) and `shape_samples` (40-point outlines).
`TripIndex.pick` chooses the candidate whose shape is nearest the vehicle, and vehicles whose best
candidate isn't a Manhattan trip are skipped. The realtime key differs per agency; see [data-sources.md](data-sources.md).
Vehicles whose trips aren't in the index (they never reach Manhattan) are skipped and counted in
`FeedStatus.skipped_trips`.

## "Commutes to Manhattan" filter

`scripts/build_static.py` marks a stop as **core** if it's inside the Manhattan borough polygon
(NYC Open Data, buffered 150 m so shoreline piers count) or within 300 m of a transfer hub
(Hoboken, Long Island City, Weehawken, Exchange Place, Newport; Atlantic Terminal is an off-by-default flag).
Hubs count only for rail (`Agency.use_hubs`): trains to Hoboken connect to PATH and ferries, but many local
buses pass those hubs without going to Manhattan, so buses must stop in Manhattan itself.

A trip qualifies if it serves any core stop. Its direction (`filtering.classify_direction`):
`inbound` ends at a core stop, `outbound` starts at one, `through` otherwise.

## Build (`scripts/`)

`build_static.py` for each agency: downloads static GTFS (cached 20 h in `data/raw/`), applies the
filter, and writes:

- `public/data/routes.geojson`: route lines for drawing (identical shapes deduplicated).
- `public/data/stops.geojson`: stations (bus stops omitted; there are thousands).
- `public/data/shapes.json`: per-trip track geometry used for animation, as distinct geometries plus an
  id → geometry map.
- `transit/data/<agency>.json`: the trip index.

`road_fill.py` fixes sparse shapes, enabled for NJT bus. The NJT API's bus shapes skipped highway stretches
(one segment from the Lincoln Tunnel to Union, 17 km); the public zip now used doesn't, so it currently finds
no gaps but stays as a safeguard. Gaps over 1.5 km are filled with the driving route from the public OSRM
server, accepted only if its length is within 20% of the shape's own `shape_dist_traveled`. Results are
cached in `data/road_fill_cache.json` (commit it) so builds only query new gaps.

## Frontend (`public/`)

No build step: plain ES modules, with MapLibre GL and deck.gl from unpkg and the OpenFreeMap dark basemap.

- `app.js`: loads map data, polls each agency at its own interval, renders layers (routes, stations,
  vehicles, number labels at zoom ≥ 11, hover highlight), and the panel (counts, direction filter,
  per-agency toggles, feed health). It also limits the map to the service area: the minimum zoom is the one
  where every route fits on screen, recomputed on resize.
- `track.js`: the motion model (below).
- Every 5 minutes it checks `data/meta.json` and reloads the map data if it was rebuilt, since shape ids in
  API responses always refer to the current build.
- Hovering a vehicle highlights its trip's shape, or nothing if the vehicle isn't on that shape (the
  feed's trip is then probably wrong).
- Light/dark theme toggle: follows the system setting until the user picks one (remembered in
  `localStorage`). Each theme has its own OpenFreeMap basemap (`dark` / `positron`) and map-layer colors
  (`THEMES` in `app.js`); panel colors are CSS variables in `style.css`.
- `window.__transit` and `window.__map` are exposed for debugging and browser tests.

### Motion model (`track.js`)

Raw positions are coarse, so they're never drawn directly:

1. Each report is snapped onto the trip's shape (`Track.snap`), preferring the part of the shape near the
   vehicle's last position so out-and-back routes don't snap to the wrong side.
2. It becomes a **plan**: from distance `d0` at time `t0`, reach the next stop at its ETA (MTA feeds; NJT
   rail via schedule + lateness), or, with no ETA, continue at the speed measured between reports for at
   most 60 s and never past the next stop. A vehicle dwelling at a station holds until its departure time.
3. Every frame the dot eases toward where the plan says it should be (time constant 1.5 s). It never moves
   backwards for small corrections and only jumps when more than 4 km off.

Vehicles whose report is more than 500 m from their shape (or that have no shape) fall back to easing
between raw GPS positions (`FreeMotion`).
