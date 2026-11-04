"""Minimal async client for Upstash Redis over its REST API (HTTPS, port 443).

Implements the subset of redis-py's asyncio API that TaskQueue uses, so the queue also runs
on hosts that block outbound Redis ports (Hugging Face Spaces only allow 80/443/8080).
Blocking commands work as long-polls: BLMOVE with a 30s timeout is one HTTP request that
returns as soon as a job arrives. Errors map onto redis-py exception types so callers'
retry/backoff logic is the same for both transports.
"""
from __future__ import annotations

from typing import Any

import httpx
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError


def _arg(v: Any) -> str:
    if isinstance(v, bytes):
        return v.decode()
    if isinstance(v, float):
        return repr(v)
    return str(v)


class UpstashRestRedis:
    def __init__(self, url: str, token: str, *, timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.url = url.rstrip("/")
        self._timeout = timeout
        self._http = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                                       timeout=timeout, transport=transport)

    async def execute(self, *args: Any, timeout: float | None = None) -> Any:
        try:
            r = await self._http.post(self.url, json=[_arg(a) for a in args],
                                      timeout=timeout or self._timeout)
        except httpx.TimeoutException as e:
            raise RedisTimeoutError(f"upstash REST timeout: {e}") from e
        except httpx.TransportError as e:
            raise RedisConnectionError(f"upstash REST transport error: {e}") from e
        try:
            data = r.json()
        except ValueError:
            raise RedisConnectionError(f"upstash REST HTTP {r.status_code}: {r.text[:200]}") from None
        if isinstance(data, dict) and data.get("error"):
            raise ResponseError(str(data["error"]))
        if r.status_code >= 400:
            raise RedisConnectionError(f"upstash REST HTTP {r.status_code}: {r.text[:200]}")
        return data.get("result") if isinstance(data, dict) else data

    # ---- connection
    async def ping(self) -> bool:
        return await self.execute("PING") == "PONG"

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- strings / keys
    async def set(self, key, value, nx: bool = False, ex: int | None = None):
        args: list[Any] = ["SET", key, value]
        if ex is not None:
            args += ["EX", int(ex)]
        if nx:
            args.append("NX")
        return True if await self.execute(*args) == "OK" else None

    async def get(self, key):
        return await self.execute("GET", key)

    async def mget(self, *keys):
        return await self.execute("MGET", *keys) if keys else []

    async def delete(self, *keys) -> int:
        return int(await self.execute("DEL", *keys)) if keys else 0

    async def exists(self, *keys) -> int:
        return int(await self.execute("EXISTS", *keys))

    # ---- lists
    async def lpush(self, key, *values) -> int:
        return int(await self.execute("LPUSH", key, *values))

    async def llen(self, key) -> int:
        return int(await self.execute("LLEN", key))

    async def lrem(self, key, count, value) -> int:
        return int(await self.execute("LREM", key, count, value))

    async def lmove(self, src, dst, src_side="LEFT", dst_side="RIGHT"):
        return await self.execute("LMOVE", src, dst, src_side, dst_side)

    async def blmove(self, src, dst, timeout, src_side="LEFT", dst_side="RIGHT"):
        # the HTTP request must outlive the server-side block
        return await self.execute("BLMOVE", src, dst, src_side, dst_side, int(timeout),
                                  timeout=float(timeout) + 15)

    # ---- sorted sets
    async def zadd(self, key, mapping: dict) -> int:
        args: list[Any] = ["ZADD", key]
        for member, score in mapping.items():
            args += [score, member]
        return int(await self.execute(*args))

    async def zrem(self, key, *members) -> int:
        return int(await self.execute("ZREM", key, *members))

    async def zcard(self, key) -> int:
        return int(await self.execute("ZCARD", key))

    async def zrange(self, key, start, end, withscores: bool = False):
        if not withscores:
            return await self.execute("ZRANGE", key, start, end)
        raw = await self.execute("ZRANGE", key, start, end, "WITHSCORES") or []
        if raw and isinstance(raw[0], list):  # tolerate [[member, score], ...]
            return [(m, float(sc)) for m, sc in raw]
        return [(raw[i], float(raw[i + 1])) for i in range(0, len(raw), 2)]  # Upstash: flat list

    async def zrangebyscore(self, key, min, max, start=None, num=None):
        args: list[Any] = ["ZRANGEBYSCORE", key, min, max]
        if start is not None and num is not None:
            args += ["LIMIT", start, num]
        return await self.execute(*args)

    # ---- sets
    async def sadd(self, key, *members) -> int:
        return int(await self.execute("SADD", key, *members))

    async def srem(self, key, *members) -> int:
        return int(await self.execute("SREM", key, *members))

    async def smembers(self, key) -> set:
        return set(await self.execute("SMEMBERS", key) or [])
