"""Synchronous DB operations shared by the API and workers (call via asyncio.to_thread)."""
from __future__ import annotations

import hashlib
import secrets
import socket
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, text, update

from db.session import session_scope
from models import ApiUsage, Client, ExecutionLog, Job, JobStatus, WorkerHealth, utcnow


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def job_to_dict(j: Job) -> dict[str, Any]:
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {
        "id": j.id, "client_id": j.client_id, "type": j.type, "status": j.status,
        "attempts": j.attempts, "max_attempts": j.max_attempts, "source": j.source,
        "payload": j.payload, "result": j.result, "error": j.error,
        "created_at": iso(j.created_at), "run_at": iso(j.run_at),
        "started_at": iso(j.started_at), "finished_at": iso(j.finished_at),
    }


# ----------------------------------------------------------------- clients
def create_client(slug: str, name: str) -> tuple[Client, str]:
    api_key = "ap_" + secrets.token_urlsafe(32)
    with session_scope() as s:
        c = Client(slug=slug, name=name, api_key_hash=hash_key(api_key),
                   webhook_secret=secrets.token_hex(32))
        s.add(c)
    return c, api_key


def client_by_api_key(api_key: str) -> Client | None:
    with session_scope() as s:
        return s.scalar(select(Client).where(Client.api_key_hash == hash_key(api_key),
                                             Client.active.is_(True)))


def client_by_slug(slug: str) -> Client | None:
    with session_scope() as s:
        return s.scalar(select(Client).where(Client.slug == slug, Client.active.is_(True)))


def list_clients() -> list[dict[str, Any]]:
    with session_scope() as s:
        return [{"id": c.id, "slug": c.slug, "name": c.name, "active": c.active,
                 "created_at": c.created_at.isoformat()}
                for c in s.scalars(select(Client).order_by(Client.created_at))]


def deactivate_client(slug: str) -> bool:
    with session_scope() as s:
        return s.execute(update(Client).where(Client.slug == slug, Client.active.is_(True))
                         .values(active=False)).rowcount == 1


# -------------------------------------------------------------------- jobs
def create_job(type_: str, payload: dict, *, client_id: str | None, max_attempts: int,
               source: str, delay: float = 0) -> Job:
    with session_scope() as s:
        j = Job(type=type_, payload=payload, client_id=client_id, max_attempts=max_attempts,
                source=source, run_at=utcnow() + timedelta(seconds=delay))
        s.add(j)
    return j


def get_job(job_id: str, client_id: str | None = None) -> dict[str, Any] | None:
    with session_scope() as s:
        j = s.get(Job, job_id)
        if j is None or (client_id is not None and j.client_id != client_id):
            return None
        return job_to_dict(j)


