// Groq <-> Gemini router over their OpenAI-compatible APIs. Same policy as core/llm_router.py:
//   429 / timeout         -> fail over immediately (429 also sets a shared cooldown, honouring Retry-After)
//   5xx / network / junk  -> retry same provider with jittered exponential backoff, then fail over
//   other 4xx             -> fail over immediately
// Cooldowns live in Redis so every isolate skips a rate-limited provider, not just this one.
import type { UsageRecord } from "./repo";
import { sleep } from "./util";

export interface Provider {
  name: string;
  baseUrl: string;
  apiKey: string;
  model: string;
  timeoutMs: number;
}

export type Kind = "rate_limit" | "timeout" | "server" | "network" | "client" | "bad_response";

export class ProviderError extends Error {
  constructor(public provider: string, public kind: Kind, message: string,
              public status: number | null = null, public retryAfterS: number | null = null) {
    super(`[${provider}] ${kind}: ${message}`);
  }
}

export class AllProvidersFailed extends Error {
  constructor(public errors: ProviderError[]) {
    super(`all LLM providers failed: ${errors.slice(-6).map((e) => e.message).join("; ") || "no providers configured"}`);
  }
}

export interface Attempt {
  provider: string; model: string; success: boolean; status_code: number | null; error_kind: Kind | null;
  prompt_tokens: number; completion_tokens: number; latency_ms: number; round: number; retry: number;
}

export interface LLMResult {
  text: string; provider: string; model: string; prompt_tokens: number; completion_tokens: number;
  total_tokens: number; latency_ms: number; attempts: Attempt[];
}

export interface CooldownStore {
  get(providers: string[]): Promise<Record<string, number>>; // provider -> unix seconds until usable
  set(provider: string, seconds: number): Promise<void>;
}

export interface RouterOptions {
  maxRetries?: number;
  maxRounds?: number;
  backoffBaseMs?: number;
  backoffMaxMs?: number;
  defaultCooldownS?: number;
  fetchFn?: typeof fetch;
  sleepFn?: (ms: number) => Promise<void>;
  cooldowns?: CooldownStore;
  now?: () => number; // unix seconds
}

export type Message = { role: string; content: string };

export function parseRetryAfter(res: Response, body: string): number | null {
  const h = res.headers.get("retry-after");
  if (h) {
    const n = Number(h);
    if (!Number.isNaN(n)) return Math.max(0, n);
    const t = Date.parse(h);
    if (!Number.isNaN(t)) return Math.max(0, (t - Date.now()) / 1000);
  }
  const m = /"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"/.exec(body); // Gemini puts it in the error body
  return m ? Number(m[1]) : null;
}

const estimateTokens = (s: string) => Math.max(1, Math.floor(s.length / 4));

export class LLMRouter {
  readonly providers: Provider[];
  private o: Required<Omit<RouterOptions, "cooldowns">> & { cooldowns?: CooldownStore };
  private localCooldown: Record<string, number> = {};

  constructor(providers: Provider[], opts: RouterOptions = {}) {
    this.providers = providers.filter((p) => p.apiKey);
    this.o = {
      maxRetries: opts.maxRetries ?? 1,
      maxRounds: Math.max(1, opts.maxRounds ?? 2),
      backoffBaseMs: opts.backoffBaseMs ?? 500,
      backoffMaxMs: opts.backoffMaxMs ?? 4000,
      defaultCooldownS: opts.defaultCooldownS ?? 30,
      fetchFn: opts.fetchFn ?? ((...a) => fetch(...a)),
      sleepFn: opts.sleepFn ?? sleep,
      now: opts.now ?? (() => Date.now() / 1000),
      cooldowns: opts.cooldowns,
    };
  }

  get configured(): string[] {
    return this.providers.map((p) => p.name);
  }

  private backoff(n: number): number {
    const d = Math.min(this.o.backoffMaxMs, this.o.backoffBaseMs * 2 ** n);
    return d * (0.5 + Math.random() / 2);
  }

