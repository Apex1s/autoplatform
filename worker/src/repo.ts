// SQL for every operation. Mirrors db/repo.py so both runtimes behave the same.
import type { Db, Statement } from "./db";
import { iso, newId, randomHex, randomToken, sha256Hex } from "./util";

export const STATUS = { QUEUED: "queued", RUNNING: "running", RETRYING: "retrying", SUCCEEDED: "succeeded", FAILED: "failed" } as const;
export const ALL_STATUSES = Object.values(STATUS);

export interface ClientRow {
  id: string;
  slug: string;
  name: string;
  webhook_secret: string;
  active: boolean;
  created_at: unknown;
}

export interface JobView {
  id: string;
  client_id: string | null;
  type: string;
  status: string;
  attempts: number;
  max_attempts: number;
  source: string;
  payload: unknown;
  result: unknown;
  error: string | null;
  created_at: string | null;
  run_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

const parseJson = (v: unknown) => (typeof v === "string" ? safeParse(v) : v);
function safeParse(s: string): unknown {
  try {
    return JSON.parse(s);
  } catch {
    return s;
  }
}

export function jobView(r: Record<string, any>): JobView {
  return {
    id: r.id, client_id: r.client_id ?? null, type: r.type, status: r.status, attempts: Number(r.attempts),
    max_attempts: Number(r.max_attempts), source: r.source, payload: parseJson(r.payload), result: parseJson(r.result),
    error: r.error ?? null, created_at: iso(r.created_at), run_at: iso(r.run_at), started_at: iso(r.started_at),
    finished_at: iso(r.finished_at),
  };
}

const JOB_COLS = "id, client_id, type, status, attempts, max_attempts, source, payload, result, error, created_at, run_at, started_at, finished_at";

// ------------------------------------------------------------------ clients
export async function createClient(db: Db, slug: string, name: string) {
  const apiKey = "ap_" + randomToken(32);
  const row = { id: newId(), slug, name, webhook_secret: randomHex(32), api_key_hash: await sha256Hex(apiKey) };
  await db.query(
    `INSERT INTO clients (id, slug, name, api_key_hash, webhook_secret, active, created_at)
     VALUES ($1, $2, $3, $4, $5, true, now())`,
    [row.id, row.slug, row.name, row.api_key_hash, row.webhook_secret],
  );
  return { ...row, apiKey };
}

export async function clientByApiKey(db: Db, apiKey: string): Promise<ClientRow | null> {
  const rows = await db.query<ClientRow>(
    "SELECT id, slug, name, webhook_secret, active, created_at FROM clients WHERE api_key_hash = $1 AND active = true",
    [await sha256Hex(apiKey)],
  );
  return rows[0] ?? null;
}

export async function clientBySlug(db: Db, slug: string): Promise<ClientRow | null> {
  const rows = await db.query<ClientRow>(
    "SELECT id, slug, name, webhook_secret, active, created_at FROM clients WHERE slug = $1 AND active = true",
    [slug],
  );
  return rows[0] ?? null;
}

export async function slugExists(db: Db, slug: string): Promise<boolean> {
  return (await db.query("SELECT 1 FROM clients WHERE slug = $1", [slug])).length > 0;
}

export async function listClients(db: Db) {
  const rows = await db.query("SELECT id, slug, name, active, created_at FROM clients ORDER BY created_at");
  return rows.map((r) => ({ id: r.id, slug: r.slug, name: r.name, active: r.active, created_at: iso(r.created_at) }));
}

export async function deactivateClient(db: Db, slug: string): Promise<boolean> {
  return (await db.query("UPDATE clients SET active = false WHERE slug = $1 AND active = true RETURNING id", [slug])).length === 1;
}

// --------------------------------------------------------------------- jobs
export async function createJob(
  db: Db, type: string, payload: unknown,
  o: { id?: string; clientId: string | null; maxAttempts: number; source: string; delayS?: number },
): Promise<string> {
  const id = o.id ?? newId();
  await db.query(
    `INSERT INTO jobs (id, client_id, type, payload, status, attempts, max_attempts, source, created_at, run_at)
     VALUES ($1, $2, $3, $4::json, 'queued', 0, $5, $6, now(), now() + make_interval(secs => $7))`,
    [id, o.clientId, type, JSON.stringify(payload ?? {}), o.maxAttempts, o.source, o.delayS ?? 0],
  );
  return id;
}

export async function getJob(db: Db, id: string, clientId?: string): Promise<JobView | null> {
  const rows = await db.query(`SELECT ${JOB_COLS} FROM jobs WHERE id = $1`, [id]);
  if (!rows[0] || (clientId !== undefined && rows[0].client_id !== clientId)) return null;
  return jobView(rows[0]);
}

export async function listJobs(db: Db, clientId: string, limit: number): Promise<JobView[]> {
  const rows = await db.query(`SELECT ${JOB_COLS} FROM jobs WHERE client_id = $1 ORDER BY created_at DESC LIMIT $2`, [clientId, limit]);
  return rows.map(jobView);
}

/** Atomic claim: only one runner can move a job from queued/retrying to running. */
export async function claimJob(db: Db, id: string): Promise<JobView | null> {
  const rows = await db.query(
    `UPDATE jobs SET status = 'running', started_at = now(), attempts = attempts + 1
     WHERE id = $1 AND status IN ('queued', 'retrying') RETURNING ${JOB_COLS}`,
    [id],
  );
  return rows[0] ? jobView(rows[0]) : null;
}

export interface UsageRecord {
  provider: string; model: string; client_id: string | null; job_id: string | null; success: boolean;
  status_code: number | null; error_kind: string | null; prompt_tokens: number; completion_tokens: number; latency_ms: number;
}

function usageInsert(records: UsageRecord[]) {
  const params: unknown[] = [];
  const values = records.map((u) => {
    const base = params.length;
    params.push(u.provider, u.model.slice(0, 100), u.client_id, u.job_id, u.success, u.status_code, u.error_kind,
      u.prompt_tokens, u.completion_tokens, u.latency_ms);
    return `(${Array.from({ length: 10 }, (_, i) => `$${base + i + 1}`).join(", ")}, now())`;
  });
  return {
    text: `INSERT INTO api_usage (provider, model, client_id, job_id, success, status_code, error_kind,
           prompt_tokens, completion_tokens, latency_ms, created_at) VALUES ${values.join(", ")}`,
    params,
  };
}

/** Job status + execution log + LLM usage rows in ONE transaction (one subrequest). */
export async function finishJob(
  db: Db, id: string,
  o: { status: string; workerId: string; attempt: number; durationMs: number; clientId: string | null;
       result?: unknown; error?: string | null; retryDelayS?: number; usage?: UsageRecord[] },
): Promise<void> {
  const stmts: Statement[] = [
    o.status === STATUS.RETRYING
      ? { text: "UPDATE jobs SET status = $2, error = $3, run_at = now() + make_interval(secs => $4) WHERE id = $1",
          params: [id, o.status, o.error ?? null, o.retryDelayS ?? 0] }
      : { text: "UPDATE jobs SET status = $2, error = $3, result = $4::json, finished_at = now() WHERE id = $1",
          params: [id, o.status, o.error ?? null, o.result === undefined ? null : JSON.stringify(o.result)] },
    { text: `INSERT INTO execution_logs (job_id, client_id, worker_id, attempt, status, duration_ms, error, created_at)
             VALUES ($1, $2, $3, $4, $5, $6, $7, now())`,
      params: [id, o.clientId, o.workerId, o.attempt, o.status, o.durationMs, o.error ? o.error.slice(0, 4000) : null] },
  ];
  if (o.usage?.length) stmts.push(usageInsert(o.usage));
  await db.transaction(stmts);
}

export async function recordUsage(db: Db, usage: UsageRecord[]): Promise<void> {
  if (usage.length) {
    const s = usageInsert(usage);
    await db.query(s.text, s.params);
  }
}

export async function jobCounts(db: Db): Promise<Record<string, number>> {
  const rows = await db.query("SELECT status, count(*)::int AS n FROM jobs GROUP BY status");
  const out: Record<string, number> = Object.fromEntries(ALL_STATUSES.map((s) => [s, 0]));
  for (const r of rows) out[r.status] = Number(r.n);
  return out;
}

/** Jobs that should be in Redis but probably aren't; running jobs past their lease go back to retrying. */
export async function stuckJobs(db: Db, waitingMinutes: number, runningSeconds: number): Promise<string[]> {
  const [waiting, running] = await db.transaction([
    { text: `SELECT id FROM jobs WHERE status IN ('queued', 'retrying') AND run_at < now() - make_interval(mins => $1) LIMIT 500`,
      params: [waitingMinutes] },
    { text: `UPDATE jobs SET status = 'retrying', error = 'recovered: runner lost mid-run'
             WHERE id IN (SELECT id FROM jobs WHERE status = 'running' AND started_at < now() - make_interval(secs => $1) LIMIT 500)
             RETURNING id`,
      params: [runningSeconds] },
  ]);
  return [...waiting, ...running].map((r) => r.id as string);
}

export async function prune(db: Db, days: number) {
  const [a, b, c] = await db.transaction([
    { text: "DELETE FROM execution_logs WHERE created_at < now() - make_interval(days => $1) RETURNING 1", params: [days] },
    { text: "DELETE FROM api_usage WHERE created_at < now() - make_interval(days => $1) RETURNING 1", params: [days] },
    { text: `DELETE FROM jobs WHERE status IN ('succeeded', 'failed') AND finished_at < now() - make_interval(days => $1) RETURNING 1`,
      params: [days] },
  ]);
  return { execution_logs: a.length, api_usage: b.length, jobs: c.length };
}

export async function usageSummary(db: Db, hours = 24) {
  const rows = await db.query(
    `SELECT provider, success, count(*)::int AS calls, coalesce(sum(prompt_tokens), 0)::int AS pt,
            coalesce(sum(completion_tokens), 0)::int AS ct
     FROM api_usage WHERE created_at >= now() - make_interval(hours => $1) GROUP BY provider, success`,
    [hours],
  );
  return rows.map((r) => ({ provider: r.provider, success: Boolean(r.success), calls: Number(r.calls),
    prompt_tokens: Number(r.pt), completion_tokens: Number(r.ct) }));
}

export async function heartbeat(db: Db, workerId: string, hostname: string, processed: number, failed: number): Promise<void> {
  await db.query(
    `INSERT INTO worker_health (worker_id, hostname, status, jobs_processed, jobs_failed, started_at, last_heartbeat)
     VALUES ($1, $2, 'alive', $3, $4, now(), now())
     ON CONFLICT (worker_id) DO UPDATE SET status = 'alive', jobs_processed = $3, jobs_failed = $4, last_heartbeat = now()`,
    [workerId, hostname, processed, failed],
  );
}

export async function dbPing(db: Db): Promise<boolean> {
  return (await db.query<{ ok: number }>("SELECT 1 AS ok"))[0]?.ok === 1;
}
