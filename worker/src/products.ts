// Revenue handlers (1:1 with workers/products.py): same prompts, guardrails, deterministic maths and output shapes.
//   lead_qualifier · whatsapp_order_triage · stripe_churn_rescuer · support_ticket_router · usage_reporter
// The LLM understands and drafts; prices, totals, tiers, discount caps, SLA deadlines and contact
// details are computed here so they can be trusted.
import { queueDeliveries } from "./delivery";
import type { Handler, JobCtx } from "./handlers";
import { completeJson, type Schema } from "./llmjson";
import * as repo from "./repo";
import { PermanentError } from "./util";

const EMAIL_RE = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/;
const PHONE_RE = /\+?\d[\d\s().-]{7,}\d/;
export const DEFAULT_MINUTES_SAVED: Record<string, number> = {
  lead_qualifier: 4, whatsapp_order_triage: 3, stripe_churn_rescuer: 10, support_ticket_router: 5,
  "webhook.event": 2, "llm.complete": 2, "http.fetch": 1, echo: 0,
};
const INTERNAL_KEYS = ["_demo", "_simulate_outage"];

// ------------------------------------------------------------------ helpers
const loadClient = (ctx: JobCtx) => (ctx.clientId ? repo.clientById(ctx.deps.db, ctx.clientId) : Promise.resolve(null));

function section(cfg: Record<string, any>, name: string): Record<string, any> {
  const sec = { ...(cfg[name] ?? {}) };
  for (const k of ["business_name", "business_description", "tone", "booking_url", "website"]) if (sec[k] == null) sec[k] = cfg[k];
  return sec;
}

function inbound(payload: any): any {
  const body = payload && typeof payload === "object" && "body" in payload ? payload.body : payload;
  if (body && typeof body === "object" && !Array.isArray(body)) {
    return Object.fromEntries(Object.entries(body).filter(([k]) => !INTERNAL_KEYS.includes(k)));
  }
  return body;
}

const asText = (v: unknown, limit = 6000) => (typeof v === "string" ? v : JSON.stringify(v ?? null)).slice(0, limit);
const normEmail = (v: unknown) => EMAIL_RE.exec(String(v ?? ""))?.[0].toLowerCase() ?? null;

export function normPhone(v: unknown): string | null {
  if (!v) return null;
  const s = String(v).trim();
  const digits = s.replace(/\D/g, "");
  if (digits.length < 8 || digits.length > 15) return null;
  return (s.startsWith("+") || s.startsWith("00") ? "+" : "") + digits.replace(/^00/, "");
}

const llmOpts = (cfg: Record<string, any>, payload: any) => ({
  providers: (cfg.llm_providers as string[] | undefined) ?? null,
  simulateOutage: payload?._demo ? (payload?._simulate_outage ?? null) : null,
});

function msSince(createdAt: string | null | undefined): number | null {
  if (!createdAt) return null;
  const t = Date.parse(createdAt);
  return Number.isNaN(t) ? null : Math.max(0, Date.now() - t);
}

const money = (x: number) => Math.round((x + 1e-9) * 100) / 100;
const strip = <T extends Record<string, any>>(o: T, key: string) => Object.fromEntries(Object.entries(o).filter(([k]) => k !== key));

// ------------------------------------------------------------ lead_qualifier
const nullable = (description?: string): Schema => ({ type: ["string", "null"], ...(description ? { description } : {}) });
export const LEAD_SCHEMA: Schema = {
  type: "object",
  required: ["lead", "score", "tier", "intent_signals", "recommended_channel", "reply", "next_action"],
  properties: {
    lead: { type: "object", required: ["name", "email", "phone", "company", "interest", "budget", "timeline", "location", "language"],
      properties: { name: nullable(), email: nullable(), phone: nullable(), company: nullable(), interest: nullable("what they want"),
        budget: nullable(), timeline: nullable(), location: nullable(), language: nullable("ISO 639-1 code of the lead's own words") } },
    score: { type: "integer", minimum: 0, maximum: 100, description: "purchase intent x fit" },
    tier: { type: "string", enum: ["hot", "warm", "cold"] },
    intent_signals: { type: "array", items: { type: "string" } },
    disqualifiers: { type: "array", items: { type: "string" } },
    recommended_channel: { type: "string", enum: ["whatsapp", "email", "call"] },
    reply: { type: "object", required: ["whatsapp", "email_subject", "email_body"], properties: {
      whatsapp: { type: "string", description: "<= 60 words" }, email_subject: { type: "string" }, email_body: { type: "string" } } },
    next_action: { type: "string" },
  },
};

