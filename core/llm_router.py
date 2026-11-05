"""LLM router with Groq <-> Gemini failover.

Both providers expose OpenAI-compatible chat-completions endpoints, so one httpx
code path serves both (no SDK lock-in, small Docker image).

Failure policy per provider attempt:
  * 429 / timeout          -> fail over to the next provider immediately
                              (429 also puts the provider in cooldown, honouring Retry-After)
  * 5xx / network errors   -> retry the same provider with exponential backoff + jitter,
                              then fail over
  * other 4xx (auth, bad request) -> fail over immediately, no retry
If a whole pass over all providers fails, the router backs off and does another pass
(up to `max_rounds`). Every attempt is reported to `usage_hook` for persistence.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger("autoplatform.llm")

UsageHook = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    model: str
    timeout: float = 30.0


@dataclass
class ProviderStats:
    requests: int = 0
    successes: int = 0
    failures: int = 0
    rate_limited: int = 0
    timeouts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_latency_ms: int = 0
    cooldown_until: float = 0.0  # monotonic clock
    last_error: str | None = None


class LLMError(Exception):
    pass


class ProviderError(LLMError):
    def __init__(self, provider: str, kind: str, message: str,
                 status_code: int | None = None, retry_after: float | None = None):
        super().__init__(f"[{provider}] {kind}: {message}")
        self.provider = provider
        self.kind = kind  # rate_limit | timeout | server | network | client | bad_response
        self.status_code = status_code
        self.retry_after = retry_after


class AllProvidersFailed(LLMError):
    def __init__(self, errors: list[ProviderError]):
        self.errors = errors
        summary = "; ".join(str(e) for e in errors[-6:]) or "no providers configured"
        super().__init__(f"all LLM providers failed: {summary}")


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    attempts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["total_tokens"] = self.total_tokens
        return d


_RETRY_DELAY_RE = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


def _parse_retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(raw).timestamp() - time.time())
            except (TypeError, ValueError):
                pass
    m = _RETRY_DELAY_RE.search(resp.text or "")  # Gemini puts it in the error body
    return float(m.group(1)) if m else None


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class LLMRouter:
    def __init__(
        self,
        providers: list[ProviderConfig],
        *,
        max_retries: int = 2,
        max_rounds: int = 2,
        backoff_base: float = 0.5,
        backoff_max: float = 8.0,
        default_cooldown: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        usage_hook: UsageHook | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.providers = [p for p in providers if p.api_key]
        self.max_retries = max_retries
        self.max_rounds = max(1, max_rounds)
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.default_cooldown = default_cooldown
        self.usage_hook = usage_hook
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._http: httpx.AsyncClient | None = None
        self.stats: dict[str, ProviderStats] = {p.name: ProviderStats() for p in self.providers}

    # ------------------------------------------------------------------ setup
    @classmethod
    def from_settings(cls, settings, **kwargs) -> "LLMRouter":
        catalog = {
            "groq": ProviderConfig("groq", settings.groq_base_url, settings.groq_api_key,
                                   settings.groq_model, settings.llm_timeout),
            "gemini": ProviderConfig("gemini", settings.gemini_base_url, settings.gemini_api_key,
                                     settings.gemini_model, settings.llm_timeout),
        }
        order = [n for n in settings.provider_order if n in catalog]
        order += [n for n in catalog if n not in order]
        opts = {
            "max_retries": settings.llm_max_retries,
            "max_rounds": settings.llm_max_rounds,
            "backoff_base": settings.llm_backoff_base,
            "backoff_max": settings.llm_backoff_max,
            "default_cooldown": settings.llm_default_cooldown,
            **kwargs,  # explicit kwargs win over settings
        }
        return cls([catalog[n] for n in order], **opts)

    @property
    def configured(self) -> list[str]:
        return [p.name for p in self.providers]

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(transport=self._transport)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # --------------------------------------------------------------- helpers
    def _backoff(self, n: int) -> float:
        delay = min(self.backoff_max, self.backoff_base * (2 ** n))
        return delay * (0.5 + random.random() / 2)

    def cooldown_remaining(self, name: str) -> float:
        return max(0.0, self.stats[name].cooldown_until - self._clock())

    async def _report(self, record: dict[str, Any]) -> None:
        if not self.usage_hook:
            return
        try:
            out = self.usage_hook(record)
            if inspect.isawaitable(out):
                await out
        except Exception:  # usage tracking must never break a request
            log.exception("usage hook failed")

    # ------------------------------------------------------------ one call
    async def _call(self, p: ProviderConfig, messages: list[dict], max_tokens: int,
                    temperature: float, model: str | None,
                    extra: dict[str, Any] | None = None) -> LLMResult:
        model = model or p.model
        st = self.stats[p.name]
        st.requests += 1
        started = time.perf_counter()
        try:
            resp = await self._client().post(
                f"{p.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {p.api_key}"},
                json={"model": model, "messages": messages,
                      "max_tokens": max_tokens, "temperature": temperature, **(extra or {})},
                timeout=p.timeout,
            )
        except httpx.TimeoutException as e:
            st.timeouts += 1
            raise ProviderError(p.name, "timeout", type(e).__name__) from e
        except httpx.TransportError as e:
            raise ProviderError(p.name, "network", f"{type(e).__name__}: {e}") from e

        latency = int((time.perf_counter() - started) * 1000)
        st.total_latency_ms += latency
        code = resp.status_code
        if code == 429:
            st.rate_limited += 1
            raise ProviderError(p.name, "rate_limit", resp.text[:200], code, _parse_retry_after(resp))
        if code in (408, 504):
            st.timeouts += 1
            raise ProviderError(p.name, "timeout", f"HTTP {code}", code)
        if code >= 500:
            raise ProviderError(p.name, "server", f"HTTP {code}: {resp.text[:200]}", code)
        if code >= 400:
            raise ProviderError(p.name, "client", f"HTTP {code}: {resp.text[:200]}", code)
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ProviderError(p.name, "bad_response", f"unparseable body: {resp.text[:200]}", code) from e

        usage = data.get("usage") or {}
        prompt_toks = int(usage.get("prompt_tokens") or _estimate_tokens(str(messages)))
        completion_toks = int(usage.get("completion_tokens") or _estimate_tokens(text))
        st.successes += 1
        st.prompt_tokens += prompt_toks
        st.completion_tokens += completion_toks
        return LLMResult(text, p.name, data.get("model") or model, prompt_toks, completion_toks, latency)

    # ---------------------------------------------------------- public API
    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 512,
        temperature: float = 0.2,
        models: dict[str, str] | None = None,
        only: list[str] | None = None,
        client_id: str | None = None,
        job_id: str | None = None,
        response_format: dict[str, Any] | None = None,
        simulate_failure: str | None = None,
    ) -> LLMResult:
        """`response_format={"type": "json_object"}` enables JSON mode on both providers.
        `simulate_failure="groq"` (demo only) records a labelled synthetic 429 for that provider
        instead of calling it, so failover can be shown live; real cooldowns are untouched."""
        pool = [p for p in self.providers if not only or p.name in only]
        if not pool:
            raise AllProvidersFailed([])
        models = models or {}
        extra = {"response_format": response_format} if response_format else None
        errors: list[ProviderError] = []
        attempts: list[dict[str, Any]] = []
        if simulate_failure and any(p.name == simulate_failure for p in pool) and len(pool) > 1:
            rec = {"provider": simulate_failure, "model": "-", "client_id": client_id, "job_id": job_id,
                   "round": 0, "retry": 0, "success": False, "status_code": 429,
                   "error_kind": "rate_limit", "simulated": True, "prompt_tokens": 0,
                   "completion_tokens": 0, "latency_ms": 0}
            attempts.append(rec)
            errors.append(ProviderError(simulate_failure, "rate_limit", "simulated outage (demo)", 429))
            pool = [p for p in pool if p.name != simulate_failure]

        for rnd in range(self.max_rounds):
            ready = [p for p in pool if self.cooldown_remaining(p.name) == 0]
            if not ready:
                # Everyone is rate-limited: wait for the earliest cooldown (capped), then try it.
                pool_sorted = sorted(pool, key=lambda p: self.stats[p.name].cooldown_until)
                await self._sleep(min(self.cooldown_remaining(pool_sorted[0].name), self.backoff_max))
                ready = pool_sorted

            for p in ready:
                for retry in range(self.max_retries + 1):
                    base = {"provider": p.name, "model": models.get(p.name, p.model),
                            "client_id": client_id, "job_id": job_id, "round": rnd, "retry": retry}
                    try:
                        res = await self._call(p, messages, max_tokens, temperature, models.get(p.name), extra)
                    except ProviderError as e:
                        st = self.stats[p.name]
                        st.failures += 1
                        st.last_error = str(e)[:300]
                        errors.append(e)
                        rec = {**base, "success": False, "status_code": e.status_code,
                               "error_kind": e.kind, "prompt_tokens": 0, "completion_tokens": 0,
                               "latency_ms": 0}
                        attempts.append(rec)
                        await self._report(rec)
                        if e.kind == "rate_limit":
                            cd = e.retry_after if e.retry_after is not None else self.default_cooldown
                            st.cooldown_until = self._clock() + cd
                            log.warning("%s rate-limited (cooldown %.1fs) -> failover", p.name, cd)
                            break
                        if e.kind == "timeout":
                            log.warning("%s timed out -> failover", p.name)
                            break
                        if e.kind in ("server", "network", "bad_response") and retry < self.max_retries:
                            delay = self._backoff(retry)
                            log.warning("%s %s; retry %d in %.2fs", p.name, e.kind, retry + 1, delay)
                            await self._sleep(delay)
                            continue
                        log.warning("%s failed (%s) -> failover", p.name, e.kind)
                        break
                    else:
                        rec = {**base, "model": res.model, "success": True, "status_code": 200,
                               "error_kind": None, "prompt_tokens": res.prompt_tokens,
                               "completion_tokens": res.completion_tokens, "latency_ms": res.latency_ms}
                        attempts.append(rec)
                        await self._report(rec)
                        res.attempts = attempts
                        return res
            if rnd < self.max_rounds - 1:
                await self._sleep(self._backoff(rnd))
        raise AllProvidersFailed(errors)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        out = {}
        for name, st in self.stats.items():
            d = asdict(st)
            d.pop("cooldown_until")
            d["cooldown_remaining"] = round(self.cooldown_remaining(name), 2)
            out[name] = d
        return out
