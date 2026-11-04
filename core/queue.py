"""Reliable Redis job queue (works with Upstash over TLS, local Redis, or in-memory fake).

Keys (prefix = QUEUE_PREFIX):
  {p}:pending              LIST  job ids ready to run (LPUSH in, BLMOVE out)
  {p}:processing:{worker}  LIST  ids a worker has reserved but not acked (crash recovery)
  {p}:delayed              ZSET  id -> unix ts when it becomes ready (retries, scheduled jobs)
  {p}:idem:{key}           STR   idempotency key -> job id (24h TTL)

Postgres is the source of truth for job state; Redis only carries ids, which keeps
command volume low enough for Upstash's free quota.
"""
from __future__ import annotations

import time

import redis.asyncio as aioredis


class TaskQueue:
    def __init__(self, client, prefix: str = "ap"):
        self.r = client
        self.prefix = prefix
        self.pending = f"{prefix}:pending"
        self.delayed = f"{prefix}:delayed"

    @classmethod
    def from_url(cls, url: str, prefix: str = "ap", poll_timeout: int = 30) -> "TaskQueue":
        if url.startswith("memory://"):
            import fakeredis

            return cls(fakeredis.FakeAsyncRedis(decode_responses=True), prefix)
        client = aioredis.from_url(
            url,
            decode_responses=True,
            socket_timeout=poll_timeout + 15,  # must exceed the blocking-pop timeout
            socket_connect_timeout=10,
            socket_keepalive=True,
            health_check_interval=30,
            retry_on_timeout=True,
        )
        return cls(client, prefix)

    def processing(self, worker_id: str) -> str:
        return f"{self.prefix}:processing:{worker_id}"

    async def ping(self) -> bool:
        return bool(await self.r.ping())

    async def enqueue(self, job_id: str, delay: float = 0) -> None:
        if delay > 0:
            await self.r.zadd(self.delayed, {job_id: time.time() + delay})
        else:
            await self.r.lpush(self.pending, job_id)

    async def promote_due(self, limit: int = 100) -> int:
        due = await self.r.zrangebyscore(self.delayed, "-inf", time.time(), start=0, num=limit)
        moved = 0
        for job_id in due:
            if await self.r.zrem(self.delayed, job_id):  # only the remover pushes -> no dupes
                await self.r.lpush(self.pending, job_id)
                moved += 1
        return moved

    async def reserve(self, worker_id: str, timeout: int) -> str | None:
        return await self.r.blmove(self.pending, self.processing(worker_id), timeout, "RIGHT", "LEFT")

    async def ack(self, worker_id: str, job_id: str) -> None:
        await self.r.lrem(self.processing(worker_id), 1, job_id)

    async def requeue_worker(self, worker_id: str) -> int:
        """Move everything a (dead) worker had reserved back to pending."""
        n = 0
        while await self.r.lmove(self.processing(worker_id), self.pending, "RIGHT", "LEFT"):
            n += 1
        return n

    async def claim_idempotency(self, key: str, job_id: str, ttl: int = 86400) -> str | None:
        """Returns None if the key is new (and stores job_id), else the existing job id."""
        k = f"{self.prefix}:idem:{key}"
        if await self.r.set(k, job_id, nx=True, ex=ttl):
            return None
        return await self.r.get(k)

    async def release_idempotency(self, key: str) -> None:
        await self.r.delete(f"{self.prefix}:idem:{key}")

    async def depth(self) -> dict[str, int]:
        return {"pending": int(await self.r.llen(self.pending)),
                "delayed": int(await self.r.zcard(self.delayed))}

    async def close(self) -> None:
        try:
            await self.r.aclose()
        except Exception:
            pass