export const leadQualifier: Handler = async (ctx, payload) => {
  const client = await loadClient(ctx);
  const cfg = client?.config ?? {};
  const lc = section(cfg, "lead");
  const text = asText(inbound(payload));
  const system =
    `You qualify inbound leads for ${lc.business_name || "the business"}${lc.business_description ? ": " + lc.business_description : ""}.\n` +
    `Ideal customer: ${lc.ideal_customer || "clear need, budget and near-term timeline"}.\n` +
    "Extract the lead's details exactly as given (null when absent; never invent contact data). " +
    "Score 0-100 for intent and fit; list concrete intent signals and disqualifiers. " +
    `Draft replies in the lead's language, tone: ${lc.tone || "warm, concise, professional"}. ` +
    "The WhatsApp reply is at most 60 words and ends with one clear next step" +
    `${lc.booking_url ? " (booking link: " + lc.booking_url + ")" : ""}. ` +
    "Do not promise prices, availability or facts that are not in the lead or the business description.";
  const [data, meta] = await completeJson(ctx, { system, user: `Inbound lead payload:\n${text}`, schema: LEAD_SCHEMA, maxTokens: 800, ...llmOpts(cfg, payload) });
  const lead = data.lead;
  lead.email = normEmail(lead.email) ?? normEmail(text);
  lead.phone = normPhone(lead.phone) ?? normPhone(PHONE_RE.exec(text)?.[0]);
  const hot = Number(lc.hot_threshold ?? 70), warm = Number(lc.warm_threshold ?? 40);
  data.tier = data.score >= hot ? "hot" : data.score >= warm ? "warm" : "cold";
  let channel = data.recommended_channel;
  if (channel === "whatsapp" && !lead.phone) channel = lead.email ? "email" : "call";
  if (channel === "email" && !lead.email) channel = lead.phone ? "whatsapp" : "call";
  data.recommended_channel = channel;
  data.disqualifiers ??= [];
  data.speed_to_lead_ms = msSince(ctx.createdAt);
  data.llm = meta;
  const icon = ({ hot: "🔥", warm: "🌤", cold: "❄️" } as Record<string, string>)[data.tier];
  const summary = `${icon} ${data.tier.toUpperCase()} lead ${data.score}/100: ${lead.name || "unknown"}` +
    `${lead.interest ? " - " + lead.interest : ""} | via ${channel} ${lead.phone || lead.email || ""}\nNext: ${data.next_action}`;
  data.deliveries = await queueDeliveries(ctx, client, "lead.qualified", strip(data, "llm"), summary);
  return data;
};

// ----------------------------------------------------- whatsapp_order_triage
export const ORDER_SCHEMA: Schema = {
  type: "object",
  required: ["items", "fulfillment", "address", "requested_time", "customer", "language", "needs_clarification", "clarification_question"],
  properties: {
    items: { type: "array", items: { type: "object", required: ["description", "quantity", "sku"], properties: {
      description: { type: "string" }, quantity: { type: "integer", minimum: 1, maximum: 999 },
      sku: nullable("catalog sku, or null if no match"), notes: nullable() } } },
    fulfillment: { type: "string", enum: ["delivery", "pickup", "unknown"] },
    address: nullable(), requested_time: nullable(),
    customer: { type: "object", required: ["name", "phone"], properties: { name: nullable(), phone: nullable() } },
    language: { type: "string", description: "ISO 639-1 code of the customer's message" },
    needs_clarification: { type: "boolean" },
    clarification_question: nullable("one short question in the customer's language, or null"),
  },
};

