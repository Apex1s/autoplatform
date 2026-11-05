// Monetization routes (mirrors app/monetization.py).
//   Self-serve     GET/PATCH /me, GET /usage
//   Agencies       POST/GET /agency/clients, PATCH/DELETE /agency/clients/:slug, GET /agency/usage
//   Admin          PATCH /admin/clients/:slug, GET /admin/clients/:slug/usage, POST /cron/weekly_reports
//   Billing        POST /webhook/stripe (auto-provision on payment), GET /billing/welcome (read-once credentials)
//   Signed ingest  POST /webhook/:slug[/:handler] (Stripe / Typeform / Shopify / HMAC), GET Meta hub.challenge
//   Demo           GET /demo, POST /demo/run, GET /demo/jobs/:id
import DEMO_HTML from "../../monetization/demo/index.html";
import { destinationFormat, render } from "./delivery";
import type { Deps } from "./deps";
import { admit, baseUrl, checkHandlers, checkHttps, credentials, enqueue, readBody, readJson, requireAdmin, requireClient,
  requireCron, type Route, type RouteEntry } from "./http";
import { plan, planForAmount, PLANS, RESERVED_SLUGS } from "./plans";
import { enqueueWeeklyReports, usageReport } from "./products";
import { clientJobTypes, isClientCallable } from "./registry";
import * as repo from "./repo";
import { drain } from "./runner";
import { detectScheme, looksValid, verify, verifyStripe } from "./signatures";
import { HttpError, json, newId, randomHex, safeEqual } from "./util";

const SLUG_RE = /^[a-z0-9][a-z0-9-]{2,39}$/;
const isObj = (v: unknown): v is Record<string, any> => typeof v === "object" && v !== null && !Array.isArray(v);

function optInt(v: unknown, field: string, min: number, max: number): number | null | undefined {
  if (v === undefined) return undefined;
  if (v === null) return null;
  if (!Number.isInteger(v) || (v as number) < min || (v as number) > max) throw new HttpError(422, `${field} must be an integer ${min}..${max}`);
  return v as number;
}

function optStr(v: unknown, field: string, max = 200): string | null | undefined {
  if (v === undefined) return undefined;
  if (v === null) return null;
  if (typeof v !== "string" || v.length > max) throw new HttpError(422, `${field} must be a string (max ${max})`);
  return v;
}

const slugify = (text: string) => {
  const base = (text || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 30).replace(/-+$/, "");
  return base.length >= 3 ? base : `client${base}`;
};

async function notifyOperator(deps: Deps, text: string): Promise<void> {
  const url = deps.cfg.operatorNotifyUrl;
  if (!url || deps.budget.left < 2) return;
  try {
    deps.budget.take();
    await deps.httpFetch(url, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(render(destinationFormat(url), { event: "operator.notice", text }, text)), signal: AbortSignal.timeout(5000) });
  } catch {
    console.warn("operator notification failed");
  }
}

// ---------------------------------------------------------------- self-serve
const me: Route = async (req, deps) => {
  const c = await requireClient(req, deps);
  return json({ ...repo.clientView(c, await deps.queue.quotaGet(c.id)), ...credentials(c, null, baseUrl(req, deps)) });
};

const patchMe: Route = async (req, deps) => {
  const c = await requireClient(req, deps);
  const body = await readJson(req, deps);
  if (!isObj(body)) throw new HttpError(422, "body must be a JSON object");
  const fields: Record<string, any> = {};
  if (body.outbound_webhook_url !== undefined) fields.outbound_webhook_url = checkHttps(body.outbound_webhook_url);
  if (body.config !== undefined) {
    if (!isObj(body.config)) throw new HttpError(422, "config must be an object");
    if (body.config.notify_url) checkHttps(body.config.notify_url);
    fields.config = body.config;
  }
  if (body.contact_email !== undefined) fields.contact_email = optStr(body.contact_email, "contact_email");
  return json(repo.clientView((await repo.updateClient(deps.db, c.id, fields)) as repo.ClientRow));
};

