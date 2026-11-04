"""Failover engine: simulated 429s, timeouts, 5xx, auth errors."""
from __future__ import annotations

import pytest

from app.config import get_settings
from core.llm_router import AllProvidersFailed, LLMRouter

MSG = [{"role": "user", "content": "hi"}]


async def test_429_fails_over_to_gemini_immediately(make_router, fake_llm, sleeps):
    fake_llm.script("groq", (429, {"retry-after": "42"}, None))
    r = make_router()
    res = await r.complete(MSG)
    assert res.provider == "gemini" and res.text == "hello from gemini"
    assert fake_llm.calls == ["groq", "gemini"]
    assert sleeps == []  # no waiting before failover
    assert [a["error_kind"] for a in res.attempts] == ["rate_limit", None]
    assert 41 < r.cooldown_remaining("groq") <= 42
    assert r.stats["groq"].rate_limited == 1


async def test_cooldown_skips_rate_limited_provider_on_next_call(make_router, fake_llm):
    fake_llm.script("groq", (429, {"retry-after": "60"}, None))
    r = make_router()
    await r.complete(MSG)
    res2 = await r.complete(MSG)
    assert res2.provider == "gemini"
    assert fake_llm.calls_to("groq") == 1  # not hammered while cooling down


async def test_gemini_429_falls_back_to_groq_with_body_retry_delay(make_router, fake_llm):
    body = {"error": {"code": 429, "details": [{"@type": "RetryInfo", "retryDelay": "17s"}]}}
    fake_llm.script("gemini", (429, {}, body))
    r = make_router()
    r.providers.reverse()  # gemini first
    res = await r.complete(MSG)
    assert res.provider == "groq"
    assert 16 < r.cooldown_remaining("gemini") <= 17


async def test_timeout_fails_over_immediately(make_router, fake_llm, sleeps):
    fake_llm.script("groq", ("timeout",))
    r = make_router()
    res = await r.complete(MSG)
    assert res.provider == "gemini" and sleeps == []
    assert r.stats["groq"].timeouts == 1
    assert r.cooldown_remaining("groq") == 0


async def test_5xx_retries_same_provider_with_exponential_backoff(make_router, fake_llm, sleeps):
    fake_llm.script("groq", 503, 502)
    r = make_router(max_retries=2, backoff_base=1.0, backoff_max=10)
    res = await r.complete(MSG)
    assert res.provider == "groq" and fake_llm.calls == ["groq"] * 3
    assert len(sleeps) == 2
    assert 0.5 <= sleeps[0] <= 1.0 and 1.0 <= sleeps[1] <= 2.0  # base*2^n with jitter


async def test_5xx_exhausts_retries_then_fails_over(make_router, fake_llm):
    fake_llm.script("groq", 500, 500, 500)
    r = make_router(max_retries=2)
    res = await r.complete(MSG)
    assert res.provider == "gemini" and fake_llm.calls == ["groq"] * 3 + ["gemini"]


async def test_auth_error_fails_over_without_retry(make_router, fake_llm, sleeps):
    fake_llm.script("groq", 401)
    res = await make_router().complete(MSG)
    assert res.provider == "gemini" and fake_llm.calls == ["groq", "gemini"] and sleeps == []


async def test_all_rate_limited_raises_after_rounds(make_router, fake_llm):
    fake_llm.script("groq", *[(429, {"retry-after": "1"}, None)] * 5)
    fake_llm.script("gemini", *[(429, {"retry-after": "1"}, None)] * 5)
    usage = []
    r = make_router(max_rounds=2, usage_hook=usage.append)
    with pytest.raises(AllProvidersFailed) as ei:
        await r.complete(MSG)
    assert len(ei.value.errors) == 4 and all(e.kind == "rate_limit" for e in ei.value.errors)
    assert len(usage) == 4 and not any(u["success"] for u in usage)


async def test_recovers_in_second_round(make_router, fake_llm):
    fake_llm.script("groq", (429, {"retry-after": "0"}, None))
    fake_llm.script("gemini", (429, {"retry-after": "0"}, None))
    res = await make_router(max_rounds=2).complete(MSG)
    assert res.provider == "groq" and fake_llm.calls == ["groq", "gemini", "groq"]


async def test_token_tracking_and_usage_hook(make_router, fake_llm):
    usage = []

    async def hook(rec):
        usage.append(rec)

    r = make_router(usage_hook=hook)
    for _ in range(3):
        await r.complete(MSG, client_id="c1", job_id="j1")
    st = r.stats["groq"]
    assert (st.requests, st.successes, st.prompt_tokens, st.completion_tokens) == (3, 3, 33, 21)
    assert usage[0]["client_id"] == "c1" and usage[0]["prompt_tokens"] == 11
    snap = r.snapshot()
    assert snap["groq"]["successes"] == 3 and "cooldown_remaining" in snap["groq"]


async def test_usage_hook_failure_does_not_break_request(make_router):
    def bad_hook(rec):
        raise RuntimeError("db down")

    res = await make_router(usage_hook=bad_hook).complete(MSG)
    assert res.provider == "groq"


async def test_malformed_body_is_retried(make_router, fake_llm):
    fake_llm.script("groq", (200, {}, {"nope": True}))
    res = await make_router().complete(MSG)
    assert res.provider == "groq" and fake_llm.calls == ["groq", "groq"]


def test_from_settings_order_and_missing_keys(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "gemini,groq")
    get_settings.cache_clear()
    try:
        assert LLMRouter.from_settings(get_settings()).configured == ["gemini"]
        monkeypatch.setenv("GROQ_API_KEY", "y")
        get_settings.cache_clear()
        assert LLMRouter.from_settings(get_settings()).configured == ["gemini", "groq"]
    finally:
        get_settings.cache_clear()
