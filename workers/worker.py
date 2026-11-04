"""Async worker: pulls job ids from Redis, runs handlers, records results in Postgres.

Embedded in the web process by default (RUN_EMBEDDED_WORKER=true) so one free container
does everything. Can also run standalone:

    python -m workers.worker            # run forever
    python -m workers.worker --drain    # process until the queue is empty, then exit
                                        # (used as a GitHub Actions fallback runner)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import socket
import time
import traceback
import uuid
from datetime import timedelta

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
        self.started_monotonic = time.monotonic()
        self.last_heartbeat_ok: float | None = None
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self._heartbeat()  # register before reserving anything
        await self._recover_dead_workers()
        n = max(1, self.settings.worker_concurrency)
        self._tasks = [asyncio.create_task(self._loop(i), name=f"worker-loop-{i}") for i in range(n)]
        self._tasks.append(asyncio.create_task(self._heartbeat_loop(), name="worker-heartbeat"))
        log.info("worker %s started (%d loops, providers=%s)", self.worker_id, n, self.router.configured)

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        # Anything reserved but unfinished goes straight back to pending.
        try:
            await self.queue.requeue_worker(self.worker_id)
            await self._heartbeat(status="stopped")
        except Exception:
            log.exception("error during worker shutdown")
        log.info("worker %s stopped", self.worker_id)

    @property
    def alive(self) -> bool:
        return bool(self._tasks) and not all(t.done() for t in self._tasks)

    # ---------------------------------------------------------------- loops
    async def _loop(self, idx: int) -> None:
        errors = 0
        while not self._stop.is_set():
            try:
                await self.run_once(timeout=self.settings.worker_poll_timeout)
                errors = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                errors += 1
                delay = min(60.0, 2 ** min(errors, 6)) * (0.5 + random.random() / 2)
                log.exception("worker loop %d error; backing off %.1fs", idx, delay)
                await asyncio.sleep(delay)

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.settings.heartbeat_interval)
            try:
                await self._heartbeat()
            except Exception:
                log.exception("heartbeat failed")

    async def _heartbeat(self, status: str = "alive") -> None:
        await asyncio.to_thread(repo.heartbeat, self.worker_id, processed=self.processed,
                                failed=self.failed, status=status)
        self.last_heartbeat_ok = time.monotonic()

    async def _recover_dead_workers(self) -> None:
        stale = timedelta(seconds=max(3 * self.settings.heartbeat_interval, 180))
        try:
            for wid in await asyncio.to_thread(repo.dead_workers, stale):
                n = await self.queue.requeue_worker(wid)
                log.warning("recovered %d job(s) from dead worker %s", n, wid)
        except Exception:
            log.exception("dead-worker recovery failed")

    # ------------------------------------------------------------- one job
    async def run_once(self, timeout: int = 1) -> bool:
        await self.queue.promote_due()
        job_id = await self.queue.reserve(self.worker_id, timeout)
        if not job_id:
            return False
        try:
            await self.process(job_id)
        finally:
            await self.queue.ack(self.worker_id, job_id)
        return True

    async def process(self, job_id: str) -> None:
        job = await asyncio.to_thread(repo.claim_job, job_id)
        if job is None:
            log.debug("job %s not claimable (duplicate or finished); skipping", job_id)
            return
        attempt, client_id = job["attempts"], job["client_id"]
        fn = HANDLERS.get(job["type"])
        ctx = JobContext(job_id, client_id, attempt, self.worker_id, self.router, self.queue, self.settings)
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
            permanent = isinstance(e, PermanentError) or attempt >= job["max_attempts"]
            if permanent:
                self.failed += 1
                log.warning("job %s failed permanently after %d attempt(s): %s", job_id, attempt, err)
                await asyncio.to_thread(repo.finish_job, job_id, status=JobStatus.FAILED,
                                        worker_id=self.worker_id, attempt=attempt, client_id=client_id,
                                        duration_ms=ms, error=err + "\n" + traceback.format_exc(limit=5))
            else:
                delay = min(600.0, 5 * 2 ** (attempt - 1)) * (0.75 + random.random() / 2)
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
    queue = TaskQueue.from_url(settings.redis_url, settings.queue_prefix, settings.worker_poll_timeout)
    router = LLMRouter.from_settings(settings, usage_hook=lambda r: asyncio.to_thread(repo.record_usage, r))
    worker = Worker(settings, queue, router)
    try:
        if drain:
            await worker._heartbeat()
            await worker._recover_dead_workers()
            deadline = time.monotonic() + max_seconds
            while time.monotonic() < deadline and await worker.run_once(timeout=2):
                pass
            await worker.queue.requeue_worker(worker.worker_id)
            await worker._heartbeat(status="stopped")
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
