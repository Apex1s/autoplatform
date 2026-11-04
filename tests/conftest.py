"""Shared fixtures.

Default backends: temp SQLite + in-memory fake Redis.
Set TEST_DATABASE_URL / TEST_REDIS_URL to run the *same* suite against real Postgres/Redis.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict, deque
from dataclasses import replace

import httpx
import pytest

os.environ["AUTOPLATFORM_SKIP_DOTENV"] = "1"  # never load the developer's real .env
for _var in ("GROQ_API_KEY", "GEMINI_API_KEY", "UPSTASH_REDIS_URL", "UPSTASH_REDIS_REST_URL",
             "UPSTASH_REDIS_REST_TOKEN", "REDIS_URL", "DATABASE_URL", "METRICS_TOKEN",
             "PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL", "SPACE_HOST", "GROQ_BASE_URL", "GEMINI_BASE_URL"):
    os.environ.pop(_var, None)
os.environ.setdefault("ADMIN_TOKEN", "test-admin")
os.environ.setdefault("CRON_TOKEN", "test-cron")

from app.config import get_settings  # noqa: E402
from core.llm_router import LLMRouter, ProviderConfig  # noqa: E402
from db.session import reset_engine  # noqa: E402

REAL_DB = os.getenv("TEST_DATABASE_URL")
REAL_REDIS = os.getenv("TEST_REDIS_URL")


class FakeLLM:
    """httpx transport emulating Groq/Gemini. Script responses per provider:
    llm.script("groq", 429, 200) -> first call 429, second 200; then default 200.
    An item may be an int status, ("timeout",), or (status, headers, body)."""

    def __init__(self):
        self.scripts: dict[str, deque] = defaultdict(deque)
        self.calls: list[str] = []

    def script(self, provider: str, *items):
        self.scripts[provider].extend(items)

    def calls_to(self, provider: str) -> int:
        return self.calls.count(provider)

    def handler(self, request: httpx.Request) -> httpx.Response:
        provider = "groq" if "groq" in request.url.host else "gemini"
        self.calls.append(provider)
        item = self.scripts[provider].popleft() if self.scripts[provider] else 200
        if item == ("timeout",):
            raise httpx.ReadTimeout("simulated timeout", request=request)
        status, headers, body = (item, {}, None) if isinstance(item, int) else item
        if status == 200 and body is None:
            req = json.loads(request.content)
            body = {"model": req["model"], "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                    "choices": [{"message": {"role": "assistant", "content": f"hello from {provider}"}}]}
        if body is None:
            body = {"error": {"message": f"simulated {status}"}}
        return httpx.Response(status, headers=headers, json=body)


@pytest.fixture
def fake_llm():
    return FakeLLM()


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def make_router(fake_llm, sleeps):
    async def fake_sleep(s):
        sleeps.append(s)

    def _make(**kw):
        providers = kw.pop("providers", None) or [
            ProviderConfig("groq", "https://api.groq.com/openai/v1", "gk", "llama-3.3-70b-versatile", 5),
            ProviderConfig("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gm",
                           "gemini-2.5-flash", 5),
        ]
        kw.setdefault("sleep", fake_sleep)
        return LLMRouter(providers, transport=httpx.MockTransport(fake_llm.handler), **kw)

    return _make


def _wipe_real_db():
    from sqlalchemy import text

    from db.session import get_engine
    with get_engine().begin() as c:
        c.execute(text("DROP TABLE IF EXISTS execution_logs, api_usage, jobs, worker_health, "
                       "clients, schema_migrations CASCADE"))


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", REAL_DB or f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("UPSTASH_REDIS_URL", REAL_REDIS or "memory://")
    get_settings.cache_clear()
    reset_engine()
    if REAL_DB:
        _wipe_real_db()
    s = replace(get_settings(), run_embedded_worker=True, worker_concurrency=2,
                worker_poll_timeout=1, heartbeat_interval=60, job_retry_base=0.05,
                public_base_url="http://testserver", queue_prefix=f"test{os.getpid()}")
    from db.migrate import run_migrations
    run_migrations()
    yield s
    if REAL_REDIS:
        import redis
        r = redis.from_url(REAL_REDIS)
        for k in r.scan_iter(f"{s.queue_prefix}:*"):
            r.delete(k)
        r.close()
    reset_engine()
    get_settings.cache_clear()


# ------------------------------------------------------------ queue backends
class UpstashRestBridge:
    """httpx transport that speaks Upstash's REST protocol on top of a fakeredis server."""

    def __init__(self):
        import fakeredis

        self.redis = fakeredis.FakeRedis(decode_responses=True)
        self.commands: list[list[str]] = []
        self.fail_with: tuple[int, dict] | None = None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        import asyncio

        assert request.headers["authorization"] == "Bearer test-token"
        if self.fail_with:
            status, body = self.fail_with
            return httpx.Response(status, json=body)
        cmd = json.loads(request.content)
        assert all(isinstance(a, str) for a in cmd), cmd  # Upstash gets strings only
        self.commands.append(cmd)
        try:
            result = await asyncio.to_thread(self.redis.execute_command, *cmd)
        except Exception as e:  # mimic Upstash error envelope
            return httpx.Response(400, json={"error": f"ERR {e}"})
        name = cmd[0].upper()
        if name == "PING":
            result = "PONG"
        elif name == "SET":
            result = "OK" if result else None
        elif name == "ZRANGE" and "WITHSCORES" in [c.upper() for c in cmd]:
            result = [str(x) for pair in result for x in pair]  # Upstash returns a flat list
        elif isinstance(result, (set, tuple)):
            result = list(result)
        return httpx.Response(200, json={"result": result})


@pytest.fixture
def rest_bridge():
    return UpstashRestBridge()


@pytest.fixture(params=["fake", "rest"] if not REAL_REDIS else ["tcp"])
def make_queue(request, settings, rest_bridge):
    """Builds TaskQueues on each transport: fakeredis, Upstash REST (bridged), or real Redis."""
    from core.queue import TaskQueue
    from core.upstash_rest import UpstashRestRedis

    made = []

    def _make():
        if request.param == "rest":
            client = UpstashRestRedis("https://fake-db.upstash.io", "test-token",
                                      transport=httpx.MockTransport(rest_bridge.handler))
            q = TaskQueue(client, settings.queue_prefix, "rest")
        else:
            q = TaskQueue.from_settings(settings)
        made.append(q)
        return q

    _make.backend = request.param
    return _make
