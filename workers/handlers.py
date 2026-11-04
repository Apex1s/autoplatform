"""Job handlers. Register new job types with @handler("name")."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable

from core.llm_router import AllProvidersFailed, LLMRouter
from core.queue import TaskQueue
from db import repo


class PermanentError(Exception):
    """Raise from a handler to fail the job without retrying."""


@dataclass
class JobContext:
    job_id: str
    client_id: str | None
    attempt: int
    worker_id: str
    router: LLMRouter
    queue: TaskQueue
    settings: Any


Handler = Callable[[JobContext, dict], Awaitable[dict]]
HANDLERS: dict[str, Handler] = {}
SYSTEM_PREFIX = "system."


def handler(name: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[name] = fn
        return fn
    return deco


def client_job_types() -> list[str]:
    return sorted(n for n in HANDLERS if not n.startswith(SYSTEM_PREFIX))


# ------------------------------------------------------------ client jobs
@handler("echo")
async def echo(ctx: JobContext, payload: dict) -> dict:
    if payload.get("fail_times", 0) >= ctx.attempt:  # test hook for retry behaviour
        raise RuntimeError(f"simulated failure on attempt {ctx.attempt}")
    return {"echo": payload, "attempt": ctx.attempt}


@handler("llm.complete")
async def llm_complete(ctx: JobContext, payload: dict) -> dict:
    messages = payload.get("messages")
    if not messages:
        prompt = payload.get("prompt")
        if not prompt:
            raise PermanentError("payload needs 'prompt' or 'messages'")
        messages = [{"role": "user", "content": str(prompt)}]
        if payload.get("system"):
            messages.insert(0, {"role": "system", "content": str(payload["system"])})
    if not ctx.router.configured:
        raise PermanentError("no LLM providers configured (set GROQ_API_KEY and/or GEMINI_API_KEY)")
    try:
        res = await ctx.router.complete(
            messages,
            max_tokens=min(int(payload.get("max_tokens", 512)), 4096),
            temperature=float(payload.get("temperature", 0.2)),
            client_id=ctx.client_id, job_id=ctx.job_id,
        )
    except AllProvidersFailed as e:
        raise RuntimeError(str(e)) from e  # retryable at the job level
    return res.to_dict()


@handler("webhook.event")
async def webhook_event(ctx: JobContext, payload: dict) -> dict:
    """Generic signed-webhook sink. If the sender includes `llm_prompt`, it's answered too."""
    body = payload.get("body")
    out: dict[str, Any] = {
        "received": True,
        "event": payload.get("event"),
        "keys": sorted(body.keys())[:50] if isinstance(body, dict) else None,
    }
    if isinstance(body, dict) and body.get("llm_prompt") and ctx.router.configured:
        res = await ctx.router.complete([{"role": "user", "content": str(body["llm_prompt"])}],
                                        max_tokens=256, client_id=ctx.client_id, job_id=ctx.job_id)
        out["llm"] = {"text": res.text, "provider": res.provider}
    return out


# ------------------------------------------------------------ system jobs
@handler("system.maintenance")
async def maintenance(ctx: JobContext, payload: dict) -> dict:
    s = ctx.settings
    stale = timedelta(seconds=max(3 * s.heartbeat_interval, 180))
    dead = await asyncio.to_thread(repo.dead_workers, stale)
    requeued_from_dead = 0
    for wid in dead:
        requeued_from_dead += await ctx.queue.requeue_worker(wid)
    stuck = await asyncio.to_thread(repo.stuck_jobs, timedelta(minutes=10),
                                    timedelta(seconds=3 * s.job_timeout))
    for jid in stuck:
        await ctx.queue.enqueue(jid)  # duplicates are harmless: claim_job is atomic
    promoted = await ctx.queue.promote_due()
    pruned = await asyncio.to_thread(repo.prune, s.log_retention_days)
    return {"dead_workers": dead, "requeued_from_dead": requeued_from_dead,
            "requeued_stuck": len(stuck), "promoted": promoted, "pruned": pruned}


@handler("system.provider_probe")
async def provider_probe(ctx: JobContext, payload: dict) -> dict:
    """Ping each provider individually so /metrics shows per-provider health."""
    results = {}
    for name in ctx.router.configured:
        try:
            r = await ctx.router.complete([{"role": "user", "content": "Reply with the single word OK."}],
                                          max_tokens=5, only=[name], job_id=ctx.job_id)
            results[name] = {"ok": True, "latency_ms": r.latency_ms, "model": r.model}
        except AllProvidersFailed as e:
            results[name] = {"ok": False, "error": str(e)[:300]}
    return {"providers": results}
