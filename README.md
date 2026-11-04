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

- **FastAPI**: webhook ingestion, per-client task API, cron triggers, health and metrics
- **Redis queue** (Upstash free tier): a reliable queue with crash recovery, delayed retries and idempotency keys
- **Postgres** (Neon or Supabase free tier): stores clients, jobs, execution logs, API usage and worker health
- **LLM router**: Groq ⇄ Gemini with instant failover on 429 or timeout, exponential backoff on 5xx, and token tracking
- **Async workers**: run inside the web container (one free container does everything) or standalone
- **GitHub Actions cron**: wakes the host, runs maintenance, probes providers, and can drain the queue itself if the host is down

Every service used is on a free tier that needs **no credit card**.

```
 client / 3rd-party ──► POST /webhook (X-API-Key)            ┌─────────────┐
                    ──► POST /webhook/{slug} (HMAC-SHA256)   │  Postgres   │ jobs, clients,
 GitHub Actions ────► POST /cron/{name} (Bearer CRON_TOKEN)  │ (Neon/Supa) │ logs, usage, health
                               │                             └──────▲──────┘
                       FastAPI ─┴─► job row ──► Redis LPUSH          │
                                                  │                 │
                          embedded worker ◄─ BLMOVE ─┘ ──► handler ──┤
                                                            │       │
                                               LLM router: groq ⇄ gemini
```

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | – | DB + Redis + worker heartbeat check (503 if DB/Redis is down) |
| GET | `/livez` | – | Liveness only (used by the Docker HEALTHCHECK) |
| GET | `/metrics` | optional `METRICS_TOKEN` | Prometheus text; add `?format=json` for JSON |
| POST | `/webhook` | `X-API-Key` | Enqueue a client job `{type, payload, max_attempts?, delay_seconds?}`; supports `Idempotency-Key` |
| POST | `/webhook/{client_slug}` | `X-Signature-256: sha256=<hmac>` (or `X-Hub-Signature-256`) | Signed third-party webhooks (GitHub/Stripe style) → `webhook.event` job |
| GET | `/tasks/{job_id}`, `/tasks` | `X-API-Key` | Job status and result (scoped to the client) |
| POST | `/cron/{maintenance\|provider_probe}` | `Bearer CRON_TOKEN` | Scheduled system jobs |
| GET | `/cron/jobs/{job_id}` | `Bearer CRON_TOKEN` | Status of a system job |
| POST/GET | `/admin/clients` | `Bearer ADMIN_TOKEN` | Create a client (returns `api_key` and `webhook_secret` once) or list clients |
| GET | `/docs` | – | OpenAPI UI |

