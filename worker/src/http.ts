// Shared HTTP helpers for api.ts and monetization.ts: body caps, auth, admission control, enqueue.
import type { Deps } from "./deps";
import { effectiveHandlers, effectiveLimit } from "./plans";
import { clientJobTypes } from "./registry";
import * as repo from "./repo";
import { drain } from "./runner";
import { bearer, HttpError, newId, safeEqual } from "./util";

export const API_KEY_RE = /^ap_[A-Za-z0-9_-]{43}$/;
export type Ctx = Pick<ExecutionContext, "waitUntil">;
export type Route = (req: Request, deps: Deps, ctx: Ctx, params: string[]) => Promise<Response>;
export type RouteEntry = [string, RegExp, Route];

// ------------------------------------------------------------------ helpers
export async function readBody(req: Request, max: number): Promise<Uint8Array> {
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

export async function readJson(req: Request, deps: Deps): Promise<any> {
  const raw = await readBody(req, deps.cfg.maxBodyBytes);
  try {
    return JSON.parse(new TextDecoder().decode(raw) || "null");
  } catch {
    throw new HttpError(422, "body must be valid JSON");
  }
}

export const baseUrl = (req: Request, deps: Deps) => deps.cfg.publicBaseUrl || new URL(req.url).origin;

export function requireAdmin(req: Request, deps: Deps): void {
  if (!deps.cfg.adminToken) throw new HttpError(503, "ADMIN_TOKEN not configured");
  if (!safeEqual(bearer(req), deps.cfg.adminToken)) throw new HttpError(401, "invalid admin token");
}

export function requireCron(req: Request, deps: Deps): void {
  const { cronToken, adminToken } = deps.cfg;
  if (!cronToken) throw new HttpError(503, "CRON_TOKEN/ADMIN_TOKEN not configured");
  const t = bearer(req);
  if (!t || !(safeEqual(t, cronToken) || (adminToken !== "" && safeEqual(t, adminToken)))) {
    throw new HttpError(401, "invalid cron token");
  }
}

export async function requireClient(req: Request, deps: Deps): Promise<repo.ClientRow> {
  const key = req.headers.get("x-api-key");
  if (!key) throw new HttpError(401, "missing X-API-Key");
  if (!API_KEY_RE.test(key)) throw new HttpError(401, "invalid API key"); // no DB round trip for junk
  const c = await repo.clientByApiKey(deps.db, key);
  if (!c) throw new HttpError(401, "invalid API key");
  return c;
}

export function validateTask(body: any): { type: string; payload: Record<string, unknown>; maxAttempts: number | null; delayS: number } {
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
export async function enqueue(
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
  // the job plus its fan-out deliveries (budget-checked per job inside drain)
  if (!o.delayS) ctx.waitUntil(drain(deps, { maxJobs: 4, deadline: Date.now() + 25_000 }).catch((e) => console.error("inline drain", e)));
  return { job_id: id, status: "queued", type, status_url: statusUrl(id) };
}


/** Plan enforcement: allowed handlers + month-to-date quota (one Redis round trip). Same rules as Python's _admit. */
export async function admit(deps: Deps, client: repo.ClientRow, jobType: string): Promise<void> {
  const allowed = effectiveHandlers(client.plan, client.allowed_handlers);
  if (allowed && !allowed.includes(jobType)) throw new HttpError(403, `'${jobType}' is not enabled on the ${client.plan} plan`);
  const limit = effectiveLimit(client.plan, client.monthly_job_limit);
  if (!limit) return;
  let n: number;
  try {
    n = await deps.queue.quotaIncr(client.id);
  } catch (e) {
    console.error("quota counter unavailable; admitting request", e);
    return;
  }
  if (n > limit) {
    await deps.queue.quotaDecr(client.id);
    throw new HttpError(429, `monthly job limit of ${limit} reached on the ${client.plan} plan`);
  }
}

export function checkHttps(url: unknown): string | null {
  if (url === null || url === undefined || url === "") return null;
  let u: URL;
  try {
    u = new URL(String(url));
  } catch {
    throw new HttpError(422, "outbound URLs must be https:// without credentials");
  }
  if (u.protocol !== "https:" || !u.hostname || u.username || u.password) throw new HttpError(422, "outbound URLs must be https:// without credentials");
  return String(url);
}

export function checkHandlers(names: unknown): string[] | null {
  if (names === null || names === undefined) return null;
  if (!Array.isArray(names)) throw new HttpError(422, "allowed_handlers must be a list");
  const allowed = new Set(clientJobTypes());
  const bad = names.filter((n) => !allowed.has(n));
  if (bad.length) throw new HttpError(422, `unknown handlers ${JSON.stringify(bad)}; available: ${JSON.stringify([...allowed].sort())}`);
  return names as string[];
}

export function credentials(c: repo.ClientRow, apiKey: string | null, base: string) {
  const handlers = effectiveHandlers(c.plan, c.allowed_handlers) ?? clientJobTypes();
  return {
    id: c.id, slug: c.slug, name: c.name, plan: c.plan, monthly_job_limit: effectiveLimit(c.plan, c.monthly_job_limit),
    allowed_handlers: effectiveHandlers(c.plan, c.allowed_handlers),
    webhook_url: `${base}/webhook`, signed_webhook_url: `${base}/webhook/${c.slug}`,
    handler_urls: Object.fromEntries(handlers.map((h) => [h, `${base}/webhook/${c.slug}/${h}`])),
    ...(apiKey ? { api_key: apiKey, webhook_secret: c.webhook_secret, note: "api_key and webhook_secret are shown once; store them now." } : {}),
  };
}
