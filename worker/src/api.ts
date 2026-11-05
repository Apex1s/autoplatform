// HTTP API: same routes, payloads and response shapes as app/main.py (FastAPI),
// so clients, scripts/e2e_smoke.py and both runtimes are interchangeable.
import type { Deps } from "./deps";
import { configWarnings } from "./env";
import { clientJobTypes, HANDLERS, SYSTEM_PREFIX } from "./handlers";
import * as repo from "./repo";
import { drain } from "./runner";
import { bearer, HttpError, hmacSha256Hex, json, newId, safeEqual } from "./util";

export const VERSION = "1.0.0";

type Ctx = Pick<ExecutionContext, "waitUntil">;
type Route = (req: Request, deps: Deps, ctx: Ctx, params: string[]) => Promise<Response>;

// ------------------------------------------------------------------ helpers
async function readBody(req: Request, max: number): Promise<Uint8Array> {
  const declared = Number(req.headers.get("content-length") ?? "");
  if (declared > max) throw new HttpError(413, "payload too large");
  if (!req.body) return new Uint8Array();
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > max) {
      await reader.cancel();
      throw new HttpError(413, "payload too large");
    }
    chunks.push(value);
  }
  const out = new Uint8Array(size);
  let off = 0;
  for (const c of chunks) {
    out.set(c, off);
    off += c.byteLength;
  }
  return out;
}

async function readJson(req: Request, deps: Deps): Promise<any> {
  const raw = await readBody(req, deps.cfg.maxBodyBytes);
  try {
    return JSON.parse(new TextDecoder().decode(raw) || "null");
  } catch {
    throw new HttpError(422, "body must be valid JSON");
  }
}

const baseUrl = (req: Request, deps: Deps) => deps.cfg.publicBaseUrl || new URL(req.url).origin;

function requireAdmin(req: Request, deps: Deps): void {
  if (!deps.cfg.adminToken) throw new HttpError(503, "ADMIN_TOKEN not configured");
  if (!safeEqual(bearer(req), deps.cfg.adminToken)) throw new HttpError(401, "invalid admin token");
}

function requireCron(req: Request, deps: Deps): void {
  const { cronToken, adminToken } = deps.cfg;
  if (!cronToken) throw new HttpError(503, "CRON_TOKEN/ADMIN_TOKEN not configured");
  const t = bearer(req);
  if (!t || !(safeEqual(t, cronToken) || (adminToken !== "" && safeEqual(t, adminToken)))) {
    throw new HttpError(401, "invalid cron token");
  }
}

async function requireClient(req: Request, deps: Deps): Promise<repo.ClientRow> {
  const key = req.headers.get("x-api-key");
  if (!key) throw new HttpError(401, "missing X-API-Key");
  const c = await repo.clientByApiKey(deps.db, key);
  if (!c) throw new HttpError(401, "invalid API key");
  return c;
}

function validateTask(body: any): { type: string; payload: Record<string, unknown>; maxAttempts: number | null; delayS: number } {
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new HttpError(422, "body must be a JSON object");
  if (typeof body.type !== "string" || !body.type) throw new HttpError(422, "'type' is required");
  const payload = body.payload ?? {};
  if (typeof payload !== "object" || Array.isArray(payload)) throw new HttpError(422, "'payload' must be an object");
  const ma = body.max_attempts ?? null;
  if (ma !== null && !(Number.isInteger(ma) && ma >= 1 && ma <= 10)) throw new HttpError(422, "'max_attempts' must be 1..10");
  const delay = body.delay_seconds ?? 0;
  if (!(Number.isInteger(delay) && delay >= 0 && delay <= 7 * 86400)) throw new HttpError(422, "'delay_seconds' must be 0..604800");
  return { type: body.type, payload, maxAttempts: ma, delayS: delay };
}

