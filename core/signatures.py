"""Inbound webhook signature schemes (mirrored in worker/src/signatures.ts).

  stripe    Stripe-Signature: t=<ts>,v1=<hex hmac_sha256(secret, "<ts>.<raw body>")>   (5 min tolerance)
  typeform  Typeform-Signature: sha256=<base64 hmac_sha256(secret, raw body)>
  shopify   X-Shopify-Hmac-Sha256: <base64 hmac_sha256(secret, raw body)>
  hmac      X-Signature-256 / X-Hub-Signature-256: sha256=<hex hmac_sha256(secret, raw body)>
            (GitHub, Meta/WhatsApp Cloud API, and AutoPlatform's own format)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Mapping

STRIPE_TOLERANCE_S = 300


def _hmac(secret: str, data: bytes) -> bytes:
    return hmac.new(secret.encode(), data, hashlib.sha256).digest()


def detect_scheme(headers: Mapping[str, str]) -> str | None:
    h = {k.lower(): v for k, v in headers.items()}
    if "stripe-signature" in h:
        return "stripe"
    if "typeform-signature" in h:
        return "typeform"
    if "x-shopify-hmac-sha256" in h:
        return "shopify"
    if "x-signature-256" in h or "x-hub-signature-256" in h:
        return "hmac"
    return None


def verify(scheme: str, headers: Mapping[str, str], raw: bytes, secret: str, now: float | None = None) -> bool:
    if not secret:
        return False
    h = {k.lower(): v for k, v in headers.items()}
    if scheme == "stripe":
        return verify_stripe(h.get("stripe-signature", ""), raw, secret, now)
    if scheme == "typeform":
        sig = h.get("typeform-signature", "").removeprefix("sha256=")
        return hmac.compare_digest(sig, base64.b64encode(_hmac(secret, raw)).decode())
    if scheme == "shopify":
        return hmac.compare_digest(h.get("x-shopify-hmac-sha256", ""), base64.b64encode(_hmac(secret, raw)).decode())
    if scheme == "hmac":
        sig = (h.get("x-signature-256") or h.get("x-hub-signature-256") or "").removeprefix("sha256=")
        return len(sig) == 64 and hmac.compare_digest(sig, _hmac(secret, raw).hex())
    return False


def verify_stripe(header: str, raw: bytes, secret: str, now: float | None = None) -> bool:
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        ts = int(parts.get("t", [""])[0])
    except ValueError:
        return False
    if abs((now or time.time()) - ts) > STRIPE_TOLERANCE_S:
        return False
    expected = _hmac(secret, f"{ts}.".encode() + raw).hex()
    return any(hmac.compare_digest(expected, v1) for v1 in parts.get("v1", []))


def sign_stripe(raw: bytes, secret: str, ts: int | None = None) -> str:
    """Builds a Stripe-Signature header (tests, local simulations)."""
    ts = ts or int(time.time())
    return f"t={ts},v1={_hmac(secret, f'{ts}.'.encode() + raw).hex()}"


def sign_hex(raw: bytes, secret: str) -> str:
    return "sha256=" + _hmac(secret, raw).hex()


def looks_valid(scheme: str, headers: Mapping[str, str]) -> bool:
    """Cheap shape check before any DB lookup (junk never wakes the database)."""
    h = {k.lower(): v for k, v in headers.items()}
    if scheme == "stripe":
        v = h.get("stripe-signature", "")
        return "t=" in v and "v1=" in v
    if scheme in ("typeform", "shopify"):
        v = (h.get("typeform-signature") or h.get("x-shopify-hmac-sha256") or "").removeprefix("sha256=")
        return 40 <= len(v) <= 64
    if scheme == "hmac":
        v = (h.get("x-signature-256") or h.get("x-hub-signature-256") or "").removeprefix("sha256=")
        return len(v) == 64 and all(c in "0123456789abcdefABCDEF" for c in v)
    return False
