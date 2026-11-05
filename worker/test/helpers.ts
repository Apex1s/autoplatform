import Redis from "ioredis";
import pg from "pg";
import type { Db } from "../src/db";
import { makeDeps } from "../src/deps";
import type { Env } from "../src/env";
import type { RedisCmd } from "../src/redis";

export const TEST_DATABASE_URL = process.env.TEST_DATABASE_URL ?? "";
export const TEST_REDIS_URL = process.env.TEST_REDIS_URL ?? "";
export const HAVE_INFRA = Boolean(TEST_DATABASE_URL && TEST_REDIS_URL);

export function pgDb(pool: pg.Pool): Db {
  return {
    async query(text, params = []) {
      return (await pool.query(text, params as any[])).rows;
    },
    async transaction(stmts) {
      const c = await pool.connect();
      try {
        await c.query("BEGIN");
        const out = [];
        for (const s of stmts) out.push((await c.query(s.text, (s.params ?? []) as any[])).rows);
        await c.query("COMMIT");
        return out;
      } catch (e) {
        await c.query("ROLLBACK");
        throw e;
      } finally {
        c.release();
      }
    },
  };
}

export function ioRedis(client: Redis): RedisCmd {
  return {
    cmd: (args) => client.call(String(args[0]), ...args.slice(1).map(String)) as Promise<any>,
    async pipeline(cmds) {
      const res = await client.pipeline(cmds.map((c) => c.map(String)) as any).exec();
      return (res ?? []).map(([err, v]) => {
        if (err) throw err;
        return v;
      });
    },
  };
}

/** Scripted stand-in for Groq/Gemini. script("groq", 429, 200) etc.; default 200. */
export class FakeLLM {
  scripts: Record<string, (number | ["timeout"] | [number, Record<string, string>, unknown])[]> = { groq: [], gemini: [] };
  calls: string[] = [];
  script(p: "groq" | "gemini", ...items: FakeLLM["scripts"]["groq"]) {
    this.scripts[p].push(...items);
  }
  callsTo(p: string) {
    return this.calls.filter((c) => c === p).length;
  }
  fetch: typeof fetch = async (input, init) => {
    const url = String(input);
    const p = url.includes("groq") ? "groq" : "gemini";
    this.calls.push(p);
    const item = this.scripts[p].shift() ?? 200;
    if (Array.isArray(item) && item[0] === "timeout") {
      const e = new Error("The operation was aborted due to timeout");
      e.name = "TimeoutError";
      throw e;
    }
    const [status, headers, body] = typeof item === "number" ? [item, {}, undefined] : (item as [number, Record<string, string>, unknown]);
    const req = JSON.parse(String(init?.body ?? "{}"));
    const payload = body ?? (status === 200
      ? { model: req.model, usage: { prompt_tokens: 11, completion_tokens: 7 },
          choices: [{ message: { role: "assistant", content: `hello from ${p}` } }] }
      : { error: { message: `simulated ${status}` } });
    return new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json", ...headers } });
  };
}

export class TestCtx {
  promises: Promise<unknown>[] = [];
  waitUntil = (p: Promise<unknown>) => {
    this.promises.push(p);
  };
  async settle() {
    while (this.promises.length) await this.promises.shift();
  }
}

export const baseEnv: Env = {
  DATABASE_URL: "postgres://test",
  UPSTASH_REDIS_REST_URL: "https://fake.upstash.io",
  UPSTASH_REDIS_REST_TOKEN: "t",
  GROQ_API_KEY: "gk",
  GEMINI_API_KEY: "gm",
  ADMIN_TOKEN: "test-admin",
  CRON_TOKEN: "test-cron",
  APP_ENV: "development",
  JOB_RETRY_BASE_SECONDS: "0.05",
};

export interface Infra {
  pool: pg.Pool;
  redis: Redis;
  llm: FakeLLM;
  prefix: string;
  deps: (env?: Env, budget?: number) => ReturnType<typeof makeDeps>;
  close: () => Promise<void>;
}

export async function setupInfra(): Promise<Infra> {
  const pool = new pg.Pool({ connectionString: TEST_DATABASE_URL, max: 4 });
  await pool.query(`DROP TABLE IF EXISTS execution_logs, api_usage, jobs, worker_health, clients, schema_migrations CASCADE`);
  const redis = new Redis(TEST_REDIS_URL);
  const prefix = `wt${process.pid}${Math.floor(Math.random() * 1e6)}`;
  const llm = new FakeLLM();
  const db = pgDb(pool);
  const rc = ioRedis(redis);
  return {
    pool, redis, llm, prefix,
    deps: (env = {}, budget?: number) =>
      makeDeps({ ...baseEnv, QUEUE_PREFIX: prefix, ...env }, { db, redis: rc, llmFetch: llm.fetch, sleepFn: async () => {}, budget }),
    async close() {
      const keys = await redis.keys(`${prefix}:*`);
      if (keys.length) await redis.del(...keys);
      redis.disconnect();
      await pool.end();
    },
  };
}

export const req = (method: string, path: string, o: { body?: unknown; raw?: BodyInit; headers?: Record<string, string> } = {}) =>
  new Request(`https://autoplatform.test${path}`, {
    method,
    headers: { ...(o.body !== undefined ? { "content-type": "application/json" } : {}), ...o.headers },
    body: o.raw ?? (o.body !== undefined ? JSON.stringify(o.body) : undefined),
    ...(o.raw instanceof ReadableStream ? { duplex: "half" } : {}),
  } as RequestInit);
