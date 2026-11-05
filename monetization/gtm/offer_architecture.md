# Offer architecture

Three offers run on the same engine. Each one maps to a plan in `core/plans.py` / `worker/src/plans.ts`, so what you sell is exactly what the platform enforces: quotas, enabled handlers and delivery.

| | Speed-to-Lead & Ops Retainer | White-Label Agency Engine | AI Webhook Guard & Router |
|---|---|---|---|
| Buyer | Business owner / ops lead at a lead-heavy SMB | Marketing, web or CRM agency | SaaS founder, RevOps |
| Price | **$1,500 setup + $1,000/month** | **$500/month** | **$99** (Starter) / **$249** (Pro) per month |
| Delivery | Done for you | Self-serve for the agency; you support the agency | Self-serve |
| Platform plan | `retainer`: 20,000 jobs/month, all handlers | `agency`: 25 sub-clients × 5,000 jobs | `guard_starter`: 2,000 jobs · `guard_pro`: 10,000 jobs |
| Sales motion | Audit → live demo → proposal | LinkedIn/email → demo → trial sub-client | Demo page → Stripe Checkout → auto-provisioned |

Live proof for every sales conversation: `https://autoplatform.data-marketplace-edge.workers.dev/demo`. Add `?cta=<your booking link>` to show a booking button.

---

## Offer 1: Speed-to-Lead & Ops Retainer ($1,500 setup + $1,000/month)

**Promise (measurable):** every inbound lead is qualified, scored and answered with a drafted personal WhatsApp or email reply within seconds. Your CRM receives it with a score and a next action, and you get a weekly ROI report.

**Included**
- Up to **3 live automations** from the handler set. Typical picks:
  - `lead_qualifier` for Meta Lead Ads, website forms, Typeform/Tally or portal leads
  - `support_ticket_router` for contact forms, Crisp or Intercom
  - `whatsapp_order_triage` for order-taking businesses
- Integration into the client's CRM or WhatsApp gateway through the signed outbound webhook (HubSpot, Pipedrive, GoHighLevel, a Make/Zapier "catch hook", or a 360dialog/Twilio relay).
- Per-client configuration: business context, ideal customer, scoring thresholds, tone, booking link, catalog and SLAs (see `monetization/configs/`).
- Slack/Discord alerts for hot leads, plus the **weekly ROI report** every Monday (`usage_reporter`).
- Monitoring (uptime check every 30 minutes, failure alerts), prompt tuning, and a 30-minute monthly optimization call.
- Up to 20,000 jobs a month. Beyond that, re-quote or add $250 per extra 10,000.

**Not included** (state this in the proposal): CRM licences, WhatsApp Business API conversation fees charged by Meta or a BSP, ad spend or ad management, and human SDR work.

**Onboarding (setup fee covers this)**

| Day | Work |
|---|---|
| 0 | Kick-off: map lead sources and response rules, collect the business context (30 minutes) |
| 1–2 | `python -m scripts.provision_client …` with the client config; connect sources to `/webhook/<slug>/<handler>` |
| 3–4 | Wire the outbound webhook into the CRM or WhatsApp gateway; replay 20 historical leads and calibrate scoring |
| 5 | Go live, with the client approving drafted replies for the first week |
| 7 | First weekly report, and agree on any auto-send rules |

**Terms:** 3-month minimum, then month-to-month with 30 days' notice. The setup fee is non-refundable once go-live is reached.

**Why it's worth $1,000 a month:** the report proves it every Monday. It shows median webhook → result time, hot leads flagged, jobs rescued by LLM failover, CRM deliveries, and hours saved × hourly rate. Set `hourly_rate_usd` and `minutes_saved` in the client config to the client's real numbers; don't inflate them.

## Offer 2: White-Label Agency Engine ($500/month)

**Promise:** your agency sells AI automations under your own brand without running servers, queues or LLM accounts, or handling rate limits.

**Included**
- An agency API key on the `agency` plan.
- Up to **25 sub-clients**, provisioned by the agency itself through `POST /agency/clients` (or `scripts/provision_client.py --agency-key`).
- Each sub-client gets branded endpoints (`/webhook/<agency>-<client>/<handler>`), its own API key and signing secret, a 5,000 jobs/month cap, and an optional handler allow-list.
- Per-client usage and ROI through `GET /agency/usage?days=30`: jobs, success rate, time-to-result, failover saves, tokens, hours saved. The agency resells this as its own reporting.
- Every handler, signed deliveries into the agency's or client's tools, and the weekly reports.
- Optional add-on ($150 one-off): serve endpoints on the agency's own domain through Cloudflare Workers custom domains. This needs the domain on Cloudflare, which is free.

