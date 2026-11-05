// Revenue handlers, delivery, plans/quotas, agencies, Stripe dogfooding, demo (Worker runtime; mirrors tests/test_products.py).
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { handle } from "../src/api";
import { MIGRATION_V1 } from "../src/db";
import { coerce, parseJsonObject, SchemaError } from "../src/llmjson";
import { drain, runCron } from "../src/runner";
import { detectScheme, signBase64, signHex, signStripe, verify, verifyStripe } from "../src/signatures";
import { hmacSha256Hex } from "../src/util";
import { HAVE_INFRA, type Infra, llmJson, req, setupInfra, Sink, TestCtx } from "./helpers";

const ADMIN = { authorization: "Bearer test-admin" };
const enc = new TextEncoder();
const LEAD = {
  lead: { name: "Sara El Amrani", email: "SARA@Example.com", phone: null, company: null, interest: "3-bedroom apartment",
          budget: "2.5M MAD", timeline: "this month", location: "Casablanca", language: "fr" },
  score: 82, tier: "cold", intent_signals: ["budget stated"], disqualifiers: [], recommended_channel: "whatsapp",
  reply: { whatsapp: "Bonjour Sara", email_subject: "Votre projet", email_body: "Bonjour Sara" }, next_action: "Call within 5 minutes",
};

describe("schema + signature units", () => {
  it("coerces like the Python validator", () => {
    const schema = { type: "object", required: ["n", "e", "opt"], properties: {
      n: { type: "integer", minimum: 0, maximum: 100 }, e: { type: "string", enum: ["hot", "cold"] }, opt: { type: ["string", "null"] } } };
    expect(coerce(schema, { n: "87.6", e: "HOT" })).toEqual({ n: 88, e: "hot", opt: null });
    expect(coerce(schema, { n: 250, e: "cold", opt: "x" }).n).toBe(100);
    expect(() => coerce(schema, { n: 1, e: "warm" })).toThrow(SchemaError);
    expect(parseJsonObject('```json\n{"a": 1}\n```')).toEqual({ a: 1 });
    expect(parseJsonObject('Here: {"a": 2} thanks')).toEqual({ a: 2 });
  });

  it("verifies Stripe, Typeform, Shopify and HMAC signatures", async () => {
    const raw = enc.encode('{"a":1}');
    const b64 = await signBase64(raw, "s3cret");
    expect(await verify("typeform", new Headers({ "typeform-signature": `sha256=${b64}` }), raw, "s3cret")).toBe(true);
    expect(await verify("shopify", new Headers({ "x-shopify-hmac-sha256": b64 }), raw, "s3cret")).toBe(true);
    expect(await verify("shopify", new Headers({ "x-shopify-hmac-sha256": b64 }), enc.encode('{"a":2}'), "s3cret")).toBe(false);
    expect(await verify("hmac", new Headers({ "x-hub-signature-256": await signHex(raw, "s3cret") }), raw, "s3cret")).toBe(true);
    const old = await signStripe(raw, "s3cret", Math.floor(Date.now() / 1000) - 400);
    expect(await verifyStripe(old, raw, "s3cret")).toBe(false); // replay outside tolerance
    expect(await verifyStripe(await signStripe(raw, "s3cret"), raw, "s3cret")).toBe(true);
    expect(detectScheme(new Headers({ "stripe-signature": "t=1,v1=x" }))).toBe("stripe");
  });
});

