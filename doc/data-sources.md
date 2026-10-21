# Data sources

All findings verified against live feeds on 2026-10-18.

## Feeds in use

| Agency | Static schedule | Realtime | Account? | Realtime → static trip match |
|---|---|---|---|---|
| LIRR | `https://rrgtfsfeeds.s3.amazonaws.com/gtfslirr.zip` | GTFS-RT `https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/lirr%2Fgtfs-lirr` | no | `trip_id` = static `trip_id` |
| Metro-North | `https://rrgtfsfeeds.s3.amazonaws.com/gtfsmnr.zip` | GTFS-RT `https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/mnr%2Fgtfs-mnr` | no | `trip_id` = static `trip_short_name` (train number) |
| NJ Transit Rail | `https://www.njtransit.com/rail_data.zip` (public) | `POST https://raildata.njtransit.com/api/TrainData/getVehicleData` (JSON) | yes | `ID` = static `block_id` (train number) |
| NJ Transit Bus | `https://www.njtransit.com/bus_data.zip` (public) | GTFS-RT `POST https://pcsdata.njtransit.com/api/GTFS/getVehiclePositions` | yes | `route_id` + `start_time` = static `route_short_name` + first departure |

Also used at build time: Manhattan boundary from NYC Open Data
(`https://data.cityofnewyork.us/resource/gthc-hcne.geojson`, cached in `data/manhattan.geojson`), and
road geometry from the OSRM public server (`router.project-osrm.org`, cached in `data/road_fill_cache.json`).

## Per-agency quirks

### LIRR
- Publishes every ~3 s, but a typical train's position is 2–4 minutes old and only a few trains move per
  update. This is why the frontend animates along the track instead of plotting raw positions.
- Trip updates include a `delay` field. The feed's vehicle label is the lead car number.

### Metro-North
- Publishes every ~11 s; positions move in steps.
- Each entity contains both the vehicle and its trip update, **with different trip_ids** (`4852` vs
  `3237204`), so the adapter pairs them by entity, not by trip_id.
- ~150 positions without coordinates are scheduled trains that haven't departed; they're skipped.
- No `delay` field: delays must be computed against the schedule (not yet done).

### NJ Transit Rail
- Login: `POST getToken` (multipart `username`, `password`) → `{Authenticated, UserToken}`. **Only 10 new
  tokens per day**, so tokens are cached and re-login is attempted at most every 15 minutes.
- Positions jump 2–6 km between polls, and `LAST_MODIFIED` doesn't change when they do: it isn't the
  position time. The adapter stamps positions with fetch time and uses `LAST_MODIFIED` only to drop dead records.
- `NEXT_STOP` is a short name ("New York", "Ramsey Rt 17") matched to static names ("NEW YORK PENN STATION",
  "RAMSEY ROUTE 17 STATION") by exact-then-prefix matching with RT→ROUTE.
- No ETAs. The index ships today's stop times; ETA = scheduled arrival at the next stop + `SEC_LATE`.
- Train numbers repeat across weekday and weekend schedules, so the index covers only today's and
  tomorrow's service.
- The public rail zip also contains light rail (HBLR, Newark LR, RiverLINE, `route_type` 0), filtered out.
- `getGTFS` doesn't exist on RailData (404); the public zip works without login.

### NJ Transit Bus
- Login: `POST authenticateUser` (form `username`, `password`); data calls are multipart POSTs with `token`.
- **About half of `getVehiclePositions` calls return a stale snapshot days old.** A kept-alive connection
  tends to stick to it. The adapter retries up to 4 times on fresh connections until the header timestamp
  is under 5 minutes old (~1.9 calls per poll on average).
- **Realtime trip_ids match neither the public `bus_data.zip` nor the API's `getGTFS` zip.** (The API zip's
  trip_ids are 1…109,441, so collisions look like matches; only 5 of 526 were on the right route.) Trips
  are matched by route number + start time against today's service: ~87% unique, the rest pick the first
  qualifying candidate.
- **Use the public `bus_data.zip`, not the API's `getGTFS`.** The API zip is a fixed two-week window (e.g. Oct 4–18)
  that can lapse before the next one is posted; on 2026-10-19 it still ended the day before. The public zip covers
  ~6 months (Sep 2026–Mar 2027), needs no login, and has detailed shapes. The API zip's express shapes skipped
  highway stretches (one 17 km segment from the Lincoln Tunnel to Union).
- With the public zip, ~98% of live buses on Manhattan routes match; ~97% sit within 500 m of their shape
  (median 6 m). Buses on local routes (~600 at weekday peak) are skipped by design.
- Vehicle label is the bus fleet number. Trip updates carry delays but no times (~900 KB per call; not fetched yet).
- Reported limit ~40,000 calls/day (from a third-party client library; NJT's agreement caps all use at 100,000/day).

## Planned sources

| Agency | Static | Realtime | Notes |
|---|---|---|---|
| PATH | `http://data.trilliumtransit.com/gtfs/path-nj-us/path-nj-us.zip` | `https://www.panynj.gov/bin/portauthority/ridepath.json` | No positions, only per-station countdowns → estimated positions |
| NYC Ferry | nycferry.com developer page (the old connexionz zip URL is dead) | GTFS-RT `https://nycferry.connexionz.net/rtt/public/utility/gtfsrealtime.aspx/vehicleposition` | Real GPS every ~5 s |
| NY Waterway, Seastreak, SI Ferry | — | AIS (aisstream.io websocket) | Needs a persistent connection: doesn't fit serverless; deferred |

## Terms we must follow

Both the [MTA developer terms](https://www.mta.info/developers/terms-and-conditions) and the
[NJ TRANSIT data feed agreement](https://developer.njtransit.com/terms/) require:

- Telling users the information **may not be real time** (required when the lag can exceed 1 minute; ours can).
- Never stating or implying the data is accurate, complete or timely.
- Never implying the app is licensed or endorsed by the agency. Don't use agency logos.
- Serving data from our own server: browsers must never call agency servers directly.

The notice in the panel (`public/index.html`) covers the first three; the API design covers the last.

NJ TRANSIT additionally: feeds may be discontinued at any time, aren't for commercial use, and are capped
at 100,000 requests/day without written consent.

Other services: OpenFreeMap tiles are free with no key (attribution is shown automatically). The OSRM
demo server asks for light use, at most 1 request/second; the build throttles and caches.
