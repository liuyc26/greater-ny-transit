# Deployment

**Status: not deployed yet.** Everything runs locally. This is the plan, all on free tiers with no payment
method (the project is non-commercial).

## Target setup

| Piece | Service | Notes |
|---|---|---|
| Frontend (`public/`) | Vercel Hobby (static) | Served from the CDN |
| API (`api/index.py` → `transit.app:app`) | Vercel Hobby (Python function) | Responses cached at the CDN per `Cache-Control` |
| NJT token store | Upstash Redis (free tier) | Required: see below |
| Daily data build | GitHub Actions (cron) | Free for public repos. Vercel Hobby cron runs at most once a day, with ±1 hour precision |

## Blocker: shared NJT token store

RailData allows only ~5 new tokens per day. Locally, tokens live in `.cache/njt_tokens.json`. Serverless
instances don't share a filesystem, so every cold start would log in again and the daily allowance would
run out within hours. Before deploying:

1. Create a free Upstash Redis database.
2. Add a store class to `transit/njt.py` with the same `get(api)` / `set(api, entry)` interface as
   `FileTokenStore`, using Upstash's REST API (`UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`).
3. Choose the store from environment variables: Upstash when set, the file store otherwise.

## Steps

1. **Token store:** as above.
2. **Vercel config:** add `vercel.json` routing `/api/*` to `api/index.py` and serving `public/` as static
   output. Make sure `transit/data/*.json` is bundled with the function. Check the current Vercel Python
   runtime docs for both; they weren't verified yet.
3. **Vercel project:** import the GitHub repo. Set environment variables `NJT_USERNAME`, `NJT_PASSWORD`,
   `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`.
4. **Daily build workflow** (`.github/workflows/build-data.yml`): every morning before service (e.g.
   08:00 UTC, about 3–4 AM in New York):
   - run `scripts/build_static.py` (secrets `NJT_USERNAME`, `NJT_PASSWORD`);
   - commit changed `public/data/*`, `transit/data/*` and `data/road_fill_cache.json`;
   - the push triggers a Vercel redeploy.

   The NJT bus schedule from the API only covers dates through the current day, so a missed build means
   bus matching fails the next day. Monitor the workflow.
5. **Check limits:** Vercel Hobby function size, duration and invocation limits weren't verified. The
   function is small (~1 MB of indexes) and does one outbound request per call.

## Request budget once deployed

With CDN caching, each agency endpoint calls its feed at most once per cache period per CDN region,
regardless of visitors:

| Agency | Cache | Feed calls/day per region (max) |
|---|---|---|
| LIRR | 5 s | ~17,000 (MTA states no limit) |
| Metro-North | 10 s | ~8,600 |
| NJT rail | 10 s | ~8,600 (limit undocumented) |
| NJT bus | 15 s | ~11,000 incl. stale-snapshot retries |

NJ TRANSIT's agreement caps total use at 100,000 requests/day; the bus API reportedly allows ~40,000/day.
If traffic comes from many CDN regions, multiply accordingly and consider raising `cache_s`.