const CONFIRM: Record<string, Record<string, string>> = {
  en: { hi: "Thanks{name}! Here is your order:", delivery: "Delivery", total: "Total", to: "Delivering to", pickup: "Pickup",
        confirm: "Reply YES to confirm.", unknown: "Not on our menu" },
  fr: { hi: "Merci{name} ! Voici votre commande :", delivery: "Livraison", total: "Total", to: "Livraison à", pickup: "À emporter",
        confirm: "Répondez OUI pour confirmer.", unknown: "Hors carte" },
  es: { hi: "¡Gracias{name}! Este es tu pedido:", delivery: "Envío", total: "Total", to: "Entrega en", pickup: "Para recoger",
        confirm: "Responde SÍ para confirmar.", unknown: "No está en la carta" },
  ar: { hi: "شكراً{name}! هذا طلبك:", delivery: "التوصيل", total: "المجموع", to: "العنوان", pickup: "استلام من المحل",
        confirm: "أرسل نعم للتأكيد.", unknown: "غير موجود في القائمة" },
};

export function whatsappMessage(body: any): [string | null, { name: string | null; phone: string | null }, string] {
  if (body && typeof body === "object" && Array.isArray(body.entry)) {
    const value = body.entry?.[0]?.changes?.[0]?.value ?? {};
    const m = (value.messages ?? [])[0];
    if (!m) return [null, { name: null, phone: null }, "whatsapp_cloud_api"];
    const text = m.text?.body ?? m.button?.text ?? m.interactive?.list_reply?.title ?? null;
    return [text, { name: value.contacts?.[0]?.profile?.name ?? null, phone: m.from ?? null }, "whatsapp_cloud_api"];
  }
  if (body && typeof body === "object") {
    const text = body.message ?? body.text ?? (typeof body.body === "string" ? body.body : null);
    const cust = body.customer && typeof body.customer === "object" ? body.customer : {};
    return [typeof text === "string" ? text : asText(body),
            { name: cust.name ?? body.name ?? null, phone: cust.phone ?? body.phone ?? body.from ?? null }, "generic"];
  }
  return [body ? String(body) : null, { name: null, phone: null }, "generic"];
}

function matchSku(catalog: any[], sku: string | null, description: string): any | null {
  const bySku = Object.fromEntries(catalog.map((c) => [String(c.sku), c]));
  if (sku && bySku[sku]) return bySku[sku];
  const d = description.toLocaleLowerCase();
  for (const c of catalog) {
    const names = [String(c.name ?? ""), ...(c.aliases ?? []).map(String)];
    if (names.some((n) => n && (d.includes(n.toLocaleLowerCase()) || n.toLocaleLowerCase().includes(d)))) return c;
  }
  return null;
}

