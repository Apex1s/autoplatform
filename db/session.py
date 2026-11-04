"""Engine/session management. Sync SQLAlchemy; async code calls it via asyncio.to_thread."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_settings

_engine: Engine | None = None
_factory: sessionmaker | None = None


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
            mode = get_settings().database_pool
            serverless = any(h in url for h in ("neon.tech", "supabase.co", "supabase.com"))
            if mode == "null" or (mode == "auto" and serverless):
                # Hold no idle connections: Neon's free tier bills compute while connections
                # keep it awake (100 CU-hours/month); NullPool lets it scale to zero.
                _engine = create_engine(url, poolclass=NullPool)
            else:
                _engine = create_engine(
                    url, pool_pre_ping=True, pool_recycle=300, pool_size=5, max_overflow=5
                )
        _factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None


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