**Job types:** `echo`, `llm.complete` (`{prompt | messages, system?, max_tokens?, temperature?}`),
`http.fetch` (`{url, params?, summarize?}`: public HTTPS APIs only, e.g. from
[public-apis](https://github.com/public-apis/public-apis); private and internal addresses are blocked), and `webhook.event`.
To add a job type, register a new function with `@handler("name")` in `workers/handlers.py`.

## Failover policy (`core/llm_router.py`)

| Provider response | Action |
|---|---|
| 429 | Fail over to the next provider **immediately**. The 429'd provider is skipped for `Retry-After` seconds (also read from Gemini's `retryDelay`; default `LLM_DEFAULT_COOLDOWN`) |
| timeout / 408 / 504 | Fail over immediately |
| 5xx, network error, malformed body | Retry the same provider with jittered exponential backoff (`LLM_MAX_RETRIES`), then fail over |
| other 4xx (bad key, etc.) | Fail over immediately, no retry |
| every provider failed | Back off and run another pass (`LLM_MAX_ROUNDS`). If that also fails, the job is retried later with job-level backoff |

Every attempt is written to `api_usage` (provider, model, tokens, latency, status, error kind).
`/metrics` exposes per-provider counters since boot plus 24h totals from the database.

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # no keys needed: falls back to SQLite + in-memory Redis
uvicorn app.main:app --port 7860
python -m pytest -q             # 34 tests
# same suite against real backends:
TEST_DATABASE_URL=postgresql://... TEST_REDIS_URL=redis://... python -m pytest -q
python -m scripts.probe_providers   # checks each configured provider and writes probe_results.json
```

To rehearse failover without real keys:
`FAKE_GROQ_MODE=429 uvicorn scripts.dev_fake_llm:app --port 58001`, then run the app with
`GROQ_BASE_URL=http://127.0.0.1:58001/groq GEMINI_BASE_URL=http://127.0.0.1:58001/gemini GROQ_API_KEY=x GEMINI_API_KEY=x`.

## Free accounts to create (none ask for a card)

| Service | Where | Value to copy |
|---|---|---|
| Groq | https://console.groq.com/keys | `GROQ_API_KEY` |
| Google AI Studio | https://aistudio.google.com/apikey | `GEMINI_API_KEY` |
| Upstash Redis | https://console.upstash.com → Create database → **TCP** tab | `UPSTASH_REDIS_URL` (`rediss://default:…@….upstash.io:6379`) |
| Neon Postgres | https://console.neon.tech → Connection string | `DATABASE_URL` (`…?sslmode=require`) |
| Hugging Face | https://huggingface.co/new-space → SDK **Docker** (blank) | the Space hosts the app |

Generate the tokens with `python -c "import secrets;print(secrets.token_urlsafe(32))"` and set them as `ADMIN_TOKEN` and `CRON_TOKEN`.

## Deploy: Hugging Face Spaces (primary)

1. Create a **Docker** Space (CPU basic, free). The YAML header at the top of this README configures it (`sdk: docker`, `app_port: 7860`).
2. In the Space's **Settings → Variables and secrets**, add the secrets `GROQ_API_KEY`, `GEMINI_API_KEY`, `DATABASE_URL`,
   `UPSTASH_REDIS_URL`, `ADMIN_TOKEN` and `CRON_TOKEN`, plus the variable `PUBLIC_BASE_URL=https://<user>-<space>.hf.space`.
3. Push this repo to the Space:
   ```bash
   git remote add space https://huggingface.co/spaces/<user>/<space>
   git push space main          # use an HF access token with write scope as the password
   ```
4. Verify it:
   ```bash
   python -m scripts.e2e_smoke --base-url https://<user>-<space>.hf.space --admin-token "$ADMIN_TOKEN" --llm
   ```

Free Spaces go to sleep after about 48h without traffic. The GitHub Actions cron calls the Space every 30 minutes, which keeps it awake.

**Alternative: Render.** `render.yaml` is a ready Blueprint (free plan, Docker). If Render asks for a credit card, stop and use HF Spaces instead.

## 24/7 cron: GitHub Actions

`.github/workflows/scheduled_runner.yml` runs every 30 minutes. On each run it:
1. Calls `/healthz`, retrying for up to 5 minutes so a sleeping host has time to start.
2. Fires `/cron/maintenance`. This marks workers whose heartbeat has stopped as dead and moves their reserved jobs back to the queue, re-enqueues jobs that never reached Redis, re-runs jobs left `running` by a crash, and prunes logs older than `LOG_RETENTION_DAYS`.
3. Every 6 hours, also fires `/cron/provider_probe`.
4. **Fallback:** if the host is unhealthy and the repo variable `RUN_DRAIN_FALLBACK=true` is set, it installs the app and runs `python -m workers.worker --drain` inside the Action, so queued work still gets done.

In the repo's **Settings → Secrets and variables → Actions**, set `PLATFORM_URL` and `CRON_TOKEN`. The fallback also needs `DATABASE_URL`, `UPSTASH_REDIS_URL`, `GROQ_API_KEY` and `GEMINI_API_KEY`.
A 30-minute cadence uses about 1,440 minutes a month, which fits the 2,000 free minutes a private repo gets. Public repos have no minute limit.

`ci.yml` runs the test suite on SQLite, then again on real Postgres 16 and Redis 7, then builds the Docker image.

## Reliability notes

- Postgres is the source of truth for job state; Redis carries only job IDs. Claiming a job is a single atomic `UPDATE … WHERE status IN (queued, retrying)`, so a job ID that is enqueued twice still runs once.
- Each worker reserves jobs into its own processing list (`BLMOVE`). On graceful shutdown, in-flight jobs go straight back to the queue. If a worker crashes, its heartbeat goes stale and maintenance recovers its jobs.
- Upstash free tier has a monthly command quota. Idle cost is about one blocking pop per `WORKER_POLL_TIMEOUT` per loop, plus a few commands per job. Heartbeats go to Postgres, not Redis. If a backend returns from the blocking pop without waiting, the worker sleeps instead of spinning.
- Neon and Supabase suspend idle compute, so the connection pool uses `pool_pre_ping` and recycles connections after 300s.
- Migrations (`db/migrate.py`) are versioned and serialized with a Postgres advisory lock. They run automatically on every boot.