export const whatsappOrderTriage: Handler = async (ctx, payload) => {
  const client = await loadClient(ctx);
  const cfg = client?.config ?? {};
  const oc = section(cfg, "order");
  const [text, hint, source] = whatsappMessage(inbound(payload));
  if (!text) return { ignored: true, reason: "no customer message in payload (e.g. a delivery-status webhook)", source };
  const catalog = (oc.catalog ?? []).filter((c: any) => c && typeof c === "object" && c.sku);
  const currency = oc.currency || "USD";
  const menu = catalog.map((c: any) => `- ${c.sku}: ${c.name}` + (c.aliases?.length ? ` (also: ${c.aliases.join(", ")})` : "")).join("\n");
  const system =
    `You take orders for ${oc.business_name || "the shop"} from WhatsApp/web messages that may be messy, ` +
    "multilingual or abbreviated. Map each requested item to a catalog sku when it clearly matches " +
    "(else sku null), with an integer quantity. Capture modifiers in notes, the delivery address, the " +
    "requested time and whether it is delivery or pickup. If anything essential is ambiguous, set " +
    "needs_clarification and ask ONE short question in the customer's language. Do not compute prices.\n" +
    `Catalog:\n${menu || "(no catalog configured: set every sku to null)"}`;
  const [data, meta] = await completeJson(ctx, { system, user: `Customer: ${JSON.stringify(hint)}\nMessage:\n${text.slice(0, 4000)}`,
    schema: ORDER_SCHEMA, maxTokens: 900, ...llmOpts(cfg, payload) });
  const lines: any[] = [], unmatched: string[] = [];
  for (const item of data.items) {
    const c = matchSku(catalog, item.sku, item.description);
    if (!c) {
      unmatched.push(item.description);
      lines.push({ ...item, sku: null, name: item.description, unit_price: null, line_total: null });
      continue;
    }
    const unit = Number(c.price ?? 0);
    lines.push({ ...item, sku: c.sku, name: c.name, unit_price: money(unit), line_total: money(unit * item.quantity) });
  }
  const subtotal = money(lines.reduce((n, l) => n + (l.line_total ?? 0), 0));
  const fee = data.fulfillment === "delivery" ? money(Number(oc.delivery_fee ?? 0)) : 0;
  const tax = money(subtotal * Number(oc.tax_rate ?? 0));
  const total = money(subtotal + fee + tax);
  const minOrder = Number(oc.min_order ?? 0);
  const customer = { name: data.customer.name ?? hint.name, phone: normPhone(data.customer.phone ?? hint.phone) };
  const needsReview = unmatched.length > 0 || data.needs_clarification || !lines.length || (minOrder > 0 && subtotal < minOrder);
  const t = CONFIRM[(data.language || "en").slice(0, 2).toLowerCase()] ?? CONFIRM.en;
  const msg = [t.hi.replace("{name}", customer.name ? ` ${customer.name}` : "")];
  for (const l of lines) {
    const price = l.line_total !== null ? `${l.line_total.toFixed(2)} ${currency}` : t.unknown;
    msg.push(`• ${l.quantity}× ${l.name}` + (l.notes ? ` (${l.notes})` : "") + ` - ${price}`);
  }
  if (fee) msg.push(`${t.delivery}: ${fee.toFixed(2)} ${currency}`);
  msg.push(`${t.total}: ${total.toFixed(2)} ${currency}`);
  if (data.fulfillment === "delivery" && data.address) msg.push(`${t.to}: ${data.address}`);
  else if (data.fulfillment === "pickup") msg.push(t.pickup);
  msg.push(data.needs_clarification && data.clarification_question ? data.clarification_question : t.confirm);
  const result: Record<string, any> = {
    source, customer, items: lines, unmatched_items: unmatched, fulfillment: data.fulfillment, address: data.address,
    requested_time: data.requested_time, language: data.language,
    totals: { currency, subtotal, delivery_fee: fee, tax, total }, below_minimum: minOrder > 0 && subtotal < minOrder,
    needs_review: Boolean(needsReview), clarification_question: data.needs_clarification ? data.clarification_question : null,
    confirmation_message: msg.join("\n"), speed_ms: msSince(ctx.createdAt), llm: meta,
  };
  const summary = `🛒 Order ${total.toFixed(2)} ${currency} - ${lines.length} item(s) - ${customer.name || "customer"} ` +
    `${customer.phone || ""}${needsReview ? " ⚠️ needs review" : ""}`;
  result.deliveries = await queueDeliveries(ctx, client, "order.received", strip(result, "llm"), summary);
  return result;
};

// ------------------------------------------------------ stripe_churn_rescuer
const CHURN_EVENTS = ["customer.subscription.deleted", "invoice.payment_failed", "customer.subscription.updated"];
export const CHURN_SCHEMA: Schema = {
  type: "object",
  required: ["segment", "risk_reason", "offer", "email", "urgency"],
  properties: {
    segment: { type: "string", enum: ["price_sensitive", "low_usage", "missing_feature", "switched_competitor", "payment_issue", "temporary_pause", "unknown"] },
    risk_reason: { type: "string" },
    offer: { type: "object", required: ["type", "value", "duration_months", "rationale"], properties: {
      type: { type: "string", enum: ["discount", "pause", "downgrade", "extension", "payment_update", "none"] },
      value: { type: ["number", "null"], description: "percent off, or months" },
      duration_months: { type: ["integer", "null"], minimum: 1, maximum: 12 }, rationale: { type: "string" } } },
    email: { type: "object", required: ["subject", "body"], properties: { subject: { type: "string" }, body: { type: "string" } } },
    urgency: { type: "string", enum: ["high", "medium", "low"] },
  },
};

