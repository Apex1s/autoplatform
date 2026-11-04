"""Crash recovery and at-most-once claiming, driven directly against queue + worker."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from core.queue import TaskQueue
from db import repo
from models import Job, WorkerHealth, utcnow
from db.session import session_scope
from workers.handlers import HANDLERS, JobContext
from workers.worker import Worker


def _queue(settings) -> TaskQueue:
    return TaskQueue.from_url(settings.redis_url, settings.queue_prefix, 5)


async def test_duplicate_enqueue_runs_once(settings, make_router):
    q, r = _queue(settings), make_router()
    w = Worker(settings, q, r, worker_id="w-dup")
    job = repo.create_job("echo", {"n": 1}, client_id=None, max_attempts=3, source="test")
    await q.enqueue(job.id)
    await q.enqueue(job.id)
    assert await w.run_once(1) and await w.run_once(1)
    assert w.processed == 1
    with session_scope() as s:
        assert s.query(__import__("models").ExecutionLog).filter_by(job_id=job.id).count() == 1
    await q.close()


async def test_dead_worker_jobs_are_recovered(settings, make_router):
    q, r = _queue(settings), make_router()
    job = repo.create_job("echo", {}, client_id=None, max_attempts=3, source="test")
    await q.enqueue(job.id)
    # worker A reserves the job, then "crashes" without acking
    assert await q.reserve("w-dead", 1) == job.id
    repo.heartbeat("w-dead", processed=0, failed=0)
    with session_scope() as s:
        s.get(WorkerHealth, "w-dead").last_heartbeat = utcnow() - timedelta(hours=1)
    assert (await q.depth())["pending"] == 0

    w = Worker(settings, q, r, worker_id="w-new")
    ctx = JobContext("sys", None, 1, w.worker_id, r, q, settings)
    out = await HANDLERS["system.maintenance"](ctx, {})
    assert out["dead_workers"] == ["w-dead"] and out["requeued_from_dead"] == 1
    assert await w.run_once(1)
    assert repo.get_job(job.id)["status"] == "succeeded"
    await q.close()


async def test_stuck_jobs_are_reenqueued(settings, make_router):
    """Job row exists but its id never reached Redis (e.g. Redis outage during enqueue)."""
    q, r = _queue(settings), make_router()
    job = repo.create_job("echo", {}, client_id=None, max_attempts=3, source="test")
    with session_scope() as s:
        s.get(Job, job.id).run_at = utcnow() - timedelta(minutes=30)
    w = Worker(settings, q, r, worker_id="w-stuck")
    ctx = JobContext("sys", None, 1, w.worker_id, r, q, settings)
    out = await HANDLERS["system.maintenance"](ctx, {})
    assert out["requeued_stuck"] == 1
    assert await w.run_once(1)
    assert repo.get_job(job.id)["status"] == "succeeded"
    await q.close()


async def test_graceful_stop_returns_inflight_job(settings, make_router):
    q, r = _queue(settings), make_router()
    started = asyncio.Event()

    async def slow(ctx, payload):
        started.set()
        await asyncio.sleep(30)
        return {}

    HANDLERS["test.slow"] = slow
    try:
        job = repo.create_job("test.slow", {}, client_id=None, max_attempts=3, source="test")
        await q.enqueue(job.id)
        w = Worker(settings, q, r, worker_id="w-stop")
        await w.start()
        await asyncio.wait_for(started.wait(), 5)
        await w.stop()
        assert repo.get_job(job.id)["status"] == "retrying"
        assert (await q.depth())["pending"] == 1  # handed back for the next worker
        assert w._tasks == []
    finally:
        HANDLERS.pop("test.slow")
        await q.close()


async def test_delayed_job_promotion(settings, make_router):
    q = _queue(settings)
    await q.enqueue("abc", delay=0.05)
    assert await q.promote_due() == 0
    await asyncio.sleep(0.1)
    assert await q.promote_due() == 1
    assert (await q.depth()) == {"pending": 1, "delayed": 0}
    await q.close()
