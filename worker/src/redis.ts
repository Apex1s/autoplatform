// Upstash Redis over its REST API (HTTPS only: Workers have no raw Redis sockets).
// Same key layout as core/queue.py, so the Python app and this Worker can share a queue.
import { Budget } from "./util";

export type Arg = string | number;

export interface RedisCmd {
  cmd(args: Arg[]): Promise<any>;
  pipeline(cmds: Arg[][]): Promise<any[]>;
}

export class RedisError extends Error {}

export class UpstashRest implements RedisCmd {
  constructor(private url: string, private token: string, private fetchFn: typeof fetch = (...a) => fetch(...a)) {}

  private async post(path: string, body: unknown): Promise<any> {
    let res: Response;
    try {
      res = await this.fetchFn(this.url + path, {
        method: "POST",
        headers: { authorization: `Bearer ${this.token}`, "content-type": "application/json" },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(10_000),
      });
    } catch (e) {
      throw new RedisError(`upstash unreachable: ${(e as Error).message}`);
    }
    const text = await res.text();
    let data: any;
    try {
      data = JSON.parse(text);
    } catch {
      throw new RedisError(`upstash HTTP ${res.status}: ${text.slice(0, 200)}`);
    }
    if (!Array.isArray(data) && data?.error) throw new RedisError(String(data.error));
    if (!res.ok) throw new RedisError(`upstash HTTP ${res.status}: ${text.slice(0, 200)}`);
    return data;
  }

  async cmd(args: Arg[]): Promise<any> {
    return (await this.post("", args.map(String))).result;
  }

  async pipeline(cmds: Arg[][]): Promise<any[]> {
    const out = await this.post("/pipeline", cmds.map((c) => c.map(String)));
    return out.map((r: any) => {
      if (r.error) throw new RedisError(String(r.error));
      return r.result;
    });
  }
}

export function budgeted(inner: RedisCmd, budget: Budget): RedisCmd {
  return {
    cmd: (args) => (budget.take(), inner.cmd(args)),
    pipeline: (cmds) => (budget.take(), inner.pipeline(cmds)),
  };
}

/** Redis replies for ZRANGE WITHSCORES / HGETALL come flat from Upstash (["m","1"]) but as
 *  pairs ([["m","1"]]) or objects from RESP3 clients. Normalise to pairs. */
export function pairs(raw: unknown): [string, string][] {
  if (!raw) return [];
  if (Array.isArray(raw)) {
    if (raw.length && Array.isArray(raw[0])) return raw.map((p: any) => [String(p[0]), String(p[1])]);
    const out: [string, string][] = [];
    for (let i = 0; i + 1 < raw.length; i += 2) out.push([String(raw[i]), String(raw[i + 1])]);
    return out;
  }
  if (raw instanceof Map) return [...raw.entries()].map(([k, v]) => [String(k), String(v)]);
  if (typeof raw === "object") return Object.entries(raw as Record<string, unknown>).map(([k, v]) => [k, String(v)]);
  return [];
}

export class Queue {
  readonly pending: string;
  readonly delayed: string;

  constructor(public r: RedisCmd, public prefix = "ap") {
    this.pending = `${prefix}:pending`;
    this.delayed = `${prefix}:delayed`;
  }

  async ping(): Promise<boolean> {
    return (await this.r.cmd(["PING"])) === "PONG";
  }

  async enqueue(jobId: string, delayS = 0): Promise<void> {
    if (delayS > 0) await this.r.cmd(["ZADD", this.delayed, Date.now() / 1000 + delayS, jobId]);
    else await this.r.cmd(["LPUSH", this.pending, jobId]);
  }

  /** FIFO pop (LPUSH in, RPOP out: same order as the Python worker's BLMOVE RIGHT). */
  async pop(): Promise<string | null> {
    return (await this.r.cmd(["RPOP", this.pending])) ?? null;
  }

  /** Moves due delayed jobs to pending. One ZRANGE; ZREM decides ownership (no duplicates). */
  async promoteDue(limit = 20): Promise<number> {
    const entries = pairs(await this.r.cmd(["ZRANGE", this.delayed, 0, limit - 1, "WITHSCORES"]));
    const now = Date.now() / 1000;
    let moved = 0;
    for (const [member, score] of entries) {
      if (Number(score) > now) break; // sorted by score
      if (Number(await this.r.cmd(["ZREM", this.delayed, member])) === 1) {
        await this.r.cmd(["LPUSH", this.pending, member]);
        moved++;
      }
    }
    return moved;
  }

  async depth(): Promise<{ pending: number; delayed: number }> {
    const [p, d] = await this.r.pipeline([["LLEN", this.pending], ["ZCARD", this.delayed]]);
    return { pending: Number(p), delayed: Number(d) };
  }

