---
title: AutoPlatform
emoji: ⚙️
colorFrom: indigo
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: 24/7 multi-client automation backend (webhooks + queue + LLM failover)
---

# AutoPlatform

A zero-cost, always-on automation backend for multi-client work. It ingests webhooks, queues
jobs per client, runs them with Groq ⇄ Gemini LLM failover, and tracks everything in Postgres.

It ships as **two interchangeable runtimes**. Both have the same HTTP API, the same database schema and the same Redis keys:

| Runtime | Where | Use it for |
|---|---|---|
| **Cloudflare Worker** (`worker/`, TypeScript) | Cloudflare Workers Free | **Production.** No card needed, never sleeps, built-in cron every minute, commercial use allowed |
| **FastAPI app** (`app/`, Python, Docker) | Any Docker host or your laptop | Local development, or hosts where you can run a container 24/7 |

External services, all on free tiers that need no credit card:

- **Neon Postgres** stores clients, jobs, execution logs, API usage and worker health.
- **Upstash Redis** holds the job queue (pending list and delayed retries), idempotency keys, and LLM cooldowns shared across Worker instances.
- **Groq and Gemini** provide the LLMs, through their OpenAI-compatible APIs.

```
 client / 3rd party ──► POST /webhook          (X-API-Key)          ┌───────────────┐
                    ──► POST /webhook/{slug}   (HMAC-SHA256)        │ Neon Postgres │
                                 │                                  └───────▲───────┘
            Worker: insert job ──┴─► Upstash LPUSH ─► respond 202           │ claim / finish
                                         │                                  │
            ctx.waitUntil: run it now ◄──┘    Cron Trigger (every minute):  │
            (after the response)              promote retries, drain queue, │
                     │                        maintenance :17/:47, probe 6h │
                     └──────────► handler ──► LLM router: Groq ⇄ Gemini ────┘
```

## Hosting choice (checked November 2026)

| Platform | Free with no card? | Verdict |
|---|---|---|
| **Cloudflare Workers** | ✅ 100k requests/day, cron triggers, permanent `*.workers.dev` URL | **Used.** Commercial use is allowed. |
| Back4App Containers | ⚠️ No card, but the free URL expires 60 min after each deploy ($5/month to keep it) | Not usable 24/7 |
| Render | ❌ Asked for a card at sign-up | `render.yaml` is kept for anyone who has a card on file |
| Hugging Face Spaces | ❌ Docker Spaces need PRO, and outbound traffic is limited to ports 80, 443 and 8080 | |
| Koyeb / Fly.io / Railway | ❌ Koyeb and Fly.io require a card; Railway offers a trial credit only | |
| Vercel Hobby | ⚠️ No card, but non-commercial use only | |

GitHub Actions is used only for CI and an uptime monitor. GitHub's terms don't allow hosted runners to serve as an application host.

## Deploy on Cloudflare (free, no card)

1. Sign up at https://dash.cloudflare.com/sign-up. On first use, choose a `workers.dev` subdomain.
2. Go to **Workers & Pages → Create → Import a repository**, connect GitHub, and pick this repo. Set **Root directory** to `worker`, leave the build command empty, and keep the deploy command as `npx wrangler deploy`. The Worker name `autoplatform` comes from `worker/wrangler.toml`.
3. Under **Settings → Variables and Secrets**, add six values of type **Secret**: `DATABASE_URL`, `UPSTASH_REDIS_URL` (the `rediss://default:…@….upstash.io:6379` URL; the REST endpoint and token are derived from it), `GROQ_API_KEY`, `GEMINI_API_KEY`, `ADMIN_TOKEN` and `CRON_TOKEN`.
4. Verify:
   ```bash
   python -m scripts.e2e_smoke --base-url https://autoplatform.<subdomain>.workers.dev --admin-token "$ADMIN_TOKEN" --llm
   ```
   `/healthz` reports `"status": "ok"` once the first cron run has happened, within a minute. If anything is missing, it lists it in `config_warnings`.

