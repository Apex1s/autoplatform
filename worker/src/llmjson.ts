// Strict-JSON LLM calls: JSON mode + schema validation + one repair round (mirrors workers/llm_json.py).
// Schema subset: type (incl. ["string","null"]), required, properties, items, enum, minimum, maximum.
// Unambiguous coercions ("87" -> 87, 87.0 -> 87, "HOT" -> "hot"), numbers clamped to their range.
import type { JobCtx } from "./handlers";
import { AllProvidersFailed, attemptsToUsage, type Attempt, type Message } from "./llm";

export interface Schema {
  type?: string | string[];
  required?: string[];
  properties?: Record<string, Schema>;
  items?: Schema;
  enum?: unknown[];
  minimum?: number;
  maximum?: number;
  description?: string;
}

export class SchemaError extends Error {}

const types = (s: Schema): string[] => (Array.isArray(s.type) ? s.type : s.type ? [s.type] : []);
const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

export function coerce(schema: Schema, value: unknown, path = "$"): any {
  const t = types(schema);
  if (value === null || value === undefined) {
    if (!t.length || t.includes("null")) return null;
    throw new SchemaError(`${path}: required value is null`);
  }
  if (schema.enum) {
    if (typeof value === "string") {
      const m = schema.enum.find((e) => typeof e === "string" && e.toLowerCase() === value.trim().toLowerCase());
      if (m !== undefined) return m;
    }
    if (schema.enum.includes(value)) return value;
    throw new SchemaError(`${path}: ${JSON.stringify(value)} not in ${JSON.stringify(schema.enum)}`);
  }
  if (t.includes("object") && isObj(value)) {
    const out: Record<string, unknown> = { ...value };
    const props = schema.properties ?? {};
    for (const key of schema.required ?? []) {
      if (!(key in out)) {
        if (types(props[key] ?? {}).includes("null")) out[key] = null;
        else throw new SchemaError(`${path}.${key}: missing`);
      }
    }
    for (const [key, sub] of Object.entries(props)) if (key in out) out[key] = coerce(sub, out[key], `${path}.${key}`);
    return out;
  }
  if (t.includes("array") && Array.isArray(value)) return value.map((v, i) => coerce(schema.items ?? {}, v, `${path}[${i}]`));
  if ((t.includes("integer") || t.includes("number")) && typeof value !== "boolean") {
    let num: number | null = typeof value === "number" ? value : null;
    if (typeof value === "string") {
      const n = Number(value.trim().replace(/%$/, ""));
      num = value.trim() !== "" && !Number.isNaN(n) ? n : null;
    }
    if (num !== null && Number.isFinite(num)) {
      if (schema.minimum !== undefined) num = Math.max(schema.minimum, num);
      if (schema.maximum !== undefined) num = Math.min(schema.maximum, num);
      return t.includes("integer") ? Math.round(num) : num;
    }
  }
  if (t.includes("boolean")) {
    if (typeof value === "boolean") return value;
    if (typeof value === "string" && ["true", "false"].includes(value.trim().toLowerCase())) return value.trim().toLowerCase() === "true";
  }
  if (t.includes("string")) {
    if (typeof value === "string") return value;
    if (typeof value === "number") return String(value);
  }
  throw new SchemaError(`${path}: expected ${t.join("/")}, got ${Array.isArray(value) ? "array" : typeof value}`);
}

export function parseJsonObject(text: string): Record<string, unknown> {
  const t = text.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
  let obj: unknown;
  try {
    obj = JSON.parse(t);
  } catch {
    const s = t.indexOf("{"), e = t.lastIndexOf("}");
    if (s < 0 || e <= s) throw new SchemaError("response is not JSON");
    try {
      obj = JSON.parse(t.slice(s, e + 1));
    } catch {
      throw new SchemaError("response is not JSON");
    }
  }
  if (!isObj(obj)) throw new SchemaError("response is not a JSON object");
  return obj;
}

export function describe(schema: Schema, indent = 0): string {
  const pad = "  ".repeat(indent);
  const t = types(schema);
  if (schema.enum) return schema.enum.map((e) => JSON.stringify(e)).join(" | ");
  if (t.includes("object")) {
    const lines = ["{"];
    for (const [k, sub] of Object.entries(schema.properties ?? {})) {
      lines.push(`${pad}  "${k}": ${describe(sub, indent + 1)}${sub.description ? "  // " + sub.description : ""}`);
    }
    lines.push(pad + "}");
    return lines.join("\n");
  }
  if (t.includes("array")) return `[${describe(schema.items ?? {}, indent)}, ...]`;
  const rng = schema.minimum !== undefined || schema.maximum !== undefined ? ` (${schema.minimum ?? ""}..${schema.maximum ?? ""})` : "";
  return t.join("/") + rng;
}

export interface LlmMeta {
  provider: string; model: string; latency_ms: number; prompt_tokens: number; completion_tokens: number;
  attempts: Partial<Attempt & { simulated: boolean }>[]; failover: boolean; repaired: boolean;
}

/** Returns [validated object, meta]. Throws AllProvidersFailed / SchemaError (both retryable at the job level). */
export async function completeJson(
  ctx: JobCtx,
  o: { system: string; user: string; schema: Schema; maxTokens?: number; providers?: string[] | null; simulateOutage?: string | null },
): Promise<[any, LlmMeta]> {
  let messages: Message[] = [
    { role: "system", content: `${o.system}\n\nRespond with ONE JSON object only, no prose, matching:\n${describe(o.schema)}` },
    { role: "user", content: o.user },
  ];
  const attempts: Attempt[] = [];
  let lastError = "";
  for (let round = 0; round < 2; round++) {
    let res;
    try {
      res = await ctx.deps.router.complete(messages, {
        maxTokens: o.maxTokens ?? 700, temperature: 0.2, only: o.providers?.length ? o.providers : undefined,
        responseFormat: { type: "json_object" }, simulateFailure: round === 0 ? o.simulateOutage ?? undefined : undefined,
      });
    } catch (e) {
      if (e instanceof AllProvidersFailed) {
        const failed: Attempt[] = ((e as any).attempts ?? []).filter((a: Attempt) => !a.simulated);
        ctx.usage.push(...attemptsToUsage(failed, ctx.clientId, ctx.jobId));
      }
      throw e;
    }
    attempts.push(...res.attempts);
    ctx.usage.push(...attemptsToUsage(res.attempts.filter((a) => !a.simulated), ctx.clientId, ctx.jobId));
    try {
      const data = coerce(o.schema, parseJsonObject(res.text));
      return [data, {
        provider: res.provider, model: res.model, latency_ms: res.latency_ms,
        prompt_tokens: attempts.reduce((n, a) => n + a.prompt_tokens, 0),
        completion_tokens: attempts.reduce((n, a) => n + a.completion_tokens, 0),
        attempts: attempts.map((a) => ({ provider: a.provider, success: a.success, error_kind: a.error_kind,
          status_code: a.status_code, latency_ms: a.latency_ms, ...(a.simulated ? { simulated: true } : {}) })),
        failover: attempts.some((a) => !a.success), repaired: round > 0,
      }];
    } catch (e) {
      if (!(e instanceof SchemaError)) throw e;
      lastError = e.message;
      messages = [...messages, { role: "assistant", content: res.text.slice(0, 4000) },
        { role: "user", content: `That was invalid (${lastError}). Reply with the corrected JSON object only.` }];
    }
  }
  throw new SchemaError(`model output failed validation twice: ${lastError}`);
}
