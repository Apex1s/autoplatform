import { describe, expect, it } from "vitest";
import { AllProvidersFailed, type CooldownStore, LLMRouter, type Provider } from "../src/llm";
import { FakeLLM } from "./helpers";

const P = (name: string): Provider => ({ name, baseUrl: `https://api.${name}.test/v1`, apiKey: "k", model: `${name}-model`, timeoutMs: 5000 });
const MSG = [{ role: "user", content: "hi" }];

function memStore(): CooldownStore & { data: Record<string, number> } {
  const data: Record<string, number> = {};
  return { data, get: async (ps) => Object.fromEntries(ps.filter((p) => data[p]).map((p) => [p, data[p]])),
    set: async (p, s) => void (data[p] = Date.now() / 1000 + s) };
}

function router(llm: FakeLLM, o: Partial<ConstructorParameters<typeof LLMRouter>[1]> = {}) {
  const sleeps: number[] = [];
  const r = new LLMRouter([P("groq"), P("gemini")], { fetchFn: llm.fetch, sleepFn: async (ms) => void sleeps.push(ms), ...o });
  return { r, sleeps };
}

describe("LLM failover", () => {
  it("429 fails over to Gemini immediately and stores a shared cooldown from Retry-After", async () => {
    const llm = new FakeLLM();
    llm.script("groq", [429, { "retry-after": "42" }, undefined]);
    const store = memStore();
    const { r, sleeps } = router(llm, { cooldowns: store });
    const res = await r.complete(MSG);
    expect([res.provider, res.text]).toEqual(["gemini", "hello from gemini"]);
    expect(llm.calls).toEqual(["groq", "gemini"]);
    expect(sleeps).toEqual([]);
    expect(res.attempts.map((a) => a.error_kind)).toEqual(["rate_limit", null]);
    expect(store.data.groq - Date.now() / 1000).toBeGreaterThan(40);
  });

  it("a fresh isolate honours the shared cooldown (never re-hits the rate-limited provider)", async () => {
    const llm = new FakeLLM();
    const store = memStore();
    store.data.groq = Date.now() / 1000 + 60;
    const res = await router(llm, { cooldowns: store }).r.complete(MSG);
    expect(res.provider).toBe("gemini");
    expect(llm.callsTo("groq")).toBe(0);
  });

  it("reads Gemini's retryDelay from the error body", async () => {
    const llm = new FakeLLM();
    llm.script("gemini", [429, {}, { error: { code: 429, details: [{ retryDelay: "17s" }] } }]);
    const store = memStore();
    const r = new LLMRouter([P("gemini"), P("groq")], { fetchFn: llm.fetch, sleepFn: async () => {}, cooldowns: store });
    expect((await r.complete(MSG)).provider).toBe("groq");
    expect(Math.round(store.data.gemini - Date.now() / 1000)).toBe(17);
  });

  it("timeouts fail over immediately", async () => {
    const llm = new FakeLLM();
    llm.script("groq", ["timeout"]);
    const { r, sleeps } = router(llm);
    expect((await r.complete(MSG)).provider).toBe("gemini");
    expect(sleeps).toEqual([]);
  });

  it("5xx retries the same provider with exponential backoff", async () => {
    const llm = new FakeLLM();
    llm.script("groq", 503, 502);
    const { r, sleeps } = router(llm, { maxRetries: 2, backoffBaseMs: 1000, backoffMaxMs: 10000 });
    expect((await r.complete(MSG)).provider).toBe("groq");
    expect(llm.calls).toEqual(["groq", "groq", "groq"]);
    expect(sleeps[0]).toBeGreaterThanOrEqual(500);
    expect(sleeps[0]).toBeLessThanOrEqual(1000);
    expect(sleeps[1]).toBeGreaterThanOrEqual(1000);
    expect(sleeps[1]).toBeLessThanOrEqual(2000);
  });

  it("auth errors fail over without retrying; malformed bodies are retried", async () => {
    const llm = new FakeLLM();
    llm.script("groq", 401);
    expect((await router(llm).r.complete(MSG)).provider).toBe("gemini");
    expect(llm.calls).toEqual(["groq", "gemini"]);
    const llm2 = new FakeLLM();
    llm2.script("groq", [200, {}, { nope: true }]);
    expect((await router(llm2).r.complete(MSG)).provider).toBe("groq");
    expect(llm2.calls).toEqual(["groq", "groq"]);
  });

  it("everything rate-limited -> AllProvidersFailed carrying every attempt", async () => {
    const llm = new FakeLLM();
    llm.script("groq", ...Array(5).fill([429, { "retry-after": "1" }, undefined]));
    llm.script("gemini", ...Array(5).fill([429, { "retry-after": "1" }, undefined]));
    const err = await router(llm, { maxRounds: 2 }).r.complete(MSG).catch((e) => e);
    expect(err).toBeInstanceOf(AllProvidersFailed);
    expect(err.errors).toHaveLength(4);
    expect((err as any).attempts).toHaveLength(4);
  });

  it("token usage is reported per attempt", async () => {
    const res = await router(new FakeLLM()).r.complete(MSG);
    expect([res.prompt_tokens, res.completion_tokens, res.total_tokens]).toEqual([11, 7, 18]);
  });
});
