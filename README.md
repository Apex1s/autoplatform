---
title: AutoPlatform
emoji: ⚙️
colorFrom: indigo
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: 24/7 multi-client automation backend (FastAPI + queue + LLM failover)
---

# AutoPlatform

A zero-cost, always-on automation backend for multi-client work:

- **FastAPI** handles webhook ingestion, a per-client task API, cron triggers, health checks and metrics.
- **Redis queue** on the Upstash free tier, over TCP/TLS or the HTTPS REST API. It recovers from crashes, retries delayed jobs and supports idempotency keys.
- **Postgres** on the Neon free tier stores clients, jobs, execution logs, API usage and worker health.
- **LLM router** sends requests to Groq or Gemini. On a 429 or a timeout it fails over to the other provider immediately, and it retries 5xx errors with exponential backoff. It also tracks token usage.
- **Async worker** runs inside the web container, so one free instance does everything.
- **GitHub Actions** pings the service to keep it awake, runs maintenance, probes the providers, and can drain the queue itself if the host is down.

None of these services needs a credit card.

```
 client / 3rd-party ──► POST /webhook (X-API-Key)            ┌──────────────┐
                    ──► POST /webhook/{slug} (HMAC-SHA256)   │ Neon Postgres│ jobs, clients, logs,
 GitHub Actions ────► POST /cron/{name} (Bearer CRON_TOKEN)  │              │ usage, worker health
                               │                             └──────▲───────┘
                       FastAPI ─┴─► job row ──► Upstash LPUSH        │
                                                  │                 │
              dispatcher (1 blocking BLMOVE) ◄────┘ ──► N handlers ──┤
                                                            │       │
                                               LLM router: Groq ⇄ Gemini
```

## Hosting choice (verified November 2026)

| Platform | Free? | Notes |
|---|---|---|
| **Render** (used here) | ✅ Free web service with no payment method | Sleeps after 15 min without inbound traffic, so GitHub Actions pings `/livez` every 10 min. 750 free instance-hours a month. Only SMTP ports are blocked outbound. |
| Hugging Face Spaces | ❌ for Docker | Docker and Gradio Spaces on compute now need a PRO plan. Spaces can also only make outbound requests on ports 80, 443 and 8080, which is why the app can reach Upstash over HTTPS (`UPSTASH_REDIS_REST_URL`). |

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | – | Checks DB, Redis and the worker heartbeat; returns 503 if DB or Redis is down |
| GET | `/livez` | – | Liveness only, with no DB or Redis access (for keep-alive pings and health checks) |
| GET | `/metrics` | optional `METRICS_TOKEN` | Prometheus text; add `?format=json` for JSON |
| POST | `/webhook` | `X-API-Key` | Enqueue a client job `{type, payload, max_attempts?, delay_seconds?}`; supports `Idempotency-Key` |
| POST | `/webhook/{client_slug}` | `X-Signature-256: sha256=<hmac>` (or `X-Hub-Signature-256`) | Signed third-party webhooks (GitHub/Stripe style), queued as a `webhook.event` job |
| GET | `/tasks/{job_id}`, `/tasks` | `X-API-Key` | Job status and result, scoped to the client |
| POST | `/cron/{maintenance\|provider_probe}` | `Bearer CRON_TOKEN` | Scheduled system jobs |
| GET | `/cron/jobs/{job_id}` | `Bearer CRON_TOKEN` | Status of a system job |
| POST/GET | `/admin/clients` | `Bearer ADMIN_TOKEN` | Create a client (returns `api_key` and `webhook_secret` once) or list clients |
| DELETE | `/admin/clients/{slug}` | `Bearer ADMIN_TOKEN` | Deactivate a client (revokes its API key) |
| GET | `/docs` | – | OpenAPI UI |