export function churnFacts(evt: any): Record<string, any> | null {
  const etype = evt?.type;
  const obj = evt?.data?.object ?? {};
  if (!CHURN_EVENTS.includes(etype)) return null;
  if (etype === "customer.subscription.updated" && !obj.cancel_at_period_end) return null;
  const meta = obj.metadata ?? {};
  const item = obj.items ? (obj.items.data ?? [{}])[0] ?? {} : {};
  const line = obj.lines ? (obj.lines.data ?? [{}])[0] ?? {} : {};
  const price = item.price ?? line.price ?? {};
  const amount = price.unit_amount ?? obj.amount_due;
  const cancel = obj.cancellation_details ?? {};
  const start = obj.start_date ?? obj.created;
  const end = obj.canceled_at ?? evt.created;
  const involuntary = etype === "invoice.payment_failed" || cancel.reason === "payment_failed";
  return {
    event_type: etype,
    churn_kind: involuntary ? "involuntary" : etype === "customer.subscription.updated" ? "scheduled_cancellation" : "voluntary",
    customer_id: obj.customer ?? null, email: obj.customer_email ?? meta.email ?? null, name: obj.customer_name ?? meta.name ?? null,
    plan: price.nickname ?? price.lookup_key ?? price.product ?? meta.plan ?? null,
    amount: typeof amount === "number" ? amount / 100 : null,
    currency: (price.currency ?? obj.currency ?? "").toUpperCase() || null,
    interval: price.recurring?.interval ?? null,
    tenure_days: Number.isInteger(start) && Number.isInteger(end) ? Math.trunc((end - start) / 86400) : null,
    cancellation_reason: cancel.reason ?? null, feedback: cancel.feedback ?? null, comment: cancel.comment ?? null,
    attempt_count: obj.attempt_count ?? null, next_payment_attempt: obj.next_payment_attempt ?? null,
    usage_metadata: Object.fromEntries(Object.entries(meta).filter(([k]) => !["email", "name", "plan"].includes(k))),
  };
}

export const stripeChurnRescuer: Handler = async (ctx, payload) => {
  const client = await loadClient(ctx);
  const cfg = client?.config ?? {};
  const cc = section(cfg, "churn");
  const evt = inbound(payload);
  if (!evt || typeof evt !== "object") throw new PermanentError("expected a Stripe event JSON object");
  const facts = churnFacts(evt);
  if (!facts) return { ignored: true, reason: `event '${evt.type}' is not a churn signal` };
  const allowed: string[] = cc.allowed_offers ?? ["discount", "pause", "downgrade", "extension"];
  const maxDiscount = Number(cc.max_discount_percent ?? 30);
  const product = cc.product_name || cc.business_name || "our product";
  const founder = cc.founder_name || "the founder";
  const system =
    `You are ${founder}, founder of ${product}, writing a short personal win-back email (max 140 words, ` +
    "plain text, no marketing fluff) to a customer who is churning. Diagnose the most likely churn segment " +
    "from the facts, pick ONE offer from the allowed list " +
    `${JSON.stringify([...allowed, "payment_update", "none"])} (discount at most ${maxDiscount}%), and explain it simply. ` +
    "For involuntary churn (failed payment) the offer must be payment_update with a friendly nudge to update " +
    "the card. Never invent features, usage numbers or deadlines that are not in the facts.";
  const [data, meta] = await completeJson(ctx, { system, user: `Churn facts:\n${JSON.stringify(facts)}`, schema: CHURN_SCHEMA,
    maxTokens: 800, ...llmOpts(cfg, payload) });
  const offer = data.offer;
  if (facts.churn_kind === "involuntary") Object.assign(offer, { type: "payment_update", value: null, duration_months: null });
  else if (![...allowed, "none"].includes(offer.type)) Object.assign(offer, { type: allowed[0] ?? "none", value: null });
  if (offer.type === "discount") {
    offer.value = Math.min(maxDiscount, Math.max(0, Number(offer.value ?? maxDiscount)));
    offer.duration_months = offer.duration_months ?? 3;
  }
  const portal = cc.billing_portal_url;
  if (portal && offer.type === "payment_update" && !data.email.body.includes(portal)) {
    data.email.body += `\n\nUpdate your payment method here: ${portal}`;
  }
  const result: Record<string, any> = { facts, segment: data.segment, risk_reason: data.risk_reason, offer,
    email: { ...data.email, to: facts.email }, urgency: data.urgency, llm: meta };
  const value = offer.value ? ` ${offer.value}${offer.type === "discount" ? "%" : ""}` : "";
  const summary = `💸 Churn risk (${facts.churn_kind}): ${facts.email ?? facts.customer_id} ${facts.plan ?? ""} ` +
    `${facts.amount ?? ""}${facts.currency ?? ""} | ${data.segment} -> offer: ${offer.type}${value} | urgency ${data.urgency}`;
  result.deliveries = await queueDeliveries(ctx, client, "churn.rescue_drafted", strip(result, "llm"), summary);
  return result;
};

