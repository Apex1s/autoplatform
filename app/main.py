"""FastAPI app: webhook ingestion, client task API, cron triggers, health and metrics."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from core import github_oidc
from core.llm_router import LLMRouter
from core.queue import TaskQueue
from db import repo
from db.migrate import run_migrations
from models import Client
from workers.handlers import HANDLERS, SYSTEM_PREFIX, client_job_types
from workers.worker import Worker

VERSION = "1.0.0"
log = logging.getLogger("autoplatform.api")


# ----------------------------------------------------------------- schemas
class TaskIn(BaseModel):
    type: str = Field(examples=["llm.complete"])
    payload: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    delay_seconds: int = Field(default=0, ge=0, le=7 * 86400)


class ClientIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,39}$")
    name: str = Field(min_length=1, max_length=200)


# -------------------------------------------------------------- middleware
class BodySizeLimit:
    """Rejects request bodies over max_bytes (Content-Length up front, chunked as it streams),
    so a single oversized webhook cannot exhaust a 512 MB free instance."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            return await JSONResponse({"detail": "payload too large"}, 413)(scope, receive, send)
        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise HTTPException(413, "payload too large")
            return message

        return await self.app(scope, limited_receive, send)


# --------------------------------------------------------------------- app
def create_app(settings: Settings | None = None, *, queue: TaskQueue | None = None,
               router: LLMRouter | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=settings.log_level,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
        if settings.auto_migrate:
            applied = await asyncio.to_thread(run_migrations)
            if applied:
                log.info("migrations applied: %s", applied)
        q = queue or TaskQueue.from_settings(settings)
        r = router or LLMRouter.from_settings(
            settings, usage_hook=lambda rec: asyncio.to_thread(repo.record_usage, rec))
        app.state.queue, app.state.router, app.state.worker = q, r, None
        app.state.started = time.time()
        if settings.run_embedded_worker:
            app.state.worker = Worker(settings, q, r)
            await app.state.worker.start()
        try:
            yield
        finally:
            if app.state.worker:
                await app.state.worker.stop()
            if router is None:
                await r.aclose()
            if queue is None:
                await q.close()

    app = FastAPI(title="AutoPlatform", version=VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(BodySizeLimit, max_bytes=settings.max_body_bytes)

    # ------------------------------------------------------------ auth deps
    def _bearer(authorization: str | None) -> str:
        if authorization and authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return ""

    def require_admin(authorization: str | None = Header(default=None)) -> None:
        if not settings.admin_token:
            raise HTTPException(503, "ADMIN_TOKEN not configured")
        if not hmac.compare_digest(_bearer(authorization), settings.admin_token):
            raise HTTPException(401, "invalid admin token")

    async def require_cron(authorization: str | None = Header(default=None)) -> None:
        token = _bearer(authorization)
        if settings.cron_oidc_repository and github_oidc.looks_like_jwt(token):
            try:
                claims = await asyncio.to_thread(
                    github_oidc.verify, token, repository=settings.cron_oidc_repository,
                    audience=settings.cron_oidc_audience, ref=settings.cron_oidc_ref or None)
            except github_oidc.OIDCError as e:
                log.warning("rejected cron OIDC token: %s", e)
                raise HTTPException(401, "invalid GitHub OIDC token") from None
            log.info("cron call from %s run %s", claims.get("workflow_ref"), claims.get("run_id"))
            return
        if not settings.cron_token:
            raise HTTPException(503, "no cron auth configured (CRON_TOKEN, ADMIN_TOKEN or CRON_OIDC_REPOSITORY)")
        if not token or not (hmac.compare_digest(token, settings.cron_token)
                             or (settings.admin_token and hmac.compare_digest(token, settings.admin_token))):
            raise HTTPException(401, "invalid cron token")

    async def require_client(x_api_key: str | None = Header(default=None)) -> Client:
        if not x_api_key:
            raise HTTPException(401, "missing X-API-Key")
        c = await asyncio.to_thread(repo.client_by_api_key, x_api_key)
        if c is None:
            raise HTTPException(401, "invalid API key")
        return c

    # -------------------------------------------------------------- helpers
    async def _enqueue(type_: str, payload: dict, *, client_id: str | None, source: str,
                       max_attempts: int | None = None, delay: float = 0,
                       idempotency_key: str | None = None) -> dict[str, Any]:
        q: TaskQueue = app.state.queue
        job = await asyncio.to_thread(repo.create_job, type_, payload, client_id=client_id,
                                      max_attempts=max_attempts or settings.job_max_attempts,
                                      source=source, delay=delay)
        if idempotency_key:
            scope = f"{client_id or 'system'}:{idempotency_key}"
            existing = await q.claim_idempotency(scope, job.id)
            if existing:
                await asyncio.to_thread(repo.finish_job, job.id, status="failed", worker_id="api",
                                        attempt=0, duration_ms=0, client_id=client_id,
                                        error=f"duplicate of {existing} (Idempotency-Key)")
                return {"job_id": existing, "status": "duplicate", "duplicate": True,
                        "status_url": f"{settings.public_base_url}/tasks/{existing}"}
        try:
            await q.enqueue(job.id, delay=delay)
        except Exception:
            # Row stays queued; system.maintenance re-enqueues it once Redis is back.
            log.exception("enqueue failed for %s; will be recovered by maintenance", job.id)
        return {"job_id": job.id, "status": "queued", "type": type_,
                "status_url": f"{settings.public_base_url}/tasks/{job.id}"}

    # --------------------------------------------------------------- routes
    @app.get("/")
    async def root():
        return {"service": "autoplatform", "version": VERSION, "docs": "/docs",
                "health": "/healthz", "metrics": "/metrics", "job_types": client_job_types()}

    @app.get("/livez")
    async def livez():
        return {"status": "alive"}

    @app.get("/healthz")
    async def healthz():
        checks: dict[str, Any] = {}
        try:
            checks["database"] = "ok" if await asyncio.wait_for(asyncio.to_thread(repo.db_ping), 10) else "fail"
        except Exception as e:
            checks["database"] = f"fail: {type(e).__name__}"
        try:
            await asyncio.wait_for(app.state.queue.ping(), 5)
            checks["redis"] = "ok"
        except Exception as e:
            checks["redis"] = f"fail: {type(e).__name__}"
        w: Worker | None = app.state.worker
        if w is None:
            checks["worker"] = "external"
        else:
            hb_age = time.monotonic() - (w.last_heartbeat_ok or 0)
            checks["worker"] = "ok" if w.alive and hb_age < 3 * settings.heartbeat_interval + 30 else "stalled"
        checks["llm_providers"] = app.state.router.configured or "none configured"
        critical_ok = checks["database"] == "ok" and checks["redis"] == "ok"
        status = "ok" if critical_ok and checks["worker"] in ("ok", "external") else (
            "degraded" if critical_ok else "down")
        body = {"status": status, "version": VERSION, "env": settings.app_env,
                "uptime_seconds": int(time.time() - app.state.started), "checks": checks}
        return JSONResponse(body, status_code=200 if critical_ok else 503)

    @app.get("/metrics")
    async def metrics(format: str = "prometheus", authorization: str | None = Header(default=None)):
        token = _bearer(authorization)
        admin_ok = bool(settings.admin_token) and hmac.compare_digest(token, settings.admin_token)
        if settings.metrics_token:
            if not (hmac.compare_digest(token, settings.metrics_token) or admin_ok):
                raise HTTPException(401, "invalid metrics token")
        elif settings.app_env == "production" and not settings.metrics_public and not admin_ok:
            raise HTTPException(401, "metrics need METRICS_TOKEN or ADMIN_TOKEN in production")
        counts, usage, history = await asyncio.gather(
            asyncio.to_thread(repo.job_counts), asyncio.to_thread(repo.usage_summary, 24),
            asyncio.to_thread(repo.workers))
        try:
            depth = await app.state.queue.depth()
            alive, dead = await app.state.queue.live_workers()
        except Exception:
            depth, alive, dead = {"pending": -1, "delayed": -1}, {}, []
        now = time.time()
        workers_ = [{"worker_id": wid, "status": "alive", "host": hb.get("host"),
                     "jobs_processed": hb.get("processed", 0), "jobs_failed": hb.get("failed", 0),
                     "inflight": hb.get("inflight", 0),
                     "heartbeat_age_seconds": round(now - hb.get("ts", now), 1)}
                    for wid, hb in alive.items()]
        router_stats = app.state.router.snapshot()
        data = {"uptime_seconds": int(time.time() - app.state.started), "jobs": counts,
                "queue": depth, "llm_since_boot": router_stats, "llm_usage_24h": usage,
                "workers": workers_, "workers_awaiting_reap": dead, "worker_history": history}
        if format == "json":
            return data
        lines = [f"autoplatform_uptime_seconds {data['uptime_seconds']}"]
        lines += [f'autoplatform_jobs{{status="{k}"}} {v}' for k, v in counts.items()]
        lines += [f'autoplatform_queue_depth{{queue="{k}"}} {v}' for k, v in depth.items()]
        for name, st in router_stats.items():
            for key in ("requests", "successes", "failures", "rate_limited", "timeouts",
                        "prompt_tokens", "completion_tokens"):
                lines.append(f'autoplatform_llm_{key}_total{{provider="{name}"}} {st[key]}')
            lines.append(f'autoplatform_llm_cooldown_seconds{{provider="{name}"}} {st["cooldown_remaining"]}')
        for u in usage:
            lbl = f'provider="{u["provider"]}",success="{str(u["success"]).lower()}"'
            lines.append(f"autoplatform_llm_calls_24h{{{lbl}}} {u['calls']}")
            lines.append(f"autoplatform_llm_tokens_24h{{{lbl}}} {u['prompt_tokens'] + u['completion_tokens']}")
        lines.append(f"autoplatform_workers_alive {len(workers_)}")
        for w in workers_:
            lbl = f'worker_id="{w["worker_id"]}"'
            lines.append(f"autoplatform_worker_up{{{lbl}}} 1")
            lines.append(f"autoplatform_worker_heartbeat_age_seconds{{{lbl}}} {w['heartbeat_age_seconds']}")
            lines.append(f"autoplatform_worker_jobs_processed{{{lbl}}} {w['jobs_processed']}")
            lines.append(f"autoplatform_worker_jobs_failed{{{lbl}}} {w['jobs_failed']}")
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    # ---- client task API (X-API-Key)
    @app.post("/webhook", status_code=202)
    async def webhook(task: TaskIn, client: Client = Depends(require_client),
                      idempotency_key: str | None = Header(default=None)):
        if task.type not in HANDLERS or task.type.startswith(SYSTEM_PREFIX):
            raise HTTPException(422, f"unknown job type; allowed: {client_job_types()}")
        return await _enqueue(task.type, task.payload, client_id=client.id, source="webhook",
                              max_attempts=task.max_attempts, delay=task.delay_seconds,
                              idempotency_key=idempotency_key)

    app.post("/tasks", status_code=202, include_in_schema=False)(webhook)  # alias

    @app.get("/tasks/{job_id}")
    async def get_task(job_id: str, client: Client = Depends(require_client)):
        job = await asyncio.to_thread(repo.get_job, job_id, client.id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job

    @app.get("/tasks")
    async def list_tasks(limit: int = 50, client: Client = Depends(require_client)):
        return await asyncio.to_thread(repo.list_jobs, client.id, max(1, min(limit, 200)))

    # ---- signed third-party webhooks (GitHub/Stripe-style HMAC-SHA256)
    @app.post("/webhook/{client_slug}", status_code=202)
    async def signed_webhook(client_slug: str, request: Request,
                             x_signature_256: str | None = Header(default=None),
                             x_hub_signature_256: str | None = Header(default=None),
                             x_event_type: str | None = Header(default=None),
                             x_github_event: str | None = Header(default=None),
                             idempotency_key: str | None = Header(default=None),
                             x_github_delivery: str | None = Header(default=None)):
        raw = await request.body()  # size already capped by BodySizeLimit
        client = await asyncio.to_thread(repo.client_by_slug, client_slug)
        sig = (x_signature_256 or x_hub_signature_256 or "").removeprefix("sha256=")
        if client is None or not sig:
            raise HTTPException(401, "invalid signature")
        expected = hmac.new(client.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(401, "invalid signature")
        try:
            body = await request.json()
        except Exception:
            body = {"raw": raw.decode(errors="replace")[:10000]}
        payload = {"event": x_event_type or x_github_event, "body": body}
        return await _enqueue("webhook.event", payload, client_id=client.id, source="signed_webhook",
                              idempotency_key=idempotency_key or x_github_delivery)

    # ---- cron triggers (GitHub Actions)
    CRON_JOBS = {"maintenance": "system.maintenance", "provider_probe": "system.provider_probe"}

    @app.post("/cron/{name}", status_code=202, dependencies=[Depends(require_cron)])
    async def cron(name: str):
        if name not in CRON_JOBS:
            raise HTTPException(404, f"unknown cron job; allowed: {sorted(CRON_JOBS)}")
        out = await _enqueue(CRON_JOBS[name], {}, client_id=None, source="cron", max_attempts=1)
        try:
            out["queue"] = await app.state.queue.depth()
        except Exception:
            pass
        return out

    @app.get("/cron/jobs/{job_id}", dependencies=[Depends(require_cron)])
    async def cron_job(job_id: str):
        job = await asyncio.to_thread(repo.get_job, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job

    # ---- admin
    @app.post("/admin/clients", status_code=201, dependencies=[Depends(require_admin)])
    async def create_client(body: ClientIn):
        if await asyncio.to_thread(repo.client_by_slug, body.slug):
            raise HTTPException(409, "slug already exists")
        c, api_key = await asyncio.to_thread(repo.create_client, body.slug, body.name)
        return {"id": c.id, "slug": c.slug, "name": c.name,
                "api_key": api_key, "webhook_secret": c.webhook_secret,
                "webhook_url": f"{settings.public_base_url}/webhook",
                "signed_webhook_url": f"{settings.public_base_url}/webhook/{c.slug}",
                "note": "api_key is shown once; store it now."}

    @app.delete("/admin/clients/{slug}", dependencies=[Depends(require_admin)])
    async def deactivate_client(slug: str):
        if not await asyncio.to_thread(repo.deactivate_client, slug):
            raise HTTPException(404, "client not found")
        return {"slug": slug, "active": False}

    @app.get("/admin/clients", dependencies=[Depends(require_admin)])
    async def clients():
        return await asyncio.to_thread(repo.list_clients)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> Response:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    return app


app = create_app()
