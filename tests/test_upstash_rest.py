"""Upstash REST client: wire format, response parsing, error mapping."""
from __future__ import annotations

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from core.upstash_rest import UpstashRestRedis


def client(bridge):
    return UpstashRestRedis("https://fake-db.upstash.io", "test-token",
                            transport=httpx.MockTransport(bridge.handler))


async def test_round_trip_commands(rest_bridge):
    r = client(rest_bridge)
    assert await r.ping() is True
    assert await r.set("k", "v", nx=True, ex=30) is True
    assert await r.set("k", "other", nx=True, ex=30) is None  # NX refused
    assert await r.get("k") == "v"
    assert await r.lpush("l", "a", "b") == 2
    assert await r.lmove("l", "l2", "RIGHT", "LEFT") == "a"
    assert await r.llen("l2") == 1 and await r.lrem("l2", 1, "a") == 1
    assert await r.zadd("z", {"x": 10.5, "y": 2}) == 2
    assert await r.zrange("z", 0, -1, withscores=True) == [("y", 2.0), ("x", 10.5)]
    assert await r.zrangebyscore("z", "-inf", 5, start=0, num=10) == ["y"]
    assert await r.zrem("z", "y") == 1 and await r.zcard("z") == 1
    assert await r.sadd("s", "w1", "w2") == 2 and await r.smembers("s") == {"w1", "w2"}
    assert await r.mget("k", "missing") == ["v", None]
    assert await r.exists("k") == 1 and await r.delete("k", "l2") >= 1
    await r.aclose()
    # everything is sent as strings, including numbers and floats
    assert ["ZADD", "z", "10.5", "x", "2", "y"] in rest_bridge.commands
    assert ["SET", "k", "v", "EX", "30", "NX"] in rest_bridge.commands


async def test_blmove_extends_http_timeout():
    seen = {}

    def handler(request: httpx.Request):
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, json={"result": None})

    r = UpstashRestRedis("https://x.upstash.io", "t", transport=httpx.MockTransport(handler))
    assert await r.blmove("a", "b", 30, "RIGHT", "LEFT") is None
    assert seen["timeout"]["read"] >= 45  # outlives the 30s server-side block


async def test_error_envelope_maps_to_response_error(rest_bridge):
    r = client(rest_bridge)
    await r.set("str", "x")
    with pytest.raises(ResponseError):
        await r.lpush("str", "y")  # WRONGTYPE


async def test_auth_failure_and_transport_errors():
    r = UpstashRestRedis("https://x.upstash.io", "bad", transport=httpx.MockTransport(
        lambda req: httpx.Response(401, json={"error": "WRONGPASS invalid password"})))
    with pytest.raises(ResponseError):
        await r.ping()

    def boom(req):
        raise httpx.ConnectError("refused", request=req)

    r = UpstashRestRedis("https://x.upstash.io", "t", transport=httpx.MockTransport(boom))
    with pytest.raises(RedisConnectionError):
        await r.ping()

    def slow(req):
        raise httpx.ReadTimeout("slow", request=req)

    r = UpstashRestRedis("https://x.upstash.io", "t", transport=httpx.MockTransport(slow))
    with pytest.raises(RedisTimeoutError):
        await r.ping()

    r = UpstashRestRedis("https://x.upstash.io", "t", transport=httpx.MockTransport(
        lambda req: httpx.Response(502, text="<html>bad gateway</html>")))
    with pytest.raises(RedisConnectionError):
        await r.ping()


def test_queue_factory_selects_transport(monkeypatch):
    from dataclasses import replace

    from app.config import get_settings
    from core.queue import TaskQueue

    base = get_settings()
    assert TaskQueue.from_settings(replace(base, redis_url="memory://", upstash_rest_url="",
                                           upstash_rest_token="")).transport == "memory"
    assert TaskQueue.from_settings(replace(base, upstash_rest_url="https://x.upstash.io",
                                           upstash_rest_token="t")).transport == "rest"
    assert TaskQueue.from_settings(replace(base, redis_url="rediss://default:p@x.upstash.io:6379",
                                           upstash_rest_url="", upstash_rest_token="")).transport == "tcp"
    with pytest.raises(ValueError):
        TaskQueue.from_url("https://x.upstash.io")


def test_suite_is_hermetic(settings):
    """Guard: the test settings must never point at real infrastructure."""
    assert settings.upstash_rest_url == "" and settings.upstash_rest_token == ""
    assert settings.groq_api_key == "" and settings.gemini_api_key == ""
    assert settings.redis_url.startswith(("memory://", "redis://127.0.0.1", "redis://localhost"))
