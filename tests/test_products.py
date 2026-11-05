"""Revenue handlers, delivery, plans/quotas, agencies, Stripe dogfooding, demo (Python runtime)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from core import signatures
from tests.test_api_e2e import ADMIN, wait_job
from workers import delivery
from workers.llm_json import SchemaError, coerce, parse_json_object


def llm_json(obj, model="m"):
    return (200, {}, {"model": model, "usage": {"prompt_tokens": 50, "completion_tokens": 40},
                      "choices": [{"message": {"role": "assistant", "content": json.dumps(obj)}}]})


LEAD_REPLY = {
    "lead": {"name": "Sara El Amrani", "email": "SARA@Example.com", "phone": None, "company": None,
             "interest": "3-bedroom apartment", "budget": "2.5M MAD", "timeline": "this month",
             "location": "Casablanca", "language": "fr"},
    "score": 82, "tier": "cold", "intent_signals": ["budget stated", "buying this month"], "disqualifiers": [],
    "recommended_channel": "whatsapp",
    "reply": {"whatsapp": "Bonjour Sara, ...", "email_subject": "Votre projet", "email_body": "Bonjour Sara"},
    "next_action": "Call within 5 minutes"}


class Sink:
    """Records outbound deliveries (CRM + Slack) and can fail the first N calls."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.fail_first = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_first > 0:
            self.fail_first -= 1
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def sink(monkeypatch):
    s = Sink()
    monkeypatch.setattr(delivery, "TRANSPORT", httpx.MockTransport(s.handler))
    monkeypatch.setattr(delivery, "_guard", lambda url: None)  # no DNS in tests
    return s


@pytest.fixture
def papi(settings, make_router, fake_llm, sink):
    async def tiny_sleep(s):
        await asyncio.sleep(min(s, 0.01))

    from db import repo
    router = make_router(sleep=tiny_sleep, usage_hook=lambda rec: asyncio.to_thread(repo.record_usage, rec))
    s = replace(settings, stripe_webhook_secret="whsec_platform", demo_ip_hourly_limit=3)
    with TestClient(create_app(s, router=router)) as c:
        c.fake_llm = fake_llm
        yield c


