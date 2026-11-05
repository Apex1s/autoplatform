"""Async worker: pulls job ids from Redis, runs handlers, records results in Postgres.

One dispatcher loop does the (blocking) Redis reads and hands jobs to up to
WORKER_CONCURRENCY concurrent tasks, so Redis command usage does not grow with concurrency.
Liveness heartbeats go to Redis; Postgres only gets a health snapshot every
WORKER_HEALTH_PERSIST_SECONDS, which lets serverless Postgres (Neon) scale to zero when idle.

Embedded in the web process by default (RUN_EMBEDDED_WORKER=true). Standalone:

    python -m workers.worker            # run forever
    python -m workers.worker --drain    # process until the queue is empty, then exit
                                        # (GitHub Actions fallback runner)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import random
import socket
import time
import traceback
import uuid

from app.config import Settings, get_settings
from core.llm_router import LLMRouter
from core.queue import TaskQueue
from db import repo
from models import JobStatus
from workers.handlers import HANDLERS, JobContext, PermanentError

log = logging.getLogger("autoplatform.worker")


class Worker:
    def __init__(self, settings: Settings, queue: TaskQueue, router: LLMRouter,
                 worker_id: str | None = None):
        self.settings = settings
        self.queue = queue
        self.router = router
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.processed = 0
        self.failed = 0
        self.last_heartbeat_ok: float | None = None
        self._last_persist = 0.0
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._inflight: set[asyncio.Task] = set()
        self._slots: asyncio.Semaphore | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._slots = asyncio.Semaphore(max(1, self.settings.worker_concurrency))
        await self.queue.register_worker(self.worker_id)
        await self._heartbeat(persist=True)
        await self.recover_dead_workers()
        self._tasks = [asyncio.create_task(self._dispatch_loop(), name="worker-dispatch"),
                       asyncio.create_task(self._heartbeat_loop(), name="worker-heartbeat")]
        log.info("worker %s started (concurrency=%d, queue=%s, providers=%s)", self.worker_id,
                 self.settings.worker_concurrency, self.queue.transport, self.router.configured)

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        inflight = list(self._inflight)
        for t in inflight:  # handlers see CancelledError and hand their job back
            t.cancel()
        await asyncio.gather(*inflight, return_exceptions=True)
        try:
            await self.queue.requeue_worker(self.worker_id)  # reserved but never started
            await self.queue.unregister_worker(self.worker_id)
            await asyncio.to_thread(repo.heartbeat, self.worker_id, processed=self.processed,
                                    failed=self.failed, status="stopped")
        except Exception:
            log.exception("error during worker shutdown")
        log.info("worker %s stopped", self.worker_id)

    @property
    def alive(self) -> bool:
        return bool(self._tasks) and not self._tasks[0].done()

    # ---------------------------------------------------------------- loops
    async def _dispatch_loop(self) -> None:
        assert self._slots is not None
        errors = 0
        while not self._stop.is_set():
            await self._slots.acquire()  # never reserve more jobs than we can run
            try:
                job_id = await self.reserve_next()
            except asyncio.CancelledError:
                self._slots.release()
                raise
            except Exception:
                self._slots.release()
                errors += 1
                delay = min(60.0, 2 ** min(errors, 6)) * (0.5 + random.random() / 2)
                log.exception("dispatch error; backing off %.1fs", delay)
                await asyncio.sleep(delay)
                continue
            errors = 0
            if job_id is None:
                self._slots.release()
                continue
            task = asyncio.create_task(self._run_and_ack(job_id), name=f"job-{job_id}")
            self._inflight.add(task)
            task.add_done_callback(self._job_done)

    def _job_done(self, task: asyncio.Task) -> None:
        self._inflight.discard(task)
        if self._slots is not None:
            self._slots.release()
        if not task.cancelled() and task.exception() is not None:
            log.error("job task crashed", exc_info=task.exception())

    async def _heartbeat_loop(self) -> None:
        from db.session import dispose_if_idle

        while not self._stop.is_set():
            await asyncio.sleep(min(self.settings.heartbeat_interval, 60))
            try:
                if time.monotonic() - (self.last_heartbeat_ok or 0) >= self.settings.heartbeat_interval:
                    await self._heartbeat()
                if dispose_if_idle():
                    log.info("closed idle DB connections (lets serverless Postgres scale to zero)")
            except Exception:
                log.exception("heartbeat failed")

    async def _heartbeat(self, persist: bool = False) -> None:
        s = self.settings
        await self.queue.heartbeat(
            self.worker_id,
            {"host": socket.gethostname(), "processed": self.processed, "failed": self.failed,
             "inflight": len(self._inflight)},
            ttl=3 * s.heartbeat_interval + 30)
        self.last_heartbeat_ok = time.monotonic()
        periodic = s.health_persist_interval > 0 and time.monotonic() - self._last_persist >= s.health_persist_interval
        if persist or periodic:  # 0 = only at start/stop + system.maintenance snapshots
            await asyncio.to_thread(repo.heartbeat, self.worker_id, processed=self.processed,
                                    failed=self.failed)
            self._last_persist = time.monotonic()

    async def recover_dead_workers(self) -> dict[str, list[str]]:
        try:
            reaped = await self.queue.reap_dead_workers()
            if reaped:
                await asyncio.to_thread(repo.mark_workers, list(reaped), "dead")
                for wid, ids in reaped.items():
                    released = await asyncio.to_thread(repo.release_jobs, ids, f"recovered from dead worker {wid}")
                    log.warning("worker %s is dead: requeued %d job(s), released %d", wid, len(ids), released)
            return reaped
        except Exception:
            log.exception("dead-worker recovery failed")
            return {}

    # ------------------------------------------------------------- one job
    async def reserve_next(self, max_timeout: int | None = None) -> str | None:
        await self.queue.promote_due()
        timeout = self.settings.worker_poll_timeout if max_timeout is None else max_timeout
        if self.queue.next_due_at is not None:  # wake up in time for the next delayed job
            timeout = min(timeout, max(1, math.ceil(self.queue.next_due_at - time.time())))
        t0 = time.monotonic()
        job_id = await self.queue.reserve(self.worker_id, timeout)
        if job_id is None:
            # Some backends (fakeredis, proxies) return instantly instead of blocking; never
            # spin hot: it starves the event loop and burns Upstash's command quota.
            remaining = timeout - (time.monotonic() - t0)
            if remaining > 0.05:
                await asyncio.sleep(min(remaining, 1.0) if timeout <= 2 else remaining)
        return job_id

    async def run_once(self, timeout: int = 1) -> bool:
        """Reserve and run a single job inline (tests, drain mode)."""
        job_id = await self.reserve_next(max_timeout=timeout)
        if job_id is None:
            return False
        await self._run_and_ack(job_id)
        return True

    async def _run_and_ack(self, job_id: str) -> None:
        try:
            await self.process(job_id)
        finally:
            await self.queue.ack(self.worker_id, job_id)

    async def process(self, job_id: str) -> None:
        job = await asyncio.to_thread(repo.claim_job, job_id)
        if job is None:
            log.debug("job %s not claimable (duplicate or finished); skipping", job_id)
            return
        attempt, client_id = job["attempts"], job["client_id"]
        fn = HANDLERS.get(job["type"])
        ctx = JobContext(job_id, client_id, attempt, self.worker_id, self.router, self.queue, self.settings,
                         created_at=job.get("created_at"))
        started = time.perf_counter()
        try:
            if fn is None:
                raise PermanentError(f"unknown job type {job['type']!r}")
            result = await asyncio.wait_for(fn(ctx, job["payload"] or {}), self.settings.job_timeout)
        except asyncio.CancelledError:
            # Shutdown mid-job: hand it back so another worker (or our next boot) runs it.
            await asyncio.to_thread(repo.finish_job, job_id, status=JobStatus.RETRYING,
                                    worker_id=self.worker_id, attempt=attempt, client_id=client_id,
                                    duration_ms=int((time.perf_counter() - started) * 1000),
                                    error="interrupted by worker shutdown")
            await self.queue.enqueue(job_id)
            raise
        except Exception as e:
            ms = int((time.perf_counter() - started) * 1000)
            err = f"{type(e).__name__}: {e}"
            if isinstance(e, PermanentError) or attempt >= job["max_attempts"]:
                self.failed += 1
                log.warning("job %s failed permanently after %d attempt(s): %s", job_id, attempt, err)
                await asyncio.to_thread(repo.finish_job, job_id, status=JobStatus.FAILED,
                                        worker_id=self.worker_id, attempt=attempt, client_id=client_id,
                                        duration_ms=ms, error=err + "\n" + traceback.format_exc(limit=5))
            else:
                delay = min(600.0, self.settings.job_retry_base * 2 ** (attempt - 1)) * (0.75 + random.random() / 2)
                log.info("job %s attempt %d failed (%s); retrying in %.0fs", job_id, attempt, err, delay)
                await asyncio.to_thread(repo.finish_job, job_id, status=JobStatus.RETRYING,
                                        worker_id=self.worker_id, attempt=attempt, client_id=client_id,
                                        duration_ms=ms, error=err, retry_delay=delay)
                await self.queue.enqueue(job_id, delay=delay)
            return
        ms = int((time.perf_counter() - started) * 1000)
        self.processed += 1
        await asyncio.to_thread(repo.finish_job, job_id, status=JobStatus.SUCCEEDED,
                                worker_id=self.worker_id, attempt=attempt, client_id=client_id,
                                duration_ms=ms, result=result)
        log.info("job %s (%s) succeeded in %dms", job_id, job["type"], ms)


# --------------------------------------------------------------------- CLI
async def _main(drain: bool, max_seconds: int) -> int:
    from db.migrate import run_migrations

    settings = get_settings()
    await asyncio.to_thread(run_migrations)
    queue = TaskQueue.from_settings(settings)
    router = LLMRouter.from_settings(settings, usage_hook=lambda r: asyncio.to_thread(repo.record_usage, r))
    worker = Worker(settings, queue, router)
    try:
        if drain:
            await queue.register_worker(worker.worker_id)
            await worker._heartbeat(persist=True)
            await worker.recover_dead_workers()
            deadline = time.monotonic() + max_seconds
            while time.monotonic() < deadline and await worker.run_once(timeout=2):
                pass
            await queue.requeue_worker(worker.worker_id)
            await queue.unregister_worker(worker.worker_id)
            await asyncio.to_thread(repo.heartbeat, worker.worker_id, processed=worker.processed,
                                    failed=worker.failed, status="stopped")
            print(f"drained: processed={worker.processed} failed={worker.failed} "
                  f"remaining={await queue.depth()}")
        else:
            await worker.start()
            stop = asyncio.Event()
            try:
                import signal

                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
            await stop.wait()
            await worker.stop()
    finally:
        await router.aclose()
        await queue.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--drain", action="store_true", help="process until queue is empty, then exit")
    parser.add_argument("--max-seconds", type=int, default=240)
    args = parser.parse_args()
    logging.basicConfig(level=get_settings().log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    raise SystemExit(asyncio.run(_main(args.drain, args.max_seconds)))