**Not included:** talking to the agency's clients (the agency owns the relationship) and custom handlers. Quote those separately at $750–$2,000 each.

**Why agencies say yes:** a 5-client agency charging $300–$800 per client per month for "AI lead response" clears $1,500+ a month of margin on a $500 cost, with no infrastructure to own. Your cost to serve them is close to zero until volume grows (see costs below).

## Offer 3: AI Webhook Guard & Router ($99 / $249 per month)

**Promise:** point your raw webhooks (Stripe, Crisp, Intercom, forms, Sentry or GitHub through `webhook.event`) at one endpoint. You get structured, enriched JSON in Slack, Discord or your CRM, with retries and signed delivery.

| | Starter $99 | Pro $249 |
|---|---|---|
| Jobs per month | 2,000 | 10,000 |
| Handlers | `support_ticket_router`, `stripe_churn_rescuer`, `webhook.event`, `usage_reporter` | + `lead_qualifier`, `http.fetch` |
| Self-serve setup | Stripe Checkout → credentials shown once → `PATCH /me` sets `notify_url`, outbound URL and `stripe_webhook_secret` | same |

Self-serve onboarding is fully automatic (`stripe_checkout_setup.md`). Support is email only, with a 2-business-day response.

---

## Reliability: what you can promise, and what it rests on

**Built-in guarantees (true today, in code and tests):**
- **No lost webhooks after a 202:** on the production path the job is written to Postgres before `202 Accepted` is returned. Execution is at-least-once with an atomic claim, so duplicates are harmless.
- **LLM failover:** a 429 or timeout from Groq switches to Gemini immediately (and vice versa), and the cooldown is shared across all instances. 5xx errors are retried with backoff. Every attempt is logged in `api_usage`.
  - The live demo recovered a **real** Groq 429 this way.
  - A production run recovered a real Gemini 5xx.
- **Guaranteed-delivery semantics:** CRM deliveries are separate jobs with 8 attempts over about 35 minutes. A down CRM never re-runs the LLM, and failures stay visible as `failed` jobs and in the weekly report.
- **Recovery:** jobs abandoned mid-run are re-queued after their lease. Jobs that never reached Redis are re-enqueued after 10 minutes.

**SLA wording to use:**
> 99.9% monthly availability **target** for the ingestion endpoint, measured by our external monitor every 30 minutes. If availability falls below target, you receive a service credit of 10% of that month's fee per 0.1% shortfall, capped at 50%.

**Before you sign anything contractual, upgrade the stack.** The free tiers it runs on today carry **no vendor SLA** and have hard limits:
- Groq free-tier rate limits are what produced the real 429 above.
- Cloudflare Workers Free has a 10 ms CPU limit and 50 subrequests per invocation.
- Neon Free has 100 CU-hours a month; Upstash Free has 500K commands a month.

Upgrading once revenue starts costs a rounding error against one retainer:

| Component | Upgrade | Why |
|---|---|---|
| Cloudflare | Workers Paid ($5/month) | Higher CPU/subrequest limits, more cron headroom |
| Neon | Launch plan (usage-based) | More compute hours, no free-tier suspension risk |
| Upstash | Pay-as-you-go ($0.2 per 100K commands) | No hard monthly cap |
| Gemini | Paid tier | **Free-tier prompts may be used by Google to improve its products.** Don't send client PII through it; until then, set `"llm_providers": ["groq"]` in PII-sensitive client configs |
| Groq | Developer tier | Higher rate limits |

**Measured LLM usage per job** (production, November 2026):

| Handler | Tokens per job |
|---|---|
| `lead_qualifier` | about 944 |
| `whatsapp_order_triage` | about 736 |
| `stripe_churn_rescuer` | about 696 |
| `support_ticket_router` | about 588 |

So 10,000 jobs is about **7–10M tokens** a month. Multiply by the current per-million-token price on the Groq and Gemini pricing pages to get your marginal LLM cost.

