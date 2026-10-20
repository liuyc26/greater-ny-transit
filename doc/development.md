# Development

## Setup

Requires Python 3.12+ (developed on 3.13). Node isn't needed: the frontend has no build step.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env    # then fill in NJT_USERNAME / NJT_PASSWORD
```

NJ Transit credentials come from a free account at https://developer.njtransit.com. Without them, LIRR and
Metro-North still work and the NJT rows in the panel show ⚠. The full build needs them for the NJT bus
schedule; without credentials, build the other agencies by name (below).

## Build the map data

```bash
.venv/bin/python scripts/build_static.py              # all agencies (~40 s; first run longer, see below)
.venv/bin/python scripts/build_static.py lirr mnr     # just some
.venv/bin/python scripts/build_static.py --refresh    # ignore cached downloads
```

- Downloads are cached in `data/raw/` for 20 hours.
- All schedules are public downloads; only the realtime NJT APIs need credentials.
- If bus shapes ever have long straight gaps, the build queries OSRM for road geometry at 1 request/second.
  Results go to `data/road_fill_cache.json`; commit that file.
- If an agency's schedule has no service today, the build re-downloads it, and if it still has none it
  fails for that agency and keeps its previous data rather than writing an empty index.
- Building a subset of agencies merges into the existing map files.
- NJT rail and bus indexes only cover **today and tomorrow**, so rebuild daily. A build made on a Sunday
  only includes Sunday's bus routes.

The build prints per-agency stats: trips qualifying, direction counts, shapes, index key conflicts, and
road-fill results. Check them after changing filters.

## Run

```bash
.venv/bin/uvicorn transit.dev:app --reload --port 8765
```

Open http://localhost:8765. The API alone:

```bash
curl localhost:8765/api/agencies
curl localhost:8765/api/vehicles/lirr
curl localhost:8765/api/health
```

`--reload` watches `.py` files only. After rebuilding data, touch a `.py` file (or restart) so adapters
reload their trip indexes.

In the browser console, `window.__transit` is the app state and `window.__map` the MapLibre map.

## Test

```bash
.venv/bin/python -m pytest
```

Tests cover direction classification, GTFS-RT normalization, NJT parsing, time zones, stop-name matching,
the token store and login cooldown, bus stale-feed retries, and road-fill acceptance rules. They don't hit
the network.

## NJT tokens

Tokens are cached in `.cache/njt_tokens.json` (gitignored, mode 600). RailData allows only ~5 new tokens
per day, so don't delete this file casually. If a token is rejected, the client re-logs in at most once
every 15 minutes per API.

## Adding an agency

1. **Config:** add an `Agency` in `transit/config.py`: static GTFS URL, and how its realtime trips map to
   static trips (`rt_trip_key`, plus `current_service_only`, `index_schedule`, `number_field` as needed).
2. **Build:** run `scripts/build_static.py <id>` and check the stats (qualifying trips, key conflicts).
3. **Adapter:** standard GTFS-RT with vehicle positions → `GtfsRtAdapter(agency=..., url=..., cache_s=...)`.
   Anything else → a new adapter in `transit/feeds/` returning `(list[Vehicle], FeedStatus)`.
4. **Register** it in `transit/feeds/__init__.py`.
5. **Frontend:** add display settings (label, color, dot size, "Train"/"Bus") to `AGENCIES` in `public/app.js`.
6. **Verify against live data:** how often positions change, whether realtime trips match the index, how
   far vehicles sit from their shapes. Each agency so far had at least one surprise; see
   [data-sources.md](data-sources.md).
7. **Terms:** read the agency's data terms and update the notice in `public/index.html` if needed.
