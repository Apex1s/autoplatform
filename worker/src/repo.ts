// SQL for every operation. Mirrors db/repo.py so both runtimes behave the same.
import type { Db, Statement } from "./db";
import { DEFAULT_PLAN, effectiveHandlers, effectiveLimit } from "./plans";
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
  plan: string;
  monthly_job_limit: number | null;
  allowed_handlers: string[] | null;
  outbound_webhook_url: string | null;
  config: Record<string, any> | null;
  agency_id: string | null;
  contact_email: string | null;
  stripe_customer_id: string | null;
}

const CLIENT_COLS = `id, slug, name, webhook_secret, active, created_at, plan, monthly_job_limit, allowed_handlers,
  outbound_webhook_url, config, agency_id, contact_email, stripe_customer_id`;

function clientRow(r: Record<string, any> | undefined): ClientRow | null {
  if (!r) return null;
  return { ...r, allowed_handlers: parseJson(r.allowed_handlers) as string[] | null,
           config: (parseJson(r.config) as Record<string, any> | null) ?? {} } as ClientRow;
}

const SECRET_SUFFIXES = ["_secret", "_token", "_key", "_password"];

export function maskConfig(cfg: Record<string, any> | null | undefined): Record<string, any> {
  const out: Record<string, any> = {};
  for (const [k, v] of Object.entries(cfg ?? {})) {
    out[k] = v && SECRET_SUFFIXES.some((sfx) => k.endsWith(sfx)) ? "****" + String(v).slice(-4) : v;
  }
  return out;
}