/** Insert + enqueue, then (after the response) run queued work right away. */
async function enqueue(
  req: Request, deps: Deps, ctx: Ctx, type: string, payload: unknown,
  o: { clientId: string | null; source: string; maxAttempts?: number | null; delayS?: number; idemKey?: string | null },
) {
  const id = newId();
  const statusUrl = (jid: string) => `${baseUrl(req, deps)}/tasks/${jid}`;
  if (o.idemKey) {
    const existing = await deps.queue.claimIdempotency(`${o.clientId ?? "system"}:${o.idemKey}`, id);
    if (existing) return { job_id: existing, status: "duplicate", duplicate: true, status_url: statusUrl(existing) };
  }
  await repo.createJob(deps.db, type, payload, {
    id, clientId: o.clientId, maxAttempts: o.maxAttempts ?? deps.cfg.jobMaxAttempts, source: o.source, delayS: o.delayS ?? 0,
  });
  try {
    await deps.queue.enqueue(id, o.delayS ?? 0);
  } catch (e) {
    // row stays queued; maintenance re-enqueues it once Redis is reachable
    console.error(`enqueue failed for ${id}; maintenance will recover it`, e);
  }
  if (!o.delayS) ctx.waitUntil(drain(deps, { maxJobs: 2, deadline: Date.now() + 25_000 }).catch((e) => console.error("inline drain", e)));
  return { job_id: id, status: "queued", type, status_url: statusUrl(id) };
}

// ------------------------------------------------------------------- routes
const root: Route = async () =>
  json({ service: "autoplatform", version: VERSION, runtime: "cloudflare-workers", health: "/healthz", metrics: "/metrics",
         job_types: clientJobTypes() });

const livez: Route = async () => json({ status: "alive" });

const healthz: Route = async (_req, deps) => {
  const checks: Record<string, unknown> = {};
  const settle = async (name: string, fn: () => Promise<boolean>) => {
    try {
      checks[name] = (await fn()) ? "ok" : "fail";
    } catch (e) {
      checks[name] = `fail: ${String((e as Error).message).slice(0, 120)}`;
    }
  };
  await Promise.all([settle("database", () => repo.dbPing(deps.db)), settle("redis", () => deps.queue.ping())]);
  let cronAge: number | null = null;
  try {
    const last = await deps.queue.lastCronRun();
    cronAge = last ? Math.round(Date.now() / 1000 - Number(last.ts)) : null;
  } catch {
    /* reported via redis check */
  }
  checks.worker = cronAge === null ? "waiting for first cron run" : cronAge <= 180 ? "ok" : `stalled (last cron ${cronAge}s ago)`;
  checks.llm_providers = deps.router.configured.length ? deps.router.configured : "none configured";
  const warnings = configWarnings(deps.cfg);
  const critical = warnings.filter((w) => !w.startsWith("only one"));
  const coreOk = checks.database === "ok" && checks.redis === "ok";
  const status = coreOk && checks.worker === "ok" && !critical.length ? "ok" : coreOk ? "degraded" : "down";
  return json({
    status, version: VERSION, env: deps.cfg.appEnv, runtime: "cloudflare-workers",
    backends: { database: "neon-https", queue: "upstash-rest" },
    checks, config_warnings: warnings, last_cron_age_seconds: cronAge,
  }, coreOk ? 200 : 503);
};

