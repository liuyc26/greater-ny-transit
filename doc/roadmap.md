# Roadmap

## Milestones

| # | Milestone | Status |
|---|---|---|
| M0 | Scaffold: FastAPI, frontend, dev server, tests | ✅ |
| M1 | Static layer: Manhattan filter, route lines, stations | ✅ |
| M2 | Live LIRR + Metro-North, direction filter, tooltips | ✅ |
| M3 | Smooth motion along the track (snap, ETA/speed plans, dwell) | ✅ |
| M4 | NJ Transit rail + bus: tokens, call budget, trip matching, per-agency endpoints | ✅ |
| — | Train/bus numbers, hover route highlight, region limit, road-filled bus shapes, data-terms notice | ✅ |
| M5 | PATH: estimated positions from ridepath.json countdowns (hollow dots) | next |
| M6 | NYC Ferry (GTFS-RT) | |
| M7 | Delays: color dots by delay (LIRR and NJT have it; MNR needs schedule comparison), service alerts, cancellations | |
| M8 | History: sample every ~5 min from GitHub Actions into a free Postgres (Neon/Supabase); on-time stats; rush-hour replay | |
| M9 | Deploy: Upstash token store, Vercel config, daily build workflow (see [deployment.md](deployment.md)) | code ready; accounts pending |

## Smaller ideas

- Fading trails behind moving vehicles.
- Hide overlapping number labels near Port Authority.
- On phones, leave room for the panel when fitting the map, or make the panel collapsible.

## Risks and open questions

- **NJT API behavior can change without notice** (stale bus snapshots, trip_id schemes). Feed health shows
  in the panel; check `skipped_trips` in API responses if counts drop.
- **NJT indexes cover only today and tomorrow**: if the daily build misses two days, rail ETAs and bus matching stop.
- **RailData `getVehicleData` limit is undocumented.** Watch for errors at 10 s polling.
- **Vercel Python limits** unverified until deploy.
- **AIS ferries** (NY Waterway, Seastreak, SI Ferry) need a persistent websocket. Options: an Oracle Cloud
  Always Free VM running only the AIS collector, or skip.
- **PATH estimation** will jitter near stations.
- **Bus shapes** come from the public zip, which has no long straight gaps today; road fill remains as a safeguard.