describe.skipIf(!HAVE_INFRA)("revenue handlers (Postgres + Redis)", () => {
  let infra: Infra;
  let sink: Sink;
  beforeAll(async () => {
    infra = await setupInfra();
  });
  afterAll(async () => infra?.close());
  beforeEach(() => {
    infra.llm.calls = [];
    infra.llm.scripts = { groq: [], gemini: [] };
    sink = new Sink();
  });

  const env = { STRIPE_WEBHOOK_SECRET: "whsec_platform", DEMO_IP_HOURLY_LIMIT: "3" };
  async function call(r: Request, extra = {}) {
    const ctx = new TestCtx();
    const res = await handle(r, infra.deps({ ...env, ...extra }, undefined, sink.fetch), ctx);
    await ctx.settle();
    const body: any = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
    return { res, body };
  }
  const mk = async (slug: string, fields: Record<string, unknown> = {}) => {
    const { res, body } = await call(req("POST", "/admin/clients", { body: { slug, name: slug, ...fields }, headers: ADMIN }));
    expect(res.status, JSON.stringify(body)).toBe(201);
    return body;
  };
  const task = async (key: string, type: string, payload: unknown, extra: Record<string, unknown> = {}) =>
    call(req("POST", "/webhook", { body: { type, payload, ...extra }, headers: { "x-api-key": key } }));
  const job = async (key: string, id: string): Promise<any> => (await call(req("GET", `/tasks/${id}`, { headers: { "x-api-key": key } }))).body;
  const cron = async () => {
    const d = infra.deps(env, undefined, sink.fetch);
    await new Promise((r) => setTimeout(r, 120));
    await runCron(d, Date.now());
  };

  it("lead_qualifier: scores, enforces tier, recovers phone, delivers signed JSON + Slack", async () => {
    const c = await mk("atlas", { outbound_webhook_url: "https://crm.example.com/hooks/leads",
      config: { business_name: "Atlas Living", notify_url: "https://hooks.slack.com/services/T/B/X", lead: { hot_threshold: 75 } } });
    infra.llm.script("groq", llmJson(LEAD));
    const r = await task(c.api_key, "lead_qualifier", { full_name: "Sara", phone_number: "+212 6 61 23 45 67", msg: "3BR Anfa" });
    const j = await job(c.api_key, r.body.job_id);
    expect(j.status).toBe("succeeded");
    expect(j.result.tier).toBe("hot");
    expect([j.result.lead.email, j.result.lead.phone]).toEqual(["sara@example.com", "+212661234567"]);
    expect(j.result.deliveries.map((d: any) => d.format)).toEqual(["json", "slack"]);
    expect(sink.requests).toHaveLength(2); // both deliveries ran in the same invocation (waitUntil drain)
    const crm = sink.requests.find((q) => q.url.includes("crm.example.com"))!;
    expect(crm.headers.get("x-autoplatform-signature")).toBe("sha256=" + (await hmacSha256Hex(c.webhook_secret, enc.encode(crm.body))));
    expect(JSON.parse(crm.body)).toMatchObject({ event: "lead.qualified", client: "atlas", data: { score: 82 } });
    expect(JSON.parse(sink.requests.find((q) => q.url.includes("slack"))!.body).text).toContain("HOT lead 82/100");
  });

  it("failover + JSON repair are recorded; no destination -> preview", async () => {
    const c = await mk("repair-co");
    infra.llm.script("groq", [429, { "retry-after": "30" }, undefined]);
    infra.llm.script("gemini", [200, {}, { choices: [{ message: { content: "Sure! Here you go" } }] }], llmJson(LEAD));
    const r = await task(c.api_key, "lead_qualifier", { text: "hi" });
    const res = (await job(c.api_key, r.body.job_id)).result;
    expect([res.llm.provider, res.llm.failover, res.llm.repaired]).toEqual(["gemini", true, true]);
    expect(res.deliveries[0].reason).toBe("no outbound_webhook_url configured");
    const usage = (await infra.pool.query("SELECT provider, success FROM api_usage WHERE job_id = $1 ORDER BY id", [r.body.job_id])).rows;
    expect(usage.map((u: any) => `${u.provider}:${u.success}`)).toEqual(["groq:false", "gemini:true", "gemini:true"]);
    await infra.redis.del(`${infra.prefix}:llm:cooldown:groq`);
  });

  it("delivery retries until the CRM recovers (the LLM ran once)", async () => {
    const c = await mk("flaky-crm", { outbound_webhook_url: "https://crm.example.com/in" });
    sink.failFirst = 1;
    infra.llm.script("groq", llmJson(LEAD));
    const r = await task(c.api_key, "lead_qualifier", {});
    const res = (await job(c.api_key, r.body.job_id)).result;
    await cron();
    const d = await job(c.api_key, res.deliveries[0].delivery_job_id);
    expect([d.status, d.attempts, d.result.status_code]).toEqual(["succeeded", 2, 200]);
    expect(infra.llm.calls).toEqual(["groq"]);
  });

  const CATALOG = { order: { business_name: "Casa Pizza", currency: "MAD", delivery_fee: 15, catalog: [
    { sku: "PIZ-MAR", name: "Margherita pizza", price: 55, aliases: ["margherita"] },
    { sku: "PIZ-4F", name: "Quattro formaggi pizza", price: 75, aliases: ["4 fromages"] },
    { sku: "COK-33", name: "Coca-Cola 33cl", price: 10, aliases: ["coca"] }] } };
  const meta = (text: string) => ({ object: "whatsapp_business_account", entry: [{ changes: [{ value: {
    contacts: [{ profile: { name: "Youssef" }, wa_id: "212670112233" }],
    messages: [{ from: "212670112233", type: "text", text: { body: text } }] } }] }] });

  it("whatsapp_order_triage prices from the catalog, never from the model", async () => {
    const c = await mk("casa-pizza", { config: CATALOG });
    infra.llm.script("groq", llmJson({ items: [
      { description: "margherita", quantity: 2, sku: "PIZ-MAR" }, { description: "4 fromages", quantity: 1, sku: null },
      { description: "coca", quantity: 2, sku: "COK-33", notes: "bien froid" }, { description: "sushi", quantity: 1, sku: "SUSHI-1" }],
      fulfillment: "delivery", address: "Maarif rue 12 imm 4 appt 7", requested_time: "20:00", customer: { name: null, phone: null },
      language: "fr", needs_clarification: false, clarification_question: null }));
    const r = await task(c.api_key, "whatsapp_order_triage", { body: meta("2 margherita w 1 4 fromages, 2 coca") });
    const res = (await job(c.api_key, r.body.job_id)).result;
    expect(res.customer).toEqual({ name: "Youssef", phone: "212670112233" });
    expect(res.items.map((i: any) => [i.sku, i.line_total])).toEqual([["PIZ-MAR", 110], ["PIZ-4F", 75], ["COK-33", 20], [null, null]]);
    expect(res.totals).toEqual({ currency: "MAD", subtotal: 205, delivery_fee: 15, tax: 0, total: 220 });
    expect([res.unmatched_items, res.needs_review]).toEqual([["sushi"], true]);
    expect(res.confirmation_message).toContain("Total: 220.00 MAD");
    expect(res.confirmation_message).toContain("Livraison à");
    const status = await task(c.api_key, "whatsapp_order_triage", { body: { entry: [{ changes: [{ value: { statuses: [{}] } }] }] } });
    expect((await job(c.api_key, status.body.job_id)).result.ignored).toBe(true);
  });

  const stripeEvt = (type: string, object: unknown) => ({ id: `evt_${type}_${Math.random()}`, type, created: Math.floor(Date.now() / 1000), data: { object } });
  const postStripe = async (path: string, evt: unknown, secret: string) => {
    const raw = JSON.stringify(evt);
    return call(req("POST", path, { raw, headers: { "stripe-signature": await signStripe(enc.encode(raw), secret), "content-type": "application/json" } }));
  };

  it("stripe_churn_rescuer: Stripe signature, discount clamped, disallowed offers replaced", async () => {
    const c = await mk("pulse", { config: { stripe_webhook_secret: "whsec_client", churn: { max_discount_percent: 25, allowed_offers: ["discount", "pause"] } } });
    const now = Math.floor(Date.now() / 1000);
    const sub = { id: "sub_1", customer: "cus_1", start_date: now - 90 * 86400, canceled_at: now,
      cancellation_details: { reason: "cancellation_requested", feedback: "too_expensive" },
      items: { data: [{ price: { nickname: "Growth", unit_amount: 14900, currency: "usd", recurring: { interval: "month" } } }] },
      metadata: { email: "lina@northwind.example", seats_used: "3 of 10" } };
    infra.llm.script("groq", llmJson({ segment: "price_sensitive", risk_reason: "too expensive",
      offer: { type: "discount", value: 60, duration_months: 3, rationale: "price" }, email: { subject: "Before you go", body: "Hi Lina" }, urgency: "high" }));
    expect((await postStripe("/webhook/pulse/stripe_churn_rescuer", stripeEvt("customer.subscription.deleted", sub), "whsec_wrong")).res.status).toBe(401);
    const r = await postStripe("/webhook/pulse/stripe_churn_rescuer", stripeEvt("customer.subscription.deleted", sub), "whsec_client");
    expect(r.res.status).toBe(202);
    const res = (await job(c.api_key, r.body.job_id)).result;
    expect([res.offer.type, res.offer.value]).toEqual(["discount", 25]);
    expect([res.facts.amount, res.facts.tenure_days, res.email.to]).toEqual([149, 90, "lina@northwind.example"]);
    infra.llm.script("groq", llmJson({ segment: "payment_issue", risk_reason: "declined",
      offer: { type: "extension", value: 2, duration_months: 2, rationale: "x" }, email: { subject: "Card", body: "Hi" }, urgency: "high" }));
    const inv = { customer: "cus_9", customer_email: "a@b.example", amount_due: 4900, currency: "eur", attempt_count: 2 };
    const r2 = await postStripe("/webhook/pulse/stripe_churn_rescuer", stripeEvt("invoice.payment_failed", inv), "whsec_client");
    expect((await job(c.api_key, r2.body.job_id)).result.offer).toMatchObject({ type: "payment_update", value: null });
    const r3 = await postStripe("/webhook/pulse/stripe_churn_rescuer", stripeEvt("charge.succeeded", {}), "whsec_client");
    expect((await job(c.api_key, r3.body.job_id)).result.ignored).toBe(true);
  });

  it("support_ticket_router: enum matching, clamping, routing, SLA, escalation", async () => {
    const c = await mk("lumen", { config: { support: { routing: { billing: "finance", default: "desk" }, sla_minutes: { critical: 10 } } } });
    infra.llm.script("groq", llmJson({ category: "Billing", urgency: "critical", sentiment: "negative", sentiment_score: -1.7, language: "fr",
      summary: "Checkout down", customer: { name: "Marc", email: null }, suggested_reply: "Bonjour Marc", tags: ["checkout"] }));
    const r = await task(c.api_key, "support_ticket_router", { from: "marc@boutique.example", message: "erreur 500" });
    const res = (await job(c.api_key, r.body.job_id)).result;
    expect([res.category, res.route_to, res.sentiment_score, res.escalate, res.sla_minutes]).toEqual(["billing", "finance", -1, true, 10]);
    expect(res.customer.email).toBe("marc@boutique.example");
  });

  it("plans: handler allow-lists and the monthly quota", async () => {
    const starter = await mk("starter-co", { plan: "guard_starter" });
    expect((await task(starter.api_key, "lead_qualifier", {})).res.status).toBe(403);
    const tiny = await mk("tiny-co", { monthly_job_limit: 2 });
    const codes = [];
    for (let i = 0; i < 3; i++) codes.push((await task(tiny.api_key, "echo", {})).res.status);
    expect(codes).toEqual([202, 202, 429]);
    expect((await call(req("GET", "/me", { headers: { "x-api-key": tiny.api_key } }))).body.month_jobs).toBe(2);
    expect((await call(req("POST", "/admin/clients", { body: { slug: "stripe", name: "x" }, headers: ADMIN }))).res.status).toBe(422);
  });

  it("usage report + usage_reporter delivery to Discord", async () => {
    const c = await mk("roi-co", { config: { notify_url: "https://discord.com/api/webhooks/1/abc", hourly_rate_usd: 30 } });
    infra.llm.script("groq", [429, { "retry-after": "30" }, undefined]);
    infra.llm.script("gemini", llmJson({ ...LEAD, score: 90 }));
    await task(c.api_key, "lead_qualifier", { x: 1 });
    await task(c.api_key, "echo", {});
    const rep = (await call(req("GET", "/usage?days=7", { headers: { "x-api-key": c.api_key } }))).body;
    expect([rep.webhooks_processed, rep.succeeded, rep.success_rate, rep.highlights.hot_leads]).toEqual([2, 2, 1, 1]);
    expect([rep.llm.failover_saves, rep.llm.calls]).toEqual([1, 2]);
    expect([rep.roi.minutes_saved, rep.roi.value_usd]).toEqual([4, 2]);
    expect(rep.summary_text).toContain("1 jobs rescued by automatic LLM failover");
    const r = await task(c.api_key, "usage_reporter", { days: 7 });
    expect((await job(c.api_key, r.body.job_id)).result.deliveries[0].format).toBe("discord");
    expect(JSON.parse(sink.requests.at(-1)!.body).content).toContain("AutoPlatform report");
    await infra.redis.del(`${infra.prefix}:llm:cooldown:groq`);
  });

  it("agency: provisions, caps, lists, masks secrets, reports, deactivates", async () => {
    const agency = await mk("growthlab", { plan: "agency" });
    const other = await mk("plain-co");
    const akey = { "x-api-key": agency.api_key };
    expect((await call(req("GET", "/agency/clients", { headers: { "x-api-key": other.api_key } }))).res.status).toBe(403);
    const r = await call(req("POST", "/agency/clients", { body: { slug: "dentist", name: "Smile Dental", monthly_job_limit: 999999,
      allowed_handlers: ["lead_qualifier", "echo"] }, headers: akey }));
    expect(r.res.status).toBe(201);
    expect([r.body.slug, r.body.monthly_job_limit]).toEqual(["growthlab-dentist", 5000]);
    expect(r.body.handler_urls.lead_qualifier).toMatch(/\/webhook\/growthlab-dentist\/lead_qualifier$/);
    expect((await task(r.body.api_key, "echo", {})).res.status).toBe(202);
    expect((await task(r.body.api_key, "support_ticket_router", {})).res.status).toBe(403);
    const list = (await call(req("GET", "/agency/clients", { headers: akey }))).body;
    expect(list.map((x: any) => [x.slug, x.month_jobs])).toEqual([["growthlab-dentist", 1]]);
    const p = await call(req("PATCH", "/agency/clients/growthlab-dentist", { body: { config: { stripe_webhook_secret: "whsec_hidden_1234" } }, headers: akey }));
    expect(p.body.config.stripe_webhook_secret).toBe("****1234");
    expect((await call(req("GET", "/agency/usage?days=7", { headers: akey }))).body.totals.webhooks_processed).toBe(1);
    expect((await call(req("DELETE", "/agency/clients/growthlab-dentist", { headers: akey }))).body.active).toBe(false);
    expect((await task(r.body.api_key, "echo", {})).res.status).toBe(401);
  });

  it("Stripe dogfooding: checkout -> client provisioned, credentials shown once, cancel -> deactivated", async () => {
    const session = { id: "cs_test_abc12345XYZ", mode: "subscription", payment_status: "paid", customer: "cus_new1", amount_total: 24900,
      currency: "usd", metadata: { plan: "guard_pro" }, customer_details: { name: "Nadia Benali", email: "nadia@saasco.example" } };
    const evt = stripeEvt("checkout.session.completed", session);
    expect((await postStripe("/webhook/stripe", evt, "whsec_wrong")).res.status).toBe(400);
    const r = await postStripe("/webhook/stripe", evt, "whsec_platform");
    expect([r.res.status, r.body.action, r.body.plan]).toEqual([200, "provisioned", "guard_pro"]);
    expect((await postStripe("/webhook/stripe", evt, "whsec_platform")).body.duplicate).toBe(true);
    const page = await call(req("GET", "/billing/welcome?session_id=cs_test_abc12345XYZ"));
    expect(page.res.status).toBe(200);
    expect(page.body).toContain("Shown once");
    const key = page.body.split("<pre>")[1].split("</pre>")[0];
    expect((await call(req("GET", "/me", { headers: { "x-api-key": key } }))).body.plan).toBe("guard_pro");
    expect((await call(req("GET", "/billing/welcome?session_id=cs_test_abc12345XYZ"))).res.status).toBe(410);
    expect((await call(req("GET", "/billing/welcome?session_id=cs_test_notyet0000"))).res.status).toBe(202);
    const gone = await postStripe("/webhook/stripe", stripeEvt("customer.subscription.deleted", { customer: "cus_new1" }), "whsec_platform");
    expect(gone.body.action).toBe("deactivated");
    expect((await call(req("GET", "/me", { headers: { "x-api-key": key } }))).res.status).toBe(401);
    const odd = { ...session, id: "cs_test_amount00002", customer: "cus_odd", amount_total: 1234, metadata: {} };
    const out = await postStripe("/webhook/stripe", stripeEvt("checkout.session.completed", odd), "whsec_platform");
    expect([out.body.needs_review, out.body.plan]).toEqual([true, "guard_starter"]);
  });

  it("Meta webhook verification handshake", async () => {
    await mk("wa-shop", { config: { meta_verify_token: "tok123" } });
    const ok = await call(req("GET", "/webhook/wa-shop/whatsapp_order_triage?hub.mode=subscribe&hub.verify_token=tok123&hub.challenge=42"));
    expect([ok.res.status, ok.body]).toEqual([200, "42"]);
    const bad = await call(req("GET", "/webhook/wa-shop/whatsapp_order_triage?hub.mode=subscribe&hub.verify_token=no&hub.challenge=42"));
    expect(bad.res.status).toBe(403);
  });

  it("demo: CORS, fast 202, simulated Groq outage -> Gemini, preview only, rate limit", async () => {
    const pre = await call(req("OPTIONS", "/demo/run"));
    expect([pre.res.status, pre.res.headers.get("access-control-allow-origin")]).toEqual([204, "*"]);
    infra.llm.script("gemini", llmJson(LEAD));
    const r = await call(req("POST", "/demo/run", { body: { scenario: "lead", simulate_outage: "groq" }, headers: { "cf-connecting-ip": "1.2.3.4" } }));
    expect(r.res.status).toBe(202);
    expect(r.res.headers.get("server-timing")).toMatch(/^ingest;dur=/);
    const j = (await call(req("GET", `/demo/jobs/${r.body.job_id}`))).body;
    expect(j.status).toBe("succeeded");
    expect([j.result.llm.provider, j.result.llm.failover]).toEqual(["gemini", true]);
    expect(j.result.llm.attempts[0]).toEqual({ provider: "groq", success: false, error_kind: "rate_limit", status_code: 429, latency_ms: 0, simulated: true });
    expect(infra.llm.callsTo("groq")).toBe(0);
    expect(j.result.deliveries[0].reason).toMatch(/^demo/);
    expect(j.payload.full_name).toBe("Sara El Amrani");
    const codes = [];
    for (let i = 0; i < 3; i++) codes.push((await call(req("POST", "/demo/run", { body: { scenario: "ticket" }, headers: { "cf-connecting-ip": "1.2.3.4" } }))).res.status);
    expect(codes.at(-1)).toBe(429);
    const page = await call(req("GET", "/demo"));
    expect([page.res.status, page.res.headers.get("content-type")]).toEqual([200, "text/html; charset=utf-8"]);
  });

  it("lazily upgrades a v1 database to v2 on the first query that needs the new columns", async () => {
    await infra.pool.query(`DROP TABLE IF EXISTS execution_logs, api_usage, jobs, worker_health, clients, schema_migrations CASCADE`);
    for (const stmt of MIGRATION_V1) await infra.pool.query(stmt);
    await infra.pool.query(`INSERT INTO schema_migrations VALUES (1, 'v1', now())`);
    await infra.pool.query(`INSERT INTO clients (id, slug, name, api_key_hash, webhook_secret, active, created_at)
                            VALUES ('c1', 'old-co', 'Old', 'h', 's', true, now())`);
    const { res, body } = await call(req("PATCH", "/admin/clients/old-co", { body: { plan: "guard_pro" }, headers: ADMIN }));
    expect(res.status, JSON.stringify(body)).toBe(200);
    expect(body.plan).toBe("guard_pro");
    const versions = (await infra.pool.query("SELECT version FROM schema_migrations ORDER BY version")).rows.map((r: any) => r.version);
    expect(versions).toEqual([1, 2]);
    expect(await drain(infra.deps(env), { maxJobs: 1, deadline: Date.now() + 1000 })).toBeTruthy();
  });
});
