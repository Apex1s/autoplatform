# Security

## Reporting
Please report vulnerabilities privately through GitHub's **Security → Report a vulnerability**
(private vulnerability reporting). Do not open a public issue.

## Secrets
No credentials live in this repository. Runtime secrets are set as environment variables
on the host (Render) and as encrypted GitHub Actions secrets. `.env`, `.env.*` (except
`.env.example`), `*.local.txt` and `probe_results.json` are git-ignored.

## Design notes
- `/admin/*` needs `ADMIN_TOKEN`. `/cron/*` takes a GitHub Actions OIDC token, which is verified
  with RS256 against GitHub's JWKS and pinned to this repo, `main`, the audience, and schedule or
  dispatch events, or else `CRON_TOKEN`. in production `/metrics`
  In production, `/metrics` needs `METRICS_TOKEN` or `ADMIN_TOKEN` unless `METRICS_PUBLIC=true`.
  Static tokens are compared in constant time.
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

## Cloudflare Worker runtime
- The same rules as above: constant-time token comparison, SHA-256-hashed client keys (compatible
  with the Python runtime), HMAC-SHA256 on the raw body, and the request body cap enforced before
  auth when Content-Length is sent and while streaming otherwise.
- `http.fetch`: https only, no credentials in URLs, private/link-local/loopback IP literals and
  internal names refused, `redirect: manual`, and a 512 KB cap. Workers' egress can't reach private
  networks, which covers DNS-rebinding tricks.
- Secrets live only in Cloudflare's encrypted secret store (dashboard → Variables and Secrets).

## Monetization surfaces (v1.2)
- **Outbound deliveries** (`internal.deliver`) are signed with
  `X-AutoPlatform-Signature: sha256=<hmac(client webhook_secret, raw body)>`. Destination URLs must be
  https with no credentials, and private addresses are refused at send time. Redirects are not followed.
- **Inbound signatures per handler:**
  - Stripe uses `t=,v1=` with a 5-minute replay window.
  - Typeform and Shopify use base64 HMAC.
  - GitHub, Meta and our own format use hex HMAC.
  - All comparisons are constant-time, and malformed signatures are rejected before any database lookup.
- **Platform Stripe webhook** (`/webhook/stripe`): signature-verified and idempotent per event id. Provisioned
  credentials are kept readable only until the first reveal (Redis `GETDEL`, 7-day TTL); only the API key hash is stored.
- **Secrets in client config** (`*_secret`, `*_token`, `*_key`) are masked in every API view (`****1234`).
- **The public demo** is rate-limited per IP and per day, runs only on the `demo` client, and never delivers outbound.
  Its simulated LLM outage is labelled as such and never recorded as provider usage.
- **Plans** enforce handler allow-lists and monthly quotas before a job is created.

## Rotating credentials
1. Generate the new key in the provider's dashboard (Groq, Google AI Studio, Upstash, Neon).
2. Update it in Cloudflare → Workers → autoplatform → Settings → Variables and Secrets (and in
   GitHub Actions secrets if the drain fallback is used).
3. Saving a secret redeploys the Worker. Revoke the old key afterwards.