**Job types:**
- `echo`
- `llm.complete`: `{prompt | messages, system?, max_tokens?, temperature?}`
- `http.fetch`: `{url, params?, summarize?}`. Public HTTPS APIs only, e.g. from [public-apis](https://github.com/public-apis/public-apis); private and internal addresses are blocked.
- `webhook.event`

To add a job type, register a new function with `@handler("name")` in `workers/handlers.py`.

## Failover policy (`core/llm_router.py`)

| Provider response | Action |
|---|---|
| 429 | Fail over to the next provider **immediately**. The rate-limited provider is skipped for `Retry-After` seconds (also read from Gemini's `retryDelay`; default `LLM_DEFAULT_COOLDOWN`) |
| timeout / 408 / 504 | Fail over immediately |
| 5xx, network error, malformed body | Retry the same provider with jittered exponential backoff (`LLM_MAX_RETRIES`), then fail over |
| other 4xx (bad key, unknown model) | Fail over immediately, no retry |
| every provider failed | Back off and run another pass (`LLM_MAX_ROUNDS`). If that also fails, the job is retried later with job-level backoff |

Every attempt is written to `api_usage` (provider, model, tokens, latency, status, error kind).
Default models are `qwen/qwen3.8-27b` on Groq and `gemini-flash-lite-latest` on Gemini. Groq's `gpt-oss` models and the larger Gemini "thinking" models spend tokens on reasoning before they answer, so they are worse for small `max_tokens` budgets.

## Free-tier budget

| Service | Free limit | What this app uses while idle |
|---|---|---|
| Upstash | 500K commands/month | About 2 commands per poll (`ZRANGE` plus a blocking `BLMOVE`), **regardless of concurrency**, plus a heartbeat `SET`. With `WORKER_POLL_TIMEOUT=40` and `HEARTBEAT_INTERVAL=120` that's about 150K a month. |
| Neon | 100 CU-hours/month; compute scales to zero after 5 min | No idle connections (`DATABASE_POOL=auto` uses NullPool for Neon). Heartbeats go to Redis, and worker health reaches Postgres only during the twice-hourly maintenance run. The database wakes about 2 times an hour, which comes to roughly 30 CU-hours a month, plus real traffic. |
| Render | 750 instance-hours/month | One service running 24/7 uses about 730–744 hours. |
| GitHub Actions | Unlimited for public repos; 2,000 min/month for private | Maintenance runs twice an hour, about 1,440 min a month. The 10-minute keep-alive adds about 4,300 min a month, so it is off by default and only runs when the repo variable `KEEPALIVE=true` is set. Enable it only on a public repo. |
| Groq / Gemini | Per-model rate limits | 429 responses fail over automatically, and the rate-limited provider cools down. |

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # no keys needed: falls back to SQLite + in-memory Redis
uvicorn app.main:app --port 7860
python -m pytest -q             # 53 tests; the suite never reads .env (hermetic)
TEST_DATABASE_URL=postgresql://... TEST_REDIS_URL=redis://... python -m pytest -q   # real backends
python -m scripts.probe_providers   # checks each configured provider and writes probe_results.json
```

To rehearse failover against a provider that is down:
`FAKE_GROQ_MODE=429 uvicorn scripts.dev_fake_llm:app --port 58001`, then start the app with
`GROQ_BASE_URL=http://127.0.0.1:58001/groq`. Gemini can stay real.

## Deploy on Render

1. Sign up at https://render.com with GitHub. No card is needed.
2. Open `https://render.com/deploy?repo=https://github.com/<you>/autoplatform`, paste the six secrets it asks for (`DATABASE_URL` with the Neon **-pooler** host, `UPSTASH_REDIS_URL`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `ADMIN_TOKEN`, `CRON_TOKEN`), and click **Apply**.
3. In the GitHub repo:
   - Add a variable `PLATFORM_URL` set to `https://<service>.onrender.com`.
   - Add a secret `CRON_TOKEN`.
   - On a public repo, also add the variable `KEEPALIVE=true`.
4. Verify:
   ```bash
   python -m scripts.e2e_smoke --base-url https://<service>.onrender.com --admin-token "$ADMIN_TOKEN" --llm
   ```

Render redeploys automatically on every push to `main`. Migrations run on boot.

## Scheduled runs: GitHub Actions

`.github/workflows/scheduled_runner.yml`:

- **keepalive** runs every 10 minutes when `KEEPALIVE=true` and calls `GET /livez`.
- **trigger** runs at :17 and :47 past each hour:
  1. Calls `/healthz`, retrying for up to 5 minutes so a sleeping instance has time to start.
  2. Fires `/cron/maintenance`. This reaps workers whose Redis heartbeat expired, requeues their jobs and releases the ones left `running`. It also re-enqueues jobs that never reached Redis, saves worker health to Postgres, and prunes logs older than `LOG_RETENTION_DAYS`.
  3. Every 6 hours, also fires `/cron/provider_probe`.
  4. Resets GitHub's 60-day inactivity timer for scheduled workflows once a week.
  5. **Fallback:** if the host is down and `RUN_DRAIN_FALLBACK=true` is set, it runs `python -m workers.worker --drain` inside the Action.

`ci.yml` runs the test suite on SQLite, then again on real Postgres 16 and Redis 7, then builds the Docker image.

## Reliability notes

- Postgres is the source of truth for job state; Redis carries only job IDs. Claiming a job is one atomic `UPDATE … WHERE status IN (queued, retrying)`, so a job ID that is enqueued twice still runs once. Delivery is at least once, so handlers should be idempotent.
- A single dispatcher reserves jobs into this worker's processing list (`BLMOVE`) and runs up to `WORKER_CONCURRENCY` of them at a time. It wakes early when a delayed retry becomes due. On graceful shutdown, in-flight jobs go straight back to pending.
- If a worker crashes, its Redis heartbeat expires (after 3 × `HEARTBEAT_INTERVAL` + 30s). Maintenance then requeues its reserved jobs and releases the ones it was running.
- If a backend returns from the blocking pop without waiting, the worker sleeps instead of spinning.
- Migrations (`db/migrate.py`) are versioned and serialized with a Postgres advisory lock. They run on every boot.
