"""Commercial plans: job quotas and which handlers each tier may run (mirrored in worker/src/plans.ts)."""
from __future__ import annotations

from typing import Any

PRODUCT_HANDLERS = ["lead_qualifier", "whatsapp_order_triage", "stripe_churn_rescuer", "support_ticket_router"]

PLANS: dict[str, dict[str, Any]] = {
    # Stream 1: done-for-you Speed-to-Lead & Ops retainer
    "retainer": {"label": "Speed-to-Lead & Ops Retainer", "price_usd": 1000, "monthly_job_limit": 20000,
                 "handlers": None},
    # Stream 2: white-label engine for agencies (provisions its own sub-clients)
    "agency": {"label": "White-Label Agency Engine", "price_usd": 500, "monthly_job_limit": 50000,
               "handlers": None, "max_sub_clients": 25, "sub_client_limit": 5000},
    "agency_client": {"label": "Agency sub-client", "price_usd": 0, "monthly_job_limit": 5000, "handlers": None},
    # Stream 3: self-serve AI Webhook Guard & Router
    "guard_starter": {"label": "AI Webhook Guard - Starter", "price_usd": 99, "monthly_job_limit": 2000,
                      "handlers": ["support_ticket_router", "stripe_churn_rescuer", "webhook.event", "usage_reporter"]},
    "guard_pro": {"label": "AI Webhook Guard - Pro", "price_usd": 249, "monthly_job_limit": 10000,
                  "handlers": ["support_ticket_router", "stripe_churn_rescuer", "lead_qualifier", "webhook.event",
                               "http.fetch", "usage_reporter"]},
    # public interactive demo (rate-limited, never delivers outbound)
    "demo": {"label": "Live demo", "price_usd": 0, "monthly_job_limit": 3000, "handlers": PRODUCT_HANDLERS},
}

DEFAULT_PLAN = "retainer"
RESERVED_SLUGS = {"stripe", "demo", "demo-admin", "admin", "api", "www", "billing", "agency"}


def plan(name: str | None) -> dict[str, Any]:
    return PLANS.get(name or DEFAULT_PLAN, PLANS[DEFAULT_PLAN])


def effective_limit(client_plan: str | None, override: int | None) -> int | None:
    return override if override is not None else plan(client_plan).get("monthly_job_limit")


def effective_handlers(client_plan: str | None, override: list[str] | None) -> list[str] | None:
    """None means every client-callable handler."""
    return override if override is not None else plan(client_plan).get("handlers")


def plan_for_amount(amount_cents: int | None, currency: str | None) -> str | None:
    """Fallback mapping for Stripe Checkout when no metadata/link mapping is configured."""
    if amount_cents is None or (currency or "usd").lower() != "usd":
        return None
    for name, p in PLANS.items():
        if p["price_usd"] and p["price_usd"] * 100 == amount_cents:
            return name
    return None
