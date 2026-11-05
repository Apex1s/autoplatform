// Inbound webhook signature schemes (mirrors core/signatures.py).
//   stripe    Stripe-Signature: t=<ts>,v1=<hex hmac(secret, "<ts>.<raw>")>   (5 min tolerance)
//   typeform  Typeform-Signature: sha256=<base64 hmac(secret, raw)>
//   shopify   X-Shopify-Hmac-Sha256: <base64 hmac(secret, raw)>
//   hmac      X-Signature-256 / X-Hub-Signature-256: sha256=<hex hmac(secret, raw)>  (GitHub, Meta, ours)
import { enc, safeEqual, toHex } from "./util";

export type Scheme = "stripe" | "typeform" | "shopify" | "hmac";
export const STRIPE_TOLERANCE_S = 300;

async function mac(secret: string, data: Uint8Array): Promise<Uint8Array> {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", key, data));
}

const b64 = (b: Uint8Array) => btoa(String.fromCharCode(...b));
const concat = (a: Uint8Array, b: Uint8Array) => {
  const out = new Uint8Array(a.length + b.length);
  out.set(a);
  out.set(b, a.length);
  return out;
};

export function detectScheme(h: Headers): Scheme | null {
  if (h.has("stripe-signature")) return "stripe";
  if (h.has("typeform-signature")) return "typeform";
  if (h.has("x-shopify-hmac-sha256")) return "shopify";
  if (h.has("x-signature-256") || h.has("x-hub-signature-256")) return "hmac";
  return null;
}

/** Shape check before any DB lookup, so junk never wakes the database. */
export function looksValid(scheme: Scheme, h: Headers): boolean {
  if (scheme === "stripe") {
    const v = h.get("stripe-signature") ?? "";
    return v.includes("t=") && v.includes("v1=");
  }
  if (scheme === "typeform" || scheme === "shopify") {
    const v = (h.get("typeform-signature") ?? h.get("x-shopify-hmac-sha256") ?? "").replace(/^sha256=/, "");
    return v.length >= 40 && v.length <= 64;
  }
  const v = (h.get("x-signature-256") ?? h.get("x-hub-signature-256") ?? "").replace(/^sha256=/, "");
  return /^[0-9a-fA-F]{64}$/.test(v);
}

export async function verifyStripe(header: string, raw: Uint8Array, secret: string, nowS = Date.now() / 1000): Promise<boolean> {
  const parts: Record<string, string[]> = {};
  for (const item of header.split(",")) {
    const i = item.indexOf("=");
    if (i < 0) continue;
    const k = item.slice(0, i).trim();
    (parts[k] ??= []).push(item.slice(i + 1).trim());
  }
  const ts = Number(parts.t?.[0]);
  if (!Number.isInteger(ts) || Math.abs(nowS - ts) > STRIPE_TOLERANCE_S) return false;
  const expected = toHex(await mac(secret, concat(enc.encode(`${ts}.`), raw)));
  return (parts.v1 ?? []).some((v) => safeEqual(v, expected));
}

export async function verify(scheme: Scheme, h: Headers, raw: Uint8Array, secret: string | null | undefined): Promise<boolean> {
  if (!secret) return false;
  if (scheme === "stripe") return verifyStripe(h.get("stripe-signature") ?? "", raw, secret);
  if (scheme === "typeform") return safeEqual((h.get("typeform-signature") ?? "").replace(/^sha256=/, ""), b64(await mac(secret, raw)));
  if (scheme === "shopify") return safeEqual(h.get("x-shopify-hmac-sha256") ?? "", b64(await mac(secret, raw)));
  const sig = (h.get("x-signature-256") ?? h.get("x-hub-signature-256") ?? "").replace(/^sha256=/, "");
  return sig.length === 64 && safeEqual(sig, toHex(await mac(secret, raw)));
}

export async function signStripe(raw: Uint8Array, secret: string, ts = Math.floor(Date.now() / 1000)): Promise<string> {
  return `t=${ts},v1=${toHex(await mac(secret, concat(enc.encode(`${ts}.`), raw)))}`;
}

export async function signHex(raw: Uint8Array, secret: string): Promise<string> {
  return "sha256=" + toHex(await mac(secret, raw));
}

export async function signBase64(raw: Uint8Array, secret: string): Promise<string> {
  return b64(await mac(secret, raw));
}
