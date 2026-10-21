"""Minimal async client for Upstash Redis's REST API (shared state across serverless instances)."""

import os

import httpx


def upstash_env() -> tuple[str, str] | None:
    """(url, token) from the Upstash names, or the KV_* names the Vercel Marketplace integration sets."""
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
    return (url.rstrip("/"), token) if url and token else None


class UpstashKV:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    async def _command(self, client: httpx.AsyncClient, *args: str):
        resp = await client.post(self.url, json=list(args), headers=self.headers, timeout=5)
        resp.raise_for_status()
        return resp.json().get("result")

    async def get(self, client: httpx.AsyncClient, key: str) -> str | None:
        return await self._command(client, "GET", key)

    async def set(self, client: httpx.AsyncClient, key: str, value: str, ex_s: int | None = None) -> None:
        args = ["SET", key, value] + (["EX", str(ex_s)] if ex_s else [])
        await self._command(client, *args)


def kv_from_env() -> UpstashKV | None:
    env = upstash_env()
    return UpstashKV(*env) if env else None
