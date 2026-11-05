"""Monetization routes (mirrored in worker/src/monetization.ts).

  Self-serve      GET/PATCH /me, GET /usage
  Agencies        POST/GET /agency/clients, PATCH/DELETE /agency/clients/{slug}, GET /agency/usage
  Admin           PATCH /admin/clients/{slug}, GET /admin/clients/{slug}/usage, POST /cron/weekly_reports
  Billing         POST /webhook/stripe (auto-provision on payment), GET /billing/welcome (read-once credentials)
  Signed ingest   POST /webhook/{slug}[/{handler}] with Stripe / Typeform / Shopify / HMAC signatures,
                  GET  /webhook/{slug}/{handler} for Meta's hub.challenge handshake
  Demo            GET /demo, POST /demo/run, GET /demo/jobs/{id}
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from core import signatures
from core.plans import PLANS, RESERVED_SLUGS, effective_handlers, effective_limit, plan, plan_for_amount
from db import repo
from models import Client
from workers.delivery import destination_format, render
from workers.handlers import HANDLERS, INTERNAL_PREFIX, SYSTEM_PREFIX, client_job_types
from workers.products import enqueue_weekly_reports, usage_report

log = logging.getLogger("autoplatform.monetization")
DEMO_PAGE = Path(__file__).resolve().parents[1] / "monetization" / "demo" / "index.html"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")


@dataclass
class Hooks:
    settings: Any
    enqueue: Callable[..., Awaitable[dict]]
    admit: Callable[[Client, str], Awaitable[None]]
    base: Callable[[Request | None], str]
    require_admin: Callable
    require_cron: Callable
    require_client: Callable


class ClientPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    plan: str | None = None
    monthly_job_limit: int | None = Field(default=None, ge=0, le=10_000_000)
    allowed_handlers: list[str] | None = None
    outbound_webhook_url: str | None = None
    config: dict[str, Any] | None = None
    contact_email: str | None = Field(default=None, max_length=200)
    active: bool | None = None


class SubClientIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,39}$")
    name: str = Field(min_length=1, max_length=200)
    monthly_job_limit: int | None = Field(default=None, ge=1, le=10_000_000)
    allowed_handlers: list[str] | None = None
    outbound_webhook_url: str | None = None
    config: dict[str, Any] | None = None
    contact_email: str | None = Field(default=None, max_length=200)


class DemoRun(BaseModel):
    scenario: str = Field(pattern=r"^(lead|order|churn|ticket)$")
    payload: dict[str, Any] | None = None
    simulate_outage: str | None = Field(default=None, pattern=r"^(groq|gemini)$")


# ------------------------------------------------------------------ helpers
def check_https(url: str | None) -> str | None:
    if url in (None, ""):
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise HTTPException(422, "outbound URLs must be https:// without credentials")
    return url


def check_handlers(names: list[str] | None) -> list[str] | None:
    if names is None:
        return None
    allowed = set(client_job_types())
    bad = [n for n in names if n not in allowed]
    if bad:
        raise HTTPException(422, f"unknown handlers {bad}; available: {sorted(allowed)}")
    return names


def credentials(c: Client, api_key: str | None, base: str) -> dict[str, Any]:
    out = {"id": c.id, "slug": c.slug, "name": c.name, "plan": c.plan,
           "monthly_job_limit": effective_limit(c.plan, c.monthly_job_limit),
           "allowed_handlers": effective_handlers(c.plan, c.allowed_handlers),
           "webhook_url": f"{base}/webhook", "signed_webhook_url": f"{base}/webhook/{c.slug}",
           "handler_urls": {h: f"{base}/webhook/{c.slug}/{h}"
                            for h in (effective_handlers(c.plan, c.allowed_handlers) or client_job_types())}}
    if api_key:
        out.update({"api_key": api_key, "webhook_secret": c.webhook_secret,
                    "note": "api_key and webhook_secret are shown once; store them now."})
    return out


def slugify(text: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:30].strip("-")
    return base if len(base) >= 3 else f"client{base}"


async def notify_operator(settings, text: str) -> None:
    url = settings.operator_notify_url
    if not url:
        return
    fmt = destination_format(url)
    body = render(fmt, {"event": "operator.notice", "text": text}, text)
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            await http.post(url, json=body)
    except httpx.HTTPError:
        log.warning("operator notification failed")


# ---------------------------------------------------------------- demo data
DEMO_CONFIG: dict[str, Any] = {
    "business_name": "Atlas Living Real Estate",
    "business_description": "Boutique agency selling apartments and villas in Casablanca and Rabat (1M-8M MAD).",
    "tone": "warm, concise, professional", "booking_url": "https://cal.com/atlas-living/visit",
    "weekly_report": False,
    "lead": {"ideal_customer": "Buyers with budget above 1.5M MAD planning to buy within 3 months",
             "hot_threshold": 70, "warm_threshold": 40},
    "order": {"business_name": "Casa Pizza", "currency": "MAD", "delivery_fee": 15, "min_order": 60,
              "catalog": [
                  {"sku": "PIZ-MAR", "name": "Margherita pizza", "price": 55, "aliases": ["margherita", "marguerite"]},
                  {"sku": "PIZ-4F", "name": "Quattro formaggi pizza", "price": 75,
                   "aliases": ["4 fromages", "quatre fromages", "4 cheese", "4 formaggi"]},
                  {"sku": "BUR-CL", "name": "Classic burger", "price": 50, "aliases": ["burger"]},
                  {"sku": "FRI-L", "name": "Large fries", "price": 20, "aliases": ["frites", "fries"]},
                  {"sku": "COK-33", "name": "Coca-Cola 33cl", "price": 10, "aliases": ["coca", "coke"]},
                  {"sku": "TIR", "name": "Tiramisu", "price": 30, "aliases": ["tiramisu"]}]},
    "churn": {"product_name": "Pulse Analytics", "founder_name": "Sami", "max_discount_percent": 30,
              "allowed_offers": ["discount", "pause", "downgrade"],
              "billing_portal_url": "https://billing.pulse-analytics.example/portal"},
    "support": {"business_name": "Lumen Commerce (e-commerce platform)",
                "routing": {"billing": "finance-team", "technical": "tier-2-engineering",
                            "account": "customer-success", "sales": "sales-team", "default": "support-desk"},
                "sla_minutes": {"critical": 15, "high": 60, "normal": 240, "low": 1440}},
}


def demo_samples() -> dict[str, tuple[str, dict]]:
    now = int(time.time())
    return {
        "lead": ("lead_qualifier", {
            "source": "meta_lead_ads", "form": "Apartment & villa enquiries", "full_name": "Sara El Amrani",
            "phone_number": "+212 6 61 23 45 67", "email": "sara.elamrani@example.com", "city": "Casablanca",
            "what_are_you_looking_for": "3-bedroom apartment near Anfa, ideally with parking",
            "budget": "2.5M - 3M MAD", "when_do_you_plan_to_buy": "This month, mortgage pre-approved"}),
        "order": ("whatsapp_order_triage", {
            "from": "+212 6 70 11 22 33", "name": "Youssef",
            "message": "salam, bghit 2 margherita w 1 4 fromages, 2 coca, livraison l Maarif rue 12 imm 4 appt 7 "
                       "vers 20h svp"}),
        "churn": ("stripe_churn_rescuer", {
            "id": "evt_demo", "type": "customer.subscription.deleted", "created": now,
            "data": {"object": {
                "id": "sub_demo", "customer": "cus_demo", "start_date": now - 200 * 86400, "canceled_at": now,
                "cancellation_details": {"reason": "cancellation_requested", "feedback": "too_expensive",
                                         "comment": "We only open the dashboards once a month now"},
                "items": {"data": [{"price": {"nickname": "Growth", "unit_amount": 14900, "currency": "usd",
                                              "recurring": {"interval": "month"}}}]},
                "metadata": {"email": "lina@northwind.example", "name": "Lina", "seats_used": "3 of 10",
                             "last_login_days_ago": "19"}}}}),
        "ticket": ("support_ticket_router", {
            "channel": "crisp", "from": {"name": "Marc Dubois", "email": "marc@boutique-lumen.example"},
            "subject": "Paiement bloqué !!",
            "message": "Bonjour, depuis ce matin aucun client ne peut payer sur notre boutique, le checkout "
                       "affiche une erreur 500. C'est urgent, on perd des ventes."}),
    }


def install(app: FastAPI, h: Hooks) -> None:
    settings = h.settings
    queue = lambda: app.state.queue  # noqa: E731
    state: dict[str, str] = {}

    async def require_agency(client: Client = Depends(h.require_client)) -> Client:
        if client.plan != "agency":
            raise HTTPException(403, "this endpoint requires the agency plan")
        return client

    # ------------------------------------------------------------- self-serve
    @app.get("/me")
    async def me(request: Request, client: Client = Depends(h.require_client)):
        month = await queue().quota_get(client.id)
        return {**repo.client_view(client, month_jobs=month), **credentials(client, None, h.base(request))}

    @app.patch("/me")
    async def patch_me(body: ClientPatch, client: Client = Depends(h.require_client)):
        fields: dict[str, Any] = {}
        if body.outbound_webhook_url is not None:
            fields["outbound_webhook_url"] = check_https(body.outbound_webhook_url)
        if body.config is not None:
            if body.config.get("notify_url"):
                check_https(body.config["notify_url"])
            fields["config"] = body.config
        if body.contact_email is not None:
            fields["contact_email"] = body.contact_email
        c = await asyncio.to_thread(repo.update_client, client.id, fields)
        return repo.client_view(c)

    @app.get("/usage")
    async def my_usage(days: int = 7, client: Client = Depends(h.require_client)):
        return await usage_report(client, max(1, min(days, 90)))

    # --------------------------------------------------------------- agencies
    @app.post("/agency/clients", status_code=201)
    async def agency_create(body: SubClientIn, request: Request, agency: Client = Depends(require_agency)):
        subs = await asyncio.to_thread(repo.sub_clients, agency.id)
        cap = int((agency.config or {}).get("max_sub_clients") or plan(agency.plan).get("max_sub_clients", 25))
        if sum(1 for s in subs if s.active) >= cap:
            raise HTTPException(403, f"sub-client cap of {cap} reached")
        slug = body.slug if body.slug.startswith(f"{agency.slug}-") else f"{agency.slug}-{body.slug}"
        if not SLUG_RE.match(slug):
            raise HTTPException(422, f"slug '{slug}' is invalid or longer than 40 characters")
        if await asyncio.to_thread(repo.client_by_slug, slug):
            raise HTTPException(409, "slug already exists")
        sub_cap = int(plan(agency.plan).get("sub_client_limit", 5000))
        c, key = await asyncio.to_thread(
            repo.create_client, slug, body.name, plan="agency_client",
            monthly_job_limit=min(body.monthly_job_limit or sub_cap, sub_cap),
            allowed_handlers=check_handlers(body.allowed_handlers),
            outbound_webhook_url=check_https(body.outbound_webhook_url), config=body.config or {},
            agency_id=agency.id, contact_email=body.contact_email)
        return credentials(c, key, h.base(request))

    @app.get("/agency/clients")
    async def agency_list(agency: Client = Depends(require_agency)):
        subs = await asyncio.to_thread(repo.sub_clients, agency.id)
        counts = await asyncio.to_thread(repo.month_job_counts, [s.id for s in subs])
        return [repo.client_view(s, month_jobs=counts.get(s.id, 0)) for s in subs]

    async def own_sub(agency: Client, slug: str) -> Client:
        sub = next((s for s in await asyncio.to_thread(repo.sub_clients, agency.id) if s.slug == slug), None)
        if sub is None:
            raise HTTPException(404, "sub-client not found")
        return sub

    @app.patch("/agency/clients/{slug}")
    async def agency_patch(slug: str, body: ClientPatch, agency: Client = Depends(require_agency)):
        sub = await own_sub(agency, slug)
        sub_cap = int(plan(agency.plan).get("sub_client_limit", 5000))
        fields: dict[str, Any] = {}
        if body.name is not None:
            fields["name"] = body.name
        if body.monthly_job_limit is not None:
            fields["monthly_job_limit"] = min(body.monthly_job_limit, sub_cap)
        if body.allowed_handlers is not None:
            fields["allowed_handlers"] = check_handlers(body.allowed_handlers)
        if body.outbound_webhook_url is not None:
            fields["outbound_webhook_url"] = check_https(body.outbound_webhook_url)
        if body.config is not None:
            fields["config"] = body.config
        if body.active is not None:
            fields["active"] = body.active
        return repo.client_view(await asyncio.to_thread(repo.update_client, sub.id, fields))

    @app.delete("/agency/clients/{slug}")
    async def agency_delete(slug: str, agency: Client = Depends(require_agency)):
        sub = await own_sub(agency, slug)
        await asyncio.to_thread(repo.update_client, sub.id, {"active": False})
        return {"slug": slug, "active": False}

    @app.get("/agency/usage")
    async def agency_usage(days: int = 30, agency: Client = Depends(require_agency)):
        days = max(1, min(days, 90))
        subs = await asyncio.to_thread(repo.sub_clients, agency.id)
        reports = [await usage_report(s, days) for s in subs]
        keep = ("client", "client_name", "webhooks_processed", "success_rate", "time_to_result_ms", "llm",
                "deliveries", "highlights", "roi")
        rows = [{k: r[k] for k in keep} for r in reports]
        return {"agency": agency.slug, "days": days, "clients": rows,
                "totals": {"webhooks_processed": sum(r["webhooks_processed"] for r in rows),
                           "failover_saves": sum(r["llm"]["failover_saves"] for r in rows),
                           "llm_tokens": sum(r["llm"]["tokens"] for r in rows),
                           "hours_saved": round(sum(r["roi"]["hours_saved"] for r in rows), 1)}}

    # ------------------------------------------------------------------ admin
    @app.patch("/admin/clients/{slug}", dependencies=[Depends(h.require_admin)])
    async def admin_patch(slug: str, body: ClientPatch):
        c = await asyncio.to_thread(repo.client_by_slug, slug)
        if c is None:
            raise HTTPException(404, "client not found")
        fields = body.model_dump(exclude_unset=True)
        if "plan" in fields and fields["plan"] not in PLANS:
            raise HTTPException(422, f"unknown plan; available: {sorted(PLANS)}")
        if "outbound_webhook_url" in fields:
            fields["outbound_webhook_url"] = check_https(fields["outbound_webhook_url"])
        if "allowed_handlers" in fields:
            check_handlers(fields["allowed_handlers"])
        return repo.client_view(await asyncio.to_thread(repo.update_client, c.id, fields))

    @app.get("/admin/clients/{slug}/usage", dependencies=[Depends(h.require_admin)])
    async def admin_usage(slug: str, days: int = 7):
        c = await asyncio.to_thread(repo.client_by_slug, slug)
        if c is None:
            raise HTTPException(404, "client not found")
        return await usage_report(c, max(1, min(days, 90)))

    @app.post("/cron/weekly_reports", status_code=202, dependencies=[Depends(h.require_cron)])
    async def weekly_reports():
        ids = await enqueue_weekly_reports(queue())
        return {"enqueued": len(ids), "job_ids": ids}

    # ---------------------------------------------- billing: Stripe dogfooding
    async def provision_from_checkout(session: dict, request: Request) -> dict:
        if session.get("payment_status") not in ("paid", "no_payment_required"):
            return {"action": "ignored", "reason": "payment not completed yet"}
        meta = session.get("metadata") or {}
        plan_name = (meta.get("plan") or settings.stripe_plan_by_link.get(session.get("payment_link") or "")
                     or plan_for_amount(session.get("amount_total"), session.get("currency")))
        review = plan_name not in PLANS or plan_name in ("demo", "agency_client")
        if review:
            plan_name = "guard_starter"
        details = session.get("customer_details") or {}
        email = details.get("email") or session.get("customer_email")
        name = (details.get("name") or (email.split("@")[0] if email else "") or "New customer")[:200]
        customer = session.get("customer")
        existing = await asyncio.to_thread(repo.client_by_stripe_customer, customer) if customer else None
        base = h.base(request)
        if existing:
            c = await asyncio.to_thread(repo.update_client, existing.id, {"plan": plan_name, "active": True})
            creds: dict[str, Any] = {**credentials(c, None, base),
                                     "note": "Your account was reactivated: keep using your existing API key."}
            action = "reactivated"
        else:
            slug = ""
            for _ in range(5):
                candidate = f"{slugify(name if details.get('name') else (email or name))}-{secrets.token_hex(2)}"
                if candidate not in RESERVED_SLUGS and not await asyncio.to_thread(repo.client_by_slug, candidate):
                    slug = candidate
                    break
            if not slug:
                raise HTTPException(500, "could not allocate a slug")
            c, key = await asyncio.to_thread(
                repo.create_client, slug, name, plan=plan_name, contact_email=email, stripe_customer_id=customer,
                config={"onboarding": {"stripe_checkout_session": session.get("id"), "needs_review": review}})
            creds = credentials(c, key, base)
            action = "provisioned"
        await queue().put_secret(f"welcome:{session.get('id')}", json.dumps(creds), ttl=7 * 86400)
        await notify_operator(settings, f"💰 {action}: {name} <{email}> on {plan_name} -> client {c.slug}"
                                        + (" ⚠️ plan not recognised, provisioned as guard_starter: review" if review else ""))
        return {"action": action, "client": c.slug, "plan": plan_name, "needs_review": review}

    @app.post("/webhook/stripe")
    async def platform_stripe(request: Request):
        if not settings.stripe_webhook_secret:
            raise HTTPException(503, "STRIPE_WEBHOOK_SECRET not configured")
        raw = await request.body()
        if not signatures.verify_stripe(request.headers.get("stripe-signature", ""), raw, settings.stripe_webhook_secret):
            raise HTTPException(400, "invalid Stripe signature")
        evt = json.loads(raw)
        key = f"stripe-platform:{evt.get('id')}"
        if await queue().claim_idempotency(key, "1", ttl=7 * 86400) is not None:
            return {"received": True, "duplicate": True}
        try:
            etype, obj = evt.get("type"), ((evt.get("data") or {}).get("object") or {})
            if etype == "checkout.session.completed":
                outcome = await provision_from_checkout(obj, request)
            elif etype == "customer.subscription.deleted":
                c = await asyncio.to_thread(repo.client_by_stripe_customer, obj.get("customer") or "")
                if c:
                    await asyncio.to_thread(repo.update_client, c.id, {"active": False})
                    await notify_operator(settings, f"👋 Subscription ended: client {c.slug} deactivated")
                    outcome = {"action": "deactivated", "client": c.slug}
                else:
                    outcome = {"action": "ignored", "reason": "unknown customer"}
            elif etype == "invoice.payment_failed":
                await notify_operator(settings, f"⚠️ Payment failed for {obj.get('customer_email') or obj.get('customer')}")
                outcome = {"action": "notified"}
            else:
                outcome = {"action": "ignored", "reason": f"unhandled event {etype}"}
        except Exception:
            await queue().release_idempotency(key)  # let Stripe's retry run it again
            raise
        return {"received": True, **outcome}

    @app.get("/billing/welcome", response_class=HTMLResponse)
    async def welcome(session_id: str = ""):
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if not re.fullmatch(r"cs_(test|live)_[A-Za-z0-9]{8,200}", session_id):
            return HTMLResponse(welcome_page("Invalid link", "<p>This onboarding link is not valid.</p>"), 400, headers)
        raw = await queue().take_secret(f"welcome:{session_id}")
        if raw is None:
            if await queue().peek_secret(f"welcome-shown:{session_id}"):
                body = ("<p>Your credentials were already shown once and are not stored in readable form. "
                        "Contact support to rotate your key.</p>")
                return HTMLResponse(welcome_page("Already revealed", body), 410, headers)
            body = "<p>Setting up your account... this page refreshes automatically.</p>"
            return HTMLResponse(welcome_page("Almost there", body, refresh=True), 202, headers)
        await queue().put_secret(f"welcome-shown:{session_id}", "1", ttl=30 * 86400)
        creds = json.loads(raw)
        return HTMLResponse(welcome_page("You're live", credentials_html(creds)), 200, headers)

    # --------------------------------------------- signed third-party ingest
    async def signed(client_slug: str, handler_name: str | None, request: Request) -> dict:
        raw = await request.body()
        scheme = signatures.detect_scheme(request.headers)
        if scheme is None or not signatures.looks_valid(scheme, request.headers):
            raise HTTPException(401, "invalid signature")
        client = await asyncio.to_thread(repo.client_by_slug, client_slug)
        if client is None:
            raise HTTPException(401, "invalid signature")
        cfg = client.config or {}
        candidates = {"stripe": [cfg.get("stripe_webhook_secret")],
                      "shopify": [cfg.get("shopify_webhook_secret"), client.webhook_secret],
                      "typeform": [cfg.get("typeform_secret"), client.webhook_secret],
                      "hmac": [client.webhook_secret, cfg.get("meta_app_secret")]}[scheme]
        if not any(signatures.verify(scheme, request.headers, raw, s) for s in candidates if s):
            raise HTTPException(401, "invalid signature")
        name = handler_name or cfg.get("default_handler") or "webhook.event"
        if name not in HANDLERS or name.startswith((SYSTEM_PREFIX, INTERNAL_PREFIX)):
            raise HTTPException(404, f"unknown handler; available: {client_job_types()}")
        await h.admit(client, name)
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"raw": raw.decode(errors="replace")[:10000]}
        hd = request.headers
        event = (hd.get("x-event-type") or hd.get("x-github-event") or hd.get("x-shopify-topic")
                 or (body.get("type") if isinstance(body, dict) else None))
        idem = (hd.get("idempotency-key") or hd.get("x-github-delivery") or hd.get("x-shopify-webhook-id")
                or (body.get("id") if scheme == "stripe" and isinstance(body, dict) else None))
        out = await h.enqueue(name, {"event": event, "body": body, "source": scheme}, client_id=client.id,
                              source="signed_webhook", request=request, idempotency_key=idem)
        if out.get("duplicate"):
            await queue().quota_decr(client.id)
        return out

    @app.post("/webhook/{client_slug}", status_code=202)
    async def signed_default(client_slug: str, request: Request):
        return await signed(client_slug, None, request)

    @app.post("/webhook/{client_slug}/{handler_name}", status_code=202)
    async def signed_handler(client_slug: str, handler_name: str, request: Request):
        return await signed(client_slug, handler_name, request)

    @app.get("/webhook/{client_slug}/{handler_name}")
    async def meta_verify(client_slug: str, handler_name: str, request: Request):
        q = request.query_params
        client = await asyncio.to_thread(repo.client_by_slug, client_slug)
        token = ((client.config or {}).get("meta_verify_token") if client else None)
        if (q.get("hub.mode") == "subscribe" and token and q.get("hub.verify_token")
                and secrets.compare_digest(q["hub.verify_token"], token)):
            return PlainTextResponse(q.get("hub.challenge", ""))
        raise HTTPException(403, "verification failed")

    # ------------------------------------------------------------------- demo
    async def demo_client() -> Client:
        if "id" in state:
            c = await asyncio.to_thread(repo.client_by_id, state["id"])
            if c and c.active:
                return c
        c = await asyncio.to_thread(repo.client_by_slug, "demo")
        if c is None:
            c, _ = await asyncio.to_thread(repo.create_client, "demo", "Live demo", plan="demo", config=DEMO_CONFIG)
        state["id"] = c.id
        return c

    @app.middleware("http")
    async def demo_cors(request: Request, call_next):
        if not request.url.path.startswith("/demo"):
            return await call_next(request)
        if request.method == "OPTIONS":
            response: Response = Response(status_code=204)
        else:
            response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "content-type"
        response.headers["Access-Control-Expose-Headers"] = "server-timing"
        return response

    @app.get("/demo", include_in_schema=False)
    async def demo_page():
        return FileResponse(DEMO_PAGE, media_type="text/html")

    @app.post("/demo/run", status_code=202)
    async def demo_run(body: DemoRun, request: Request):
        t0 = time.perf_counter()
        if not settings.demo_enabled:
            raise HTTPException(404, "demo disabled")
        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")
        q = queue()
        if await q.over_rate(f"demo:ip:{ip}", 3600, settings.demo_ip_hourly_limit):
            raise HTTPException(429, "demo limit reached for your network; try again in an hour")
        if await q.over_rate(f"demo:day:{time.strftime('%Y%m%d', time.gmtime())}", 86400, settings.demo_daily_limit):
            raise HTTPException(429, "the public demo hit today's cap; book a call for a private run")
        handler_name, sample = demo_samples()[body.scenario]
        payload = {"body": body.payload or sample, "_demo": True, "_simulate_outage": body.simulate_outage}
        if len(json.dumps(payload)) > 8000:
            raise HTTPException(413, "demo payload too large")
        c = await demo_client()
        out = await h.enqueue(handler_name, payload, client_id=c.id, source="demo", request=request, max_attempts=2)
        ms = (time.perf_counter() - t0) * 1000
        return JSONResponse({**out, "scenario": body.scenario, "handler": handler_name, "ingest_ms": round(ms, 1),
                             "status_url": f"/demo/jobs/{out['job_id']}"}, status_code=202,
                            headers={"Server-Timing": f"ingest;dur={ms:.1f}"})

    @app.get("/demo/jobs/{job_id}")
    async def demo_job(job_id: str):
        c = await demo_client()
        job = await asyncio.to_thread(repo.get_job, job_id, c.id)
        if job is None:
            raise HTTPException(404, "job not found (yet)")
        job["payload"] = (job.get("payload") or {}).get("body")
        return job


def welcome_page(title: str, body: str, refresh: bool = False) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{'<meta http-equiv="refresh" content="3">' if refresh else ''}
<title>{html.escape(title)} - AutoPlatform</title>
<style>body{{margin:0;background:#141414;color:#f2f2f2;font:16px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:720px;margin:0 auto;padding:48px 20px}}h1{{font-size:28px;margin:0 0 8px}}
code,pre{{font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}}pre{{background:#1d1d1d;border:1px solid #2a2a2a;
border-radius:8px;padding:14px;overflow-x:auto;white-space:pre-wrap;word-break:break-all}}
.k{{color:#9a9a9a;font-size:13px;text-transform:uppercase;letter-spacing:.06em;margin:22px 0 6px}}
.warn{{background:#2a2110;border:1px solid #5c4413;color:#ffd68a;padding:12px 14px;border-radius:8px}}</style></head>
<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"""