const daysParam = (req: Request, def: number) => Math.max(1, Math.min(Number(new URL(req.url).searchParams.get("days") ?? def) || def, 90));

const myUsage: Route = async (req, deps) => {
  const c = await requireClient(req, deps);
  return json(await usageReport({ deps }, c, daysParam(req, 7)));
};

// ------------------------------------------------------------------ agencies
async function requireAgency(req: Request, deps: Deps): Promise<repo.ClientRow> {
  const c = await requireClient(req, deps);
  if (c.plan !== "agency") throw new HttpError(403, "this endpoint requires the agency plan");
  return c;
}

async function ownSub(deps: Deps, agency: repo.ClientRow, slug: string): Promise<repo.ClientRow> {
  const sub = (await repo.subClients(deps.db, agency.id)).find((s) => s.slug === slug);
  if (!sub) throw new HttpError(404, "sub-client not found");
  return sub;
}

const agencyCreate: Route = async (req, deps) => {
  const agency = await requireAgency(req, deps);
  const body = await readJson(req, deps);
  if (!isObj(body) || typeof body.slug !== "string" || !/^[a-z0-9][a-z0-9-]{1,39}$/.test(body.slug)) throw new HttpError(422, "invalid slug");
  if (typeof body.name !== "string" || !body.name || body.name.length > 200) throw new HttpError(422, "invalid name");
  const subs = await repo.subClients(deps.db, agency.id);
  const cap = Number(agency.config?.max_sub_clients ?? plan(agency.plan).max_sub_clients ?? 25);
  if (subs.filter((s) => s.active).length >= cap) throw new HttpError(403, `sub-client cap of ${cap} reached`);
  const slug = body.slug.startsWith(`${agency.slug}-`) ? body.slug : `${agency.slug}-${body.slug}`;
  if (!SLUG_RE.test(slug)) throw new HttpError(422, `slug '${slug}' is invalid or longer than 40 characters`);
  if (await repo.slugExists(deps.db, slug)) throw new HttpError(409, "slug already exists");
  const subCap = plan(agency.plan).sub_client_limit ?? 5000;
  const limit = optInt(body.monthly_job_limit, "monthly_job_limit", 1, 10_000_000);
  const { client, apiKey } = await repo.createClient(deps.db, slug, body.name, {
    plan: "agency_client", monthly_job_limit: Math.min(limit ?? subCap, subCap), allowed_handlers: checkHandlers(body.allowed_handlers),
    outbound_webhook_url: checkHttps(body.outbound_webhook_url), config: isObj(body.config) ? body.config : {},
    agency_id: agency.id, contact_email: optStr(body.contact_email, "contact_email") ?? null,
  });
  return json(credentials(client, apiKey, baseUrl(req, deps)), 201);
};

const agencyList: Route = async (req, deps) => {
  const agency = await requireAgency(req, deps);
  const subs = await repo.subClients(deps.db, agency.id);
  const counts = await repo.monthJobCounts(deps.db, subs.map((s) => s.id));
  return json(subs.map((s) => repo.clientView(s, counts[s.id] ?? 0)));
};

const agencyPatch: Route = async (req, deps, _ctx, [slug]) => {
  const agency = await requireAgency(req, deps);
  const sub = await ownSub(deps, agency, slug);
  const body = await readJson(req, deps);
  if (!isObj(body)) throw new HttpError(422, "body must be a JSON object");
  const subCap = plan(agency.plan).sub_client_limit ?? 5000;
  const fields: Record<string, any> = {};
  if (body.name !== undefined) fields.name = optStr(body.name, "name");
  const limit = optInt(body.monthly_job_limit, "monthly_job_limit", 0, 10_000_000);
  if (limit !== undefined && limit !== null) fields.monthly_job_limit = Math.min(limit, subCap);
  if (body.allowed_handlers !== undefined) fields.allowed_handlers = checkHandlers(body.allowed_handlers);
  if (body.outbound_webhook_url !== undefined) fields.outbound_webhook_url = checkHttps(body.outbound_webhook_url);
  if (body.config !== undefined) fields.config = isObj(body.config) ? body.config : {};
  if (typeof body.active === "boolean") fields.active = body.active;
  return json(repo.clientView((await repo.updateClient(deps.db, sub.id, fields)) as repo.ClientRow));
};

