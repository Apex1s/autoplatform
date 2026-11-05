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


# v2 columns, exactly as worker/src/db.ts adds them (ADD COLUMN IF NOT EXISTS there).
V2_CLIENT_COLUMNS = {
    "plan": "VARCHAR(32) NOT NULL DEFAULT 'retainer'",
    "monthly_job_limit": "INTEGER",
    "allowed_handlers": "JSON",
    "outbound_webhook_url": "TEXT",
    "config": "JSON",
    "agency_id": "VARCHAR(32) REFERENCES clients (id) ON DELETE SET NULL",
    "contact_email": "VARCHAR(200)",
    "stripe_customer_id": "VARCHAR(64)",
}
V2_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_clients_agency_id ON clients (agency_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_clients_stripe_customer_id ON clients (stripe_customer_id)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_client_created ON jobs (client_id, created_at)",
]


def _v2_monetization(conn: Connection) -> None:
    """Fresh databases already got these columns from create_all in v1; existing ones
    (e.g. a database first created by the Worker) are altered in place. SQLite-safe."""
    have = {c["name"] for c in inspect(conn).get_columns("clients")}
    for col, ddl in V2_CLIENT_COLUMNS.items():
        if col not in have:
            conn.execute(text(f"ALTER TABLE clients ADD COLUMN {col} {ddl}"))
    for stmt in V2_INDEXES:
        conn.execute(text(stmt))


MIGRATIONS: list[tuple[int, str, Callable[[Connection], None]]] = [
    (1, "initial schema: clients, jobs, execution_logs, api_usage, worker_health", _v1_initial),
    (2, "monetization: plans, quotas, outbound delivery, agencies, billing", _v2_monetization),
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