export function clientView(c: ClientRow, monthJobs?: number) {
  return {
    id: c.id, slug: c.slug, name: c.name, active: c.active, plan: c.plan,
    monthly_job_limit: effectiveLimit(c.plan, c.monthly_job_limit),
    allowed_handlers: effectiveHandlers(c.plan, c.allowed_handlers),
    outbound_webhook_url: c.outbound_webhook_url, config: maskConfig(c.config), agency_id: c.agency_id,
    contact_email: c.contact_email, created_at: iso(c.created_at), ...(monthJobs !== undefined ? { month_jobs: monthJobs } : {}),
  };
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
export interface NewClient {
  plan?: string; monthly_job_limit?: number | null; allowed_handlers?: string[] | null; outbound_webhook_url?: string | null;
  config?: Record<string, any> | null; agency_id?: string | null; contact_email?: string | null; stripe_customer_id?: string | null;
}

export async function createClient(db: Db, slug: string, name: string, o: NewClient = {}) {
  const apiKey = "ap_" + randomToken(32);
  const id = newId(), webhook_secret = randomHex(32);
  const rows = await db.query(
    `INSERT INTO clients (id, slug, name, api_key_hash, webhook_secret, active, created_at, plan, monthly_job_limit,
       allowed_handlers, outbound_webhook_url, config, agency_id, contact_email, stripe_customer_id)
     VALUES ($1, $2, $3, $4, $5, true, now(), $6, $7, $8::json, $9, $10::json, $11, $12, $13) RETURNING ${CLIENT_COLS}`,
    [id, slug, name, await sha256Hex(apiKey), webhook_secret, o.plan ?? DEFAULT_PLAN, o.monthly_job_limit ?? null,
     o.allowed_handlers ? JSON.stringify(o.allowed_handlers) : null, o.outbound_webhook_url ?? null,
     JSON.stringify(o.config ?? {}), o.agency_id ?? null, o.contact_email ?? null, o.stripe_customer_id ?? null],
  );
  return { client: clientRow(rows[0]) as ClientRow, apiKey };
}

export async function clientByApiKey(db: Db, apiKey: string): Promise<ClientRow | null> {
  const rows = await db.query(`SELECT ${CLIENT_COLS} FROM clients WHERE api_key_hash = $1 AND active = true`, [await sha256Hex(apiKey)]);
  return clientRow(rows[0]);
}

export async function clientBySlug(db: Db, slug: string): Promise<ClientRow | null> {
  const rows = await db.query(`SELECT ${CLIENT_COLS} FROM clients WHERE slug = $1 AND active = true`, [slug]);
  return clientRow(rows[0]);
}

export async function clientById(db: Db, id: string): Promise<ClientRow | null> {
  return clientRow((await db.query(`SELECT ${CLIENT_COLS} FROM clients WHERE id = $1`, [id]))[0]);
}

export async function clientByStripeCustomer(db: Db, customerId: string): Promise<ClientRow | null> {
  return clientRow((await db.query(`SELECT ${CLIENT_COLS} FROM clients WHERE stripe_customer_id = $1`, [customerId]))[0]);
}

const UPDATABLE = ["name", "plan", "monthly_job_limit", "allowed_handlers", "outbound_webhook_url", "config",
  "contact_email", "active", "stripe_customer_id"];

/** Partial update; `config` is merged key-by-key (null values delete keys), like repo.update_client in Python. */
export async function updateClient(db: Db, id: string, fields: Record<string, any>): Promise<ClientRow | null> {
  const current = await clientById(db, id);
  if (!current) return null;
  const sets: string[] = [];
  const params: unknown[] = [id];
  for (const [k, raw] of Object.entries(fields)) {
    if (!UPDATABLE.includes(k) || raw === undefined) continue;
    let v = raw;
    if (k === "config") {
      const merged: Record<string, any> = { ...(current.config ?? {}), ...(raw ?? {}) };
      v = Object.fromEntries(Object.entries(merged).filter(([, x]) => x !== null));
    }
    const json = k === "config" || k === "allowed_handlers";
    params.push(json ? (v === null ? null : JSON.stringify(v)) : v);
    sets.push(`${k} = $${params.length}${json ? "::json" : ""}`);
  }
  if (!sets.length) return current;
  return clientRow((await db.query(`UPDATE clients SET ${sets.join(", ")} WHERE id = $1 RETURNING ${CLIENT_COLS}`, params))[0]);
}

export async function subClients(db: Db, agencyId: string): Promise<ClientRow[]> {
  const rows = await db.query(`SELECT ${CLIENT_COLS} FROM clients WHERE agency_id = $1 ORDER BY created_at`, [agencyId]);
  return rows.map((r) => clientRow(r) as ClientRow);
}

export async function monthJobCounts(db: Db, ids: string[]): Promise<Record<string, number>> {
  if (!ids.length) return {};
  const rows = await db.query(
    `SELECT client_id, count(*)::int AS n FROM jobs WHERE client_id = ANY($1::varchar[])
       AND created_at >= date_trunc('month', now()) AND type NOT LIKE 'internal.%' GROUP BY client_id`, [ids]);
  return Object.fromEntries(rows.map((r) => [r.client_id, Number(r.n)]));
}

export async function clientsForWeeklyReports(db: Db): Promise<string[]> {
  const rows = await db.query(`SELECT id, config FROM clients WHERE active = true AND plan <> 'demo'`);
  return rows.filter((r) => ((parseJson(r.config) as any) ?? {}).weekly_report !== false).map((r) => r.id as string);
}

/** Raw rows for the ROI report in ONE round trip (aggregated in products.ts, identically to Python). */
export async function reportRows(db: Db, clientId: string, days: number) {
  const [jobs, durations, usage] = await db.transaction([
    { text: `SELECT type, status, created_at, finished_at, result FROM jobs
             WHERE client_id = $1 AND created_at >= now() - make_interval(days => $2) LIMIT 20000`, params: [clientId, days] },
    { text: `SELECT l.duration_ms FROM execution_logs l JOIN jobs j ON j.id = l.job_id
             WHERE l.client_id = $1 AND l.created_at >= now() - make_interval(days => $2) AND l.status = 'succeeded'
               AND j.type NOT LIKE 'internal.%' LIMIT 20000`, params: [clientId, days] },
    { text: `SELECT job_id, provider, success, prompt_tokens, completion_tokens FROM api_usage
             WHERE client_id = $1 AND created_at >= now() - make_interval(days => $2) LIMIT 50000`, params: [clientId, days] },
  ]);
  return {
    jobs: jobs.map((r) => [r.type, r.status, r.created_at, r.finished_at, parseJson(r.result)] as [string, string, unknown, unknown, any]),
    durations: durations.map((r) => Number(r.duration_ms)),
    usage: usage.map((r) => [r.job_id, r.provider, Boolean(r.success), Number(r.prompt_tokens), Number(r.completion_tokens)] as
      [string | null, string, boolean, number, number]),
  };
}

/** Many jobs in one INSERT (delivery fan-out, weekly reports): one subrequest regardless of count. */
export async function createJobsBulk(
  db: Db, jobs: { type: string; payload: unknown; clientId: string | null; maxAttempts: number; source: string }[],
): Promise<string[]> {
  if (!jobs.length) return [];
  const ids = jobs.map(() => newId());
  const params: unknown[] = [];
  const values = jobs.map((j, i) => {
    params.push(ids[i], j.clientId, j.type, JSON.stringify(j.payload ?? {}), j.maxAttempts, j.source);
    const b = params.length - 6;
    return `($${b + 1}, $${b + 2}, $${b + 3}, $${b + 4}::json, 'queued', 0, $${b + 5}, $${b + 6}, now(), now())`;
  });
  await db.query(`INSERT INTO jobs (id, client_id, type, payload, status, attempts, max_attempts, source, created_at, run_at)
                  VALUES ${values.join(", ")}`, params);
  return ids;
}

export async function slugExists(db: Db, slug: string): Promise<boolean> {
  return (await db.query("SELECT 1 FROM clients WHERE slug = $1", [slug])).length > 0;
}

export async function listClients(db: Db) {
  const rows = await db.query("SELECT id, slug, name, active, plan, agency_id, created_at FROM clients ORDER BY created_at");
  return rows.map((r) => ({ id: r.id, slug: r.slug, name: r.name, active: r.active, plan: r.plan, agency_id: r.agency_id,
                            created_at: iso(r.created_at) }));
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