const agencyDelete: Route = async (req, deps, _ctx, [slug]) => {
  const agency = await requireAgency(req, deps);
  const sub = await ownSub(deps, agency, slug);
  await repo.updateClient(deps.db, sub.id, { active: false });
  return json({ slug, active: false });
};

const agencyUsage: Route = async (req, deps) => {
  const agency = await requireAgency(req, deps);
  const days = daysParam(req, 30);
  const subs = await repo.subClients(deps.db, agency.id);
  const keep = ["client", "client_name", "webhooks_processed", "success_rate", "time_to_result_ms", "llm", "deliveries", "highlights", "roi"];
  const rows = [];
  for (const s of subs) {
    if (deps.budget.left < 3) break;
    const r = await usageReport({ deps }, s, days);
    rows.push(Object.fromEntries(keep.map((k) => [k, r[k]])));
  }
  return json({ agency: agency.slug, days, clients: rows, totals: {
    webhooks_processed: rows.reduce((n, r: any) => n + r.webhooks_processed, 0),
    failover_saves: rows.reduce((n, r: any) => n + r.llm.failover_saves, 0),
    llm_tokens: rows.reduce((n, r: any) => n + r.llm.tokens, 0),
    hours_saved: Math.round(rows.reduce((n, r: any) => n + r.roi.hours_saved, 0) * 10) / 10 } });
};

// --------------------------------------------------------------------- admin
const adminPatch: Route = async (req, deps, _ctx, [slug]) => {
  requireAdmin(req, deps);
  const c = await repo.clientBySlug(deps.db, slug);
  if (!c) throw new HttpError(404, "client not found");
  const body = await readJson(req, deps);
  if (!isObj(body)) throw new HttpError(422, "body must be a JSON object");
  const fields: Record<string, any> = {};
  if (body.name !== undefined) fields.name = optStr(body.name, "name");
  if (body.plan !== undefined) {
    if (!(body.plan in PLANS)) throw new HttpError(422, `unknown plan; available: ${JSON.stringify(Object.keys(PLANS).sort())}`);
    fields.plan = body.plan;
  }
  const limit = optInt(body.monthly_job_limit, "monthly_job_limit", 0, 10_000_000);
  if (limit !== undefined) fields.monthly_job_limit = limit;
  if (body.allowed_handlers !== undefined) fields.allowed_handlers = checkHandlers(body.allowed_handlers);
  if (body.outbound_webhook_url !== undefined) fields.outbound_webhook_url = checkHttps(body.outbound_webhook_url);
  if (body.config !== undefined) fields.config = isObj(body.config) ? body.config : {};
  if (body.contact_email !== undefined) fields.contact_email = optStr(body.contact_email, "contact_email");
  if (typeof body.active === "boolean") fields.active = body.active;
  return json(repo.clientView((await repo.updateClient(deps.db, c.id, fields)) as repo.ClientRow));
};

const adminUsage: Route = async (req, deps, _ctx, [slug]) => {
  requireAdmin(req, deps);
  const c = await repo.clientBySlug(deps.db, slug);
  if (!c) throw new HttpError(404, "client not found");
  return json(await usageReport({ deps }, c, daysParam(req, 7)));
};

const weeklyReports: Route = async (req, deps) => {
  requireCron(req, deps);
  const ids = await enqueueWeeklyReports(deps);
  return json({ enqueued: ids.length, job_ids: ids }, 202);
};

