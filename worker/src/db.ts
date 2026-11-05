// Database access. Production: Neon's HTTPS driver (one fetch per query, no TCP).
// Tests: node-postgres against a real Postgres. Same SQL, $1-style placeholders.
import { neon } from "@neondatabase/serverless";
import { Budget } from "./util";

export interface Statement {
  text: string;
  params?: unknown[];
}

export interface Db {
  query<T = Record<string, any>>(text: string, params?: unknown[]): Promise<T[]>;
  /** Runs statements in one transaction; returns each statement's rows. */
  transaction(stmts: Statement[]): Promise<Record<string, any>[][]>;
}

export function neonDb(url: string): Db {
  const sql = neon(url);
  return {
    async query(text, params = []) {
      return (await sql.query(text, params as any[])) as any[];
    },
    async transaction(stmts) {
      return (await sql.transaction(stmts.map((s) => sql.query(s.text, (s.params ?? []) as any[])))) as any[];
    },
  };
}

// Schema identical to the Python app (generated from its SQLAlchemy models), so both
// runtimes can share one database. Version 1 is recorded exactly like db/migrate.py does.
export const MIGRATION_V1_DESC = "initial schema: clients, jobs, execution_logs, api_usage, worker_health";
export const MIGRATION_V1: string[] = [
  `CREATE TABLE IF NOT EXISTS schema_migrations (version SERIAL NOT NULL, description VARCHAR(200) NOT NULL,
     applied_at TIMESTAMP WITH TIME ZONE NOT NULL, PRIMARY KEY (version))`,
  `CREATE TABLE IF NOT EXISTS clients (id VARCHAR(32) NOT NULL, slug VARCHAR(40) NOT NULL, name VARCHAR(200) NOT NULL,
     api_key_hash VARCHAR(64) NOT NULL, webhook_secret VARCHAR(128) NOT NULL, active BOOLEAN NOT NULL,
     created_at TIMESTAMP WITH TIME ZONE NOT NULL, PRIMARY KEY (id))`,
  `CREATE UNIQUE INDEX IF NOT EXISTS ix_clients_api_key_hash ON clients (api_key_hash)`,
  `CREATE UNIQUE INDEX IF NOT EXISTS ix_clients_slug ON clients (slug)`,
  `CREATE TABLE IF NOT EXISTS jobs (id VARCHAR(32) NOT NULL, client_id VARCHAR(32), type VARCHAR(64) NOT NULL,
     payload JSON NOT NULL, status VARCHAR(16) NOT NULL, result JSON, error TEXT, attempts INTEGER NOT NULL,
     max_attempts INTEGER NOT NULL, source VARCHAR(32) NOT NULL, created_at TIMESTAMP WITH TIME ZONE NOT NULL,
     run_at TIMESTAMP WITH TIME ZONE NOT NULL, started_at TIMESTAMP WITH TIME ZONE, finished_at TIMESTAMP WITH TIME ZONE,
     PRIMARY KEY (id), FOREIGN KEY(client_id) REFERENCES clients (id) ON DELETE SET NULL)`,
  `CREATE INDEX IF NOT EXISTS ix_jobs_client_id ON jobs (client_id)`,
  `CREATE INDEX IF NOT EXISTS ix_jobs_created_at ON jobs (created_at)`,
  `CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs (status)`,
  `CREATE INDEX IF NOT EXISTS ix_jobs_status_run_at ON jobs (status, run_at)`,
  `CREATE INDEX IF NOT EXISTS ix_jobs_type ON jobs (type)`,
  `CREATE TABLE IF NOT EXISTS execution_logs (id SERIAL NOT NULL, job_id VARCHAR(32) NOT NULL, client_id VARCHAR(32),
     worker_id VARCHAR(100) NOT NULL, attempt INTEGER NOT NULL, status VARCHAR(16) NOT NULL, duration_ms INTEGER NOT NULL,
     error TEXT, created_at TIMESTAMP WITH TIME ZONE NOT NULL, PRIMARY KEY (id))`,
  `CREATE INDEX IF NOT EXISTS ix_execution_logs_client_id ON execution_logs (client_id)`,
  `CREATE INDEX IF NOT EXISTS ix_execution_logs_created_at ON execution_logs (created_at)`,
  `CREATE INDEX IF NOT EXISTS ix_execution_logs_job_id ON execution_logs (job_id)`,
  `CREATE TABLE IF NOT EXISTS api_usage (id SERIAL NOT NULL, provider VARCHAR(32) NOT NULL, model VARCHAR(100) NOT NULL,
     client_id VARCHAR(32), job_id VARCHAR(32), success BOOLEAN NOT NULL, status_code INTEGER, error_kind VARCHAR(32),
     prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL, latency_ms INTEGER NOT NULL,
     created_at TIMESTAMP WITH TIME ZONE NOT NULL, PRIMARY KEY (id))`,
  `CREATE INDEX IF NOT EXISTS ix_api_usage_client_id ON api_usage (client_id)`,
  `CREATE INDEX IF NOT EXISTS ix_api_usage_created_at ON api_usage (created_at)`,
  `CREATE INDEX IF NOT EXISTS ix_api_usage_provider ON api_usage (provider)`,
  `CREATE TABLE IF NOT EXISTS worker_health (worker_id VARCHAR(100) NOT NULL, hostname VARCHAR(200) NOT NULL,
     status VARCHAR(16) NOT NULL, jobs_processed INTEGER NOT NULL, jobs_failed INTEGER NOT NULL,
     started_at TIMESTAMP WITH TIME ZONE NOT NULL, last_heartbeat TIMESTAMP WITH TIME ZONE NOT NULL, PRIMARY KEY (worker_id))`,
  `CREATE INDEX IF NOT EXISTS ix_worker_health_last_heartbeat ON worker_health (last_heartbeat)`,
];

