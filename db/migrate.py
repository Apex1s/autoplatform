"""Versioned, idempotent schema migrations.

Append new (version, description, fn) tuples to MIGRATIONS; never edit applied ones.
Run: python -m db.migrate
"""
from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import Connection, inspect, text

from db.session import get_engine
from models import Base, SchemaMigration

log = logging.getLogger("autoplatform.migrate")

_V1_TABLES = ["clients", "jobs", "execution_logs", "api_usage", "worker_health"]


def _v1_initial(conn: Connection) -> None:
    tables = [Base.metadata.tables[name] for name in _V1_TABLES]
    Base.metadata.create_all(conn, tables=tables, checkfirst=True)


MIGRATIONS: list[tuple[int, str, Callable[[Connection], None]]] = [
    (1, "initial schema: clients, jobs, execution_logs, api_usage, worker_health", _v1_initial),
]


def run_migrations() -> list[int]:
    engine = get_engine()
    applied_now: list[int] = []
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            # Serialize concurrent boots (e.g. two replicas) on the same database.
            conn.execute(text("SELECT pg_advisory_xact_lock(727001)"))
        SchemaMigration.__table__.create(conn, checkfirst=True)
        done = {row[0] for row in conn.execute(text("SELECT version FROM schema_migrations"))}
        for version, description, fn in MIGRATIONS:
            if version in done:
                continue
            log.info("applying migration %s: %s", version, description)
            fn(conn)
            conn.execute(
                SchemaMigration.__table__.insert().values(version=version, description=description)
            )
            applied_now.append(version)
    return applied_now


def current_tables() -> list[str]:
    return sorted(inspect(get_engine()).get_table_names())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    applied = run_migrations()
    print(f"applied: {applied or 'none (up to date)'}; tables: {current_tables()}")