// ------------------------------------------------------ billing (dogfooding)
async function provisionFromCheckout(req: Request, deps: Deps, session: Record<string, any>) {
  if (!["paid", "no_payment_required"].includes(session.payment_status)) return { action: "ignored", reason: "payment not completed yet" };
  let planName: string | null = session.metadata?.plan ?? deps.cfg.stripePlanByLink[session.payment_link ?? ""] ??
    planForAmount(session.amount_total, session.currency);
  const review = !planName || !(planName in PLANS) || ["demo", "agency_client"].includes(planName);
  if (review) planName = "guard_starter";
  const details = session.customer_details ?? {};
  const email: string | null = details.email ?? session.customer_email ?? null;
  const name = String(details.name || (email ? email.split("@")[0] : "") || "New customer").slice(0, 200);
  const customer: string | null = session.customer ?? null;
  const existing = customer ? await repo.clientByStripeCustomer(deps.db, customer) : null;
  const base = baseUrl(req, deps);
  let creds: Record<string, any>, action: string, client: repo.ClientRow;
  if (existing) {
    client = (await repo.updateClient(deps.db, existing.id, { plan: planName, active: true })) as repo.ClientRow;
    creds = { ...credentials(client, null, base), note: "Your account was reactivated: keep using your existing API key." };
    action = "reactivated";
  } else {
    let slug = "";
    for (let i = 0; i < 5 && !slug; i++) {
      const candidate = `${slugify(details.name ? name : email ?? name)}-${randomHex(2)}`;
      if (!RESERVED_SLUGS.has(candidate) && !(await repo.slugExists(deps.db, candidate))) slug = candidate;
    }
    if (!slug) throw new HttpError(500, "could not allocate a slug");
    const created = await repo.createClient(deps.db, slug, name, { plan: planName!, contact_email: email, stripe_customer_id: customer,
      config: { onboarding: { stripe_checkout_session: session.id ?? null, needs_review: review } } });
    client = created.client;
    creds = credentials(client, created.apiKey, base);
    action = "provisioned";
  }
  await deps.queue.putSecret(`welcome:${session.id}`, JSON.stringify(creds), 7 * 86400);
  await notifyOperator(deps, `💰 ${action}: ${name} <${email}> on ${planName} -> client ${client.slug}` +
    (review ? " ⚠️ plan not recognised, provisioned as guard_starter: review" : ""));
  return { action, client: client.slug, plan: planName, needs_review: review };
}

const platformStripe: Route = async (req, deps) => {
  if (!deps.cfg.stripeWebhookSecret) throw new HttpError(503, "STRIPE_WEBHOOK_SECRET not configured");
  const raw = await readBody(req, deps.cfg.maxBodyBytes);
  if (!(await verifyStripe(req.headers.get("stripe-signature") ?? "", raw, deps.cfg.stripeWebhookSecret))) {
    throw new HttpError(400, "invalid Stripe signature");
  }
  let evt: any;
  try {
    evt = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    throw new HttpError(400, "body must be JSON");
  }
  const key = `stripe-platform:${evt.id}`;
  if ((await deps.queue.claimIdempotency(key, "1", 7 * 86400)) !== null) return json({ received: true, duplicate: true });
  try {
    const obj = evt.data?.object ?? {};
    let outcome: Record<string, unknown>;
    if (evt.type === "checkout.session.completed") outcome = await provisionFromCheckout(req, deps, obj);
    else if (evt.type === "customer.subscription.deleted") {
      const c = await repo.clientByStripeCustomer(deps.db, obj.customer ?? "");
      if (c) {
        await repo.updateClient(deps.db, c.id, { active: false });
        await notifyOperator(deps, `👋 Subscription ended: client ${c.slug} deactivated`);
        outcome = { action: "deactivated", client: c.slug };
      } else outcome = { action: "ignored", reason: "unknown customer" };
    } else if (evt.type === "invoice.payment_failed") {
      await notifyOperator(deps, `⚠️ Payment failed for ${obj.customer_email ?? obj.customer}`);
      outcome = { action: "notified" };
    } else outcome = { action: "ignored", reason: `unhandled event ${evt.type}` };
    return json({ received: true, ...outcome });
  } catch (e) {
    await deps.queue.releaseIdempotency(key).catch(() => undefined); // let Stripe's retry run it again
    throw e;
  }
};

