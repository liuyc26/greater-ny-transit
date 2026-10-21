"""Copy NJT tokens from the local token file into the shared Upstash Redis store.

NJ Transit RailData allows only 10 logins per day, but tokens stay valid for days. If production
has no working token (e.g. the daily login limit was hit), seed it with a token that works locally
instead of waiting for the limit to reset.

Needs the Upstash REST credentials in .env (or the environment), under either name pair:
  KV_REST_API_URL / KV_REST_API_TOKEN            (what the Vercel Marketplace integration sets)
  UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN

Usage: python scripts/seed_njt_tokens.py [--force] [rail|bus ...]
"""

import argparse
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transit import config  # noqa: E402
from transit.njt import RAIL_BASE, BUS_BASE, USER_AGENT, FileTokenStore, RedisTokenStore, default_store  # noqa: E402


def token_works(api: str, token: str) -> bool:
    url = RAIL_BASE + "getVehicleData" if api == "rail" else BUS_BASE + "getVehiclePositions"
    resp = httpx.post(url, files={"token": (None, token)}, headers={"User-Agent": USER_AGENT}, timeout=30)
    return resp.status_code == 200 and "invalid token" not in resp.text[:2000].lower()


def main():
    load_dotenv(config.ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("apis", nargs="*", default=["rail", "bus"])
    parser.add_argument("--force", action="store_true", help="overwrite a token already in Redis")
    args = parser.parse_args()

    redis = default_store()
    if not isinstance(redis, RedisTokenStore):
        sys.exit("Upstash Redis env vars not set (see the docstring).")
    local = FileTokenStore()

    for api in args.apis:
        entry = local.get(api)
        token = entry.get("token")
        if not token:
            print(f"{api}: no local token; skipping")
            continue
        if not token_works(api, token):
            print(f"{api}: local token is rejected by NJT; skipping")
            continue
        existing = redis.get(api)
        if existing.get("token") and not args.force:
            print(f"{api}: Redis already has a token; use --force to overwrite")
            continue
        # Clear the login cooldown and error so production uses the token immediately.
        redis.set(api, {"token": token, "obtained_at": entry.get("obtained_at") or int(time.time()),
                        "last_login_attempt": None, "last_login_error": None})
        print(f"{api}: copied local token (verified working) to Redis")


if __name__ == "__main__":
    main()