const metrics: Route = async (req, deps) => {
  const { cfg } = deps;
  const token = bearer(req);
  const adminOk = cfg.adminToken !== "" && safeEqual(token, cfg.adminToken);
  if (cfg.metricsToken) {
    if (!(safeEqual(token, cfg.metricsToken) || adminOk)) throw new HttpError(401, "invalid metrics token");
  } else if (cfg.production && !cfg.metricsPublic && !adminOk) {
    throw new HttpError(401, "metrics need METRICS_TOKEN or ADMIN_TOKEN in production");
  }
  const [jobs, usage] = await Promise.all([repo.jobCounts(deps.db), repo.usageSummary(deps.db, 24)]);
  let depth = { pending: -1, delayed: -1 }, stats: Record<string, number> = {}, last: Record<string, any> | null = null;
  try {
    [depth, stats, last] = await Promise.all([deps.queue.depth(), deps.queue.stats(), deps.queue.lastCronRun()]);
  } catch {
    /* redis down: still report DB metrics */
  }
  const cronAge = last ? Math.round(Date.now() / 1000 - Number(last.ts)) : -1;
  const alive = cronAge >= 0 && cronAge <= 180;
  const workers = alive ? [{ worker_id: "cloudflare-cron", status: "alive", jobs_processed: stats.processed ?? 0,
    jobs_failed: stats.failed ?? 0, heartbeat_age_seconds: cronAge }] : [];
  const data = { jobs, queue: depth, llm_usage_24h: usage, workers, last_cron: last };
  if (new URL(req.url).searchParams.get("format") === "json") return json(data);
  const lines: string[] = [];
  for (const [k, v] of Object.entries(jobs)) lines.push(`autoplatform_jobs{status="${k}"} ${v}`);
  for (const [k, v] of Object.entries(depth)) lines.push(`autoplatform_queue_depth{queue="${k}"} ${v}`);
  for (const u of usage) {
    const lbl = `provider="${u.provider}",success="${u.success}"`;
    lines.push(`autoplatform_llm_calls_24h{${lbl}} ${u.calls}`);
    lines.push(`autoplatform_llm_tokens_24h{${lbl}} ${u.prompt_tokens + u.completion_tokens}`);
  }
  lines.push(`autoplatform_workers_alive ${workers.length}`);
  lines.push(`autoplatform_worker_up{worker_id="cloudflare-cron"} ${alive ? 1 : 0}`);
  lines.push(`autoplatform_worker_heartbeat_age_seconds{worker_id="cloudflare-cron"} ${cronAge}`);
  lines.push(`autoplatform_worker_jobs_processed{worker_id="cloudflare-cron"} ${stats.processed ?? 0}`);
  lines.push(`autoplatform_worker_jobs_failed{worker_id="cloudflare-cron"} ${stats.failed ?? 0}`);
  return new Response(lines.join("\n") + "\n", { headers: { "content-type": "text/plain; version=0.0.4" } });
};

const postTask: Route = async (req, deps, ctx) => {
  const client = await requireClient(req, deps);
  const t = validateTask(await readJson(req, deps));
  if (!(t.type in HANDLERS) || t.type.startsWith(SYSTEM_PREFIX)) {
    throw new HttpError(422, `unknown job type; allowed: ${JSON.stringify(clientJobTypes())}`);
  }
  return json(await enqueue(req, deps, ctx, t.type, t.payload, {
    clientId: client.id, source: "webhook", maxAttempts: t.maxAttempts, delayS: t.delayS,
    idemKey: req.headers.get("idempotency-key"),
  }), 202);
};

const getTask: Route = async (req, deps, _ctx, [id]) => {
  const client = await requireClient(req, deps);
  const job = await repo.getJob(deps.db, id, client.id);
  if (!job) throw new HttpError(404, "job not found");
  return json(job);
};

const listTasks: Route = async (req, deps) => {
  const client = await requireClient(req, deps);
  const limit = Math.max(1, Math.min(Number(new URL(req.url).searchParams.get("limit") ?? 50) || 50, 200));
  return json(await repo.listJobs(deps.db, client.id, limit));
};

const signedWebhook: Route = async (req, deps, ctx, [slug]) => {
  const raw = await readBody(req, deps.cfg.maxBodyBytes);
  const client = await repo.clientBySlug(deps.db, slug);
  const sig = (req.headers.get("x-signature-256") ?? req.headers.get("x-hub-signature-256") ?? "").replace(/^sha256=/, "");
  if (!client || !sig) throw new HttpError(401, "invalid signature");
  if (!safeEqual(sig, await hmacSha256Hex(client.webhook_secret, raw))) throw new HttpError(401, "invalid signature");
  let body: unknown;
  try {
    body = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    body = { raw: new TextDecoder().decode(raw).slice(0, 10000) };
  }
  const event = req.headers.get("x-event-type") ?? req.headers.get("x-github-event");
  return json(await enqueue(req, deps, ctx, "webhook.event", { event, body }, {
    clientId: client.id, source: "signed_webhook",
    idemKey: req.headers.get("idempotency-key") ?? req.headers.get("x-github-delivery"),
  }), 202);
};

const CRON_JOBS: Record<string, string> = { maintenance: "system.maintenance", provider_probe: "system.provider_probe" };