def make_client(api, slug, **fields):
    r = api.post("/admin/clients", json={"slug": slug, "name": slug.title(), **fields}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def job_of(api, key, job_id, timeout=10):
    return wait_job(api, job_id, {"X-API-Key": key}, timeout=timeout)


def wait_deliveries(sink, n, timeout=10):
    deadline = time.time() + timeout
    while len(sink.requests) < n and time.time() < deadline:
        time.sleep(0.05)
    return sink.requests


# ------------------------------------------------------------- lead_qualifier
def test_lead_qualifier_scores_enforces_tier_and_delivers_signed(papi, sink):
    c = make_client(papi, "atlas", outbound_webhook_url="https://crm.example.com/hooks/leads",
                    config={"business_name": "Atlas Living", "notify_url": "https://hooks.slack.com/services/T/B/X",
                            "lead": {"hot_threshold": 75}})
    papi.fake_llm.script("groq", llm_json(LEAD_REPLY))
    r = papi.post("/webhook", headers={"X-API-Key": c["api_key"]}, json={"type": "lead_qualifier", "payload": {
        "full_name": "Sara", "phone_number": "+212 6 61 23 45 67", "msg": "3BR Anfa, 2.5M MAD, buying this month"}})
    assert r.status_code == 202
    job = job_of(papi, c["api_key"], r.json()["job_id"])
    res = job["result"]
    assert job["status"] == "succeeded", job
    assert res["tier"] == "hot"                       # 82 >= 75: corrected from the model's "cold"
    assert res["lead"]["email"] == "sara@example.com"
    assert res["lead"]["phone"] == "+212661234567"    # recovered from the raw payload
    assert res["recommended_channel"] == "whatsapp"
    assert res["speed_to_lead_ms"] is not None and res["llm"]["provider"] == "groq"
    assert [d["format"] for d in res["deliveries"]] == ["json", "slack"]
    reqs = wait_deliveries(sink, 2)
    crm = next(q for q in reqs if q.url.host == "crm.example.com")
    slack = next(q for q in reqs if q.url.host == "hooks.slack.com")
    expected = "sha256=" + hmac.new(c["webhook_secret"].encode(), crm.content, hashlib.sha256).hexdigest()
    assert crm.headers["x-autoplatform-signature"] == expected
    env = json.loads(crm.content)
    assert env["event"] == "lead.qualified" and env["client"] == "atlas" and env["data"]["score"] == 82
    assert "HOT lead 82/100" in json.loads(slack.content)["text"]


def test_lead_failover_and_json_repair(papi, sink):
    c = make_client(papi, "repair-co")
    papi.fake_llm.script("groq", (429, {"retry-after": "30"}, None))
    papi.fake_llm.script("gemini", (200, {}, {"choices": [{"message": {"content": "Sure! Here you go"}}]}),
                         llm_json(LEAD_REPLY))
    r = papi.post("/webhook", headers={"X-API-Key": c["api_key"]}, json={"type": "lead_qualifier",
                                                                          "payload": {"text": "hi"}})
    res = job_of(papi, c["api_key"], r.json()["job_id"])["result"]
    assert res["llm"]["provider"] == "gemini" and res["llm"]["failover"] is True and res["llm"]["repaired"] is True
    assert res["deliveries"][0]["reason"] == "no outbound_webhook_url configured"
    assert res["deliveries"][0]["preview"]["body"]["event"] == "lead.qualified"


def test_delivery_retries_until_the_crm_recovers(papi, sink):
    c = make_client(papi, "flaky-crm", outbound_webhook_url="https://crm.example.com/in")
    sink.fail_first = 1
    papi.fake_llm.script("groq", llm_json(LEAD_REPLY))
    r = papi.post("/webhook", headers={"X-API-Key": c["api_key"]}, json={"type": "lead_qualifier", "payload": {}})
    res = job_of(papi, c["api_key"], r.json()["job_id"])["result"]
    deadline = time.time() + 10
    while time.time() < deadline:
        d = papi.get(f"/tasks/{res['deliveries'][0]['delivery_job_id']}", headers={"X-API-Key": c["api_key"]}).json()
        if d["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.05)
    assert d["status"] == "succeeded" and d["attempts"] == 2 and d["result"]["status_code"] == 200
    assert len(sink.requests) == 2  # one 503, one success; the LLM ran once


# ------------------------------------------------------ whatsapp_order_triage
CATALOG = {"order": {"business_name": "Casa Pizza", "currency": "MAD", "delivery_fee": 15, "catalog": [
    {"sku": "PIZ-MAR", "name": "Margherita pizza", "price": 55, "aliases": ["margherita"]},
    {"sku": "PIZ-4F", "name": "Quattro formaggi pizza", "price": 75, "aliases": ["4 fromages"]},
    {"sku": "COK-33", "name": "Coca-Cola 33cl", "price": 10, "aliases": ["coca"]}]}}


def meta_message(text):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "contacts": [{"profile": {"name": "Youssef"}, "wa_id": "212670112233"}],
        "messages": [{"from": "212670112233", "type": "text", "text": {"body": text}}]}}]}]}


