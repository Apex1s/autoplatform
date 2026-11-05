"""Send a correctly signed Stripe event to POST /webhook/stripe, without needing a Stripe account.

    python -m scripts.simulate_stripe --url https://<host> --secret whsec_xxx --plan guard_pro --email you@example.com
    python -m scripts.simulate_stripe --url https://<host> --secret whsec_xxx --cancel --customer cus_sim_123

The first form provisions a client and prints the one-time welcome URL. The second deactivates it.
Uses the same signing scheme as Stripe: t=<ts>,v1=hmac_sha256(secret, "<ts>.<body>").
"""
from __future__ import annotations

import argparse
import json
import secrets
import time

import httpx

from core.signatures import sign_stripe


def event(etype: str, obj: dict) -> dict:
    return {"id": f"evt_sim_{secrets.token_hex(8)}", "type": etype, "created": int(time.time()),
            "livemode": False, "data": {"object": obj}}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True, help="deployment base URL")
    p.add_argument("--secret", required=True, help="STRIPE_WEBHOOK_SECRET configured on the deployment")
    p.add_argument("--plan", default="guard_starter")
    p.add_argument("--email", default="founder@example.com")
    p.add_argument("--name", default="Test Customer")
    p.add_argument("--customer", default=f"cus_sim_{secrets.token_hex(4)}")
    p.add_argument("--cancel", action="store_true", help="send customer.subscription.deleted instead")
    a = p.parse_args(argv)
    base = a.url.rstrip("/")
    if a.cancel:
        evt = event("customer.subscription.deleted", {"id": f"sub_sim_{secrets.token_hex(4)}", "customer": a.customer})
    else:
        session_id = f"cs_test_sim{secrets.token_hex(12)}"
        evt = event("checkout.session.completed", {
            "id": session_id, "object": "checkout.session", "mode": "subscription", "payment_status": "paid",
            "customer": a.customer, "amount_total": 0, "currency": "usd", "metadata": {"plan": a.plan},
            "customer_details": {"name": a.name, "email": a.email}})
    raw = json.dumps(evt).encode()
    r = httpx.post(f"{base}/webhook/stripe", content=raw, timeout=30,
                   headers={"Stripe-Signature": sign_stripe(raw, a.secret), "Content-Type": "application/json",
                            "User-Agent": "Stripe/1.0 (simulated by scripts/simulate_stripe.py)"})
    print(f"HTTP {r.status_code}: {r.text}")
    if r.status_code == 200 and not a.cancel and r.json().get("action") == "provisioned":
        print(f"\nOne-time credentials page: {base}/billing/welcome?session_id={evt['data']['object']['id']}")
        print(f"Customer id (for --cancel): {a.customer}")
    return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