// v2 = same columns/indexes as db/migrate.py's _v2_monetization.
export const MIGRATION_V2_DESC = "monetization: plans, quotas, outbound delivery, agencies, billing";
export const MIGRATION_V2: string[] = [
  `ALTER TABLE clients ADD COLUMN IF NOT EXISTS plan VARCHAR(32) NOT NULL DEFAULT 'retainer'`,
  `ALTER TABLE clients ADD COLUMN IF NOT EXISTS monthly_job_limit INTEGER`,
  `ALTER TABLE clients ADD COLUMN IF NOT EXISTS allowed_handlers JSON`,
  `ALTER TABLE clients ADD COLUMN IF NOT EXISTS outbound_webhook_url TEXT`,
  `ALTER TABLE clients ADD COLUMN IF NOT EXISTS config JSON`,
  `ALTER TABLE clients ADD COLUMN IF NOT EXISTS agency_id VARCHAR(32) REFERENCES clients (id) ON DELETE SET NULL`,
  `ALTER TABLE clients ADD COLUMN IF NOT EXISTS contact_email VARCHAR(200)`,
  `ALTER TABLE clients ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(64)`,
  `CREATE INDEX IF NOT EXISTS ix_clients_agency_id ON clients (agency_id)`,
  `CREATE UNIQUE INDEX IF NOT EXISTS ix_clients_stripe_customer_id ON clients (stripe_customer_id)`,
  `CREATE INDEX IF NOT EXISTS ix_jobs_client_created ON jobs (client_id, created_at)`,
];

export async function migrate(db: Db): Promise<void> {
  const record = (v: number, d: string) => ({
    text: "INSERT INTO schema_migrations (version, description, applied_at) VALUES ($1, $2, now()) ON CONFLICT (version) DO NOTHING",
    params: [v, d],
  });
  await db.transaction([
    { text: "SELECT pg_advisory_xact_lock(727001)" }, // same lock id as the Python migrator
    ...MIGRATION_V1.map((text) => ({ text })),
    record(1, MIGRATION_V1_DESC),
    ...MIGRATION_V2.map((text) => ({ text })),
    record(2, MIGRATION_V2_DESC),
  ]);
}

// 42P01 undefined_table (fresh DB), 42703 undefined_column (DB on an older schema version)
const isMissingTable = (e: unknown) => ["42P01", "42703"].includes((e as { code?: string })?.code ?? "");

/**
 * Wraps a Db with (1) the per-invocation subrequest budget and (2) lazy migration: the schema
 * is only created/upgraded when a query hits a missing table or column, so routine traffic never spends
 * a round trip (or wakes Neon) just to check the schema.
 */
export function managedDb(inner: Db, budget: Budget): Db {
  const retryOnMissing = async <T>(fn: () => Promise<T>): Promise<T> => {
    try {
      budget.take();
      return await fn();
    } catch (e) {
      if (!isMissingTable(e)) throw e;
      budget.take(2);
      await migrate(inner);
      return await fn();
    }
  };
  return {
    query: (text, params) => retryOnMissing(() => inner.query(text, params)),
    transaction: (stmts) => retryOnMissing(() => inner.transaction(stmts)),
  };
}