  /** null if the key is new (stores jobId), else the job id stored first. */
  async claimIdempotency(key: string, jobId: string, ttlS = 86400): Promise<string | null> {
    const k = `${this.prefix}:idem:${key}`;
    if ((await this.r.cmd(["SET", k, jobId, "NX", "EX", ttlS])) === "OK") return null;
    return await this.r.cmd(["GET", k]);
  }

  // --- quotas, rate limits, one-time secrets (same keys as core/queue.py)
  private quotaKey(clientId: string): string {
    const d = new Date();
    return `${this.prefix}:quota:${clientId}:${d.getUTCFullYear()}${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
  }

  /** Month-to-date job counter: INCR + TTL in one round trip. */
  async quotaIncr(clientId: string): Promise<number> {
    const k = this.quotaKey(clientId);
    const [n] = await this.r.pipeline([["INCR", k], ["EXPIRE", k, 40 * 86400]]);
    return Number(n);
  }

  async quotaDecr(clientId: string): Promise<void> {
    await this.r.cmd(["DECR", this.quotaKey(clientId)]);
  }

  async quotaGet(clientId: string): Promise<number> {
    return Number((await this.r.cmd(["GET", this.quotaKey(clientId)])) ?? 0);
  }

  async overRate(key: string, windowS: number, limit: number): Promise<boolean> {
    const k = `${this.prefix}:rl:${key}`;
    const [n, ttl] = await this.r.pipeline([["INCR", k], ["TTL", k]]);
    if (Number(ttl) < 0) await this.r.cmd(["EXPIRE", k, windowS]);
    return Number(n) > limit;
  }

  /** Several fixed-window counters in ONE round trip; returns which keys are over their limit. */
  async overRates(limits: { key: string; windowS: number; limit: number }[]): Promise<boolean[]> {
    const keys = limits.map((l) => `${this.prefix}:rl:${l.key}`);
    const res = await this.r.pipeline(keys.flatMap((k) => [["INCR", k], ["TTL", k]]));
    const fix = limits.flatMap((l, i) => (Number(res[i * 2 + 1]) < 0 ? [["EXPIRE", keys[i], l.windowS]] : []));
    if (fix.length) await this.r.pipeline(fix);
    return limits.map((l, i) => Number(res[i * 2]) > l.limit);
  }

  async putSecret(key: string, value: string, ttlS: number): Promise<void> {
    await this.r.cmd(["SET", `${this.prefix}:secret:${key}`, value, "EX", ttlS]);
  }

  async takeSecret(key: string): Promise<string | null> {
    return (await this.r.cmd(["GETDEL", `${this.prefix}:secret:${key}`])) ?? null;
  }

  async peekSecret(key: string): Promise<string | null> {
    return (await this.r.cmd(["GET", `${this.prefix}:secret:${key}`])) ?? null;
  }

  async releaseIdempotency(key: string): Promise<void> {
    await this.r.cmd(["DEL", `${this.prefix}:idem:${key}`]);
  }

  /** LPUSH many ids in one command (bulk-created delivery / report jobs). */
  async enqueueMany(ids: string[]): Promise<void> {
    if (ids.length) await this.r.cmd(["LPUSH", this.pending, ...ids]);
  }

  // --- scheduler liveness (stands in for worker heartbeats on a serverless runtime)
  async markCronRun(info: Record<string, unknown>): Promise<void> {
    await this.r.cmd(["SET", `${this.prefix}:cron:last`, JSON.stringify({ ...info, ts: Date.now() / 1000 }), "EX", 86400]);
  }

  async lastCronRun(): Promise<Record<string, any> | null> {
    const raw = await this.r.cmd(["GET", `${this.prefix}:cron:last`]);
    return raw ? JSON.parse(raw) : null;
  }

  // --- LLM provider cooldowns shared by every isolate (only written on a 429)
  async cooldowns(providers: string[]): Promise<Record<string, number>> {
    if (!providers.length) return {};
    const keys = providers.map((p) => `${this.prefix}:llm:cooldown:${p}`);
    const vals: (string | null)[] = await this.r.cmd(["MGET", ...keys]);
    const out: Record<string, number> = {};
    providers.forEach((p, i) => {
      if (vals?.[i]) out[p] = Number(vals[i]);
    });
    return out;
  }

  async setCooldown(provider: string, seconds: number): Promise<void> {
    const until = Date.now() / 1000 + seconds;
    await this.r.cmd(["SET", `${this.prefix}:llm:cooldown:${provider}`, until, "EX", Math.max(1, Math.ceil(seconds))]);
  }

  // --- counters for /metrics without touching Postgres
  async incr(field: string): Promise<void> {
    await this.r.cmd(["HINCRBY", `${this.prefix}:stats`, field, 1]);
  }

  async stats(): Promise<Record<string, number>> {
    return Object.fromEntries(pairs(await this.r.cmd(["HGETALL", `${this.prefix}:stats`])).map(([k, v]) => [k, Number(v)]));
  }
}
