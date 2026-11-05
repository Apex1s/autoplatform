"""Outbound delivery (mirrors worker/src/delivery.ts).

Handlers never POST to a CRM inline: they save their result and enqueue `internal.deliver` jobs.
Each delivery retries on its own (timeouts, 408/425/429, 5xx; 8 attempts, about 35 minutes of
backoff), so a flaky CRM never re-runs the LLM. JSON deliveries are signed:
    X-AutoPlatform-Signature: sha256=<hex hmac_sha256(client webhook_secret, raw body)>
Slack / Discord webhook URLs receive a human-readable summary instead of the JSON envelope.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from db import repo
from models import Client, utcnow
from workers.handlers import JobContext, PermanentError, _assert_public_https, _peer_guard, handler

DELIVERY_MAX_ATTEMPTS = 8
USER_AGENT = "AutoPlatform-Webhooks/1.1"
TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests inject httpx.MockTransport


def destination_format(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == "hooks.slack.com":
        return "slack"
    if host in ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com") \
            and parts.path.startswith("/api/webhooks/"):
        return "discord"
    return "json"


def envelope(event: str, client_slug: str, source_job_id: str, data: dict) -> dict[str, Any]:
    return {"event": event, "client": client_slug, "source_job_id": source_job_id,
            "created_at": utcnow().isoformat(), "data": data}


def render(fmt: str, env: dict, summary: str) -> dict:
    if fmt == "slack":
        return {"text": summary}
    if fmt == "discord":
        return {"content": summary[:1900]}
    return env


def body_bytes(body: dict) -> bytes:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()


def sign(secret: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def destinations(client: Client) -> list[str]:
    out = []
    if client.outbound_webhook_url:
        out.append(client.outbound_webhook_url)
    notify = (client.config or {}).get("notify_url")
    if notify and notify not in out:
        out.append(notify)
    return out


async def queue_deliveries(ctx: JobContext, client: Client | None, event: str, data: dict,
                           summary: str) -> list[dict[str, Any]]:
    """Enqueues one delivery job per destination. Without destinations (or for the demo
    client) returns a preview of exactly what would be sent, signature included."""
    if client is None:
        return []
    env = envelope(event, client.slug, ctx.job_id, data)
    urls = destinations(client)
    if not urls or client.plan == "demo":
        raw = body_bytes(env)
        return [{"event": event, "delivered": False,
                 "reason": "demo: nothing is sent" if client.plan == "demo" else "no outbound_webhook_url configured",
                 "preview": {"headers": {"Content-Type": "application/json", "X-AutoPlatform-Event": event,
                                         "X-AutoPlatform-Signature": sign(client.webhook_secret, raw)},
                             "body": env, "summary": summary}}]
    out = []
    for url in urls:
        fmt = destination_format(url)
        payload = {"url": url, "format": fmt, "event": event, "body": render(fmt, env, summary),
                   "source_job_id": ctx.job_id}
        job = await asyncio.to_thread(repo.create_job, "internal.deliver", payload, client_id=client.id,
                                      max_attempts=DELIVERY_MAX_ATTEMPTS, source="internal")
        await ctx.queue.enqueue(job.id)
        out.append({"event": event, "destination": urlsplit(url).hostname, "format": fmt,
                    "delivery_job_id": job.id})
    return out


def _guard(url: str) -> None:
    _assert_public_https(url)


@handler("internal.deliver")
async def deliver(ctx: JobContext, payload: dict) -> dict:
    url = str(payload.get("url") or "")
    await asyncio.to_thread(_guard, url)
    client = await asyncio.to_thread(repo.client_by_id, ctx.client_id) if ctx.client_id else None
    if client is None or not client.active:
        raise PermanentError("client missing or inactive; delivery dropped")
    raw = body_bytes(payload.get("body") or {})
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT,
               "X-AutoPlatform-Event": str(payload.get("event") or ""), "X-AutoPlatform-Delivery": ctx.job_id,
               "X-AutoPlatform-Attempt": str(ctx.attempt)}
    if payload.get("format", "json") == "json":
        headers["X-AutoPlatform-Signature"] = sign(client.webhook_secret, raw)
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False, transport=TRANSPORT) as http:
        try:
            resp = await http.post(url, content=raw, headers=headers,
                                   extensions={} if TRANSPORT else {"trace": _peer_guard})
        except httpx.TimeoutException as e:
            raise RuntimeError(f"destination timeout: {type(e).__name__}") from e
        except httpx.TransportError as e:
            raise RuntimeError(f"destination unreachable: {type(e).__name__}") from e
    code = resp.status_code
    if code in (408, 425, 429) or code >= 500:
        raise RuntimeError(f"destination HTTP {code}")  # retried with backoff
    if code >= 400:
        raise PermanentError(f"destination rejected the delivery: HTTP {code} {resp.text[:200]}")
    return {"delivered": True, "status_code": code, "destination": urlsplit(url).hostname,
            "format": payload.get("format", "json"), "latency_ms": int((time.perf_counter() - started) * 1000),
            "source_job_id": payload.get("source_job_id")}
