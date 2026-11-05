// HTTP API: same routes, payloads and response shapes as app/main.py (FastAPI),
// so clients, scripts/e2e_smoke.py and both runtimes are interchangeable.
import type { Deps } from "./deps";
import { configWarnings } from "./env";
import { admit, baseUrl, checkHandlers, checkHttps, credentials, enqueue, readJson, requireAdmin, requireClient, requireCron,
  type Ctx, type Route, type RouteEntry, validateTask } from "./http";
import { CORS_HEADERS, MONETIZATION_ROUTES } from "./monetization";
import { DEFAULT_PLAN, PLANS, RESERVED_SLUGS } from "./plans";
import { clientJobTypes, isClientCallable } from "./registry";
import * as repo from "./repo";
import { bearer, HttpError, json, safeEqual } from "./util";

export const VERSION = "1.2.0";
let healthCache: { at: number; status: number; body: unknown } | null = null;


// ------------------------------------------------------------------- routes
const root: Route = async () =>
  json({ service: "autoplatform", version: VERSION, runtime: "cloudflare-workers", health: "/healthz", metrics: "/metrics",
         job_types: clientJobTypes() });

const livez: Route = async () => json({ status: "alive" });

const healthz: Route = async (_req, deps) => {
  const ttlMs = deps.cfg.healthzCacheS * 1000;
  if (ttlMs > 0 && healthCache && Date.now() - healthCache.at < ttlMs) {
    return json(healthCache.body, healthCache.status, { "x-cache": "hit" });
  }
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
  const body = {
    status, version: VERSION, env: deps.cfg.appEnv, runtime: "cloudflare-workers",
    backends: { database: "neon-https", queue: "upstash-rest" },
    checks, config_warnings: warnings, last_cron_age_seconds: cronAge,
  };
  healthCache = { at: Date.now(), status: coreOk ? 200 : 503, body };
  return json(body, coreOk ? 200 : 503);
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
  if (!isClientCallable(t.type)) throw new HttpError(422, `unknown job type; allowed: ${JSON.stringify(clientJobTypes())}`);
  await admit(deps, client, t.type);
  const out: Record<string, unknown> = await enqueue(req, deps, ctx, t.type, t.payload, {
    clientId: client.id, source: "webhook", maxAttempts: t.maxAttempts, delayS: t.delayS,
    idemKey: req.headers.get("idempotency-key"),
  });
  if (out.duplicate) await deps.queue.quotaDecr(client.id);
  return json(out, 202);
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
  if (RESERVED_SLUGS.has(slug)) throw new HttpError(422, "slug is reserved");
  if (typeof name !== "string" || !name.length || name.length > 200) throw new HttpError(422, "invalid name");
  const planName = body.plan ?? DEFAULT_PLAN;
  if (!(planName in PLANS)) throw new HttpError(422, `unknown plan; available: ${JSON.stringify(Object.keys(PLANS).sort())}`);
  const limit = body.monthly_job_limit ?? null;
  if (limit !== null && !(Number.isInteger(limit) && limit >= 0 && limit <= 10_000_000)) throw new HttpError(422, "invalid monthly_job_limit");
  if (body.config !== undefined && body.config !== null && (typeof body.config !== "object" || Array.isArray(body.config))) {
    throw new HttpError(422, "config must be an object");
  }
  if (await repo.slugExists(deps.db, slug)) throw new HttpError(409, "slug already exists");
  const { client, apiKey } = await repo.createClient(deps.db, slug, name, {
    plan: planName, monthly_job_limit: limit, allowed_handlers: checkHandlers(body.allowed_handlers),
    outbound_webhook_url: checkHttps(body.outbound_webhook_url), config: body.config ?? {},
    contact_email: typeof body.contact_email === "string" ? body.contact_email.slice(0, 200) : null,
  });
  return json(credentials(client, apiKey, baseUrl(req, deps)), 201);
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

const ROUTES: RouteEntry[] = [
  ...MONETIZATION_ROUTES,
  ["GET", /^\/$/, root],
  ["GET", /^\/livez$/, livez],
  ["GET", /^\/healthz$/, healthz],
  ["GET", /^\/metrics$/, metrics],
  ["POST", /^\/webhook$/, postTask],
  ["POST", /^\/tasks$/, postTask],
  ["GET", /^\/tasks$/, listTasks],
  ["GET", /^\/tasks\/([A-Za-z0-9]{1,64})$/, getTask],
  ["POST", /^\/cron\/([a-z_]{1,40})$/, cron],
  ["GET", /^\/cron\/jobs\/([A-Za-z0-9]{1,64})$/, cronJob],
  ["POST", /^\/admin\/clients$/, createClient],
  ["GET", /^\/admin\/clients$/, listClients],
  ["DELETE", /^\/admin\/clients\/([a-z0-9-]{1,40})$/, deactivateClient],
];

export async function handle(req: Request, deps: Deps, ctx: Ctx): Promise<Response> {
  const path = new URL(req.url).pathname;
  if (path.startsWith("/demo")) {
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS_HEADERS });
    const res = await route(req, deps, ctx, path);
    const out = new Response(res.body, res);
    for (const [k, v] of Object.entries(CORS_HEADERS)) out.headers.set(k, v);
    return out;
  }
  return route(req, deps, ctx, path);
}

async function route(req: Request, deps: Deps, ctx: Ctx, path: string): Promise<Response> {
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