const cron: Route = async (req, deps, ctx, [name]) => {
  requireCron(req, deps);
  if (!(name in CRON_JOBS)) throw new HttpError(404, `unknown cron job; allowed: ${JSON.stringify(Object.keys(CRON_JOBS).sort())}`);
  const out: Record<string, unknown> = await enqueue(req, deps, ctx, CRON_JOBS[name], {}, { clientId: null, source: "cron", maxAttempts: 1 });
  try {
    out.queue = await deps.queue.depth();
  } catch {
    /* optional */
  }
  return json(out, 202);
};

const cronJob: Route = async (req, deps, _ctx, [id]) => {
  requireCron(req, deps);
  const job = await repo.getJob(deps.db, id);
  if (!job) throw new HttpError(404, "job not found");
  return json(job);
};

const createClient: Route = async (req, deps) => {
  requireAdmin(req, deps);
  const body = await readJson(req, deps);
  const slug = body?.slug, name = body?.name;
  if (typeof slug !== "string" || !/^[a-z0-9][a-z0-9-]{2,39}$/.test(slug)) throw new HttpError(422, "invalid slug");
  if (typeof name !== "string" || !name.length || name.length > 200) throw new HttpError(422, "invalid name");
  if (await repo.slugExists(deps.db, slug)) throw new HttpError(409, "slug already exists");
  const c = await repo.createClient(deps.db, slug, name);
  const base = baseUrl(req, deps);
  return json({ id: c.id, slug: c.slug, name: c.name, api_key: c.apiKey, webhook_secret: c.webhook_secret,
    webhook_url: `${base}/webhook`, signed_webhook_url: `${base}/webhook/${c.slug}`,
    note: "api_key is shown once; store it now." }, 201);
};

const listClients: Route = async (req, deps) => {
  requireAdmin(req, deps);
  return json(await repo.listClients(deps.db));
};

const deactivateClient: Route = async (req, deps, _ctx, [slug]) => {
  requireAdmin(req, deps);
  if (!(await repo.deactivateClient(deps.db, slug))) throw new HttpError(404, "client not found");
  return json({ slug, active: false });
};

const ROUTES: [string, RegExp, Route][] = [
  ["GET", /^\/$/, root],
  ["GET", /^\/livez$/, livez],
  ["GET", /^\/healthz$/, healthz],
  ["GET", /^\/metrics$/, metrics],
  ["POST", /^\/webhook$/, postTask],
  ["POST", /^\/tasks$/, postTask],
  ["GET", /^\/tasks$/, listTasks],
  ["GET", /^\/tasks\/([A-Za-z0-9]{1,64})$/, getTask],
  ["POST", /^\/webhook\/([a-z0-9-]{1,40})$/, signedWebhook],
  ["POST", /^\/cron\/([a-z_]{1,40})$/, cron],
  ["GET", /^\/cron\/jobs\/([A-Za-z0-9]{1,64})$/, cronJob],
  ["POST", /^\/admin\/clients$/, createClient],
  ["GET", /^\/admin\/clients$/, listClients],
  ["DELETE", /^\/admin\/clients\/([a-z0-9-]{1,40})$/, deactivateClient],
];

export async function handle(req: Request, deps: Deps, ctx: Ctx): Promise<Response> {
  const path = new URL(req.url).pathname;
  try {
    // like the FastAPI BodySizeLimit middleware: reject oversized bodies before auth or routing
    if (Number(req.headers.get("content-length") ?? "") > deps.cfg.maxBodyBytes) throw new HttpError(413, "payload too large");
    let allowed = false;
    for (const [method, re, fn] of ROUTES) {
      const m = re.exec(path);
      if (!m) continue;
      allowed = true;
      if (method === req.method || (method === "GET" && req.method === "HEAD")) return await fn(req, deps, ctx, m.slice(1));
    }
    return json({ detail: allowed ? "Method Not Allowed" : "Not Found" }, allowed ? 405 : 404);
  } catch (e) {
    if (e instanceof HttpError) return json({ detail: e.message }, e.status);
    console.error(`unhandled error on ${req.method} ${path}`, e);
    return json({ detail: "internal error" }, 500);
  }
}

