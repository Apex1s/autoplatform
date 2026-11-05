// Per-invocation dependencies. Everything that leaves the isolate goes through one Budget,
// so a request + its follow-up work never exceeds the Workers Free subrequest limit (50).
import { type Db, managedDb, neonDb } from "./db";
import { type Config, type Env, loadConfig } from "./env";
import { LLMRouter, type Provider } from "./llm";
import { Queue, type RedisCmd, UpstashRest, budgeted } from "./redis";
import { Budget } from "./util";

export interface Deps {
  cfg: Config;
  db: Db;
  queue: Queue;
  router: LLMRouter;
  budget: Budget;
  workerId: string;
}

export interface DepOverrides {
  db?: Db;               // raw Db (tests: node-postgres); budget + lazy migration are added here
  redis?: RedisCmd;      // raw Redis (tests: ioredis)
  llmFetch?: typeof fetch;
  sleepFn?: (ms: number) => Promise<void>;
  budget?: number;
}

const notConfigured = (what: string) => async () => {
  throw new Error(`${what} is not configured`);
};

export function makeDeps(env: Env, o: DepOverrides = {}): Deps {
  const cfg = loadConfig(env);
  const budget = new Budget(o.budget ?? cfg.subrequestBudget);
  const rawDb: Db = o.db ?? (cfg.databaseUrl
    ? neonDb(cfg.databaseUrl)
    : { query: notConfigured("DATABASE_URL"), transaction: notConfigured("DATABASE_URL") });
  const rawRedis: RedisCmd = o.redis ?? (cfg.redisRestUrl && cfg.redisRestToken
    ? new UpstashRest(cfg.redisRestUrl, cfg.redisRestToken)
    : { cmd: notConfigured("UPSTASH_REDIS_URL"), pipeline: notConfigured("UPSTASH_REDIS_URL") });
  const queue = new Queue(budgeted(rawRedis, budget), cfg.queuePrefix);
  const baseFetch = o.llmFetch ?? ((...a: Parameters<typeof fetch>) => fetch(...a));
  const llmFetch: typeof fetch = (input, init) => {
    budget.take();
    return baseFetch(input, init);
  };
  const catalog: Record<string, Provider> = {
    groq: { name: "groq", baseUrl: cfg.groqBaseUrl, apiKey: cfg.groqApiKey, model: cfg.groqModel, timeoutMs: cfg.llmTimeoutMs },
    gemini: { name: "gemini", baseUrl: cfg.geminiBaseUrl, apiKey: cfg.geminiApiKey, model: cfg.geminiModel, timeoutMs: cfg.llmTimeoutMs },
  };
  const order = [...cfg.providerOrder.filter((n) => n in catalog), ...Object.keys(catalog)].filter((n, i, a) => a.indexOf(n) === i);
  const router = new LLMRouter(order.map((n) => catalog[n]), {
    maxRetries: cfg.llmMaxRetries,
    maxRounds: cfg.llmMaxRounds,
    defaultCooldownS: cfg.llmDefaultCooldownS,
    fetchFn: llmFetch,
    sleepFn: o.sleepFn,
    cooldowns: { get: (p) => queue.cooldowns(p), set: (p, s) => queue.setCooldown(p, s) },
  });
  return { cfg, db: managedDb(rawDb, budget), queue, router, budget, workerId: "cloudflare-worker" };
}
