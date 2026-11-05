import { describe, expect, it } from "vitest";
import { configWarnings, loadConfig, upstashRestFromUrl } from "../src/env";
import { assertPublicHttps } from "../src/handlers";
import { pairs, UpstashRest } from "../src/redis";
import { Budget, BudgetExceeded, hmacSha256Hex, PermanentError, safeEqual, sha256Hex } from "../src/util";

describe("config", () => {
  it("derives Upstash REST credentials from the TCP URL", () => {
    expect(upstashRestFromUrl("rediss://default:AbC123@direct-mongoose-1.upstash.io:6379"))
      .toEqual({ url: "https://direct-mongoose-1.upstash.io", token: "AbC123" });
    expect(upstashRestFromUrl("redis://localhost:6379")).toBeNull();
    const cfg = loadConfig({ UPSTASH_REDIS_URL: "rediss://default:tok@x.upstash.io:6379" });
    expect([cfg.redisRestUrl, cfg.redisRestToken]).toEqual(["https://x.upstash.io", "tok"]);
  });

  it("explicit REST settings win and defaults are production-safe", () => {
    const cfg = loadConfig({ UPSTASH_REDIS_URL: "rediss://default:a@x.upstash.io:6379",
      UPSTASH_REDIS_REST_URL: "https://y.upstash.io/", UPSTASH_REDIS_REST_TOKEN: "b" });
    expect([cfg.redisRestUrl, cfg.redisRestToken, cfg.production]).toEqual(["https://y.upstash.io", "b", true]);
    expect(cfg.cronToken).toBe("");
    expect(loadConfig({ ADMIN_TOKEN: "x" }).cronToken).toBe("x"); // falls back to admin token
  });

  it("flags a misconfigured deployment", () => {
    const w = configWarnings(loadConfig({}));
    expect(w.join("|")).toMatch(/DATABASE_URL.*UPSTASH.*no LLM key.*ADMIN_TOKEN/);
    expect(configWarnings(loadConfig({ DATABASE_URL: "x", UPSTASH_REDIS_URL: "rediss://default:a@x.upstash.io:6379",
      GROQ_API_KEY: "g", ADMIN_TOKEN: "a" }))).toEqual(["only one LLM provider configured: no failover"]);
  });
});

describe("crypto helpers", () => {
  it("sha256 matches Python hashlib (API keys work in both runtimes)", async () => {
    expect(await sha256Hex("abc")).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  });
  it("hmac-sha256 matches RFC 4231 test case 2", async () => {
    expect(await hmacSha256Hex("Jefe", new TextEncoder().encode("what do ya want for nothing?")))
      .toBe("5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843");
  });
  it("safeEqual", () => {
    expect(safeEqual("abc", "abc")).toBe(true);
    expect(safeEqual("abc", "abd")).toBe(false);
    expect(safeEqual("abc", "abcd")).toBe(false);
    expect(safeEqual("", "")).toBe(true);
  });
  it("budget refuses work beyond the subrequest limit", () => {
    const b = new Budget(2);
    b.take();
    b.take();
    expect(() => b.take()).toThrow(BudgetExceeded);
    expect(b.left).toBe(0);
  });
});

describe("SSRF guard", () => {
  it.each(["http://example.com", "https://user:pw@example.com/", "https://localhost/", "https://127.0.0.1/",
    "https://10.0.0.5/", "https://169.254.169.254/latest", "https://[::1]/", "https://[fd00::1]/",
    "https://[::ffff:127.0.0.1]/", "https://svc.internal/", "ftp://x", "not a url"])("rejects %s", (u) => {
    expect(() => assertPublicHttps(u)).toThrow(PermanentError);
  });
  it.each(["https://api.github.com/repos", "https://fcbarcelona.com/", "https://fd.example.org/", "https://8.8.8.8/"])(
    "allows %s", (u) => {
      expect(() => assertPublicHttps(u)).not.toThrow();
    });
});

describe("Upstash REST client", () => {
  const mk = (handler: (body: any, url: string) => Response) => {
    const seen: any[] = [];
    const f: typeof fetch = async (input, init) => {
      const body = JSON.parse(String(init?.body));
      seen.push({ url: String(input), body, auth: new Headers(init?.headers).get("authorization") });
      return handler(body, String(input));
    };
    return { client: new UpstashRest("https://x.upstash.io", "tok", f), seen };
  };

  it("sends string-only commands with the bearer token", async () => {
    const { client, seen } = mk(() => Response.json({ result: 1 }));
    expect(await client.cmd(["ZADD", "z", 10.5, "m"])).toBe(1);
    expect(seen[0]).toEqual({ url: "https://x.upstash.io", body: ["ZADD", "z", "10.5", "m"], auth: "Bearer tok" });
  });

  it("pipelines and surfaces per-command errors", async () => {
    const { client, seen } = mk(() => Response.json([{ result: 3 }, { result: 0 }]));
    expect(await client.pipeline([["LLEN", "a"], ["ZCARD", "b"]])).toEqual([3, 0]);
    expect(seen[0].url).toBe("https://x.upstash.io/pipeline");
    const bad = mk(() => Response.json([{ result: 1 }, { error: "WRONGTYPE" }])).client;
    await expect(bad.pipeline([["GET", "a"], ["LPUSH", "a", "x"]])).rejects.toThrow("WRONGTYPE");
  });

  it("maps error envelopes and non-JSON gateway errors", async () => {
    await expect(mk(() => Response.json({ error: "WRONGPASS" }, { status: 401 })).client.cmd(["PING"])).rejects.toThrow("WRONGPASS");
    await expect(mk(() => new Response("<html>502</html>", { status: 502 })).client.cmd(["PING"])).rejects.toThrow(/HTTP 502/);
  });
});

describe("redis reply shapes", () => {
  it("normalises flat (Upstash), paired (RESP3) and object replies", () => {
    expect(pairs(["a", "1", "b", "2"])).toEqual([["a", "1"], ["b", "2"]]);
    expect(pairs([["a", "1"], ["b", 2]])).toEqual([["a", "1"], ["b", "2"]]);
    expect(pairs({ a: 1 })).toEqual([["a", "1"]]);
    expect(pairs(null)).toEqual([]);
  });
});