def test_order_triage_prices_from_catalog_never_from_the_model(papi, sink):
    c = make_client(papi, "casa-pizza", config=CATALOG)
    papi.fake_llm.script("groq", llm_json({
        "items": [{"description": "margherita", "quantity": 2, "sku": "PIZ-MAR"},
                  {"description": "4 fromages", "quantity": 1, "sku": None},
                  {"description": "coca", "quantity": 2, "sku": "COK-33", "notes": "bien froid"},
                  {"description": "sushi", "quantity": 1, "sku": "SUSHI-1"}],
        "fulfillment": "delivery", "address": "Maarif rue 12 imm 4 appt 7", "requested_time": "20:00",
        "customer": {"name": None, "phone": None}, "language": "fr", "needs_clarification": False,
        "clarification_question": None}))
    r = papi.post("/webhook", headers={"X-API-Key": c["api_key"]}, json={
        "type": "whatsapp_order_triage", "payload": {"body": meta_message("2 margherita w 1 4 fromages, 2 coca")}})
    res = job_of(papi, c["api_key"], r.json()["job_id"])["result"]
    assert res["source"] == "whatsapp_cloud_api"
    assert res["customer"] == {"name": "Youssef", "phone": "212670112233"}
    assert [(i["sku"], i["line_total"]) for i in res["items"]] == [("PIZ-MAR", 110.0), ("PIZ-4F", 75.0),
                                                                   ("COK-33", 20.0), (None, None)]
    assert res["totals"] == {"currency": "MAD", "subtotal": 205.0, "delivery_fee": 15.0, "tax": 0.0, "total": 220.0}
    assert res["unmatched_items"] == ["sushi"] and res["needs_review"] is True
    assert "Total: 220.00 MAD" in res["confirmation_message"] and "Livraison à" in res["confirmation_message"]


