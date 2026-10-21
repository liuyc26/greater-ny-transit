"""NJ Transit API access: login, token caching, authenticated POST calls.

Tokens are scarce (RailData allows 10 logins per day: "Daily usage limit:10"), so they're cached in a store
and a fresh login is attempted at most once per LOGIN_COOLDOWN_S per API.

Local dev uses a JSON file. Serverless instances don't share a filesystem, so production uses
Upstash Redis (RedisTokenStore), selected automatically when its environment variables are set.
"""

import json
import os
import time
from pathlib import Path
from typing import Literal

import httpx

from . import config

Api = Literal["rail", "bus"]

RAIL_BASE = "https://raildata.njtransit.com/api/TrainData/"
BUS_BASE = "https://pcsdata.njtransit.com/api/GTFS/"
USER_AGENT = "greater-ny-transit/0.1"
LOGIN_COOLDOWN_S = 15 * 60
LOGIN_TIMEOUT_S = 25


class NjtAuthError(RuntimeError):
    pass


class FileTokenStore:
    """Local JSON file (dev). Entries are also kept in memory, so if the file can't be written
    (e.g. a read-only filesystem) a running process still reuses its token instead of logging in
    on every request."""

    shared = False  # not visible to other processes/instances

    def __init__(self, path: Path | None = None):
        self.path = path or Path(os.environ.get("NJT_TOKEN_CACHE", config.ROOT / ".cache" / "njt_tokens.json"))
        self.memory: dict[str, dict] = {}

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def get(self, api: Api) -> dict:
        if api in self.memory:
            return dict(self.memory[api])
        return self._load().get(api, {})

    def set(self, api: Api, entry: dict) -> None:
        self.memory[api] = dict(entry)
        try:
            data = self._load()
            data[api] = entry
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)
        except OSError as e:
            print(f"NJT token file not writable ({e}); keeping {api} token in memory only")


