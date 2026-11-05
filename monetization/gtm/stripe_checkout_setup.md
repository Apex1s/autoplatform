# Stripe Checkout: self-serve auto-provisioning (dogfooding)

When a customer pays, Stripe calls `POST /webhook/stripe` on your deployment. AutoPlatform verifies the Stripe signature, creates the client on the right plan, and stores its credentials for **one** reveal. The customer then lands on `/billing/welcome?session_id=…` and sees their API key, signing secret and endpoints exactly once. When the subscription ends, the client is deactivated automatically.

This is implemented and tested in both runtimes:
- Python: `app/monetization.py`, tested in `tests/test_products.py::test_stripe_checkout_*`
- Worker: `worker/src/monetization.ts`, tested in `worker/test/products.test.ts`

## 0. Can you use Stripe?

Stripe only onboards businesses registered in [its supported countries](https://stripe.com/global). **Morocco is not on that list**: a Morocco-registered company needs an entity in a supported country ([Kolonell, Oct 2026](https://kolonell.com/en/blog/payment-gateway-morocco-cmi-vs-stripe-sme-2026); [Dodo Payments](https://dodopayments.com/blogs/stripe-supported-countries-alternatives)). Options:

- **A company in a supported country** (EU or US LLC). This has setup costs, so do it only once revenue justifies it.
- **A merchant of record** that pays out to Morocco, e.g. [Polar](https://polar.sh/docs/merchant-of-record/supported-countries), which pays out via Stripe Connect Express. Its webhook format differs from Stripe's: `/webhook/stripe` would need a small adapter (not implemented; about a day of work).
- **Until then:** invoice retainer clients manually (bank transfer) and provision them with `scripts/provision_client.py`. Self-serve checkout matters most for the $99/$249 Guard plans.

Everything below assumes a Stripe account (test mode works anywhere for development).

## 1. Products and prices

In **Product catalog → Add product**:

| Product | Price | Plan key |
|---|---|---|
| AI Webhook Guard Starter | $99 / month, recurring | `guard_starter` |
| AI Webhook Guard Pro | $249 / month, recurring | `guard_pro` |
| White-Label Agency Engine | $500 / month, recurring | `agency` |
| Speed-to-Lead Retainer | $1,000 / month, recurring, + one-time $1,500 "Setup" price on the same link | `retainer` |

## 2. One Payment Link per plan

**Payment Links → New** for each product, then:

1. **Metadata:** add `plan` = the plan key above (e.g. `plan` → `guard_pro`). Metadata on a Payment Link is copied to every Checkout Session it creates, and that's what AutoPlatform reads first.
2. **After payment → Don't show confirmation page → redirect to:**
   `https://autoplatform.data-marketplace-edge.workers.dev/billing/welcome?session_id={CHECKOUT_SESSION_ID}`
   Stripe replaces `{CHECKOUT_SESSION_ID}` with the real id (`cs_live_…`).
3. Collect the customer's name and email; tax ID collection is optional.

How the plan is resolved (first match wins), as implemented:
1. `session.metadata.plan`
2. the `STRIPE_PLAN_BY_LINK` secret, a JSON object mapping Payment Link id to plan, e.g. `{"plink_1Q…":"guard_pro"}`
3. the amount: $99 → `guard_starter`, $249 → `guard_pro`, $500 → `agency`, $1,000 → `retainer`
4. otherwise the client is provisioned as `guard_starter` with `needs_review: true`, and you're notified

## 3. Webhook endpoint

**Developers → Webhooks → Add endpoint**:

- **URL:** `https://autoplatform.data-marketplace-edge.workers.dev/webhook/stripe`
- **Events:**
  - `checkout.session.completed` provisions the client, or reactivates it for a returning customer.
  - `customer.subscription.deleted` deactivates it; the API key stops working.
  - `invoice.payment_failed` notifies you; Stripe's own retries and dunning continue.
- Copy the **signing secret** (`whsec_…`).

Then in **Cloudflare → Workers → autoplatform → Settings → Variables and Secrets**, add these as **Secrets**:

| Name | Value |
|---|---|
| `STRIPE_WEBHOOK_SECRET` | the `whsec_…` from Stripe (one per mode: test and live have different secrets) |
| `OPERATOR_NOTIFY_URL` | *(optional)* your Slack or Discord webhook URL, for "💰 new customer" and "⚠️ payment failed" alerts |
| `STRIPE_PLAN_BY_LINK` | *(optional)* the JSON mapping from step 2 |

Saving a secret redeploys the Worker. Until `STRIPE_WEBHOOK_SECRET` is set, the endpoint answers `503`.

## 4. Test it

**Without Stripe** (signed exactly like Stripe):
```bash
python -m scripts.simulate_stripe --url https://autoplatform.data-marketplace-edge.workers.dev --secret "$STRIPE_WEBHOOK_SECRET" --plan guard_pro --email you@example.com
```
It prints the one-time welcome URL. Open it once, and opening it again shows "Already revealed". To deactivate the test client again, rerun the script with `--cancel --customer <id printed above>`.

**With Stripe test mode:** make a test purchase through your test Payment Link with card `4242 4242 4242 4242`, or run `stripe trigger checkout.session.completed` from the Stripe CLI. Then check:
```bash
curl -s https://autoplatform.data-marketplace-edge.workers.dev/admin/clients -H "Authorization: Bearer $ADMIN_TOKEN"
```
The new client appears with its plan and `contact_email`. In Stripe → Webhooks → your endpoint, the event shows `200` and a body like `{"received":true,"action":"provisioned",…}`.

## 5. Behaviour you can rely on

- **Signature:** a missing, invalid or older-than-5-minutes `Stripe-Signature` gets a `400`, and nothing is created.
- **Idempotent:** Stripe retries are recognised by event id for 7 days and answered `{"duplicate": true}`. If provisioning throws, the idempotency key is released so Stripe's retry runs it again.
- **Returning customers:** the same `stripe_customer_id` reactivates the existing client and updates its plan, without generating new keys.
- **Credentials:**
  - Only a SHA-256 hash of the API key is stored.
  - The readable copy for the welcome page lives in Redis for at most 7 days and is deleted the moment it's shown (`GETDEL`).
  - The page sends `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.
- **Before the webhook arrives:** if the redirect beats the webhook, the welcome page shows "Almost there…" and refreshes every 3 seconds.

## 6. After signup (self-serve customers)

The welcome page gives the customer their key. They configure everything through the API:
```bash
curl -X PATCH https://autoplatform.data-marketplace-edge.workers.dev/me -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{"config":{"notify_url":"https://hooks.slack.com/services/…","stripe_webhook_secret":"whsec_their_own"},"outbound_webhook_url":"https://their-crm.example/hooks/autoplatform"}'
```
- Their own Stripe churn events go to `https://…/webhook/<slug>/stripe_churn_rescuer`.
- Support messages go to `/webhook/<slug>/support_ticket_router`.
- `GET /usage?days=7` returns the same numbers as the Monday report.

**Dogfood the churn rescuer on yourself:**
1. Create a client for your own business (`provision_client.py --slug me --plan guard_pro …`).
2. Set `stripe_webhook_secret` on it.
3. Add a *second* Stripe endpoint pointing at `/webhook/me/stripe_churn_rescuer` for `customer.subscription.deleted` and `invoice.payment_failed`.

Every churning customer of yours then triggers a founder-voice win-back draft in your Slack.

## 7. Customer portal

Enable **Settings → Billing → Customer portal** (cancel at period end, update payment method). A cancellation becomes `customer.subscription.deleted` when the period ends, which deactivates the client. Add the portal link to your client config as `churn.billing_portal_url`, so payment-failure emails include it.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `503 STRIPE_WEBHOOK_SECRET not configured` | Secret not set on the Worker, or set as a plain variable instead of a Secret |
| `400 invalid Stripe signature` | Wrong secret (test vs live), or a proxy re-encoded the body |
| `{"action":"ignored","reason":"payment not completed yet"}` | Asynchronous payment methods: also subscribe to `checkout.session.async_payment_succeeded` and re-send |
| Client provisioned as `guard_starter` with `needs_review` | No `plan` metadata, no link mapping, and an amount that doesn't match a plan; fix it with `PATCH /admin/clients/<slug>` |
| Calls from Python's built-in `urllib` get `403` | Cloudflare's edge bot filter blocks that user agent; use `requests` or `httpx`, or set a User-Agent header |