Every push to `main` redeploys through Cloudflare's Git integration. The schema is created automatically the first time a query finds a table missing.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | – | DB, Redis and scheduler checks, the backends in use, and `config_warnings`; returns 503 if DB or Redis is down |
| GET | `/livez` | – | Liveness only |
| GET | `/metrics` | `METRICS_TOKEN`, or `ADMIN_TOKEN` in production | Prometheus text; add `?format=json` for JSON |
| POST | `/webhook` (alias `/tasks`) | `X-API-Key` | Enqueue `{type, payload, max_attempts?, delay_seconds?}`; supports `Idempotency-Key` |
| POST | `/webhook/{client_slug}` | `X-Signature-256` / `X-Hub-Signature-256` (`sha256=<hmac>`) | Signed third-party webhooks (GitHub/Stripe style), queued as a `webhook.event` job |
| GET | `/tasks/{job_id}`, `/tasks` | `X-API-Key` | Job status and result, scoped to the client |
| POST | `/cron/{maintenance\|provider_probe}` | `Bearer CRON_TOKEN` | Run a system job now |
| GET | `/cron/jobs/{job_id}` | `Bearer CRON_TOKEN` | Status of a system job |
| POST/GET | `/admin/clients` | `Bearer ADMIN_TOKEN` | Create a client (returns `api_key` and `webhook_secret` once) or list clients |
| DELETE | `/admin/clients/{slug}` | `Bearer ADMIN_TOKEN` | Deactivate a client (revokes its API key) |
| PATCH | `/admin/clients/{slug}` · GET `/admin/clients/{slug}/usage` | `Bearer ADMIN_TOKEN` | Change plan, limits, handlers, outbound URL or config · ROI report |
| GET/PATCH | `/me` · GET `/usage?days=7` | `X-API-Key` | Self-serve profile, quota, endpoints and config · ROI report |
| POST/GET/PATCH/DELETE | `/agency/clients[/{slug}]` · GET `/agency/usage` | `X-API-Key` (agency plan) | White-label sub-clients and their usage |
| POST | `/webhook/{slug}/{handler}` | Stripe / Typeform / Shopify / HMAC signature | Signed ingest straight into a handler (GET answers Meta's `hub.challenge`) |
| POST | `/webhook/stripe` · GET `/billing/welcome` | Stripe signature · session id | Auto-provisioning on payment · read-once credentials |
| GET/POST | `/demo` · `/demo/run` · `/demo/jobs/{id}` | none (rate-limited) | Live demo page and API |
| POST | `/cron/weekly_reports` | `Bearer CRON_TOKEN` | Queue every client's weekly ROI report |

**Job types:**
- Revenue handlers (`workers/products.py` and `worker/src/products.ts`, identical behaviour):
  - `lead_qualifier`: extracts the lead's details and scores intent and fit 0–100 (the tier comes from your thresholds). It recovers contact details from the raw payload, drafts a WhatsApp and an email reply, and delivers to your CRM.
  - `whatsapp_order_triage`: accepts Meta WhatsApp Cloud API or any message JSON. Items are matched to *your* catalog and prices and totals are computed in code, never by the model. Sends a multilingual confirmation.
  - `stripe_churn_rescuer`: takes Stripe-signed events. Offers are kept within your guardrails (allowed list, discount cap, payment update for failed payments), with a founder-voice email.
  - `support_ticket_router`: category, urgency and sentiment, plus routing, an SLA due time, escalation and a suggested reply.
  - `usage_reporter`: the weekly ROI digest (volume, time-to-result, failover saves, deliveries, highlights, hours and $ saved). It runs automatically on Mondays at 08:17 UTC.
- Utilities:
  - `echo`
  - `llm.complete`
  - `webhook.event`
  - `http.fetch`: public HTTPS APIs only; private and internal addresses are blocked.

All LLM output is strict JSON: JSON mode on both providers, schema coercion, and one repair round. Results reach your CRM through HMAC-signed `internal.deliver` jobs with their own retries (8 attempts over about 35 minutes), so a down CRM never re-runs the AI. Slack and Discord URLs get readable summaries.

**Monetization layer:**
- **Plans and limits:** `retainer`, `agency`, `agency_client`, `guard_starter`, `guard_pro`, `demo`, each with monthly quotas and handler allow-lists.
- **Self-serve:** `/me`, `/usage`.
- **Agencies:** `/agency/clients`, `/agency/usage`.
- **Signed ingest per handler:** `/webhook/{slug}/{handler}` verifies Stripe, Typeform, Shopify and HMAC signatures, and handles Meta's `hub.challenge`.
- **Stripe auto-provisioning:** `POST /webhook/stripe`, with a credentials page that shows them once.
- **Live demo:** `/demo`.

Go-to-market material lives in `monetization/`:
- `gtm/offer_architecture.md`: offers, SLA wording, costs, and the Zapier/Make calculator.
- `gtm/outbound_playbooks.md`: five outreach sequences.
- `gtm/stripe_checkout_setup.md`: self-serve billing setup.
- `configs/`: per-vertical client config templates.
- `demo/index.html`: the demo page.

```bash
python -m scripts.provision_client --api https://<host> --admin-token "$ADMIN_TOKEN" --slug acme --name "Acme" \
  --plan retainer --handlers lead_qualifier --outbound https://crm.example.com/hook --config monetization/configs/real_estate.json
python -m scripts.simulate_stripe --url https://<host> --secret "$STRIPE_WEBHOOK_SECRET" --plan guard_pro   # test billing without Stripe
```

## Failover policy

| Provider response | Action |
|---|---|
| 429 | Fail over **immediately**. The provider is skipped for `Retry-After` seconds (also read from Gemini's `retryDelay`). On the Worker this cooldown is stored in Redis, so every instance honours it. |
| timeout / 408 / 504 | Fail over immediately |
| 5xx, network error, malformed body | Retry the same provider with jittered exponential backoff, then fail over |
| other 4xx (bad key, unknown model) | Fail over immediately, no retry |
| every provider failed | Back off and run another pass. If that also fails, the job is retried later (30 s, 60 s, 120 s …) |

Every attempt is saved to `api_usage` (provider, model, tokens, latency, status, error kind).
Default models are `qwen/qwen3.8-27b` on Groq and `gemini-flash-lite-latest` on Gemini.

## Free-tier budget (Worker)

| Service | Free limit | Usage |
|---|---|---|
| Cloudflare Workers | 100k requests/day, 10 ms CPU and 50 subrequests per invocation, 5 cron triggers | One cron trigger. Each invocation tracks a subrequest **budget** (45), so a drain stops cleanly before the limit. Network waits (LLM, DB, Redis) don't count as CPU. |
| Upstash | 500K commands/month | About 3 commands per cron minute (ZRANGE, RPOP, liveness SET), roughly 130K a month idle, plus a few per job |
| Neon | 100 CU-hours/month; scales to zero after 5 min idle | Postgres is touched only when there is work, plus maintenance at :17 and :47. That's about 30 CU-hours a month idle. |

Throughput: jobs run right after the webhook response, and the cron drains up to 4 more per minute.

## Development

```bash
# Python runtime
python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
python -m pytest -q                       # 102 hermetic tests (never read .env)
TEST_DATABASE_URL=postgresql://... TEST_REDIS_URL=redis://... python -m pytest -q
uvicorn app.main:app --port 7860

# Worker runtime
cd worker && npm ci
npx tsc --noEmit && npx vitest run        # 68 tests (integration parts need TEST_DATABASE_URL / TEST_REDIS_URL)
NODE_USE_ENV_PROXY=1 npm run live         # run the Worker in Node against the real services in ../.env
npx wrangler deploy --dry-run --outdir dist    # bundle exactly what Cloudflare deploys
```

`python -m scripts.probe_providers` checks each configured provider. `python -m scripts.e2e_smoke` black-box tests any deployment, Worker or Docker.

## Scheduling and monitoring

- **Cloudflare Cron Trigger** (`* * * * *`, in `worker/wrangler.toml`):
  - promotes due retries and drains the queue;
  - at :17 and :47, re-enqueues stuck jobs, releases jobs abandoned mid-run, prunes old logs, and records worker health in Postgres;
  - every 6 hours, probes each LLM provider.
- **GitHub Actions `uptime-monitor`** (`scheduled_runner.yml`) calls `/healthz` every 30 minutes and fails if it isn't `ok`, which triggers GitHub's failure email (free alerting). Set the repo variable `PLATFORM_URL` to enable it. With `RUN_DRAIN_FALLBACK=true` plus four secrets, it can also drain the queue with the Python worker while the host is down.
- **CI** (`ci.yml`): Python tests on SQLite, then on Postgres 16 and Redis 7, and the Docker build; then the Worker typecheck, tests on Postgres and Redis, and the bundle check.

## Reliability notes

- Postgres is the source of truth. Claiming a job is one atomic `UPDATE … WHERE status IN (queued, retrying)`, so delivery is at least once and duplicate queue entries are harmless. Handlers should be idempotent.
- Idempotency keys are claimed in Redis **before** the job row is written, so duplicates leave no extra rows.
- If a run dies mid-job (an invocation limit, a deploy), the job stays `running` until its lease runs out (3 × `JOB_TIMEOUT_SECONDS`). Maintenance then puts it back in the queue. Jobs whose ID never reached Redis are re-enqueued after 10 minutes.
- Cloudflare's edge bot filter rejects Python's built-in `urllib` user agent with 403 before the Worker runs. Use `requests`/`httpx` or set a User-Agent (all webhook senders tested pass).
- `/healthz` reports the backends actually in use and any missing configuration. A deploy without its secrets shows `degraded` with reasons instead of looking healthy.
- The Python runtime adds a resident worker (one blocking `BLMOVE` poller, N concurrent jobs, Redis heartbeats, dead-worker reaping). See `workers/worker.py`.
