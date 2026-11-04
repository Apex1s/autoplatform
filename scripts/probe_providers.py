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


async def probe_redis(url: str) -> dict:
    if url.startswith("memory://"):
        return {"status": "missing", "hint": "set UPSTASH_REDIS_URL (rediss://default:...@...upstash.io:6379)"}
    q = TaskQueue.from_url(url, "probe")
    t = time.perf_counter()
    try:
        await q.r.set("probe:ping", "1", ex=30)
        ok = await q.r.get("probe:ping") == "1"
        return {"status": "ok" if ok else "fail", "latency_ms": int((time.perf_counter() - t) * 1000)}
    except Exception as e:
        return {"status": "fail", "error": f"{type(e).__name__}: {e}"[:400]}
    finally:
        await q.close()


def probe_db(url: str) -> dict:
    if url.startswith("sqlite"):
        return {"status": "missing", "hint": "set DATABASE_URL (Neon/Supabase postgres URL)"}
    from sqlalchemy import text

    from db.session import get_engine
    t = time.perf_counter()
    try:
        with get_engine().connect() as c:
            version = c.execute(text("SELECT version()")).scalar()
        return {"status": "ok", "server": str(version)[:60], "latency_ms": int((time.perf_counter() - t) * 1000)}
    except Exception as e:
        return {"status": "fail", "error": f"{type(e).__name__}: {e}"[:400]}


async def main() -> int:
    s = get_settings()
    router = LLMRouter.from_settings(s, max_retries=0, max_rounds=1)
    try:
        results = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "groq": await probe_llm(router, "groq"),
            "gemini": await probe_llm(router, "gemini"),
            "upstash_redis": await probe_redis(s.redis_url),
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
