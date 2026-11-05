"""Reliable Redis job queue: Upstash (TCP/TLS or REST/HTTPS), local Redis, or in-memory fake.

Keys (prefix = QUEUE_PREFIX):
  {p}:pending              LIST  job ids ready to run (LPUSH in, BLMOVE out)
  {p}:processing:{worker}  LIST  ids a worker reserved but has not acked (crash recovery)
  {p}:delayed              ZSET  id -> unix ts when it becomes ready (retries, scheduled jobs)
  {p}:idem:{key}           STR   idempotency key -> job id (24h TTL)
  {p}:workers              SET   registered worker ids
  {p}:hb:{worker}          STR   worker heartbeat (JSON, expires after ~3 intervals)

Postgres is the source of truth for job state; Redis carries ids and liveness only.
Idle cost is ~2 commands per poll (ZRANGE + blocking BLMOVE) regardless of concurrency,
which keeps a 24/7 worker well inside Upstash's free 500K commands/month.
"""
from __future__ import annotations

import json
import time
from typing import Any

import redis.asyncio as aioredis


class TaskQueue:
    def __init__(self, client, prefix: str = "ap", transport: str = "tcp"):
        self.r = client
        self.prefix = prefix
        self.transport = transport  # tcp | rest | memory
        self.pending = f"{prefix}:pending"
        self.delayed = f"{prefix}:delayed"
        self.workers_key = f"{prefix}:workers"
        self.next_due_at: float | None = None  # earliest future delayed job, set by promote_due

    # ------------------------------------------------------------ factory
    @classmethod
    def from_url(cls, url: str, prefix: str = "ap", poll_timeout: int = 30,
                 rest_token: str = "") -> "TaskQueue":
        if url.startswith("memory://"):
            import fakeredis

            return cls(fakeredis.FakeAsyncRedis(decode_responses=True), prefix, "memory")
        if url.startswith("https://"):
            from core.upstash_rest import UpstashRestRedis

            if not rest_token:
                raise ValueError("Upstash REST URL given without UPSTASH_REDIS_REST_TOKEN")
            return cls(UpstashRestRedis(url, rest_token), prefix, "rest")
        client = aioredis.from_url(
            url,
            decode_responses=True,
            socket_timeout=poll_timeout + 15,  # must exceed the blocking-pop timeout
            socket_connect_timeout=10,
            socket_keepalive=True,
            health_check_interval=30,
            retry_on_timeout=True,
        )
        return cls(client, prefix, "tcp")

    @classmethod
    def from_settings(cls, s) -> "TaskQueue":
        if s.upstash_rest_url and s.upstash_rest_token:
            return cls.from_url(s.upstash_rest_url, s.queue_prefix, s.worker_poll_timeout,
                                rest_token=s.upstash_rest_token)
        return cls.from_url(s.redis_url, s.queue_prefix, s.worker_poll_timeout)

    def processing(self, worker_id: str) -> str:
        return f"{self.prefix}:processing:{worker_id}"

    def _hb(self, worker_id: str) -> str:
        return f"{self.prefix}:hb:{worker_id}"

    # ---------------------------------------------------------------- jobs
    async def ping(self) -> bool:
        return bool(await self.r.ping())

    async def enqueue(self, job_id: str, delay: float = 0) -> None:
        if delay > 0:
            await self.r.zadd(self.delayed, {job_id: time.time() + delay})
            self.next_due_at = min(self.next_due_at or float("inf"), time.time() + delay)
        else:
            await self.r.lpush(self.pending, job_id)

    async def promote_due(self, limit: int = 100) -> int:
        """Move due delayed jobs to pending. One ZRANGE also tells us when the next one is due."""
        entries = await self.r.zrange(self.delayed, 0, limit - 1, withscores=True)
        now = time.time()
        moved = 0
        future = [score for _, score in entries if score > now]
        for job_id, score in entries:
            if score <= now and await self.r.zrem(self.delayed, job_id):  # remover owns it -> no dupes
                await self.r.lpush(self.pending, job_id)
                moved += 1
        if future:
            self.next_due_at = min(future)
        elif len(entries) == limit:
            self.next_due_at = now  # more due items beyond this page
        else:
            self.next_due_at = None
        return moved

    async def reserve(self, worker_id: str, timeout: int) -> str | None:
        return await self.r.blmove(self.pending, self.processing(worker_id), max(1, int(timeout)),
                                   "RIGHT", "LEFT")

    async def ack(self, worker_id: str, job_id: str) -> None:
        await self.r.lrem(self.processing(worker_id), 1, job_id)

    async def requeue_worker(self, worker_id: str) -> list[str]:
        """Move everything a (dead or stopping) worker had reserved back to pending."""
        ids = []
        while job_id := await self.r.lmove(self.processing(worker_id), self.pending, "RIGHT", "LEFT"):
            ids.append(job_id)
        return ids

    async def claim_idempotency(self, key: str, job_id: str, ttl: int = 86400) -> str | None:
        """Returns None if the key is new (and stores job_id), else the existing job id."""
        k = f"{self.prefix}:idem:{key}"
        if await self.r.set(k, job_id, nx=True, ex=ttl):
            return None
        return await self.r.get(k)

    async def depth(self) -> dict[str, int]:
        return {"pending": int(await self.r.llen(self.pending)),
                "delayed": int(await self.r.zcard(self.delayed))}

    # ------------------------------------------- quotas, rate limits, one-time secrets
    def _quota_key(self, client_id: str) -> str:
        return f"{self.prefix}:quota:{client_id}:{time.strftime('%Y%m', time.gmtime())}"

    async def quota_incr(self, client_id: str) -> int:
        """Month-to-date job counter (same key as the Worker). One INCR per accepted webhook."""
        key = self._quota_key(client_id)
        n = int(await self.r.incr(key))
        if n == 1:
            await self.r.expire(key, 40 * 86400)
        return n

    async def quota_decr(self, client_id: str) -> None:
        await self.r.decr(self._quota_key(client_id))

    async def quota_get(self, client_id: str) -> int:
        return int(await self.r.get(self._quota_key(client_id)) or 0)

    async def over_rate(self, key: str, window_s: int, limit: int) -> bool:
        k = f"{self.prefix}:rl:{key}"
        n = int(await self.r.incr(k))
        if n == 1:
            await self.r.expire(k, window_s)
        return n > limit

    async def put_secret(self, key: str, value: str, ttl: int) -> None:
        await self.r.set(f"{self.prefix}:secret:{key}", value, ex=ttl)

    async def peek_secret(self, key: str) -> str | None:
        return await self.r.get(f"{self.prefix}:secret:{key}")

    async def release_idempotency(self, key: str) -> None:
        await self.r.delete(f"{self.prefix}:idem:{key}")

    async def take_secret(self, key: str) -> str | None:
        """Read-once (GETDEL): onboarding credentials are revealed a single time."""
        return await self.r.getdel(f"{self.prefix}:secret:{key}")

    # ------------------------------------------------------- worker liveness
    async def register_worker(self, worker_id: str) -> None:
        await self.r.sadd(self.workers_key, worker_id)

    async def heartbeat(self, worker_id: str, info: dict[str, Any], ttl: int) -> None:
        await self.r.set(self._hb(worker_id), json.dumps({**info, "ts": time.time()}), ex=max(1, int(ttl)))

    async def unregister_worker(self, worker_id: str) -> None:
        await self.r.delete(self._hb(worker_id))
        await self.r.srem(self.workers_key, worker_id)

    async def live_workers(self) -> tuple[dict[str, dict], list[str]]:
        """(alive {id: heartbeat info}, dead [ids whose heartbeat expired])."""
        ids = sorted(await self.r.smembers(self.workers_key))
        if not ids:
            return {}, []
        values = await self.r.mget(*[self._hb(w) for w in ids])
        alive, dead = {}, []
        for wid, raw in zip(ids, values):
            if raw is None:
                dead.append(wid)
            else:
                try:
                    alive[wid] = json.loads(raw)
                except ValueError:
                    alive[wid] = {}
        return alive, dead

    async def reap_dead_workers(self) -> dict[str, list[str]]:
        """Requeue jobs held by workers whose heartbeat expired; returns {worker_id: [job ids]}."""
        _, dead = await self.live_workers()
        out: dict[str, list[str]] = {}
        for wid in dead:
            out[wid] = await self.requeue_worker(wid)
            await self.r.srem(self.workers_key, wid)
        return out

    async def close(self) -> None:
        try:
            await self.r.aclose()
        except Exception:
            pass
