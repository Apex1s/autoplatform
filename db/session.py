"""Engine/session management. Sync SQLAlchemy; async code calls it via asyncio.to_thread."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import time

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_settings

_engine: Engine | None = None
_factory: sessionmaker | None = None
_idle_dispose: int = 0          # >0: close pooled connections after this many idle seconds
_last_checkout: float = 0.0


def _touch(*_args) -> None:
    global _last_checkout
    _last_checkout = time.monotonic()


def dispose_if_idle() -> bool:
    """Close idle pooled connections once the DB has been unused for a while (cheap, no I/O
    when there is nothing to close). Called from the worker's heartbeat loop."""
    if _engine is None or _idle_dispose <= 0:
        return False
    if time.monotonic() - _last_checkout < _idle_dispose or _engine.pool.checkedin() == 0:
        return False
    _engine.pool.dispose()  # only checked-in (idle) connections are closed
    return True


def normalize_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def get_engine() -> Engine:
    global _engine, _factory
    if _engine is None:
        url = normalize_url(get_settings().database_url)
        if url.startswith("sqlite"):
            _engine = create_engine(url, connect_args={"check_same_thread": False})
        else:
            global _idle_dispose
            mode = get_settings().database_pool
            serverless = any(h in url for h in ("neon.tech", "supabase.co", "supabase.com"))
            if mode == "null":
                _engine = create_engine(url, poolclass=NullPool)
            else:
                # Reuse connections during bursts (a new TLS+SCRAM connection costs ~4 round
                # trips, ~0.5 s across the Atlantic). For serverless Postgres the pool is emptied
                # after DB_IDLE_DISPOSE_SECONDS idle (dispose_if_idle), so Neon's free compute
                # (100 CU-hours/month) can still scale to zero between bursts.
                _engine = create_engine(url, pool_pre_ping=True, pool_recycle=240,
                                        pool_size=3, max_overflow=4)
                if mode == "auto" and serverless:
                    _idle_dispose = get_settings().db_idle_dispose_seconds
                event.listen(_engine, "checkout", _touch)
        _factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    global _engine, _factory, _idle_dispose
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None
    _idle_dispose = 0


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _factory is not None
    session = _factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
