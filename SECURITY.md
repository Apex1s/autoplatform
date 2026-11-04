# Security

## Reporting
Please report vulnerabilities privately through GitHub's **Security → Report a vulnerability**
(private vulnerability reporting). Do not open a public issue.

## Secrets
No credentials live in this repository. Runtime secrets are set as environment variables
on the host (Render) and as encrypted GitHub Actions secrets. `.env`, `.env.*` (except
`.env.example`), `*.local.txt` and `probe_results.json` are git-ignored.

## Design notes
- `/admin/*` needs `ADMIN_TOKEN`; `/cron/*` needs `CRON_TOKEN`; in production `/metrics`
  needs `METRICS_TOKEN` or `ADMIN_TOKEN` unless `METRICS_PUBLIC=true`. Tokens are compared
  in constant time.
- Client API keys are 256-bit random values, stored only as SHA-256 hashes, and shown once.
- Signed webhooks use HMAC-SHA256 over the raw body (`X-Signature-256` / `X-Hub-Signature-256`).
  Pair them with `Idempotency-Key` (or GitHub's `X-GitHub-Delivery`) to reject replays.
- Request bodies are capped at `MAX_BODY_BYTES` (1 MB default), whether or not a
  Content-Length is sent.
- `http.fetch` allows only `https://` URLs to public IPs. The IP is checked at DNS resolution
  and again at TCP connect time (DNS-rebinding safe) before any byte is sent. Redirects are
  not followed, env proxies are ignored, and response bodies are capped at 512 KB.
- The container runs as a non-root user (uid 1000). CORS is not enabled.
- Job delivery is at least once, so handlers that cause external side effects should be idempotent.

## Rotating credentials
1. Generate the new key in the provider's dashboard (Groq, Google AI Studio, Upstash, Neon).
2. Update it in Render → Environment (and in GitHub Actions secrets if the drain fallback is used).
3. Render redeploys on change. Revoke the old key afterwards.
