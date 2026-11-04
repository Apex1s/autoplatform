"""Central configuration, read from environment (and .env when present)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    return default if not raw else raw.lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    app_env: str
    log_level: str
    public_base_url: str
    database_url: str
    redis_url: str
    queue_prefix: str
    admin_token: str
    cron_token: str
    metrics_token: str
    auto_migrate: bool
    # LLM
    groq_api_key: str
    groq_model: str
    groq_base_url: str
    gemini_api_key: str
    gemini_model: str
    gemini_base_url: str
    provider_order: tuple[str, ...]
    llm_timeout: float
    llm_max_retries: int
    llm_max_rounds: int
    llm_backoff_base: float
    llm_backoff_max: float
    llm_default_cooldown: float
    # Worker
    run_embedded_worker: bool
    worker_concurrency: int
    worker_poll_timeout: int
    heartbeat_interval: int
    job_timeout: float
    job_max_attempts: int
    job_retry_base: float
    log_retention_days: int


@lru_cache
def get_settings() -> Settings:
    return Settings(
        app_env=_env("APP_ENV", "development"),
        log_level=_env("LOG_LEVEL", "INFO"),
        public_base_url=_env("PUBLIC_BASE_URL", "http://localhost:7860").rstrip("/"),
        database_url=_env("DATABASE_URL") or "sqlite:///./autoplatform.db",
        redis_url=_env("UPSTASH_REDIS_URL") or _env("REDIS_URL") or "memory://",
        queue_prefix=_env("QUEUE_PREFIX", "ap"),
        admin_token=_env("ADMIN_TOKEN"),
        cron_token=_env("CRON_TOKEN") or _env("ADMIN_TOKEN"),
        metrics_token=_env("METRICS_TOKEN"),
        auto_migrate=_bool("AUTO_MIGRATE", True),
        groq_api_key=_env("GROQ_API_KEY"),
        groq_model=_env("GROQ_MODEL", "llama-3.3-70b-versatile"),
        groq_base_url=_env("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        gemini_api_key=_env("GEMINI_API_KEY"),
        gemini_model=_env("GEMINI_MODEL", "gemini-2.5-flash"),
        gemini_base_url=_env(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
        ),
        provider_order=tuple(
            p.strip() for p in _env("LLM_PROVIDER_ORDER", "groq,gemini").split(",") if p.strip()
        ),
        llm_timeout=_float("LLM_TIMEOUT_SECONDS", 30.0),
        llm_max_retries=_int("LLM_MAX_RETRIES", 2),
        llm_max_rounds=_int("LLM_MAX_ROUNDS", 2),
        llm_backoff_base=_float("LLM_BACKOFF_BASE", 0.5),
        llm_backoff_max=_float("LLM_BACKOFF_MAX", 8.0),
        llm_default_cooldown=_float("LLM_DEFAULT_COOLDOWN", 30.0),
        run_embedded_worker=_bool("RUN_EMBEDDED_WORKER", True),
        worker_concurrency=_int("WORKER_CONCURRENCY", 2),
        worker_poll_timeout=_int("WORKER_POLL_TIMEOUT", 30),
        heartbeat_interval=_int("HEARTBEAT_INTERVAL", 60),
        job_timeout=_float("JOB_TIMEOUT_SECONDS", 120.0),
        job_max_attempts=_int("JOB_MAX_ATTEMPTS", 3),
        job_retry_base=_float("JOB_RETRY_BASE_SECONDS", 5.0),
        log_retention_days=_int("LOG_RETENTION_DAYS", 14),
    )
