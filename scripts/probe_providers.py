"""Connectivity probe for every external dependency. Writes probe_results.json.

    python -m scripts.probe_providers
Exit code 0 only if every *configured* provider passes; unconfigured ones are reported as "missing".
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from app.config import get_settings
from core.llm_router import AllProvidersFailed, LLMRouter
from core.queue import TaskQueue

logging.basicConfig(level=logging.WARNING)


async def probe_llm(router: LLMRouter, name: str) -> dict:
    if name not in router.configured:
        return {"status": "missing", "hint": f"set {name.upper()}_API_KEY"}
    try:
        r = await router.complete([{"role": "user", "content": "Reply with the single word OK."}],
                                  max_tokens=5, only=[name])
        return {"status": "ok", "model": r.model, "latency_ms": r.latency_ms, "reply": r.text.strip()[:20]}
    except AllProvidersFailed as e:
        return {"status": "fail", "error": str(e)[:400]}


async def probe_redis(settings) -> dict:
    if settings.redis_url.startswith("memory://") and not settings.upstash_rest_url:
        return {"status": "missing", "hint": "set UPSTASH_REDIS_URL (rediss://default:...@...upstash.io:6379)"}
    from dataclasses import replace

    out = {}
    variants = []
    if settings.upstash_rest_url and settings.upstash_rest_token:
        variants.append(("rest", settings))
    if not settings.redis_url.startswith("memory://"):
        variants.append(("tcp", replace(settings, upstash_rest_url="", upstash_rest_token="")))
    for name, s in variants:
        q = TaskQueue.from_settings(s)
        t = time.perf_counter()
        try:
            await q.r.set("probe:ping", "1", ex=30)
            ok = await q.r.get("probe:ping") == "1"
            out[name] = {"status": "ok" if ok else "fail", "latency_ms": int((time.perf_counter() - t) * 1000)}
        except Exception as e:
            out[name] = {"status": "fail", "error": f"{type(e).__name__}: {e}"[:300]}
        finally:
            await q.close()
    best = "ok" if any(v["status"] == "ok" for v in out.values()) else "fail"
    return {"status": best, **out}


def probe_db(url: str) -> dict:
    if url.startswith("sqlite"):
        return {"status": "missing", "hint": "set DATABASE_URL (Neon/Supabase postgres URL)"}
    from sqlalchemy import text

    from db.session import get_engine
    t = time.perf_counter()
    tcp_error = ""
    try:
        with get_engine().connect() as c:
            version = c.execute(text("SELECT version()")).scalar()
        return {"status": "ok", "server": str(version)[:60], "latency_ms": int((time.perf_counter() - t) * 1000)}
    except Exception as e:
        tcp_error = f"{type(e).__name__}: {e}"[:300]
    if "neon.tech" in url:  # TCP blocked (sandbox / port-restricted host)? try Neon's HTTPS SQL API
        return {**probe_neon_http(url), "tcp": {"status": "fail", "error": tcp_error}}
    return {"status": "fail", "error": tcp_error}


def probe_neon_http(url: str) -> dict:
    import re

    import httpx

    host = re.search(r"@([^/:]+)", url).group(1)
    t = time.perf_counter()
    try:
        r = httpx.post(f"https://{host}/sql", headers={"Neon-Connection-String": url},
                       json={"query": "select version() as v", "params": []}, timeout=30)
        r.raise_for_status()
        return {"status": "ok", "via": "neon https /sql", "server": r.json()["rows"][0]["v"][:40],
                "latency_ms": int((time.perf_counter() - t) * 1000)}
    except Exception as e:
        return {"status": "fail", "error": f"{type(e).__name__}: {e}"[:300]}


async def main() -> int:
    s = get_settings()
    router = LLMRouter.from_settings(s, max_retries=0, max_rounds=1)
    try:
        results = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "groq": await probe_llm(router, "groq"),
            "gemini": await probe_llm(router, "gemini"),
            "upstash_redis": await probe_redis(s),
            "postgres": await asyncio.to_thread(probe_db, s.database_url),
        }
    finally:
        await router.aclose()
    with open("probe_results.json", "w") as f:
        json.dump(results, f, indent=2)
    for k, v in results.items():
        if k != "timestamp":
            print(f"{k:14s} {v['status']:8s} {json.dumps({x: y for x, y in v.items() if x != 'status'})}")
    return 1 if any(isinstance(v, dict) and v.get("status") == "fail" for v in results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
