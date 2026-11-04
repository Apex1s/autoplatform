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
    reaped = await ctx.queue.reap_dead_workers()  # heartbeat expired -> requeue its reserved jobs
    released = 0
    if reaped:
        await asyncio.to_thread(repo.mark_workers, list(reaped), "dead")
        for wid, ids in reaped.items():
            released += await asyncio.to_thread(repo.release_jobs, ids, f"recovered from dead worker {wid}")
    stuck = await asyncio.to_thread(repo.stuck_jobs, timedelta(minutes=10),
                                    timedelta(seconds=3 * s.job_timeout))
    for jid in stuck:
        await ctx.queue.enqueue(jid)  # duplicates are harmless: claim_job is atomic
    promoted = await ctx.queue.promote_due()
    pruned = await asyncio.to_thread(repo.prune, s.log_retention_days)
    alive, _ = await ctx.queue.live_workers()
    snapshot = await asyncio.to_thread(repo.snapshot_workers, alive)  # worker_health in Postgres
    return {"dead_workers": sorted(reaped), "requeued_from_dead": sum(len(v) for v in reaped.values()),
            "released_running": released, "requeued_stuck": len(stuck), "promoted": promoted,
            "pruned": pruned, "workers_snapshotted": snapshot}


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


# ------------------------------------------------------- public API fetch
_FETCH_MAX_BYTES = 512_000


def _assert_public_https(url: str) -> None:
    """SSRF guard: only https to hosts that resolve exclusively to public IPs."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise PermanentError("only https:// URLs are allowed")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise RuntimeError(f"DNS lookup failed for {parts.hostname}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise PermanentError(f"refusing non-public address {ip} for {parts.hostname}")


@handler("http.fetch")
async def http_fetch(ctx: JobContext, payload: dict) -> dict:
    """GET a public JSON/text API (e.g. one from github.com/public-apis/public-apis).

    payload: {"url": "https://...", "params": {...}, "summarize": "optional LLM instruction"}
    """
    import httpx

    url = str(payload.get("url") or "")
    await asyncio.to_thread(_assert_public_https, url)
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as http:
        resp = await http.get(url, params=payload.get("params") or None,
                              headers={"User-Agent": "autoplatform/1.0", "Accept": "application/json, text/*"})
    if resp.status_code == 429 or resp.status_code >= 500:
        raise RuntimeError(f"upstream HTTP {resp.status_code}")  # retryable
    body_text = resp.text[:_FETCH_MAX_BYTES]
    try:
        data: Any = resp.json()
    except ValueError:
        data = body_text
    out: dict[str, Any] = {"status_code": resp.status_code,
                           "content_type": resp.headers.get("content-type"), "data": data}
    if payload.get("summarize") and ctx.router.configured:
        res = await ctx.router.complete(
            [{"role": "system", "content": "You summarize API responses concisely."},
             {"role": "user", "content": f"{payload['summarize']}\n\nAPI response:\n{body_text[:12000]}"}],
            max_tokens=400, client_id=ctx.client_id, job_id=ctx.job_id)
        out["summary"] = {"text": res.text, "provider": res.provider}
    return out
