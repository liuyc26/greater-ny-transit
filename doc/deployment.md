# Deployment

All on free tiers with no payment method (the project is non-commercial).

| Piece | Service | How |
|---|---|---|
| Frontend (`public/`) | Vercel Hobby | Served from Vercel's CDN at the site root |
| API (`transit.app:app`) | Vercel Hobby, one Python function | Set by `[tool.vercel] entrypoint` in `pyproject.toml`; responses cached at the CDN per `Cache-Control` |
| NJT token store | Upstash Redis (free) | `RedisTokenStore` in `transit/njt.py`, used automatically when its env vars are set |
| Daily data build | GitHub Actions | `.github/workflows/build-data.yml`, 08:00 UTC; pushes to `main`, which redeploys Vercel |

## Why the token store

NJ Transit RailData allows only ~5 new tokens per day. Serverless instances don't share a filesystem, so
without a shared store every cold start would log in again. `RedisTokenStore` keeps tokens in memory and
reads Redis once per cold start, writing only on login: a few commands a day against Upstash's 500K/month.

It reads `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`, or the `KV_REST_API_URL` /
`KV_REST_API_TOKEN` names the Vercel Marketplace integration sets. Without them it falls back to the local
file `.cache/njt_tokens.json`.

## First deploy

1. **Vercel:** sign up at vercel.com with GitHub (Hobby plan). *Add New → Project*, import
   `liuyc26/greater-ny-transit`. Framework preset: FastAPI (auto-detected from `pyproject.toml`).
2. **Upstash:** in the Vercel project, *Storage → Marketplace → Upstash (Redis)*, free plan, connect it to
   the project. This creates the database and sets the `KV_REST_API_*` variables.
3. **Environment variables** (Project → Settings → Environment Variables): `NJT_USERNAME`, `NJT_PASSWORD`.
4. **Deploy** (or redeploy after adding variables), then check:
   - `/api/health` and `/api/agencies`
   - `/api/vehicles/lirr`, `/mnr`, `/njt_rail`, `/njt_bus`: each `feeds.<agency>.ok` should be `true`
   - the map loads, vehicles move, hover works, both themes work
5. **GitHub Actions:** the daily build needs no secrets. Run it once from the Actions tab (*Daily data
   build → Run workflow*) to confirm it commits and that Vercel redeploys.

## Daily build

The workflow rebuilds all map data and trip indexes, runs the tests, and commits changes to `main` as
`github-actions[bot]`. NJT indexes cover only today and tomorrow, so if it fails two days in a row, NJT rail
arrival times and bus matching stop. GitHub emails the repo owner when a scheduled run fails.

GitHub disables scheduled workflows in repositories with no activity for 60 days; the daily commits count
as activity.

## Free-tier budget

Vercel Hobby (monthly): 4 h active CPU, 1M function invocations, 100 GB transfer, 300 s max duration.
Measured CPU per request: ~4 ms for each rail feed, ~20 ms for NJT bus (more at peak).

Each agency endpoint calls its feed at most once per cache period per CDN location, regardless of visitors:

| Agency | Cache | Max feed calls/day per CDN location |
|---|---|---|
| LIRR | 10 s | ~8,600 |
| Metro-North | 10 s | ~8,600 |
| NJT rail | 10 s | ~8,600 (limit undocumented) |
| NJT bus | 15 s | ~11,000 incl. stale-snapshot retries |

That's ~32k invocations/day if someone watches 24/7, close to the 1M/month allowance. Typical hobby
traffic is far below. If usage approaches the limits (Vercel → Usage), raise `cache_s` in
`transit/feeds/__init__.py`. Hobby has no card on file, so exceeding limits pauses the site instead of billing.

NJ TRANSIT caps total use at 100,000 requests/day; the bus API reportedly allows ~40,000/day.