def credentials_html(c: dict) -> str:
    esc = lambda v: html.escape(str(v))  # noqa: E731
    parts = []
    if c.get("api_key"):
        parts.append('<p class="warn">Shown once. Copy these into your password manager now; '
                     "we only keep a hash of the API key.</p>")
        parts.append(f'<div class="k">API key (header X-API-Key)</div><pre>{esc(c["api_key"])}</pre>')
        parts.append(f'<div class="k">Webhook signing secret</div><pre>{esc(c["webhook_secret"])}</pre>')
    elif c.get("note"):
        parts.append(f"<p>{esc(c['note'])}</p>")
    parts.append(f'<div class="k">Plan</div><pre>{esc(c.get("plan"))} · {esc(c.get("monthly_job_limit"))} jobs/month</pre>')
    parts.append(f'<div class="k">Send jobs</div><pre>POST {esc(c.get("webhook_url"))}</pre>')
    for name, url in (c.get("handler_urls") or {}).items():
        parts.append(f'<div class="k">{esc(name)} (signed webhooks)</div><pre>{esc(url)}</pre>')
    example = (f'curl -X POST {c.get("webhook_url")} -H "X-API-Key: <your key>" -H "Content-Type: application/json" '
               "-d '{\"type\":\"support_ticket_router\",\"payload\":{\"message\":\"Hi, I was charged twice\"}}'")
    parts.append(f'<div class="k">Try it</div><pre>{esc(example)}</pre>')
    return "\n".join(parts)