**Compliance:** leads and tickets are personal data. You are the processor and your client is the controller, so put a DPA in your contract. List Groq, Google, Cloudflare, Neon and Upstash as sub-processors. Check local rules: GDPR/CNIL for EU clients, and law 09-08 with CNDP declarations for Moroccan clients. This isn't legal advice; have one template reviewed once.

---

## Zapier / Make cost calculator

**Formula**

```
monthly SaaS cost  ≈ runs_per_month × billable_steps_per_run × price_per_task
                   + AI step / model costs billed separately
AutoPlatform cost  ≈ your plan price (fixed) + LLM tokens (tokens_per_job × jobs × $/token)
```

**Price assumptions** (third-party 2026 summaries that disagree with each other, so verify on [zapier.com/pricing](https://zapier.com/pricing) and [make.com/en/pricing](https://www.make.com/en/pricing) before quoting):
- **Zapier:** Professional about $29.99/month for 750 tasks, or 2,000 tasks for about $73.50/month billed monthly; Team about $103.50/month for 2,000 tasks. Effective price falls to roughly **$0.02 per task around 10,000 tasks**, and tiers scale to 2M tasks a month. Sources: [JetAdmin](https://www.jetadmin.io/blog/zapier-pricing/), [eesel](https://eesel.ai/blog/zapier-subscription), [Toolradar](https://toolradar.com/blog/zapier-pricing-2026).
- **Make:** about $10.59 (Core), $18.82 (Pro) or $34.12 (Teams) per month for 10,000 credits billed monthly, so roughly **$0.001–0.0035 per credit**. AI modules consume variable credits, and failed operations still count. Sources: [JetAdmin](https://www.jetadmin.io/blog/make-pricing/), [DEV](https://dev.to/trackstack/makecom-pricing-2026-what-operations-actually-cost-and-the-math-most-guides-skip-50bn).

**Worked examples:**

| Business | Runs/month | Steps/run | Zapier at about $0.02/task | Make at about $0.002/credit |
|---|---|---|---|---|
| Real-estate agency, Meta lead ads | 1,500 leads | 7 (trigger, formatter, AI, CRM, WhatsApp, Slack, filter) | 10,500 tasks ≈ **$210/month** + AI | ≈ $21/month + AI credits |
| Dental clinic group (3 sites) | 900 leads + 600 tickets | 7 | 10,500 tasks ≈ **$210/month** + AI | ≈ $21/month |
| Restaurant/e-commerce WhatsApp orders | 4,000 orders | 9 (parse, lookup, price, CRM, notify, …) | 36,000 tasks ≈ **$600–720/month** + AI | ≈ $72/month |
| Agency running 10 clients | 15,000 events | 6 | 90,000 tasks ≈ **$1,500–1,800/month** | ≈ $180/month |

**How to use this honestly in sales:**
- **$600–$2,000 a month of fee savings exists only above about 30,000–100,000 Zapier tasks a month,** typically multi-step order routing or agencies with many clients. Show the math with *their* zap history (Zapier → Task History export).
- **Against Make, fees are rarely the argument.** Sell what Make doesn't give them:
  - sub-5-second AI qualification with schema-validated output;
  - automatic Groq⇄Gemini failover;
  - deliveries that retry for 35 minutes without re-running the AI;
  - a weekly ROI report;
  - deterministic prices and discount caps the AI can't override;
  - someone accountable when it breaks.
- **Below about 10,000 tasks a month, lead with speed-to-lead revenue, not tool cost.** One extra closed deal a month pays the retainer for most real-estate and high-ticket clinic clients.

---

## Path to $10,000+ MRR (planning arithmetic, not a forecast)

| Mix | MRR |
|---|---|
| 5 retainers × $1,000 | $5,000 |
| 6 agencies × $500 | $3,000 |
| 10 Guard Pro × $249 | $2,490 |
| **Total** | **$10,490** (plus $7,500 one-off setup fees) |

Working backwards with **assumed** rates (measure your own and update): 3–5% positive reply rate on cold email, 20–40% demo-to-close for audit-led outreach, and 1–3% visitor-to-checkout on the self-serve page. 5 retainers then need about 15–25 demos, which needs about 400–800 well-targeted contacts. The outreach plan is in `outbound_playbooks.md`.
