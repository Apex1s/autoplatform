// Outbound delivery (mirrors workers/delivery.py). Handlers save their result and fan out
// `internal.deliver` jobs (one bulk INSERT + one LPUSH), each retried on its own: timeouts,
// 408/425/429 and 5xx back off (8 attempts, about 35 minutes); other 4xx fail fast. JSON deliveries carry
//   X-AutoPlatform-Signature: sha256=<hex hmac_sha256(client webhook_secret, raw body)>
// Slack / Discord webhook URLs get a human-readable summary instead of the envelope.
import type { Handler, JobCtx } from "./handlers";
import { assertPublicHttps } from "./handlers";
import * as repo from "./repo";
import { enc, hmacSha256Hex, PermanentError } from "./util";

export const DELIVERY_MAX_ATTEMPTS = 8;
const USER_AGENT = "AutoPlatform-Webhooks/1.1";

export type Format = "slack" | "discord" | "json";

export function destinationFormat(url: string): Format {
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return "json";
  }
  const host = u.hostname.toLowerCase();
  if (host === "hooks.slack.com") return "slack";
  if (["discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"].includes(host) && u.pathname.startsWith("/api/webhooks/")) {
    return "discord";
  }
  return "json";
}

export const envelope = (event: string, clientSlug: string, sourceJobId: string, data: unknown) =>
  ({ event, client: clientSlug, source_job_id: sourceJobId, created_at: new Date().toISOString(), data });

export function render(fmt: Format, env: unknown, summary: string): unknown {
  if (fmt === "slack") return { text: summary };
  if (fmt === "discord") return { content: summary.slice(0, 1900) };
  return env;
}

export const sign = async (secret: string, raw: string) => "sha256=" + (await hmacSha256Hex(secret, enc.encode(raw)));

export function destinations(c: repo.ClientRow): string[] {
  const out: string[] = [];
  if (c.outbound_webhook_url) out.push(c.outbound_webhook_url);
  const notify = c.config?.notify_url;
  if (notify && !out.includes(notify)) out.push(notify);
  return out;
}

export async function queueDeliveries(ctx: JobCtx, client: repo.ClientRow | null, event: string, data: unknown, summary: string) {
  if (!client) return [];
  const env = envelope(event, client.slug, ctx.jobId, data);
  const urls = destinations(client);
  if (!urls.length || client.plan === "demo") {
    const raw = JSON.stringify(env);
    return [{ event, delivered: false,
      reason: client.plan === "demo" ? "demo: nothing is sent" : "no outbound_webhook_url configured",
      preview: { headers: { "Content-Type": "application/json", "X-AutoPlatform-Event": event,
        "X-AutoPlatform-Signature": await sign(client.webhook_secret, raw) }, body: env, summary } }];
  }
  const jobs = urls.map((url) => {
    const fmt = destinationFormat(url);
    return { type: "internal.deliver", clientId: client.id, maxAttempts: DELIVERY_MAX_ATTEMPTS, source: "internal",
             payload: { url, format: fmt, event, body: render(fmt, env, summary), source_job_id: ctx.jobId } };
  });
  const ids = await repo.createJobsBulk(ctx.deps.db, jobs);
  await ctx.deps.queue.enqueueMany(ids);
  return jobs.map((j, i) => ({ event, destination: new URL(j.payload.url).hostname, format: j.payload.format, delivery_job_id: ids[i] }));
}

export const deliver: Handler = async (ctx, payload) => {
  const url = assertPublicHttps(String(payload?.url ?? "")).toString();
  const client = ctx.clientId ? await repo.clientById(ctx.deps.db, ctx.clientId) : null;
  if (!client || !client.active) throw new PermanentError("client missing or inactive; delivery dropped");
  const raw = JSON.stringify(payload?.body ?? {});
  const headers: Record<string, string> = {
    "content-type": "application/json", "user-agent": USER_AGENT, "x-autoplatform-event": String(payload?.event ?? ""),
    "x-autoplatform-delivery": ctx.jobId, "x-autoplatform-attempt": String(ctx.attempt),
  };
  if ((payload?.format ?? "json") === "json") headers["x-autoplatform-signature"] = await sign(client.webhook_secret, raw);
  const started = Date.now();
  ctx.deps.budget.take();
  let res: Response;
  try {
    res = await ctx.deps.httpFetch(url, { method: "POST", headers, body: raw, redirect: "manual", signal: AbortSignal.timeout(10_000) });
  } catch (e) {
    throw new Error(`destination unreachable: ${(e as Error).name}`);
  }
  const code = res.status;
  if ([408, 425, 429].includes(code) || code >= 500) throw new Error(`destination HTTP ${code}`);
  if (code >= 400) throw new PermanentError(`destination rejected the delivery: HTTP ${code} ${(await res.text()).slice(0, 200)}`);
  return { delivered: true, status_code: code, destination: new URL(url).hostname, format: payload?.format ?? "json",
           latency_ms: Date.now() - started, source_job_id: payload?.source_job_id ?? null };
};