// ----------------------------------------------------- support_ticket_router
const DEFAULT_CATEGORIES = ["billing", "technical", "account", "sales", "feedback", "other"];
const DEFAULT_SLA_MIN: Record<string, number> = { critical: 15, high: 60, normal: 240, low: 1440 };

export const ticketSchema = (categories: string[]): Schema => ({
  type: "object",
  required: ["category", "urgency", "sentiment", "sentiment_score", "language", "summary", "customer", "suggested_reply", "tags"],
  properties: {
    category: { type: "string", enum: categories },
    urgency: { type: "string", enum: ["critical", "high", "normal", "low"] },
    sentiment: { type: "string", enum: ["negative", "neutral", "positive"] },
    sentiment_score: { type: "number", minimum: -1, maximum: 1 },
    language: { type: "string" }, summary: { type: "string", description: "one sentence" },
    customer: { type: "object", required: ["name", "email"], properties: { name: nullable(), email: nullable() } },
    suggested_reply: { type: "string" }, tags: { type: "array", items: { type: "string" } },
  },
});

export const supportTicketRouter: Handler = async (ctx, payload) => {
  const client = await loadClient(ctx);
  const cfg = client?.config ?? {};
  const sc = section(cfg, "support");
  const categories: string[] = (sc.categories ?? DEFAULT_CATEGORIES).map(String);
  if (!categories.includes("other")) categories.push("other");
  const text = asText(inbound(payload));
  const system =
    `You triage support messages for ${sc.business_name || "the company"}. Classify category, urgency ` +
    "(critical = outage, data loss, security or payment blocked for many; high = a customer is blocked; " +
    "normal = needs help; low = question or feedback), sentiment and language, summarise in one sentence, and " +
    "draft a ready-to-send reply in the customer's language for a human agent to approve. " +
    `Tone: ${sc.tone || "empathetic, precise"}. Never promise refunds, credits or timelines ` +
    `unless this policy allows it: ${sc.policy || "no promises"}.`;
  const [data, meta] = await completeJson(ctx, { system, user: `Inbound message payload:\n${text}`, schema: ticketSchema(categories),
    maxTokens: 800, ...llmOpts(cfg, payload) });
  const routing = sc.routing ?? {};
  const sla = { ...DEFAULT_SLA_MIN, ...(sc.sla_minutes ?? {}) };
  const minutes = Number(sla[data.urgency] ?? 240);
  const due = new Date(Date.now() + minutes * 60_000);
  const escalate = data.urgency === "critical" || (data.sentiment === "negative" && data.urgency === "high");
  data.customer.email = normEmail(data.customer.email) ?? normEmail(text);
  const result: Record<string, any> = { ...data, route_to: routing[data.category] ?? routing.default ?? null,
    due_at: due.toISOString(), sla_minutes: minutes, escalate, speed_ms: msSince(ctx.createdAt), llm: meta };
  const summary = `🎫 [${data.urgency}${escalate ? " ⚠️ ESCALATE" : ""}] ${data.category} | ${data.sentiment} | ${data.summary} ` +
    `-> ${result.route_to ?? "unassigned"} (due ${due.toISOString().slice(11, 16)} UTC)`;
  result.deliveries = await queueDeliveries(ctx, client, "ticket.triaged", strip(result, "llm"), summary);
  return result;
};

