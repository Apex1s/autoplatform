"""Crash recovery, at-least-once claiming and graceful shutdown, on every queue transport."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from db import repo
from db.session import session_scope
from models import ExecutionLog, Job, WorkerHealth, utcnow
from workers.handlers import HANDLERS, JobContext
from workers.worker import Worker


async def test_duplicate_enqueue_runs_once(settings, make_router, make_queue):
    q = make_queue()
    w = Worker(settings, q, make_router(), worker_id="w-dup")
    job = repo.create_job("echo", {"n": 1}, client_id=None, max_attempts=3, source="test")
    await q.enqueue(job.id)
    await q.enqueue(job.id)
    assert await w.run_once(1) and await w.run_once(1)
    assert w.processed == 1
    with session_scope() as s:
        assert s.query(ExecutionLog).filter_by(job_id=job.id).count() == 1
    await q.close()


async def test_dead_worker_jobs_are_recovered(settings, make_router, make_queue):
    q, r = make_queue(), make_router()
    claimed = repo.create_job("echo", {"which": "running"}, client_id=None, max_attempts=3, source="test")
    reserved = repo.create_job("echo", {"which": "reserved"}, client_id=None, max_attempts=3, source="test")
    await q.enqueue(claimed.id)
    await q.enqueue(reserved.id)
    # worker A registers, reserves both, claims one in the DB, then "crashes" (heartbeat lapses)
    await q.register_worker("w-dead")
    await q.heartbeat("w-dead", {}, ttl=60)
    repo.heartbeat("w-dead", processed=0, failed=0)
    assert await q.reserve("w-dead", 1) == claimed.id
    assert await q.reserve("w-dead", 1) == reserved.id
    assert repo.claim_job(claimed.id)["status"] == "running"
    await q.r.delete(q._hb("w-dead"))  # TTL expiry
    assert (await q.depth())["pending"] == 0

    w = Worker(settings, q, r, worker_id="w-new")
    out = await HANDLERS["system.maintenance"](JobContext("sys", None, 1, w.worker_id, r, q, settings), {})
    assert out["dead_workers"] == ["w-dead"]
    assert out["requeued_from_dead"] == 2 and out["released_running"] == 1
    assert await w.run_once(1) and await w.run_once(1)
    assert repo.get_job(claimed.id)["status"] == "succeeded"
    assert repo.get_job(reserved.id)["status"] == "succeeded"
    with session_scope() as s:
        assert s.get(WorkerHealth, "w-dead").status == "dead"
    assert (await q.live_workers()) == ({}, [])
    await q.close()


async def test_live_worker_is_not_reaped(settings, make_router, make_queue):
    q = make_queue()
    w = Worker(settings, q, make_router(), worker_id="w-live")
    await w.start()
    try:
        alive, dead = await q.live_workers()
        assert "w-live" in alive and dead == []
        assert await q.reap_dead_workers() == {}
    finally:
        await w.stop()
    assert await q.live_workers() == ({}, [])  # unregistered on clean stop
    await q.close()


async def test_stuck_jobs_are_reenqueued(settings, make_router, make_queue):
    """Job row exists but its id never reached Redis (e.g. Redis outage during enqueue)."""
    q, r = make_queue(), make_router()
    job = repo.create_job("echo", {}, client_id=None, max_attempts=3, source="test")
    with session_scope() as s:
        s.get(Job, job.id).run_at = utcnow() - timedelta(minutes=30)
    w = Worker(settings, q, r, worker_id="w-stuck")
    out = await HANDLERS["system.maintenance"](JobContext("sys", None, 1, w.worker_id, r, q, settings), {})
    assert out["requeued_stuck"] == 1
    assert await w.run_once(1)
    assert repo.get_job(job.id)["status"] == "succeeded"
    await q.close()


async def test_graceful_stop_returns_inflight_job(settings, make_router, make_queue):
    q = make_queue()
    started = asyncio.Event()

    async def slow(ctx, payload):
        started.set()
        await asyncio.sleep(30)
        return {}

    HANDLERS["test.slow"] = slow
    try:
        job = repo.create_job("test.slow", {}, client_id=None, max_attempts=3, source="test")
        await q.enqueue(job.id)
        w = Worker(settings, q, make_router(), worker_id="w-stop")
        await w.start()
        await asyncio.wait_for(started.wait(), 5)
        await w.stop()
        assert repo.get_job(job.id)["status"] == "retrying"
        assert (await q.depth())["pending"] == 1  # handed back for the next worker
        assert w._tasks == [] and not w._inflight
    finally:
        HANDLERS.pop("test.slow")
        await q.close()


async def test_concurrency_uses_one_poller(settings, make_router, make_queue):
    """N jobs run in parallel while a single dispatcher does the Redis reads."""
    from dataclasses import replace

    q = make_queue()
    running, peak = 0, 0
    gate = asyncio.Event()

    async def par(ctx, payload):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await gate.wait()
        running -= 1
        return {}

    HANDLERS["test.par"] = par
    try:
        ids = [repo.create_job("test.par", {}, client_id=None, max_attempts=1, source="t").id for _ in range(5)]
        for i in ids:
            await q.enqueue(i)
        w = Worker(replace(settings, worker_concurrency=3), q, make_router(), worker_id="w-par")
        await w.start()
        for _ in range(100):
            if peak == 3:
                break
            await asyncio.sleep(0.05)
        assert peak == 3  # never more than WORKER_CONCURRENCY at once
        gate.set()
        for _ in range(100):
            if all(repo.get_job(i)["status"] == "succeeded" for i in ids):
                break
            await asyncio.sleep(0.05)
        assert all(repo.get_job(i)["status"] == "succeeded" for i in ids)
        await w.stop()
    finally:
        HANDLERS.pop("test.par")
        await q.close()


async def test_delayed_job_promotion_and_next_due(settings, make_queue):
    q = make_queue()
    await q.enqueue("later", delay=60)
    await q.enqueue("soon", delay=1.0)
    assert await q.promote_due() == 0
    assert q.next_due_at is not None and q.next_due_at - __import__("time").time() <= 1.0
    await asyncio.sleep(1.1)
    assert await q.promote_due() == 1
    assert (await q.depth()) == {"pending": 1, "delayed": 1}
    assert 50 < q.next_due_at - __import__("time").time() <= 60  # worker will wake for "later"
    await q.close()


async def test_maintenance_snapshots_live_workers(settings, make_router, make_queue):
    from dataclasses import replace

    q, r = make_queue(), make_router()
    w = Worker(replace(settings, health_persist_interval=0), q, r, worker_id="w-snap")
    await w.start()
    try:
        w.processed = 7
        await w._heartbeat()  # Redis only (persist interval 0)
        with session_scope() as s:
            assert s.get(WorkerHealth, "w-snap").jobs_processed == 0  # DB untouched by heartbeat
        out = await HANDLERS["system.maintenance"](JobContext("sys", None, 1, "x", r, q, settings), {})
        assert out["workers_snapshotted"] == 1
        with session_scope() as s:
            assert s.get(WorkerHealth, "w-snap").jobs_processed == 7
    finally:
        await w.stop()
        await q.close()


def test_idle_pool_is_disposed(settings, monkeypatch):
    """Pooled connections are closed after DB_IDLE_DISPOSE_SECONDS idle (serverless Postgres)."""
    import time as _time

    from sqlalchemy import event, text

    from db import session as dbs

    engine = dbs.get_engine()
    event.listen(engine, "checkout", dbs._touch)
    monkeypatch.setattr(dbs, "_idle_dispose", 1)
    with dbs.session_scope() as s:
        s.execute(text("SELECT 1"))
    assert engine.pool.checkedin() >= 1
    assert dbs.dispose_if_idle() is False  # used just now
    monkeypatch.setattr(dbs, "_last_checkout", _time.monotonic() - 5)
    assert dbs.dispose_if_idle() is True
    assert dbs.dispose_if_idle() is False  # nothing left to close
    with dbs.session_scope() as s:  # pool transparently reconnects afterwards
        assert s.execute(text("SELECT 1")).scalar() == 1
