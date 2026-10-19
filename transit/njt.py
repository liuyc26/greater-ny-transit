"""NJ Transit API access: login, token caching, authenticated POST calls.

Tokens are scarce (RailData allows only ~5 new tokens per day), so they're cached in a store
and a fresh login is attempted at most once per LOGIN_COOLDOWN_S per API.

Local dev uses a JSON file. Serverless instances don't share a filesystem, so production needs
a shared store (e.g. Upstash Redis) implementing the same get/set interface.
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


class NjtAuthError(RuntimeError):
    pass


class FileTokenStore:
    def __init__(self, path: Path | None = None):
        self.path = path or Path(os.environ.get("NJT_TOKEN_CACHE", config.ROOT / ".cache" / "njt_tokens.json"))

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def get(self, api: Api) -> dict:
        return self._load().get(api, {})

    def set(self, api: Api, entry: dict) -> None:
        data = self._load()
        data[api] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)


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
    resp.raise_for_status()
    body = resp.json()
    if str(body.get("Authenticated")).lower() != "true" or not body.get("UserToken"):
        raise NjtAuthError(f"login rejected: {body.get('errorMessage') or body.get('Authenticated')}")
    return body["UserToken"]


def _is_invalid_token(resp: httpx.Response) -> bool:
    if resp.status_code in (401, 403):
        return True
    if "json" in resp.headers.get("content-type", "") and len(resp.content) < 2000:
        return "invalid token" in resp.text.lower()
    return False


class NjtClient:
    """Authenticated calls to the NJT rail or bus API, sharing one token store."""

    def __init__(self, store: FileTokenStore | None = None):
        self.store = store or FileTokenStore()

    def _cached_token(self, api: Api) -> str | None:
        return self.store.get(api).get("token")

    def _may_login(self, api: Api) -> bool:
        return time.time() - self.store.get(api).get("last_login_attempt", 0) > LOGIN_COOLDOWN_S

    def _record_attempt(self, api: Api, token: str | None) -> None:
        entry = self.store.get(api)
        entry["last_login_attempt"] = int(time.time())
        if token:
            entry.update(token=token, obtained_at=int(time.time()))
        self.store.set(api, entry)

    # --- async (API functions) ---

    async def _login_async(self, client: httpx.AsyncClient, api: Api) -> str:
        if not self._may_login(api):
            raise NjtAuthError(f"{api}: login on cooldown (token invalid or missing)")
        url, kwargs = _login_request(api)
        token = None
        try:
            token = _parse_login(await client.post(url, headers={"User-Agent": USER_AGENT}, **kwargs))
        finally:
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
                    if not self._may_login(api):
                        raise NjtAuthError(f"{api}: login on cooldown")
                    url, kwargs = _login_request(api)
                    try:
                        token = _parse_login(client.post(url, **kwargs))
                    finally:
                        self._record_attempt(api, token)
                resp = client.post(base + method, files={"token": (None, token)})
                if not _is_invalid_token(resp):
                    resp.raise_for_status()
                    return resp
                token = None
        raise NjtAuthError(f"{api}: token rejected after re-login")