  private async call(p: Provider, messages: Message[], maxTokens: number, temperature: number) {
    const started = Date.now();
    let res: Response;
    try {
      res = await this.o.fetchFn(`${p.baseUrl.replace(/\/+$/, "")}/chat/completions`, {
        method: "POST",
        headers: { authorization: `Bearer ${p.apiKey}`, "content-type": "application/json" },
        body: JSON.stringify({ model: p.model, messages, max_tokens: maxTokens, temperature }),
        signal: AbortSignal.timeout(p.timeoutMs),
      });
    } catch (e) {
      const err = e as Error;
      if (err.name === "TimeoutError" || err.name === "AbortError") throw new ProviderError(p.name, "timeout", err.name);
      throw new ProviderError(p.name, "network", `${err.name}: ${err.message}`);
    }
    const body = await res.text();
    const code = res.status;
    if (code === 429) throw new ProviderError(p.name, "rate_limit", body.slice(0, 200), code, parseRetryAfter(res, body));
    if (code === 408 || code === 504) throw new ProviderError(p.name, "timeout", `HTTP ${code}`, code);
    if (code >= 500) throw new ProviderError(p.name, "server", `HTTP ${code}: ${body.slice(0, 200)}`, code);
    if (code >= 400) throw new ProviderError(p.name, "client", `HTTP ${code}: ${body.slice(0, 200)}`, code);
    let data: any;
    try {
      data = JSON.parse(body);
    } catch {
      throw new ProviderError(p.name, "bad_response", `unparseable body: ${body.slice(0, 200)}`, code);
    }
    const text = data?.choices?.[0]?.message?.content;
    if (typeof text !== "string") throw new ProviderError(p.name, "bad_response", `no content: ${body.slice(0, 200)}`, code);
    return {
      text,
      model: (data.model as string) || p.model,
      prompt_tokens: Number(data.usage?.prompt_tokens) || estimateTokens(JSON.stringify(messages)),
      completion_tokens: Number(data.usage?.completion_tokens) || estimateTokens(text),
      latency_ms: Date.now() - started,
    };
  }

  async complete(messages: Message[], o: { maxTokens?: number; temperature?: number; only?: string[] } = {}): Promise<LLMResult> {
    const pool = this.providers.filter((p) => !o.only || o.only.includes(p.name));
    if (!pool.length) throw new AllProvidersFailed([]);
    const errors: ProviderError[] = [];
    const attempts: Attempt[] = [];
    let shared: Record<string, number> = {};
    try {
      shared = this.o.cooldowns ? await this.o.cooldowns.get(pool.map((p) => p.name)) : {};
    } catch {
      /* cooldown store unavailable: fall back to local knowledge */
    }
    const until = (name: string) => Math.max(shared[name] ?? 0, this.localCooldown[name] ?? 0);

    for (let round = 0; round < this.o.maxRounds; round++) {
      let ready = pool.filter((p) => until(p.name) <= this.o.now());
      if (!ready.length) {
        // everyone is rate-limited: wait for the earliest cooldown (capped), then try it
        const sorted = [...pool].sort((a, b) => until(a.name) - until(b.name));
        await this.o.sleepFn(Math.min(Math.max(0, until(sorted[0].name) - this.o.now()) * 1000, this.o.backoffMaxMs));
        ready = sorted;
      }
      for (const p of ready) {
        for (let retry = 0; retry <= this.o.maxRetries; retry++) {
          try {
            const r = await this.call(p, messages, o.maxTokens ?? 512, o.temperature ?? 0.2);
            attempts.push({ provider: p.name, model: r.model, success: true, status_code: 200, error_kind: null,
              prompt_tokens: r.prompt_tokens, completion_tokens: r.completion_tokens, latency_ms: r.latency_ms, round, retry });
            return { ...r, provider: p.name, total_tokens: r.prompt_tokens + r.completion_tokens, attempts };
          } catch (e) {
            if (!(e instanceof ProviderError)) throw e;
            errors.push(e);
            attempts.push({ provider: p.name, model: p.model, success: false, status_code: e.status, error_kind: e.kind,
              prompt_tokens: 0, completion_tokens: 0, latency_ms: 0, round, retry });
            if (e.kind === "rate_limit") {
              const cd = e.retryAfterS ?? this.o.defaultCooldownS;
              this.localCooldown[p.name] = this.o.now() + cd;
              try {
                await this.o.cooldowns?.set(p.name, cd);
              } catch {
                /* best effort */
              }
              break; // fail over immediately
            }
            if (e.kind === "timeout") break;
            if ((e.kind === "server" || e.kind === "network" || e.kind === "bad_response") && retry < this.o.maxRetries) {
              await this.o.sleepFn(this.backoff(retry));
              continue;
            }
            break;
          }
        }
      }
      if (round < this.o.maxRounds - 1) await this.o.sleepFn(this.backoff(round));
    }
    const err = new AllProvidersFailed(errors);
    (err as any).attempts = attempts;
    throw err;
  }
}

export function attemptsToUsage(attempts: Attempt[], clientId: string | null, jobId: string | null): UsageRecord[] {
  return attempts.map((a) => ({
    provider: a.provider, model: a.model, client_id: clientId, job_id: jobId, success: a.success,
    status_code: a.status_code, error_kind: a.error_kind, prompt_tokens: a.prompt_tokens,
    completion_tokens: a.completion_tokens, latency_ms: a.latency_ms,
  }));
}