def test_order_triage_ignores_status_webhooks(papi):
    c = make_client(papi, "status-co", config=CATALOG)
    status_only = {"entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]}
    r = papi.post("/webhook", headers={"X-API-Key": c["api_key"]},
                  json={"type": "whatsapp_order_triage", "payload": {"body": status_only}})
    res = job_of(papi, c["api_key"], r.json()["job_id"])["result"]
    assert res["ignored"] is True and papi.fake_llm.calls == []


# ------------------------------------------------------- stripe_churn_rescuer
def stripe_event(etype, obj):
    return {"id": f"evt_{etype}_{time.time_ns()}", "type": etype, "created": int(time.time()),
            "data": {"object": obj}}


def post_stripe(api, path, evt, secret):
    raw = json.dumps(evt).encode()
    return api.post(path, content=raw, headers={"Stripe-Signature": signatures.sign_stripe(raw, secret),
                                                "Content-Type": "application/json"})


def test_churn_rescuer_via_stripe_signature_with_offer_guardrails(papi, sink):
    c = make_client(papi, "pulse", config={"churn": {"stripe_webhook_secret": None, "max_discount_percent": 25,
                                                      "allowed_offers": ["discount", "pause"]},
                                           "stripe_webhook_secret": "whsec_client"})
    sub = {"id": "sub_1", "customer": "cus_1", "start_date": int(time.time()) - 90 * 86400,
           "canceled_at": int(time.time()), "cancellation_details": {"reason": "cancellation_requested",
                                                                     "feedback": "too_expensive"},
           "items": {"data": [{"price": {"nickname": "Growth", "unit_amount": 14900, "currency": "usd",
                                         "recurring": {"interval": "month"}}}]},
           "metadata": {"email": "lina@northwind.example", "seats_used": "3 of 10"}}
    papi.fake_llm.script("groq", llm_json({"segment": "price_sensitive", "risk_reason": "too expensive",
                                           "offer": {"type": "discount", "value": 60, "duration_months": 3,
                                                     "rationale": "price"},
                                           "email": {"subject": "Before you go", "body": "Hi Lina"},
                                           "urgency": "high"}))
    bad = post_stripe(papi, "/webhook/pulse/stripe_churn_rescuer", stripe_event("customer.subscription.deleted", sub),
                      "whsec_wrong")
    assert bad.status_code == 401
    r = post_stripe(papi, "/webhook/pulse/stripe_churn_rescuer", stripe_event("customer.subscription.deleted", sub),
                    "whsec_client")
    assert r.status_code == 202, r.text
    res = job_of(papi, c["api_key"], r.json()["job_id"])["result"]
    assert res["offer"]["type"] == "discount" and res["offer"]["value"] == 25  # clamped from 60
    assert res["facts"]["amount"] == 149.0 and res["facts"]["tenure_days"] == 90
    assert res["email"]["to"] == "lina@northwind.example"


def test_churn_payment_failure_forces_payment_update_and_ignores_noise(papi):
    c = make_client(papi, "pulse2", config={"stripe_webhook_secret": "whsec_c2",
                                            "churn": {"billing_portal_url": "https://billing.example.com/p"}})
    papi.fake_llm.script("groq", llm_json({"segment": "payment_issue", "risk_reason": "card declined",
                                           "offer": {"type": "discount", "value": 10, "duration_months": 1,
                                                     "rationale": "x"},
                                           "email": {"subject": "Card issue", "body": "Hi"}, "urgency": "high"}))
    inv = {"customer": "cus_9", "customer_email": "a@b.example", "amount_due": 4900, "currency": "eur",
           "attempt_count": 2}
    r = post_stripe(papi, "/webhook/pulse2/stripe_churn_rescuer", stripe_event("invoice.payment_failed", inv), "whsec_c2")
    res = job_of(papi, c["api_key"], r.json()["job_id"])["result"]
    assert res["offer"]["type"] == "payment_update" and res["offer"]["value"] is None
    assert "https://billing.example.com/p" in res["email"]["body"]
    r2 = post_stripe(papi, "/webhook/pulse2/stripe_churn_rescuer", stripe_event("charge.succeeded", {}), "whsec_c2")
    assert job_of(papi, c["api_key"], r2.json()["job_id"])["result"]["ignored"] is True


# ------------------------------------------------------ support_ticket_router
def test_ticket_router_routes_and_sets_sla(papi):
    c = make_client(papi, "lumen", config={"support": {"routing": {"billing": "finance", "default": "desk"},
                                                       "sla_minutes": {"critical": 10}}})
    papi.fake_llm.script("groq", llm_json({
        "category": "Billing", "urgency": "critical", "sentiment": "negative", "sentiment_score": -1.7,
        "language": "fr", "summary": "Checkout down", "customer": {"name": "Marc", "email": None},
        "suggested_reply": "Bonjour Marc...", "tags": ["checkout"]}))
    r = papi.post("/webhook", headers={"X-API-Key": c["api_key"]}, json={"type": "support_ticket_router", "payload": {
        "from": "marc@boutique.example", "message": "Le checkout affiche une erreur 500"}})
    res = job_of(papi, c["api_key"], r.json()["job_id"])["result"]
    assert res["category"] == "billing" and res["route_to"] == "finance"     # enum matched case-insensitively
    assert res["sentiment_score"] == -1                                      # clamped to [-1, 1]
    assert res["escalate"] is True and res["sla_minutes"] == 10
    assert res["customer"]["email"] == "marc@boutique.example"


# ------------------------------------------------------------ plans & quotas
def test_plan_handlers_and_monthly_quota(papi):
    starter = make_client(papi, "starter-co", plan="guard_starter")
    r = papi.post("/webhook", headers={"X-API-Key": starter["api_key"]}, json={"type": "lead_qualifier"})
    assert r.status_code == 403
    tiny = make_client(papi, "tiny-co", monthly_job_limit=2)
    key = {"X-API-Key": tiny["api_key"]}
    assert [papi.post("/webhook", headers=key, json={"type": "echo"}).status_code for _ in range(3)] == [202, 202, 429]
    assert papi.get("/me", headers=key).json()["month_jobs"] == 2
    assert papi.post("/admin/clients", json={"slug": "stripe", "name": "x"}, headers=ADMIN).status_code == 422


# ------------------------------------------------------------- usage reports
def test_usage_report_and_reporter_delivery(papi, sink):
    c = make_client(papi, "roi-co", config={"notify_url": "https://discord.com/api/webhooks/1/abc",
                                            "hourly_rate_usd": 30})
    key = {"X-API-Key": c["api_key"]}
    papi.fake_llm.script("groq", (429, {"retry-after": "30"}, None))
    papi.fake_llm.script("gemini", llm_json({**LEAD_REPLY, "score": 90}))
    j = papi.post("/webhook", headers=key, json={"type": "lead_qualifier", "payload": {"x": 1}}).json()
    job_of(papi, c["api_key"], j["job_id"])
    j2 = papi.post("/webhook", headers=key, json={"type": "echo", "payload": {}}).json()
    job_of(papi, c["api_key"], j2["job_id"])
    wait_deliveries(sink, 1)
    rep = papi.get("/usage?days=7", headers=key).json()
    assert rep["webhooks_processed"] == 2 and rep["succeeded"] == 2 and rep["success_rate"] == 1.0
    assert rep["highlights"]["hot_leads"] == 1
    assert rep["llm"]["failover_saves"] == 1 and rep["llm"]["calls"] == 2
    assert rep["roi"]["minutes_saved"] == 4 and rep["roi"]["value_usd"] == 2.0
    assert "1 jobs rescued by automatic LLM failover" in rep["summary_text"]
    r = papi.post("/webhook", headers=key, json={"type": "usage_reporter", "payload": {"days": 7}}).json()
    res = job_of(papi, c["api_key"], r["job_id"])["result"]
    assert res["deliveries"][0]["format"] == "discord"
    reqs = wait_deliveries(sink, 2)
    assert "AutoPlatform report" in json.loads(reqs[-1].content)["content"]


# ------------------------------------------------------------------ agencies
def test_agency_provisions_and_reports_sub_clients(papi):
    agency = make_client(papi, "growthlab", plan="agency")
    akey = {"X-API-Key": agency["api_key"]}
    other = make_client(papi, "plain-co")
    assert papi.get("/agency/clients", headers={"X-API-Key": other["api_key"]}).status_code == 403
    r = papi.post("/agency/clients", headers=akey, json={"slug": "dentist", "name": "Smile Dental",
                                                         "monthly_job_limit": 999999,
                                                         "allowed_handlers": ["lead_qualifier", "echo"]})
    assert r.status_code == 201, r.text
    sub = r.json()
    assert sub["slug"] == "growthlab-dentist" and sub["monthly_job_limit"] == 5000  # capped by the plan
    assert sub["handler_urls"]["lead_qualifier"].endswith("/webhook/growthlab-dentist/lead_qualifier")
    skey = {"X-API-Key": sub["api_key"]}
    assert papi.post("/webhook", headers=skey, json={"type": "echo"}).status_code == 202
    assert papi.post("/webhook", headers=skey, json={"type": "support_ticket_router"}).status_code == 403
    listing = papi.get("/agency/clients", headers=akey).json()
    assert [(x["slug"], x["month_jobs"]) for x in listing] == [("growthlab-dentist", 1)]
    p = papi.patch("/agency/clients/growthlab-dentist", headers=akey,
                   json={"config": {"business_name": "Smile Dental", "stripe_webhook_secret": "whsec_hidden_1234"}})
    assert p.json()["config"]["stripe_webhook_secret"] == "****1234"  # secrets masked in views
    assert papi.get("/agency/usage?days=7", headers=akey).json()["totals"]["webhooks_processed"] >= 0
    assert papi.delete("/agency/clients/growthlab-dentist", headers=akey).json()["active"] is False
    assert papi.post("/webhook", headers=skey, json={"type": "echo"}).status_code == 401


# --------------------------------------------- Stripe dogfooding (platform)
def test_stripe_checkout_auto_provisions_and_reveals_credentials_once(papi):
    session = {"id": "cs_test_abc12345XYZ", "mode": "subscription", "payment_status": "paid",
               "customer": "cus_new1", "amount_total": 24900, "currency": "usd", "metadata": {"plan": "guard_pro"},
               "customer_details": {"name": "Nadia Benali", "email": "nadia@saasco.example"}}
    evt = stripe_event("checkout.session.completed", session)
    assert post_stripe(papi, "/webhook/stripe", evt, "whsec_wrong").status_code == 400
    r = post_stripe(papi, "/webhook/stripe", evt, "whsec_platform")
    assert r.status_code == 200 and r.json()["action"] == "provisioned" and r.json()["plan"] == "guard_pro"
    assert post_stripe(papi, "/webhook/stripe", evt, "whsec_platform").json()["duplicate"] is True
    page = papi.get("/billing/welcome?session_id=cs_test_abc12345XYZ")
    assert page.status_code == 200 and "ap_" in page.text and "Shown once" in page.text
    key = page.text.split("<pre>")[1].split("</pre>")[0]
    assert papi.get("/me", headers={"X-API-Key": key}).json()["plan"] == "guard_pro"
    assert papi.get("/billing/welcome?session_id=cs_test_abc12345XYZ").status_code == 410
    assert papi.get("/billing/welcome?session_id=cs_test_notyet0000").status_code == 202
    gone = post_stripe(papi, "/webhook/stripe", stripe_event("customer.subscription.deleted", {"customer": "cus_new1"}),
                       "whsec_platform")
    assert gone.json()["action"] == "deactivated"
    assert papi.get("/me", headers={"X-API-Key": key}).status_code == 401


def test_amount_fallback_and_unknown_plan_review(papi):
    s = {"id": "cs_test_amount00001", "payment_status": "paid", "customer": "cus_amt", "amount_total": 9900,
         "currency": "usd", "customer_details": {"email": "x@y.example"}}
    assert post_stripe(papi, "/webhook/stripe", stripe_event("checkout.session.completed", s),
                       "whsec_platform").json()["plan"] == "guard_starter"
    s2 = {**s, "id": "cs_test_amount00002", "customer": "cus_odd", "amount_total": 1234}
    out = post_stripe(papi, "/webhook/stripe", stripe_event("checkout.session.completed", s2), "whsec_platform").json()
    assert out["needs_review"] is True and out["plan"] == "guard_starter"


# ---------------------------------------------------------- Meta + signatures
def test_meta_verification_handshake(papi):
    make_client(papi, "wa-shop", config={"meta_verify_token": "tok123"})
    ok = papi.get("/webhook/wa-shop/whatsapp_order_triage",
                  params={"hub.mode": "subscribe", "hub.verify_token": "tok123", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    assert papi.get("/webhook/wa-shop/whatsapp_order_triage",
                    params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"}).status_code == 403


def test_signature_schemes():
    raw, secret = b'{"a":1}', "s3cret"
    import base64
    b64 = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    assert signatures.verify("typeform", {"Typeform-Signature": f"sha256={b64}"}, raw, secret)
    assert signatures.verify("shopify", {"X-Shopify-Hmac-Sha256": b64}, raw, secret)
    assert not signatures.verify("shopify", {"X-Shopify-Hmac-Sha256": b64}, raw + b" ", secret)
    assert signatures.verify("hmac", {"X-Hub-Signature-256": signatures.sign_hex(raw, secret)}, raw, secret)
    old = signatures.sign_stripe(raw, secret, ts=int(time.time()) - 400)
    assert not signatures.verify_stripe(old, raw, secret)              # replay outside tolerance
    assert signatures.verify_stripe(signatures.sign_stripe(raw, secret), raw, secret)
    assert signatures.detect_scheme({"Stripe-Signature": "t=1,v1=x"}) == "stripe"


def test_json_coercion_rules():
    schema = {"type": "object", "required": ["n", "e", "opt"], "properties": {
        "n": {"type": "integer", "minimum": 0, "maximum": 100}, "e": {"type": "string", "enum": ["hot", "cold"]},
        "opt": {"type": ["string", "null"]}}}
    assert coerce(schema, {"n": "87.6", "e": "HOT"}) == {"n": 88, "e": "hot", "opt": None}
    assert coerce(schema, {"n": 250, "e": "cold", "opt": "x"})["n"] == 100
    with pytest.raises(SchemaError):
        coerce(schema, {"n": 1, "e": "warm"})
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Here: {"a": 2} thanks') == {"a": 2}


# ---------------------------------------------------------------------- demo
def test_demo_failover_simulation_preview_and_limits(papi):
    pre = papi.options("/demo/run")
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "*"
    papi.fake_llm.script("gemini", llm_json(LEAD_REPLY))
    r = papi.post("/demo/run", json={"scenario": "lead", "simulate_outage": "groq"})
    assert r.status_code == 202 and "ingest;dur=" in r.headers["server-timing"]
    jid = r.json()["job_id"]
    deadline = time.time() + 10
    while (j := papi.get(f"/demo/jobs/{jid}").json())["status"] not in ("succeeded", "failed"):
        assert time.time() < deadline
        time.sleep(0.05)
    res = j["result"]
    assert res["llm"]["provider"] == "gemini" and res["llm"]["failover"] is True
    assert res["llm"]["attempts"][0] == {"provider": "groq", "success": False, "error_kind": "rate_limit",
                                         "status_code": 429, "latency_ms": 0, "simulated": True}
    assert papi.fake_llm.calls_to("groq") == 0                  # the outage was simulated, not a real call
    assert res["deliveries"][0]["reason"].startswith("demo")    # never sends anything outbound
    assert j["payload"]["full_name"] == "Sara El Amrani"
    assert [papi.post("/demo/run", json={"scenario": "ticket"}).status_code for _ in range(3)][-1] == 429
    assert papi.get("/demo").status_code == 200


def test_v2_migration_alters_an_existing_v1_database(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text

    from db import migrate

    eng = create_engine(f"sqlite:///{tmp_path}/v1.db")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE clients (id VARCHAR(32) PRIMARY KEY, slug VARCHAR(40), name VARCHAR(200), "
                       "api_key_hash VARCHAR(64), webhook_secret VARCHAR(128), active BOOLEAN, created_at TIMESTAMP)"))
        c.execute(text("CREATE TABLE jobs (id VARCHAR(32) PRIMARY KEY, client_id VARCHAR(32), created_at TIMESTAMP)"))
        c.execute(text("INSERT INTO clients VALUES ('c1','old','Old','h','s',1,'2026-01-01')"))
        migrate._v2_monetization(c)
        cols = {col["name"] for col in inspect(c).get_columns("clients")}
        assert set(migrate.V2_CLIENT_COLUMNS) <= cols
        assert c.execute(text("SELECT plan FROM clients WHERE id='c1'")).scalar() == "retainer"
        migrate._v2_monetization(c)  # idempotent


def test_provision_cli_api_and_agency_modes(papi, tmp_path):
    from scripts import provision_client as pc

    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"business_name": "Atlas", "lead": {"hot_threshold": 80}}))
    args = ["--api", "http://testserver", "--slug", "atlas-cli", "--name", "Atlas CLI", "--plan", "guard_pro",
            "--handlers", "lead_qualifier,support_ticket_router", "--outbound", "https://crm.example.com/x",
            "--notify", "https://hooks.slack.com/services/T/B/C", "--config", str(cfg), "--out", str(tmp_path / "c.json")]
    body = pc.build_body(pc.make_parser().parse_args(args))
    creds = pc.provision_via_api("http://testserver", body, admin_token="test-admin", client=papi)
    pc.report(creds, str(tmp_path / "c.json"))
    saved = json.loads((tmp_path / "c.json").read_text())
    assert saved["slug"] == "atlas-cli" and saved["plan"] == "guard_pro" and saved["api_key"].startswith("ap_")
    assert oct((tmp_path / "c.json").stat().st_mode)[-3:] == "600"
    me = papi.get("/me", headers={"X-API-Key": saved["api_key"]}).json()
    assert me["allowed_handlers"] == ["lead_qualifier", "support_ticket_router"]
    assert me["config"]["notify_url"] == "https://hooks.slack.com/services/T/B/C" and me["config"]["lead"]["hot_threshold"] == 80
    agency = make_client(papi, "agencyco", plan="agency")
    sub = pc.provision_via_api("http://testserver", {"slug": "shop", "name": "Shop"}, agency_key=agency["api_key"], client=papi)
    assert sub["slug"] == "agencyco-shop"