class RedisTokenStore:
    """Token store shared by all serverless instances, via the Upstash Redis REST API.

    Entries with a token are cached in memory, so Redis is read once per cold start and written only
    on login: a few commands a day, far under Upstash's free tier (500K commands/month). Entries
    without a token are re-read every RECHECK_S, so a token another instance (or the seed script)
    stores is picked up without waiting for a cold start.
    """

    KEY_PREFIX = "njt_token:"
    RECHECK_S = 30
    shared = True

    def __init__(self, url: str, token: str, client: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.client = client or httpx.Client(timeout=5, headers={"Authorization": f"Bearer {token}"})
        self.cache: dict[str, tuple[float, dict]] = {}  # api -> (fetched_at, entry)

    def _command(self, *args: str):
        resp = self.client.post(self.url, json=list(args))
        resp.raise_for_status()
        return resp.json().get("result")

    def get(self, api: Api) -> dict:
        cached = self.cache.get(api)
        if cached is None or (not cached[1].get("token") and time.time() - cached[0] > self.RECHECK_S):
            raw = self._command("GET", self.KEY_PREFIX + api)
            cached = self.cache[api] = (time.time(), json.loads(raw) if raw else {})
        return dict(cached[1])

    def set(self, api: Api, entry: dict) -> None:
        self._command("SET", self.KEY_PREFIX + api, json.dumps(entry))
        self.cache[api] = (time.time(), dict(entry))


def default_store() -> FileTokenStore | RedisTokenStore:
    """Upstash Redis when configured (production), else the local JSON file. Accepts both the
    Upstash names and the KV_* names the Vercel Marketplace integration sets."""
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
    return RedisTokenStore(url, token) if url and token else FileTokenStore()


def _credentials() -> tuple[str, str]:
    user, password = os.environ.get("NJT_USERNAME"), os.environ.get("NJT_PASSWORD")
    if not user or not password:
        raise NjtAuthError("NJT_USERNAME / NJT_PASSWORD not set")
    return user, password


def _login_request(api: Api) -> tuple[str, dict]:
    """(url, httpx request kwargs) for a login call."""
    user, password = _credentials()
    if api == "rail":
        return RAIL_BASE + "getToken", {"files": {"username": (None, user), "password": (None, password)}}
    return BUS_BASE + "authenticateUser", {"data": {"username": user, "password": password}}


def _parse_login(resp: httpx.Response) -> str:
    # NJT reports failures as HTTP 500 with {"errorMessage": ...}; surface that message.
    try:
        body = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise NjtAuthError(f"login: unexpected response ({resp.status_code})")
    if not isinstance(body, dict) or str(body.get("Authenticated")).lower() != "true" or not body.get("UserToken"):
        detail = body.get("errorMessage") or body.get("Authenticated") if isinstance(body, dict) else body
        raise NjtAuthError(f"login rejected (HTTP {resp.status_code}): {detail}")
    return body["UserToken"]


def _is_invalid_token(resp: httpx.Response) -> bool:
    if resp.status_code in (401, 403):
        return True
    if "json" in resp.headers.get("content-type", "") and len(resp.content) < 2000:
        return "invalid token" in resp.text.lower()
    return False


class NjtClient:
    """Authenticated calls to the NJT rail or bus API, sharing one token store."""

    def __init__(self, store: FileTokenStore | RedisTokenStore | None = None):
        self.store = store or default_store()

    def status(self) -> dict:
        """Token-store diagnostics for /api/health (never includes tokens or credentials)."""
        now = time.time()
        out = {"store": type(self.store).__name__, "shared": self.store.shared}
        for api in ("rail", "bus"):
            try:
                e = self.store.get(api)
            except Exception as ex:  # e.g. Redis unreachable
                out[api] = {"error": f"{type(ex).__name__}: {ex}"}
                continue
            out[api] = {
                "has_token": bool(e.get("token")),
                "token_age_h": round((now - e["obtained_at"]) / 3600, 1) if e.get("obtained_at") else None,
                "last_login_min_ago": round((now - e["last_login_attempt"]) / 60) if e.get("last_login_attempt") else None,
                "last_login_error": e.get("last_login_error"),
            }
        return out

    def _cached_token(self, api: Api) -> str | None:
        return self.store.get(api).get("token")

    def _check_cooldown(self, api: Api) -> None:
        # On Vercel every instance has its own memory and a read-only filesystem: without a shared
        # store each cold start would log in again, and RailData allows only 10 logins a day.
        if os.environ.get("VERCEL") and not self.store.shared:
            raise NjtAuthError(f"{api}: no shared token store configured (set the Upstash Redis "
                               "env vars); refusing to log in from a serverless instance")
        entry = self.store.get(api)
        if time.time() - entry.get("last_login_attempt", 0) <= LOGIN_COOLDOWN_S:
            raise NjtAuthError(f"{api}: login on cooldown; last login error: {entry.get('last_login_error')}")

    def _record_attempt(self, api: Api, token: str | None, error: str | None = None) -> None:
        entry = self.store.get(api)
        entry["last_login_attempt"] = int(time.time())
        entry["last_login_error"] = error
        if token:
            entry.update(token=token, obtained_at=int(time.time()))
        self.store.set(api, entry)

    # --- async (API functions) ---

    async def _login_async(self, client: httpx.AsyncClient, api: Api) -> str:
        self._check_cooldown(api)
        url, kwargs = _login_request(api)
        try:
            # Generous timeout: a failed login costs one of RailData's ~5 daily tokens' worth of retries.
            resp = await client.post(url, headers={"User-Agent": USER_AGENT}, timeout=LOGIN_TIMEOUT_S, **kwargs)
            token = _parse_login(resp)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            print(f"NJT {api} login failed: {error}")  # shows in Vercel runtime logs
            self._record_attempt(api, None, error)
            raise
        self._record_attempt(api, token)
        return token

    async def post(self, client: httpx.AsyncClient, api: Api, method: str) -> httpx.Response:
        base = RAIL_BASE if api == "rail" else BUS_BASE
        token = self._cached_token(api) or await self._login_async(client, api)
        for attempt in range(2):
            resp = await client.post(base + method, headers={"User-Agent": USER_AGENT},
                                     files={"token": (None, token)})
            if not _is_invalid_token(resp):
                resp.raise_for_status()
                return resp
            if attempt == 0:
                token = await self._login_async(client, api)
        raise NjtAuthError(f"{api}: token rejected after re-login")

    # --- sync (build script) ---

    def post_sync(self, api: Api, method: str, timeout: float = 300) -> httpx.Response:
        base = RAIL_BASE if api == "rail" else BUS_BASE
        with httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
            token = self._cached_token(api)
            for attempt in range(2):
                if token is None:
                    self._check_cooldown(api)
                    url, kwargs = _login_request(api)
                    try:
                        token = _parse_login(client.post(url, **kwargs))
                    except Exception as e:
                        self._record_attempt(api, None, f"{type(e).__name__}: {e}")
                        raise
                    self._record_attempt(api, token)
                resp = client.post(base + method, files={"token": (None, token)})
                if not _is_invalid_token(resp):
                    resp.raise_for_status()
                    return resp
                token = None
        raise NjtAuthError(f"{api}: token rejected after re-login")
