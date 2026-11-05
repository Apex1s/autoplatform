export const enc = new TextEncoder();

export class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export class PermanentError extends Error {}

export function json(data: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

export const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export function toHex(buf: ArrayBuffer | Uint8Array): string {
  const b = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

export function randomBytes(n: number): Uint8Array {
  const b = new Uint8Array(n);
  crypto.getRandomValues(b);
  return b;
}

export const randomHex = (n: number) => toHex(randomBytes(n));

/** URL-safe base64 without padding, like Python's secrets.token_urlsafe. */
export function randomToken(n: number): string {
  let s = "";
  for (const x of randomBytes(n)) s += String.fromCharCode(x);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export const newId = () => crypto.randomUUID().replace(/-/g, ""); // same shape as uuid4().hex

export async function sha256Hex(s: string): Promise<string> {
  return toHex(await crypto.subtle.digest("SHA-256", enc.encode(s)));
}

export async function hmacSha256Hex(secret: string, data: Uint8Array): Promise<string> {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return toHex(await crypto.subtle.sign("HMAC", key, data));
}

/** Constant-time comparison (length differences leak only the length). */
export function safeEqual(a: string, b: string): boolean {
  const x = enc.encode(a), y = enc.encode(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

export function bearer(req: Request): string {
  const h = req.headers.get("authorization") ?? "";
  return h.toLowerCase().startsWith("bearer ") ? h.slice(7).trim() : "";
}

export const iso = (d: unknown): string | null =>
  d == null ? null : d instanceof Date ? d.toISOString() : String(d);

/** Counts outbound calls so one invocation never exceeds the Workers Free subrequest limit. */
export class Budget {
  used = 0;
  constructor(public max: number) {}
  take(n = 1): void {
    if (this.used + n > this.max) throw new BudgetExceeded(`subrequest budget of ${this.max} exhausted`);
    this.used += n;
  }
  get left(): number {
    return this.max - this.used;
  }
}

export class BudgetExceeded extends Error {}
