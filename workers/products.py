"""Revenue handlers (mirrored 1:1 in worker/src/products.ts).

  lead_qualifier          form / ad / chat lead -> entities, 0-100 score, drafted WhatsApp + email, CRM delivery
  whatsapp_order_triage   messy WhatsApp / web order -> items priced from YOUR catalog, totals, confirmation
  stripe_churn_rescuer    Stripe cancel / failed payment -> segment, guard-railed offer, founder email
  support_ticket_router   inbound ticket -> category, urgency, sentiment, routing, SLA due time, draft reply
  usage_reporter          weekly ROI digest: volume, speed, failover saves, deliveries, hours saved

The LLM does understanding and drafting. Everything that must be exact (prices, totals, tiers,
discount caps, SLA deadlines, contact details) is computed deterministically here.
Per-client behaviour comes from clients.config (see monetization/gtm/offer_architecture.md).
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from db import repo
from models import Client, JobStatus, utcnow
from workers.delivery import queue_deliveries
from workers.handlers import JobContext, PermanentError, handler
from workers.llm_json import complete_json

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
DEFAULT_MINUTES_SAVED = {"lead_qualifier": 4, "whatsapp_order_triage": 3, "stripe_churn_rescuer": 10,
                         "support_ticket_router": 5, "webhook.event": 2, "llm.complete": 2, "http.fetch": 1,
                         "echo": 0}
INTERNAL_KEYS = ("_demo", "_simulate_outage")


# ------------------------------------------------------------------- helpers
async def load_client(ctx: JobContext) -> Client | None:
    return await asyncio.to_thread(repo.client_by_id, ctx.client_id) if ctx.client_id else None


def section(cfg: dict, name: str) -> dict:
    sec = dict(cfg.get(name) or {})
    for key in ("business_name", "business_description", "tone", "booking_url", "website"):
        sec.setdefault(key, cfg.get(key))
    return sec


def inbound(payload: dict) -> Any:
    body = payload.get("body", payload)
    if isinstance(body, dict):
        return {k: v for k, v in body.items() if k not in INTERNAL_KEYS}
    return body


def as_text(value: Any, limit: int = 6000) -> str:
    return (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str))[:limit]


def norm_email(v: Any) -> str | None:
    m = EMAIL_RE.search(str(v or ""))
    return m.group(0).lower() if m else None


def norm_phone(v: Any) -> str | None:
    if not v:
        return None
    s = str(v).strip()
    digits = re.sub(r"\D", "", s)
    if len(digits) < 8 or len(digits) > 15:
        return None
    return ("+" if s.startswith("+") or s.startswith("00") else "") + digits.removeprefix("00")


def llm_opts(cfg: dict, payload: dict) -> dict:
    return {"providers": cfg.get("llm_providers") or None,
            "simulate_outage": payload.get("_simulate_outage") if payload.get("_demo") else None}


def ms_since(created_at: str | None) -> int | None:
    if not created_at:
        return None
    try:
        t = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0, int((utcnow() - t).total_seconds() * 1000))


def money(x: float) -> float:
    return round(x + 1e-9, 2)


# ---------------------------------------------------------------- lead_qualifier
LEAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["lead", "score", "tier", "intent_signals", "recommended_channel", "reply", "next_action"],
    "properties": {
        "lead": {"type": "object",
                 "required": ["name", "email", "phone", "company", "interest", "budget", "timeline", "location", "language"],
                 "properties": {
                     "name": {"type": ["string", "null"]}, "email": {"type": ["string", "null"]},
                     "phone": {"type": ["string", "null"]}, "company": {"type": ["string", "null"]},
                     "interest": {"type": ["string", "null"], "description": "what they want"},
                     "budget": {"type": ["string", "null"]}, "timeline": {"type": ["string", "null"]},
                     "location": {"type": ["string", "null"]},
                     "language": {"type": ["string", "null"], "description": "ISO 639-1 code of the lead's own words"}}},
        "score": {"type": "integer", "minimum": 0, "maximum": 100, "description": "purchase intent x fit"},
        "tier": {"type": "string", "enum": ["hot", "warm", "cold"]},
        "intent_signals": {"type": "array", "items": {"type": "string"}},
        "disqualifiers": {"type": "array", "items": {"type": "string"}},
        "recommended_channel": {"type": "string", "enum": ["whatsapp", "email", "call"]},
        "reply": {"type": "object", "required": ["whatsapp", "email_subject", "email_body"],
                  "properties": {"whatsapp": {"type": "string", "description": "<= 60 words"},
                                 "email_subject": {"type": "string"}, "email_body": {"type": "string"}}},
        "next_action": {"type": "string"},
    },
}


@handler("lead_qualifier")
async def lead_qualifier(ctx: JobContext, payload: dict) -> dict:
    client = await load_client(ctx)
    cfg = (client.config if client else None) or {}
    lc = section(cfg, "lead")
    raw = inbound(payload)
    text = as_text(raw)
    system = (
        f"You qualify inbound leads for {lc.get('business_name') or 'the business'}"
        f"{': ' + lc['business_description'] if lc.get('business_description') else ''}.\n"
        f"Ideal customer: {lc.get('ideal_customer') or 'clear need, budget and near-term timeline'}.\n"
        "Extract the lead's details exactly as given (null when absent; never invent contact data). "
        "Score 0-100 for intent and fit; list concrete intent signals and disqualifiers. "
        f"Draft replies in the lead's language, tone: {lc.get('tone') or 'warm, concise, professional'}. "
        "The WhatsApp reply is at most 60 words and ends with one clear next step"
        f"{' (booking link: ' + lc['booking_url'] + ')' if lc.get('booking_url') else ''}. "
        "Do not promise prices, availability or facts that are not in the lead or the business description."
    )
    data, meta = await complete_json(ctx, system=system, user=f"Inbound lead payload:\n{text}", schema=LEAD_SCHEMA,
                                     max_tokens=800, **llm_opts(cfg, payload))
    lead = data["lead"]
    lead["email"] = norm_email(lead.get("email")) or norm_email(text)
    phone_hint = PHONE_RE.search(text)
    lead["phone"] = norm_phone(lead.get("phone")) or norm_phone(phone_hint.group(0) if phone_hint else None)
    hot, warm = int(lc.get("hot_threshold", 70)), int(lc.get("warm_threshold", 40))
    score = data["score"]
    data["tier"] = "hot" if score >= hot else "warm" if score >= warm else "cold"
    channel = data["recommended_channel"]
    if channel == "whatsapp" and not lead["phone"]:
        channel = "email" if lead["email"] else "call"
    if channel == "email" and not lead["email"]:
        channel = "whatsapp" if lead["phone"] else "call"
    data["recommended_channel"] = channel
    data.setdefault("disqualifiers", [])
    data["speed_to_lead_ms"] = ms_since(ctx.created_at)
    data["llm"] = meta
    icon = {"hot": "🔥", "warm": "🌤", "cold": "❄️"}[data["tier"]]
    summary = (f"{icon} {data['tier'].upper()} lead {score}/100: {lead.get('name') or 'unknown'}"
               f"{' - ' + lead['interest'] if lead.get('interest') else ''} | via {channel} "
               f"{lead['phone'] or lead['email'] or ''}\nNext: {data['next_action']}")
    data["deliveries"] = await queue_deliveries(ctx, client, "lead.qualified",
                                                {k: v for k, v in data.items() if k != "llm"}, summary)
    return data


# --------------------------------------------------------- whatsapp_order_triage
ORDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["items", "fulfillment", "address", "requested_time", "customer", "language",
                 "needs_clarification", "clarification_question"],
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object", "required": ["description", "quantity", "sku"],
            "properties": {"description": {"type": "string"},
                           "quantity": {"type": "integer", "minimum": 1, "maximum": 999},
                           "sku": {"type": ["string", "null"], "description": "catalog sku, or null if no match"},
                           "notes": {"type": ["string", "null"]}}}},
        "fulfillment": {"type": "string", "enum": ["delivery", "pickup", "unknown"]},
        "address": {"type": ["string", "null"]},
        "requested_time": {"type": ["string", "null"]},
        "customer": {"type": "object", "required": ["name", "phone"],
                     "properties": {"name": {"type": ["string", "null"]}, "phone": {"type": ["string", "null"]}}},
        "language": {"type": "string", "description": "ISO 639-1 code of the customer's message"},
        "needs_clarification": {"type": "boolean"},
        "clarification_question": {"type": ["string", "null"],
                                   "description": "one short question in the customer's language, or null"},
    },
}

CONFIRM_TEMPLATES = {
    "en": {"hi": "Thanks{name}! Here is your order:", "delivery": "Delivery", "total": "Total",
           "to": "Delivering to", "pickup": "Pickup", "confirm": "Reply YES to confirm.",
           "unknown": "Not on our menu"},
    "fr": {"hi": "Merci{name} ! Voici votre commande :", "delivery": "Livraison", "total": "Total",
           "to": "Livraison à", "pickup": "À emporter", "confirm": "Répondez OUI pour confirmer.",
           "unknown": "Hors carte"},
    "es": {"hi": "¡Gracias{name}! Este es tu pedido:", "delivery": "Envío", "total": "Total",
           "to": "Entrega en", "pickup": "Para recoger", "confirm": "Responde SÍ para confirmar.",
           "unknown": "No está en la carta"},
    "ar": {"hi": "شكراً{name}! هذا طلبك:", "delivery": "التوصيل", "total": "المجموع",
           "to": "العنوان", "pickup": "استلام من المحل", "confirm": "أرسل نعم للتأكيد.",
           "unknown": "غير موجود في القائمة"},
}


def whatsapp_message(body: Any) -> tuple[str | None, dict, str]:
    """Meta WhatsApp Cloud API webhook, or any {message|text|body, customer} JSON."""
    if isinstance(body, dict) and isinstance(body.get("entry"), list):
        try:
            value = body["entry"][0]["changes"][0]["value"]
        except (KeyError, IndexError, TypeError):
            value = {}
        msgs = value.get("messages") or []
        if not msgs:
            return None, {}, "whatsapp_cloud_api"
        m = msgs[0]
        text = ((m.get("text") or {}).get("body") or (m.get("button") or {}).get("text")
                or ((m.get("interactive") or {}).get("list_reply") or {}).get("title"))
        contact = (value.get("contacts") or [{}])[0]
        return text, {"name": (contact.get("profile") or {}).get("name"), "phone": m.get("from")}, "whatsapp_cloud_api"
    if isinstance(body, dict):
        text = body.get("message") or body.get("text") or (body.get("body") if isinstance(body.get("body"), str) else None)
        cust = body.get("customer") if isinstance(body.get("customer"), dict) else {}
        return (text if isinstance(text, str) else as_text(body)), {
            "name": cust.get("name") or body.get("name"),
            "phone": cust.get("phone") or body.get("phone") or body.get("from")}, "generic"
    return (str(body) if body else None), {}, "generic"


def match_sku(catalog: list[dict], sku: str | None, description: str) -> dict | None:
    by_sku = {str(c.get("sku")): c for c in catalog}
    if sku and sku in by_sku:
        return by_sku[sku]
    d = description.casefold()
    for c in catalog:
        names = [str(c.get("name", ""))] + [str(a) for a in c.get("aliases") or []]
        if any(n and (n.casefold() in d or d in n.casefold()) for n in names):
            return c
    return None


@handler("whatsapp_order_triage")
async def whatsapp_order_triage(ctx: JobContext, payload: dict) -> dict:
    client = await load_client(ctx)
    cfg = (client.config if client else None) or {}
    oc = section(cfg, "order")
    body = inbound(payload)
    text, customer_hint, source = whatsapp_message(body)
    if not text:
        return {"ignored": True, "reason": "no customer message in payload (e.g. a delivery-status webhook)",
                "source": source}
    catalog = [c for c in (oc.get("catalog") or []) if isinstance(c, dict) and c.get("sku")]
    currency = oc.get("currency") or "USD"
    menu = "\n".join(f"- {c['sku']}: {c.get('name')}"
                     + (f" (also: {', '.join(map(str, c['aliases']))})" if c.get("aliases") else "") for c in catalog)
    system = (
        f"You take orders for {oc.get('business_name') or 'the shop'} from WhatsApp/web messages that may be messy, "
        "multilingual or abbreviated. Map each requested item to a catalog sku when it clearly matches "
        "(else sku null), with an integer quantity. Capture modifiers in notes, the delivery address, the "
        "requested time and whether it is delivery or pickup. If anything essential is ambiguous, set "
        "needs_clarification and ask ONE short question in the customer's language. Do not compute prices.\n"
        f"Catalog:\n{menu or '(no catalog configured: set every sku to null)'}"
    )
    user = f"Customer: {json.dumps(customer_hint, ensure_ascii=False)}\nMessage:\n{text[:4000]}"
    data, meta = await complete_json(ctx, system=system, user=user, schema=ORDER_SCHEMA, max_tokens=900,
                                     **llm_opts(cfg, payload))
    lines, unmatched = [], []
    for item in data["items"]:
        c = match_sku(catalog, item.get("sku"), item["description"])
        if c is None:
            unmatched.append(item["description"])
            lines.append({**item, "sku": None, "name": item["description"], "unit_price": None, "line_total": None})
            continue
        unit = float(c.get("price") or 0)
        lines.append({**item, "sku": c["sku"], "name": c.get("name"), "unit_price": money(unit),
                      "line_total": money(unit * item["quantity"])})
    subtotal = money(sum(li["line_total"] or 0 for li in lines))
    fee = money(float(oc.get("delivery_fee") or 0)) if data["fulfillment"] == "delivery" else 0.0
    tax = money(subtotal * float(oc.get("tax_rate") or 0))
    total = money(subtotal + fee + tax)
    min_order = float(oc.get("min_order") or 0)
    customer = {"name": data["customer"].get("name") or customer_hint.get("name"),
                "phone": norm_phone(data["customer"].get("phone") or customer_hint.get("phone"))}
    needs_review = bool(unmatched) or data["needs_clarification"] or not lines or (min_order and subtotal < min_order)
    t = CONFIRM_TEMPLATES.get((data["language"] or "en")[:2].lower(), CONFIRM_TEMPLATES["en"])
    msg = [t["hi"].format(name=f" {customer['name']}" if customer["name"] else "")]
    for li in lines:
        price = f"{li['line_total']:.2f} {currency}" if li["line_total"] is not None else t["unknown"]
        msg.append(f"• {li['quantity']}× {li['name']}" + (f" ({li['notes']})" if li.get("notes") else "") + f" - {price}")
    if fee:
        msg.append(f"{t['delivery']}: {fee:.2f} {currency}")
    msg.append(f"{t['total']}: {total:.2f} {currency}")
    if data["fulfillment"] == "delivery" and data.get("address"):
        msg.append(f"{t['to']}: {data['address']}")
    elif data["fulfillment"] == "pickup":
        msg.append(t["pickup"])
    msg.append(data["clarification_question"] if data["needs_clarification"] and data.get("clarification_question")
               else t["confirm"])
    result = {"source": source, "customer": customer, "items": lines, "unmatched_items": unmatched,
              "fulfillment": data["fulfillment"], "address": data.get("address"),
              "requested_time": data.get("requested_time"), "language": data["language"],
              "totals": {"currency": currency, "subtotal": subtotal, "delivery_fee": fee, "tax": tax, "total": total},
              "below_minimum": bool(min_order and subtotal < min_order), "needs_review": bool(needs_review),
              "clarification_question": data.get("clarification_question") if data["needs_clarification"] else None,
              "confirmation_message": "\n".join(msg), "speed_ms": ms_since(ctx.created_at), "llm": meta}
    summary = (f"🛒 Order {total:.2f} {currency} - {len(lines)} item(s) - {customer['name'] or 'customer'} "
               f"{customer['phone'] or ''}{' ⚠️ needs review' if needs_review else ''}")
    result["deliveries"] = await queue_deliveries(ctx, client, "order.received",
                                                  {k: v for k, v in result.items() if k != "llm"}, summary)
    return result


# ---------------------------------------------------------- stripe_churn_rescuer
CHURN_EVENTS = {"customer.subscription.deleted", "invoice.payment_failed", "customer.subscription.updated"}
CHURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["segment", "risk_reason", "offer", "email", "urgency"],
    "properties": {
        "segment": {"type": "string", "enum": ["price_sensitive", "low_usage", "missing_feature",
                                                "switched_competitor", "payment_issue", "temporary_pause", "unknown"]},
        "risk_reason": {"type": "string"},
        "offer": {"type": "object", "required": ["type", "value", "duration_months", "rationale"],
                  "properties": {"type": {"type": "string", "enum": ["discount", "pause", "downgrade", "extension",
                                                                      "payment_update", "none"]},
                                 "value": {"type": ["number", "null"], "description": "percent off, or months"},
                                 "duration_months": {"type": ["integer", "null"], "minimum": 1, "maximum": 12},
                                 "rationale": {"type": "string"}}},
        "email": {"type": "object", "required": ["subject", "body"],
                  "properties": {"subject": {"type": "string"}, "body": {"type": "string"}}},
        "urgency": {"type": "string", "enum": ["high", "medium", "low"]},
    },
}


def churn_facts(evt: dict) -> dict | None:
    etype = evt.get("type")
    obj = ((evt.get("data") or {}).get("object") or {})
    if etype not in CHURN_EVENTS:
        return None
    if etype == "customer.subscription.updated" and not obj.get("cancel_at_period_end"):
        return None
    meta = obj.get("metadata") or {}
    item = (((obj.get("items") or {}).get("data") or [{}])[0]) if obj.get("items") else {}
    line = (((obj.get("lines") or {}).get("data") or [{}])[0]) if obj.get("lines") else {}
    price = item.get("price") or line.get("price") or {}
    amount = price.get("unit_amount") if price.get("unit_amount") is not None else obj.get("amount_due")
    cancel = obj.get("cancellation_details") or {}
    start = obj.get("start_date") or obj.get("created")
    end = obj.get("canceled_at") or evt.get("created")
    involuntary = etype == "invoice.payment_failed" or cancel.get("reason") == "payment_failed"
    return {
        "event_type": etype,
        "churn_kind": "involuntary" if involuntary else ("scheduled_cancellation"
                                                         if etype == "customer.subscription.updated" else "voluntary"),
        "customer_id": obj.get("customer"),
        "email": obj.get("customer_email") or meta.get("email"),
        "name": obj.get("customer_name") or meta.get("name"),
        "plan": price.get("nickname") or price.get("lookup_key") or price.get("product") or meta.get("plan"),
        "amount": (amount / 100) if isinstance(amount, (int, float)) else None,
        "currency": (price.get("currency") or obj.get("currency") or "").upper() or None,
        "interval": (price.get("recurring") or {}).get("interval"),
        "tenure_days": int((end - start) / 86400) if isinstance(start, int) and isinstance(end, int) else None,
        "cancellation_reason": cancel.get("reason"), "feedback": cancel.get("feedback"),
        "comment": cancel.get("comment"), "attempt_count": obj.get("attempt_count"),
        "next_payment_attempt": obj.get("next_payment_attempt"),
        "usage_metadata": {k: v for k, v in meta.items() if k not in ("email", "name", "plan")},
    }


@handler("stripe_churn_rescuer")
async def stripe_churn_rescuer(ctx: JobContext, payload: dict) -> dict:
    client = await load_client(ctx)
    cfg = (client.config if client else None) or {}
    cc = section(cfg, "churn")
    evt = inbound(payload)
    if not isinstance(evt, dict):
        raise PermanentError("expected a Stripe event JSON object")
    facts = churn_facts(evt)
    if facts is None:
        return {"ignored": True, "reason": f"event {evt.get('type')!r} is not a churn signal"}
    allowed = [o for o in (cc.get("allowed_offers") or ["discount", "pause", "downgrade", "extension"])]
    max_discount = float(cc.get("max_discount_percent", 30))
    product = cc.get("product_name") or cc.get("business_name") or "our product"
    founder = cc.get("founder_name") or "the founder"
    system = (
        f"You are {founder}, founder of {product}, writing a short personal win-back email (max 140 words, "
        "plain text, no marketing fluff) to a customer who is churning. Diagnose the most likely churn segment "
        "from the facts, pick ONE offer from the allowed list "
        f"{allowed + ['payment_update', 'none']} (discount at most {max_discount:g}%), and explain it simply. "
        "For involuntary churn (failed payment) the offer must be payment_update with a friendly nudge to update "
        "the card. Never invent features, usage numbers or deadlines that are not in the facts."
    )
    data, meta = await complete_json(ctx, system=system, user=f"Churn facts:\n{json.dumps(facts, default=str)}",
                                     schema=CHURN_SCHEMA, max_tokens=800, **llm_opts(cfg, payload))
    offer = data["offer"]
    if facts["churn_kind"] == "involuntary":
        offer.update({"type": "payment_update", "value": None, "duration_months": None})
    elif offer["type"] not in allowed + ["none"]:
        offer.update({"type": allowed[0] if allowed else "none", "value": None})
    if offer["type"] == "discount":
        offer["value"] = min(max_discount, max(0.0, float(offer.get("value") or max_discount)))
        offer["duration_months"] = offer.get("duration_months") or 3
    portal = cc.get("billing_portal_url")
    if portal and offer["type"] == "payment_update" and portal not in data["email"]["body"]:
        data["email"]["body"] += f"\n\nUpdate your payment method here: {portal}"
    result = {"facts": facts, "segment": data["segment"], "risk_reason": data["risk_reason"], "offer": offer,
              "email": {**data["email"], "to": facts["email"]}, "urgency": data["urgency"], "llm": meta}
    value = f" {offer['value']:g}{'%' if offer['type'] == 'discount' else ''}" if offer.get("value") else ""
    summary = (f"💸 Churn risk ({facts['churn_kind']}): {facts['email'] or facts['customer_id']} "
               f"{facts['plan'] or ''} {facts['amount'] or ''}{facts['currency'] or ''} | {data['segment']} "
               f"-> offer: {offer['type']}{value} | urgency {data['urgency']}")
    result["deliveries"] = await queue_deliveries(ctx, client, "churn.rescue_drafted",
                                                  {k: v for k, v in result.items() if k != "llm"}, summary)
    return result


# --------------------------------------------------------- support_ticket_router
DEFAULT_CATEGORIES = ["billing", "technical", "account", "sales", "feedback", "other"]
DEFAULT_SLA_MIN = {"critical": 15, "high": 60, "normal": 240, "low": 1440}


def ticket_schema(categories: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["category", "urgency", "sentiment", "sentiment_score", "language", "summary", "customer",
                     "suggested_reply", "tags"],
        "properties": {
            "category": {"type": "string", "enum": categories},
            "urgency": {"type": "string", "enum": ["critical", "high", "normal", "low"]},
            "sentiment": {"type": "string", "enum": ["negative", "neutral", "positive"]},
            "sentiment_score": {"type": "number", "minimum": -1, "maximum": 1},
            "language": {"type": "string"},
            "summary": {"type": "string", "description": "one sentence"},
            "customer": {"type": "object", "required": ["name", "email"],
                         "properties": {"name": {"type": ["string", "null"]}, "email": {"type": ["string", "null"]}}},
            "suggested_reply": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
    }


@handler("support_ticket_router")
async def support_ticket_router(ctx: JobContext, payload: dict) -> dict:
    client = await load_client(ctx)
    cfg = (client.config if client else None) or {}
    sc = section(cfg, "support")
    categories = [str(c) for c in (sc.get("categories") or DEFAULT_CATEGORIES)]
    if "other" not in categories:
        categories.append("other")
    text = as_text(inbound(payload))
    system = (
        f"You triage support messages for {sc.get('business_name') or 'the company'}. Classify category, urgency "
        "(critical = outage, data loss, security or payment blocked for many; high = a customer is blocked; "
        "normal = needs help; low = question or feedback), sentiment and language, summarise in one sentence, and "
        "draft a ready-to-send reply in the customer's language for a human agent to approve. "
        f"Tone: {sc.get('tone') or 'empathetic, precise'}. Never promise refunds, credits or timelines "
        f"unless this policy allows it: {sc.get('policy') or 'no promises'}."
    )
    data, meta = await complete_json(ctx, system=system, user=f"Inbound message payload:\n{text}",
                                     schema=ticket_schema(categories), max_tokens=800, **llm_opts(cfg, payload))
    routing = sc.get("routing") or {}
    sla = {**DEFAULT_SLA_MIN, **(sc.get("sla_minutes") or {})}
    due = utcnow() + timedelta(minutes=int(sla.get(data["urgency"], 240)))
    escalate = data["urgency"] == "critical" or (data["sentiment"] == "negative" and data["urgency"] == "high")
    data["customer"]["email"] = norm_email(data["customer"].get("email")) or norm_email(text)
    result = {**data, "route_to": routing.get(data["category"]) or routing.get("default"),
              "due_at": due.isoformat(), "sla_minutes": int(sla.get(data["urgency"], 240)), "escalate": escalate,
              "speed_ms": ms_since(ctx.created_at), "llm": meta}
    summary = (f"🎫 [{data['urgency']}{' ⚠️ ESCALATE' if escalate else ''}] {data['category']} | {data['sentiment']} | "
               f"{data['summary']} -> {result['route_to'] or 'unassigned'} (due {due.strftime('%H:%M UTC')})")
    result["deliveries"] = await queue_deliveries(ctx, client, "ticket.triaged",
                                                  {k: v for k, v in result.items() if k != "llm"}, summary)
    return result


# ----------------------------------------------------------------- usage_reporter
def _pct(sorted_vals: list[float], q: float) -> int | None:
    if not sorted_vals:
        return None
    return int(sorted_vals[max(0, math.ceil(q * len(sorted_vals)) - 1)])


def _as_dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def build_report(client: Client, rows: dict, days: int) -> dict[str, Any]:
    """Pure aggregation (identical in worker/src/products.ts)."""
    cfg = client.config or {}
    minutes_map = {**DEFAULT_MINUTES_SAVED, **(cfg.get("minutes_saved") or {})}
    rate = float(cfg.get("hourly_rate_usd", 25))
    jobs = rows["jobs"]
    skip = ("internal.", "system.")
    work = [j for j in jobs if not str(j[0]).startswith(skip) and j[0] != "usage_reporter"]
    ok = [j for j in work if j[1] == JobStatus.SUCCEEDED]
    failed = [j for j in work if j[1] == JobStatus.FAILED]
    ttr = sorted(int((_as_dt(j[3]) - _as_dt(j[2])).total_seconds() * 1000)
                 for j in ok if _as_dt(j[3]) and _as_dt(j[2]))
    durations = sorted(int(d) for d in rows["durations"])
    per_job: dict[str, list[bool]] = {}
    tokens = 0
    for job_id, _provider, success, pt, ct in rows["usage"]:
        tokens += int(pt or 0) + int(ct or 0)
        if job_id:
            per_job.setdefault(job_id, []).append(bool(success))
    saves = sum(1 for outcomes in per_job.values() if (False in outcomes) and (True in outcomes))
    deliveries = [j for j in jobs if j[0] == "internal.deliver"]
    results = [(j[0], j[4] if isinstance(j[4], dict) else {}) for j in ok]
    orders = [r for t, r in results if t == "whatsapp_order_triage" and not r.get("ignored")]
    order_value: dict[str, float] = {}
    for r in orders:
        tot = r.get("totals") or {}
        if tot.get("currency"):
            order_value[tot["currency"]] = money(order_value.get(tot["currency"], 0) + float(tot.get("total") or 0))
    minutes = sum(minutes_map.get(j[0], 4) for j in ok)
    by_handler = Counter(j[0] for j in work)
    now = utcnow()
    report = {
        "client": client.slug, "client_name": client.name,
        "period": {"from": (now - timedelta(days=days)).isoformat(), "to": now.isoformat(), "days": days},
        "webhooks_processed": len(work), "by_handler": dict(sorted(by_handler.items())),
        "succeeded": len(ok), "failed": len(failed), "in_progress": len(work) - len(ok) - len(failed),
        "success_rate": round(len(ok) / len(work), 4) if work else None,
        "processing_ms": {"avg": int(sum(durations) / len(durations)) if durations else None,
                          "p95": _pct(durations, 0.95)},
        "time_to_result_ms": {"p50": _pct(ttr, 0.5), "p95": _pct(ttr, 0.95)},
        "llm": {"calls": len(rows["usage"]), "tokens": tokens, "failover_saves": saves},
        "deliveries": {"delivered": sum(1 for d in deliveries if d[1] == JobStatus.SUCCEEDED),
                       "failed": sum(1 for d in deliveries if d[1] == JobStatus.FAILED),
                       "pending": sum(1 for d in deliveries if d[1] not in (JobStatus.SUCCEEDED, JobStatus.FAILED))},
        "highlights": {
            "hot_leads": sum(1 for t, r in results if t == "lead_qualifier" and r.get("tier") == "hot"),
            "orders": len(orders), "orders_value": order_value,
            "churn_rescues_drafted": sum(1 for t, r in results if t == "stripe_churn_rescuer" and not r.get("ignored")),
            "tickets": sum(1 for t, _ in results if t == "support_ticket_router"),
            "tickets_escalated": sum(1 for t, r in results if t == "support_ticket_router" and r.get("escalate")),
        },
        "roi": {"minutes_saved": minutes, "hours_saved": round(minutes / 60, 1), "hourly_rate_usd": rate,
                "value_usd": round(minutes / 60 * rate, 2)},
    }
    report["summary_text"] = report_text(report)
    return report


def report_text(r: dict) -> str:
    h, roi = r["highlights"], r["roi"]
    period = f"{r['period']['from'][:10]} to {r['period']['to'][:10]}"
    lines = [f"📊 {r['client_name']}: AutoPlatform report ({period})"]
    rate = f" · {r['success_rate'] * 100:.1f}% success" if r["success_rate"] is not None else ""
    lines.append(f"• {r['webhooks_processed']} webhooks processed{rate}")
    if r["time_to_result_ms"]["p50"] is not None:
        lines.append(f"• Webhook → result: median {r['time_to_result_ms']['p50'] / 1000:.1f}s "
                     f"(p95 {r['time_to_result_ms']['p95'] / 1000:.1f}s)")
    parts = []
    if h["hot_leads"]:
        parts.append(f"{h['hot_leads']} hot leads")
    if h["orders"]:
        parts.append(f"{h['orders']} orders (" + ", ".join(f"{v:,.2f} {c}" for c, v in h["orders_value"].items()) + ")")
    if h["churn_rescues_drafted"]:
        parts.append(f"{h['churn_rescues_drafted']} churn rescues drafted")
    if h["tickets"]:
        parts.append(f"{h['tickets']} tickets triaged ({h['tickets_escalated']} escalated)")
    if parts:
        lines.append("• " + " · ".join(parts))
    lines.append(f"• {r['llm']['failover_saves']} jobs rescued by automatic LLM failover · "
                 f"{r['deliveries']['delivered']} CRM deliveries ({r['deliveries']['failed']} failed)")
    lines.append(f"• ≈ {roi['hours_saved']:.1f} hours of manual work saved "
                 f"(≈ ${roi['value_usd']:,.0f} at ${roi['hourly_rate_usd']:g}/h)")
    return "\n".join(lines)


async def usage_report(client: Client, days: int) -> dict[str, Any]:
    rows = await asyncio.to_thread(repo.report_rows, client.id, days)
    return build_report(client, rows, days)


@handler("usage_reporter")
async def usage_reporter(ctx: JobContext, payload: dict) -> dict:
    client = await load_client(ctx)
    if client is None:
        raise PermanentError("usage_reporter needs a client")
    days = max(1, min(int(payload.get("days") or 7), 90))
    report = await usage_report(client, days)
    if payload.get("deliver", True):
        report["deliveries"] = await queue_deliveries(ctx, client, "report.weekly", report, report["summary_text"])
    return report


async def enqueue_weekly_reports(queue) -> list[str]:
    ids = []
    for client_id in await asyncio.to_thread(repo.clients_for_weekly_reports):
        job = await asyncio.to_thread(repo.create_job, "usage_reporter", {"days": 7, "deliver": True},
                                      client_id=client_id, max_attempts=3, source="cron")
        await queue.enqueue(job.id)
        ids.append(job.id)
    return ids