def list_jobs(client_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.scalars(select(Job).where(Job.client_id == client_id)
                         .order_by(Job.created_at.desc()).limit(limit))
        return [job_to_dict(j) for j in rows]


def claim_job(job_id: str) -> dict[str, Any] | None:
    """Atomically move a job to running. Returns None if someone else has it / it's done."""
    with session_scope() as s:
        res = s.execute(
            update(Job)
            .where(Job.id == job_id, Job.status.in_(JobStatus.CLAIMABLE))
            .values(status=JobStatus.RUNNING, started_at=utcnow(), attempts=Job.attempts + 1)
        )
        if res.rowcount != 1:
            return None
        return job_to_dict(s.get(Job, job_id))


def finish_job(job_id: str, *, status: str, worker_id: str, attempt: int, duration_ms: int,
               result: dict | None = None, error: str | None = None,
               retry_delay: float = 0, client_id: str | None = None) -> None:
    with session_scope() as s:
        values: dict[str, Any] = {"status": status, "error": error}
        if status == JobStatus.RETRYING:
            values["run_at"] = utcnow() + timedelta(seconds=retry_delay)
        else:
            values["finished_at"] = utcnow()
            values["result"] = result
        s.execute(update(Job).where(Job.id == job_id).values(**values))
        s.add(ExecutionLog(job_id=job_id, client_id=client_id, worker_id=worker_id,
                           attempt=attempt, status=status, duration_ms=duration_ms,
                           error=(error or None) and error[:4000]))


def job_counts() -> dict[str, int]:
    with session_scope() as s:
        rows = s.execute(select(Job.status, func.count()).group_by(Job.status)).all()
    counts = {st: 0 for st in JobStatus.ALL}
    counts.update({st: n for st, n in rows})
    return counts


def stuck_jobs(older_than: timedelta, running_older_than: timedelta) -> list[str]:
    """Jobs that should be in Redis but probably aren't (Redis flush, crash mid-job)."""
    now = utcnow()
    with session_scope() as s:
        waiting = s.scalars(select(Job.id).where(Job.status.in_(JobStatus.CLAIMABLE),
                                                 Job.run_at < now - older_than).limit(500)).all()
        running = s.scalars(select(Job.id).where(Job.status == JobStatus.RUNNING,
                                                 Job.started_at < now - running_older_than)
                            .limit(500)).all()
        if running:
            s.execute(update(Job).where(Job.id.in_(running))
                      .values(status=JobStatus.RETRYING, error="recovered: worker lost mid-run"))
    return list(waiting) + list(running)


def prune(retention_days: int) -> dict[str, int]:
    cutoff = utcnow() - timedelta(days=retention_days)
    with session_scope() as s:
        a = s.execute(delete(ExecutionLog).where(ExecutionLog.created_at < cutoff)).rowcount
        b = s.execute(delete(ApiUsage).where(ApiUsage.created_at < cutoff)).rowcount
        c = s.execute(delete(Job).where(Job.status.in_([JobStatus.SUCCEEDED, JobStatus.FAILED]),
                                        Job.finished_at < cutoff)).rowcount
    return {"execution_logs": a, "api_usage": b, "jobs": c}


# --------------------------------------------------------------- api usage
def record_usage(rec: dict[str, Any]) -> None:
    with session_scope() as s:
        s.add(ApiUsage(provider=rec["provider"], model=rec.get("model") or "", client_id=rec.get("client_id"),
                       job_id=rec.get("job_id"), success=bool(rec["success"]),
                       status_code=rec.get("status_code"), error_kind=rec.get("error_kind"),
                       prompt_tokens=rec.get("prompt_tokens", 0),
                       completion_tokens=rec.get("completion_tokens", 0),
                       latency_ms=rec.get("latency_ms", 0)))


def usage_summary(hours: int = 24) -> list[dict[str, Any]]:
    since = utcnow() - timedelta(hours=hours)
    with session_scope() as s:
        rows = s.execute(
            select(ApiUsage.provider, ApiUsage.success, func.count(),
                   func.coalesce(func.sum(ApiUsage.prompt_tokens), 0),
                   func.coalesce(func.sum(ApiUsage.completion_tokens), 0))
            .where(ApiUsage.created_at >= since)
            .group_by(ApiUsage.provider, ApiUsage.success)
        ).all()
    return [{"provider": p, "success": bool(ok), "calls": n, "prompt_tokens": int(pt),
             "completion_tokens": int(ct)} for p, ok, n, pt, ct in rows]


# ---------------------------------------------------------- worker health
def heartbeat(worker_id: str, *, processed: int, failed: int, status: str = "alive") -> None:
    with session_scope() as s:
        w = s.get(WorkerHealth, worker_id)
        if w is None:
            w = WorkerHealth(worker_id=worker_id, hostname=socket.gethostname())
            s.add(w)
        w.status = status
        w.jobs_processed = processed
        w.jobs_failed = failed
        w.last_heartbeat = utcnow()


def snapshot_workers(alive: dict[str, dict]) -> int:
    """Persist live heartbeats (read from Redis) in one DB round trip, during maintenance."""
    from datetime import datetime, timezone

    with session_scope() as s:
        for wid, hb in alive.items():
            w = s.get(WorkerHealth, wid)
            if w is None:
                w = WorkerHealth(worker_id=wid, hostname=str(hb.get("host") or "?"))
                s.add(w)
            w.status = "alive"
            w.jobs_processed = int(hb.get("processed", 0))
            w.jobs_failed = int(hb.get("failed", 0))
            if hb.get("ts"):
                w.last_heartbeat = datetime.fromtimestamp(float(hb["ts"]), tz=timezone.utc)
    return len(alive)


def mark_workers(worker_ids: list[str], status: str) -> None:
    if not worker_ids:
        return
    with session_scope() as s:
        s.execute(update(WorkerHealth).where(WorkerHealth.worker_id.in_(worker_ids)).values(status=status))


def release_jobs(job_ids: list[str], reason: str) -> int:
    """Running jobs whose worker died go back to claimable (at-least-once delivery)."""
    if not job_ids:
        return 0
    with session_scope() as s:
        return s.execute(update(Job).where(Job.id.in_(job_ids), Job.status == JobStatus.RUNNING)
                         .values(status=JobStatus.RETRYING, error=reason)).rowcount


def workers(limit: int = 20) -> list[dict[str, Any]]:
    now = utcnow()
    with session_scope() as s:
        rows = s.scalars(select(WorkerHealth).order_by(WorkerHealth.last_heartbeat.desc()).limit(limit))
        out = []
        for w in rows:
            hb = w.last_heartbeat if w.last_heartbeat.tzinfo else w.last_heartbeat.replace(tzinfo=now.tzinfo)
            out.append({"worker_id": w.worker_id, "hostname": w.hostname, "status": w.status,
                        "jobs_processed": w.jobs_processed, "jobs_failed": w.jobs_failed,
                        "last_heartbeat": hb.isoformat(),
                        "age_seconds": round((now - hb).total_seconds(), 1)})
        return out


def db_ping() -> bool:
    with session_scope() as s:
        return s.execute(text("SELECT 1")).scalar() == 1