const esc = (v: unknown) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;" })[c]!);

export function welcomePage(title: string, body: string, refresh = false): string {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">${refresh ? '<meta http-equiv="refresh" content="3">' : ""}
<title>${esc(title)} - AutoPlatform</title>
<style>body{margin:0;background:#141414;color:#f2f2f2;font:16px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:720px;margin:0 auto;padding:48px 20px}h1{font-size:28px;margin:0 0 8px}
code,pre{font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}pre{background:#1d1d1d;border:1px solid #2a2a2a;
border-radius:8px;padding:14px;overflow-x:auto;white-space:pre-wrap;word-break:break-all}
.k{color:#9a9a9a;font-size:13px;text-transform:uppercase;letter-spacing:.06em;margin:22px 0 6px}
.warn{background:#2a2110;border:1px solid #5c4413;color:#ffd68a;padding:12px 14px;border-radius:8px}</style></head>
<body><main><h1>${esc(title)}</h1>${body}</main></body></html>`;
}

export function credentialsHtml(c: Record<string, any>): string {
  const parts: string[] = [];
  if (c.api_key) {
    parts.push('<p class="warn">Shown once. Copy these into your password manager now; we only keep a hash of the API key.</p>');
    parts.push(`<div class="k">API key (header X-API-Key)</div><pre>${esc(c.api_key)}</pre>`);
    parts.push(`<div class="k">Webhook signing secret</div><pre>${esc(c.webhook_secret)}</pre>`);
  } else if (c.note) parts.push(`<p>${esc(c.note)}</p>`);
  parts.push(`<div class="k">Plan</div><pre>${esc(c.plan)} · ${esc(c.monthly_job_limit)} jobs/month</pre>`);
  parts.push(`<div class="k">Send jobs</div><pre>POST ${esc(c.webhook_url)}</pre>`);
  for (const [name, url] of Object.entries(c.handler_urls ?? {})) parts.push(`<div class="k">${esc(name)} (signed webhooks)</div><pre>${esc(url)}</pre>`);
  const example = `curl -X POST ${c.webhook_url} -H "X-API-Key: <your key>" -H "Content-Type: application/json" ` +
    `-d '{"type":"support_ticket_router","payload":{"message":"Hi, I was charged twice"}}'`;
  parts.push(`<div class="k">Try it</div><pre>${esc(example)}</pre>`);
  return parts.join("\n");
}

const html = (body: string, status: number) => new Response(body, { status,
  headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "referrer-policy": "no-referrer" } });

const welcome: Route = async (req, deps) => {
  const sid = new URL(req.url).searchParams.get("session_id") ?? "";
  if (!/^cs_(test|live)_[A-Za-z0-9]{8,200}$/.test(sid)) return html(welcomePage("Invalid link", "<p>This onboarding link is not valid.</p>"), 400);
  const raw = await deps.queue.takeSecret(`welcome:${sid}`);
  if (raw === null) {
    if (await deps.queue.peekSecret(`welcome-shown:${sid}`)) {
      return html(welcomePage("Already revealed", "<p>Your credentials were already shown once and are not stored in readable form. " +
        "Contact support to rotate your key.</p>"), 410);
    }
    return html(welcomePage("Almost there", "<p>Setting up your account... this page refreshes automatically.</p>", true), 202);
  }
  await deps.queue.putSecret(`welcome-shown:${sid}`, "1", 30 * 86400);
  return html(welcomePage("You're live", credentialsHtml(JSON.parse(raw))), 200);
};

// ----------------------------------------------------------- signed ingest
async function signed(req: Request, deps: Deps, ctx: Parameters<Route>[2], slug: string, handlerName: string | null): Promise<Response> {
  const raw = await readBody(req, deps.cfg.maxBodyBytes);
  const scheme = detectScheme(req.headers);
  if (!scheme || !looksValid(scheme, req.headers)) throw new HttpError(401, "invalid signature");
  const client = await repo.clientBySlug(deps.db, slug);
  if (!client) throw new HttpError(401, "invalid signature");
  const cfg = client.config ?? {};
  const candidates = { stripe: [cfg.stripe_webhook_secret], shopify: [cfg.shopify_webhook_secret, client.webhook_secret],
    typeform: [cfg.typeform_secret, client.webhook_secret], hmac: [client.webhook_secret, cfg.meta_app_secret] }[scheme];
  let ok = false;
  for (const s of candidates) if (s && (await verify(scheme, req.headers, raw, s))) ok = true;
  if (!ok) throw new HttpError(401, "invalid signature");
  const name = handlerName ?? cfg.default_handler ?? "webhook.event";
  if (!isClientCallable(name)) throw new HttpError(404, `unknown handler; available: ${JSON.stringify(clientJobTypes())}`);
  await admit(deps, client, name);
  let body: any;
  try {
    body = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    body = { raw: new TextDecoder().decode(raw).slice(0, 10000) };
  }
  const h = req.headers;
  const event = h.get("x-event-type") ?? h.get("x-github-event") ?? h.get("x-shopify-topic") ?? (isObj(body) ? body.type ?? null : null);
  const idem = h.get("idempotency-key") ?? h.get("x-github-delivery") ?? h.get("x-shopify-webhook-id") ??
    (scheme === "stripe" && isObj(body) ? body.id ?? null : null);
  const out = await enqueue(req, deps, ctx, name, { event, body, source: scheme }, { clientId: client.id, source: "signed_webhook", idemKey: idem });
  if ((out as any).duplicate) await deps.queue.quotaDecr(client.id);
  return json(out, 202);
}

const signedDefault: Route = (req, deps, ctx, [slug]) => signed(req, deps, ctx, slug, null);
const signedHandler: Route = (req, deps, ctx, [slug, handler]) => signed(req, deps, ctx, slug, handler);

const metaVerify: Route = async (req, deps, _ctx, [slug]) => {
  const q = new URL(req.url).searchParams;
  const client = await repo.clientBySlug(deps.db, slug);
  const token = client?.config?.meta_verify_token;
  const given = q.get("hub.verify_token");
  if (q.get("hub.mode") === "subscribe" && token && given && safeEqual(given, String(token))) {
    return new Response(q.get("hub.challenge") ?? "", { headers: { "content-type": "text/plain" } });
  }
  throw new HttpError(403, "verification failed");
};

// ---------------------------------------------------------------------- demo
export const DEMO_CONFIG: Record<string, any> = {
  business_name: "Atlas Living Real Estate",
  business_description: "Boutique agency selling apartments and villas in Casablanca and Rabat (1M-8M MAD).",
  tone: "warm, concise, professional", booking_url: "https://cal.com/atlas-living/visit", weekly_report: false,
  lead: { ideal_customer: "Buyers with budget above 1.5M MAD planning to buy within 3 months", hot_threshold: 70, warm_threshold: 40 },
  order: { business_name: "Casa Pizza", currency: "MAD", delivery_fee: 15, min_order: 60, catalog: [
    { sku: "PIZ-MAR", name: "Margherita pizza", price: 55, aliases: ["margherita", "marguerite"] },
    { sku: "PIZ-4F", name: "Quattro formaggi pizza", price: 75, aliases: ["4 fromages", "quatre fromages", "4 cheese", "4 formaggi"] },
    { sku: "BUR-CL", name: "Classic burger", price: 50, aliases: ["burger"] },
    { sku: "FRI-L", name: "Large fries", price: 20, aliases: ["frites", "fries"] },
    { sku: "COK-33", name: "Coca-Cola 33cl", price: 10, aliases: ["coca", "coke"] },
    { sku: "TIR", name: "Tiramisu", price: 30, aliases: ["tiramisu"] }] },
  churn: { product_name: "Pulse Analytics", founder_name: "Sami", max_discount_percent: 30, allowed_offers: ["discount", "pause", "downgrade"],
           billing_portal_url: "https://billing.pulse-analytics.example/portal" },
  support: { business_name: "Lumen Commerce (e-commerce platform)",
             routing: { billing: "finance-team", technical: "tier-2-engineering", account: "customer-success", sales: "sales-team", default: "support-desk" },
             sla_minutes: { critical: 15, high: 60, normal: 240, low: 1440 } },
};

export function demoSamples(): Record<string, [string, Record<string, unknown>]> {
  const now = Math.floor(Date.now() / 1000);
  return {
    lead: ["lead_qualifier", { source: "meta_lead_ads", form: "Apartment & villa enquiries", full_name: "Sara El Amrani",
      phone_number: "+212 6 61 23 45 67", email: "sara.elamrani@example.com", city: "Casablanca",
      what_are_you_looking_for: "3-bedroom apartment near Anfa, ideally with parking", budget: "2.5M - 3M MAD",
      when_do_you_plan_to_buy: "This month, mortgage pre-approved" }],
    order: ["whatsapp_order_triage", { from: "+212 6 70 11 22 33", name: "Youssef",
      message: "salam, bghit 2 margherita w 1 4 fromages, 2 coca, livraison l Maarif rue 12 imm 4 appt 7 vers 20h svp" }],
    churn: ["stripe_churn_rescuer", { id: "evt_demo", type: "customer.subscription.deleted", created: now, data: { object: {
      id: "sub_demo", customer: "cus_demo", start_date: now - 200 * 86400, canceled_at: now,
      cancellation_details: { reason: "cancellation_requested", feedback: "too_expensive", comment: "We only open the dashboards once a month now" },
      items: { data: [{ price: { nickname: "Growth", unit_amount: 14900, currency: "usd", recurring: { interval: "month" } } }] },
      metadata: { email: "lina@northwind.example", name: "Lina", seats_used: "3 of 10", last_login_days_ago: "19" } } } }],
    ticket: ["support_ticket_router", { channel: "crisp", from: { name: "Marc Dubois", email: "marc@boutique-lumen.example" },
      subject: "Paiement bloqué !!", message: "Bonjour, depuis ce matin aucun client ne peut payer sur notre boutique, le checkout " +
        "affiche une erreur 500. C'est urgent, on perd des ventes." }],
  };
}

let demoClientId: string | null = null;

async function demoClient(deps: Deps): Promise<repo.ClientRow> {
  if (demoClientId) {
    const c = await repo.clientById(deps.db, demoClientId);
    if (c?.active) return c;
  }
  let c = await repo.clientBySlug(deps.db, "demo");
  if (!c) c = (await repo.createClient(deps.db, "demo", "Live demo", { plan: "demo", config: DEMO_CONFIG })).client;
  demoClientId = c.id;
  return c;
}

const demoPage: Route = async () => new Response(DEMO_HTML, { headers: { "content-type": "text/html; charset=utf-8" } });

const demoRun: Route = async (req, deps, ctx) => {
  const t0 = performance.now();
  if (!deps.cfg.demoEnabled) throw new HttpError(404, "demo disabled");
  const body = await readJson(req, deps);
  const scenario = isObj(body) ? body.scenario : null;
  const samples = demoSamples();
  if (typeof scenario !== "string" || !(scenario in samples)) throw new HttpError(422, "scenario must be one of lead, order, churn, ticket");
  const outage = isObj(body) ? body.simulate_outage ?? null : null;
  if (outage !== null && !["groq", "gemini"].includes(outage)) throw new HttpError(422, "simulate_outage must be groq, gemini or null");
  if (isObj(body) && body.payload !== undefined && body.payload !== null && !isObj(body.payload)) throw new HttpError(422, "payload must be an object");
  const ip = req.headers.get("cf-connecting-ip") ?? "unknown";
  const day = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  const [ipOver, dayOver] = await deps.queue.overRates([
    { key: `demo:ip:${ip}`, windowS: 3600, limit: deps.cfg.demoIpHourlyLimit },
    { key: `demo:day:${day}`, windowS: 86400, limit: deps.cfg.demoDailyLimit },
  ]);
  if (ipOver) throw new HttpError(429, "demo limit reached for your network; try again in an hour");
  if (dayOver) throw new HttpError(429, "the public demo hit today's cap; book a call for a private run");
  const [handler, sample] = samples[scenario];
  const payload = { body: (isObj(body) && isObj(body.payload) ? body.payload : sample), _demo: true, _simulate_outage: outage };
  if (JSON.stringify(payload).length > 8000) throw new HttpError(413, "demo payload too large");
  // Fast ack: persist + run right after responding (the demo shows real ingest latency at the edge).
  const id = newId();
  ctx.waitUntil((async () => {
    const c = await demoClient(deps);
    await repo.createJob(deps.db, handler, payload, { id, clientId: c.id, maxAttempts: 2, source: "demo" });
    await deps.queue.enqueue(id);
    await drain(deps, { maxJobs: 2, deadline: Date.now() + 25_000 });
  })().catch((e) => console.error("demo run failed", e)));
  const ms = Math.round((performance.now() - t0) * 10) / 10;
  return json({ job_id: id, status: "queued", type: handler, scenario, handler, ingest_ms: ms, status_url: `/demo/jobs/${id}` }, 202,
    { "server-timing": `ingest;dur=${ms}` });
};

const demoJob: Route = async (_req, deps, _ctx, [id]) => {
  const c = await demoClient(deps);
  const job = await repo.getJob(deps.db, id, c.id);
  if (!job) throw new HttpError(404, "job not found (yet)");
  return json({ ...job, payload: (job.payload as any)?.body ?? null });
};

// Ordered: /webhook/stripe must win over /webhook/:slug.
export const MONETIZATION_ROUTES: RouteEntry[] = [
  ["GET", /^\/me$/, me],
  ["PATCH", /^\/me$/, patchMe],
  ["GET", /^\/usage$/, myUsage],
  ["POST", /^\/agency\/clients$/, agencyCreate],
  ["GET", /^\/agency\/clients$/, agencyList],
  ["PATCH", /^\/agency\/clients\/([a-z0-9-]{1,40})$/, agencyPatch],
  ["DELETE", /^\/agency\/clients\/([a-z0-9-]{1,40})$/, agencyDelete],
  ["GET", /^\/agency\/usage$/, agencyUsage],
  ["PATCH", /^\/admin\/clients\/([a-z0-9-]{1,40})$/, adminPatch],
  ["GET", /^\/admin\/clients\/([a-z0-9-]{1,40})\/usage$/, adminUsage],
  ["POST", /^\/cron\/weekly_reports$/, weeklyReports],
  ["POST", /^\/webhook\/stripe$/, platformStripe],
  ["GET", /^\/billing\/welcome$/, welcome],
  ["POST", /^\/webhook\/([a-z0-9-]{1,40})$/, signedDefault],
  ["POST", /^\/webhook\/([a-z0-9-]{1,40})\/([a-z_.]{1,64})$/, signedHandler],
  ["GET", /^\/webhook\/([a-z0-9-]{1,40})\/([a-z_.]{1,64})$/, metaVerify],
  ["GET", /^\/demo\/?$/, demoPage],
  ["POST", /^\/demo\/run$/, demoRun],
  ["GET", /^\/demo\/jobs\/([A-Za-z0-9]{1,64})$/, demoJob],
];

export const CORS_HEADERS: Record<string, string> = {
  "access-control-allow-origin": "*", "access-control-allow-methods": "GET, POST, OPTIONS",
  "access-control-allow-headers": "content-type", "access-control-expose-headers": "server-timing",
};

export { PLANS };