// ------------------------------------------------------------ usage_reporter
const pct = (sorted: number[], q: number) => (sorted.length ? sorted[Math.max(0, Math.ceil(q * sorted.length) - 1)] : null);
const asMs = (v: unknown) => (v instanceof Date ? v.getTime() : typeof v === "string" ? Date.parse(v) : NaN);

export function buildReport(client: repo.ClientRow, rows: Awaited<ReturnType<typeof repo.reportRows>>, days: number) {
  const cfg = client.config ?? {};
  const minutesMap = { ...DEFAULT_MINUTES_SAVED, ...(cfg.minutes_saved ?? {}) };
  const rate = Number(cfg.hourly_rate_usd ?? 25);
  const jobs = rows.jobs;
  const work = jobs.filter(([t]) => !t.startsWith("internal.") && !t.startsWith("system.") && t !== "usage_reporter");
  const ok = work.filter((j) => j[1] === "succeeded");
  const failed = work.filter((j) => j[1] === "failed");
  const ttr = ok.map((j) => asMs(j[3]) - asMs(j[2])).filter((n) => Number.isFinite(n)).map(Math.trunc).sort((a, b) => a - b);
  const durations = rows.durations.map(Math.trunc).sort((a, b) => a - b);
  const perJob: Record<string, boolean[]> = {};
  let tokens = 0;
  for (const [jobId, , success, pt, ct] of rows.usage) {
    tokens += (pt || 0) + (ct || 0);
    if (jobId) (perJob[jobId] ??= []).push(success);
  }
  const saves = Object.values(perJob).filter((o) => o.includes(false) && o.includes(true)).length;
  const deliveries = jobs.filter(([t]) => t === "internal.deliver");
  const results = ok.map((j) => [j[0], j[4] && typeof j[4] === "object" ? j[4] : {}] as [string, any]);
  const orders = results.filter(([t, r]) => t === "whatsapp_order_triage" && !r.ignored).map(([, r]) => r);
  const ordersValue: Record<string, number> = {};
  for (const r of orders) {
    const c = r.totals?.currency;
    if (c) ordersValue[c] = money((ordersValue[c] ?? 0) + Number(r.totals.total ?? 0));
  }
  const minutes = ok.reduce((n, [t]) => n + (minutesMap[t] ?? 4), 0);
  const byHandler: Record<string, number> = {};
  for (const [t] of work) byHandler[t] = (byHandler[t] ?? 0) + 1;
  const now = new Date();
  const report: Record<string, any> = {
    client: client.slug, client_name: client.name,
    period: { from: new Date(now.getTime() - days * 86400_000).toISOString(), to: now.toISOString(), days },
    webhooks_processed: work.length,
    by_handler: Object.fromEntries(Object.entries(byHandler).sort(([a], [b]) => a.localeCompare(b))),
    succeeded: ok.length, failed: failed.length, in_progress: work.length - ok.length - failed.length,
    success_rate: work.length ? Math.round((ok.length / work.length) * 10000) / 10000 : null,
    processing_ms: { avg: durations.length ? Math.trunc(durations.reduce((a, b) => a + b, 0) / durations.length) : null, p95: pct(durations, 0.95) },
    time_to_result_ms: { p50: pct(ttr, 0.5), p95: pct(ttr, 0.95) },
    llm: { calls: rows.usage.length, tokens, failover_saves: saves },
    deliveries: {
      delivered: deliveries.filter((d) => d[1] === "succeeded").length,
      failed: deliveries.filter((d) => d[1] === "failed").length,
      pending: deliveries.filter((d) => d[1] !== "succeeded" && d[1] !== "failed").length,
    },
    highlights: {
      hot_leads: results.filter(([t, r]) => t === "lead_qualifier" && r.tier === "hot").length,
      orders: orders.length, orders_value: ordersValue,
      churn_rescues_drafted: results.filter(([t, r]) => t === "stripe_churn_rescuer" && !r.ignored).length,
      tickets: results.filter(([t]) => t === "support_ticket_router").length,
      tickets_escalated: results.filter(([t, r]) => t === "support_ticket_router" && r.escalate).length,
    },
    roi: { minutes_saved: minutes, hours_saved: Math.round((minutes / 60) * 10) / 10, hourly_rate_usd: rate,
           value_usd: Math.round((minutes / 60) * rate * 100) / 100 },
  };
  report.summary_text = reportText(report);
  return report;
}

export function reportText(r: Record<string, any>): string {
  const h = r.highlights, roi = r.roi;
  const lines = [`📊 ${r.client_name}: AutoPlatform report (${r.period.from.slice(0, 10)} to ${r.period.to.slice(0, 10)})`];
  lines.push(`• ${r.webhooks_processed} webhooks processed${r.success_rate !== null ? ` · ${(r.success_rate * 100).toFixed(1)}% success` : ""}`);
  if (r.time_to_result_ms.p50 !== null) {
    lines.push(`• Webhook → result: median ${(r.time_to_result_ms.p50 / 1000).toFixed(1)}s (p95 ${(r.time_to_result_ms.p95 / 1000).toFixed(1)}s)`);
  }
  const parts: string[] = [];
  if (h.hot_leads) parts.push(`${h.hot_leads} hot leads`);
  if (h.orders) parts.push(`${h.orders} orders (` + Object.entries(h.orders_value).map(([c, v]) => `${(v as number).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${c}`).join(", ") + ")");
  if (h.churn_rescues_drafted) parts.push(`${h.churn_rescues_drafted} churn rescues drafted`);
  if (h.tickets) parts.push(`${h.tickets} tickets triaged (${h.tickets_escalated} escalated)`);
  if (parts.length) lines.push("• " + parts.join(" · "));
  lines.push(`• ${r.llm.failover_saves} jobs rescued by automatic LLM failover · ${r.deliveries.delivered} CRM deliveries (${r.deliveries.failed} failed)`);
  lines.push(`• ≈ ${roi.hours_saved.toFixed(1)} hours of manual work saved (≈ $${Math.round(roi.value_usd).toLocaleString("en-US")} at $${roi.hourly_rate_usd}/h)`);
  return lines.join("\n");
}

export async function usageReport(ctx: { deps: JobCtx["deps"] }, client: repo.ClientRow, days: number) {
  return buildReport(client, await repo.reportRows(ctx.deps.db, client.id, days), days);
}

export const usageReporter: Handler = async (ctx, payload) => {
  const client = await loadClient(ctx);
  if (!client) throw new PermanentError("usage_reporter needs a client");
  const days = Math.max(1, Math.min(Number(payload?.days ?? 7) || 7, 90));
  const report = await usageReport(ctx, client, days);
  if (payload?.deliver ?? true) report.deliveries = await queueDeliveries(ctx, client, "report.weekly", report, report.summary_text);
  return report;
};

export async function enqueueWeeklyReports(deps: JobCtx["deps"]): Promise<string[]> {
  const ids = await repo.clientsForWeeklyReports(deps.db);
  const jobIds = await repo.createJobsBulk(deps.db, ids.map((clientId) => ({
    type: "usage_reporter", payload: { days: 7, deliver: true }, clientId, maxAttempts: 3, source: "cron" })));
  await deps.queue.enqueueMany(jobIds);
  return jobIds;
}

export const PRODUCT_HANDLERS: Record<string, Handler> = {
  lead_qualifier: leadQualifier,
  whatsapp_order_triage: whatsappOrderTriage,
  stripe_churn_rescuer: stripeChurnRescuer,
  support_ticket_router: supportTicketRouter,
  usage_reporter: usageReporter,
};
