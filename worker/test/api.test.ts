// Integration: the real request handlers + runner against Postgres 16 and Redis 7.
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { handle } from "../src/api";
import { MIGRATION_V1_DESC } from "../src/db";
import { drain, runCron } from "../src/runner";
import { hmacSha256Hex } from "../src/util";
import { HAVE_INFRA, type Infra, req, setupInfra, TestCtx } from "./helpers";

const ADMIN = { authorization: "Bearer test-admin" };
const CRON = { authorization: "Bearer test-cron" };

describe.skipIf(!HAVE_INFRA)("worker API (Postgres + Redis)", () => {
  let infra: Infra;
  beforeAll(async () => {
    infra = await setupInfra();
  });
  afterAll(async () => infra?.close());
  beforeEach(async () => {
    infra.llm.calls = [];
    infra.llm.scripts = { groq: [], gemini: [] };
  });

  async function call(r: Request, env = {}) {
    const ctx = new TestCtx();
    const deps = infra.deps(env);
    const res = await handle(r, deps, ctx);
    await ctx.settle(); // run the waitUntil work (inline drain) like Workers would after responding
    const body: any = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
    return { res, body, deps };
  }

  async function newClient(slug: string) {
    const { res, body } = await call(req("POST", "/admin/clients", { body: { slug, name: slug.toUpperCase() }, headers: ADMIN }));
    expect(res.status).toBe(201);
    return body as { api_key: string; webhook_secret: string; webhook_url: string; signed_webhook_url: string };
  }

  const job = async (id: string, key: string): Promise<any> => (await call(req("GET", `/tasks/${id}`, { headers: { "x-api-key": key } }))).body;

  it("creates the schema lazily on first use, recorded like the Python migrator", async () => {
    const { res, body } = await call(req("GET", "/admin/clients", { headers: ADMIN }));
    expect(res.status).toBe(200);
    expect(body).toEqual([]);
    const rows = (await infra.pool.query("SELECT version, description FROM schema_migrations")).rows;
    expect(rows).toEqual([{ version: 1, description: MIGRATION_V1_DESC }]);
  });

  it("root, livez and healthz (degraded until the first cron run, then ok)", async () => {
    expect((await call(req("GET", "/livez"))).body).toEqual({ status: "alive" });
    expect((await call(req("GET", "/"))).body.job_types).toEqual(["echo", "http.fetch", "llm.complete", "webhook.event"]);
    let h = await call(req("GET", "/healthz"));
    expect(h.res.status).toBe(200);
    expect(h.body.checks).toMatchObject({ database: "ok", redis: "ok", worker: "waiting for first cron run" });
    expect(h.body.status).toBe("degraded");
    await runCron(infra.deps(), Date.now());
    h = await call(req("GET", "/healthz"));
    expect([h.body.status, h.body.checks.worker, h.body.backends.queue]).toEqual(["ok", "ok", "upstash-rest"]);
    expect(h.body.config_warnings).toEqual([]);
  });

  it("healthz exposes a misconfigured deployment instead of looking healthy", async () => {
    const h = await call(req("GET", "/healthz"), { GROQ_API_KEY: "", GEMINI_API_KEY: "", ADMIN_TOKEN: "" });
    expect(h.body.status).toBe("degraded");
    expect(h.body.config_warnings.join(" ")).toMatch(/no LLM key.*ADMIN_TOKEN/);
  });

  it("admin auth, validation, duplicate slug, list, deactivate", async () => {
    expect((await call(req("POST", "/admin/clients", { body: { slug: "x-co", name: "X" } }))).res.status).toBe(401);
    expect((await call(req("POST", "/admin/clients", { body: { slug: "Bad Slug", name: "X" }, headers: ADMIN }))).res.status).toBe(422);
    const c = await newClient("acme");
    expect(c.api_key).toMatch(/^ap_/);
    expect(c.signed_webhook_url).toBe("https://autoplatform.test/webhook/acme");
    expect((await call(req("POST", "/admin/clients", { body: { slug: "acme", name: "dup" }, headers: ADMIN }))).res.status).toBe(409);
    const key = { "x-api-key": c.api_key };
    expect((await call(req("POST", "/webhook", { body: { type: "echo" }, headers: key }))).res.status).toBe(202);
    expect((await call(req("DELETE", "/admin/clients/acme", { headers: ADMIN }))).body).toEqual({ slug: "acme", active: false });
    expect((await call(req("POST", "/webhook", { body: { type: "echo" }, headers: key }))).res.status).toBe(401);
  });

  it("webhook auth + validation", async () => {
    const c = await newClient("valid-co");
    const key = { "x-api-key": c.api_key };
    expect((await call(req("POST", "/webhook", { body: { type: "echo" } }))).res.status).toBe(401);
    expect((await call(req("POST", "/webhook", { body: { type: "echo" }, headers: { "x-api-key": "nope" } }))).res.status).toBe(401);
    expect((await call(req("POST", "/webhook", { body: { type: "nope" }, headers: key }))).res.status).toBe(422);
    expect((await call(req("POST", "/webhook", { body: { type: "system.maintenance" }, headers: key }))).res.status).toBe(422);
    expect((await call(req("POST", "/webhook", { body: { type: "echo", max_attempts: 99 }, headers: key }))).res.status).toBe(422);
    expect((await call(req("POST", "/webhook", { raw: "{bad json", headers: { ...key, "content-type": "application/json" } }))).res.status).toBe(422);
  });

  it("echo job runs right after the response (waitUntil) and is visible via /tasks", async () => {
    const c = await newClient("echo-co");
    const key = { "x-api-key": c.api_key };
    const { res, body } = await call(req("POST", "/webhook", { body: { type: "echo", payload: { hello: "world" } }, headers: key }));
    expect(res.status).toBe(202);
    expect(body.status_url).toBe(`https://autoplatform.test/tasks/${body.job_id}`);
    const j = await job(body.job_id, c.api_key);
    expect([j.status, j.attempts, j.result.echo]).toEqual(["succeeded", 1, { hello: "world" }]);
    expect((await call(req("GET", "/tasks", { headers: key }))).body.map((x: any) => x.id)).toEqual([body.job_id]);
    const other = await newClient("other-co");
    expect((await call(req("GET", `/tasks/${body.job_id}`, { headers: { "x-api-key": other.api_key } }))).res.status).toBe(404);
  });

  it("LLM job: Groq 429 -> Gemini, usage rows saved, shared cooldown set in Redis", async () => {
    const c = await newClient("llm-co");
    infra.llm.script("groq", [429, { "retry-after": "30" }, undefined]);
    const { body } = await call(req("POST", "/webhook", { body: { type: "llm.complete", payload: { prompt: "Say hi" } },
      headers: { "x-api-key": c.api_key } }));
    const j = await job(body.job_id, c.api_key);
    expect(j.status).toBe("succeeded");
    expect([j.result.provider, j.result.text]).toEqual(["gemini", "hello from gemini"]);
    expect(j.result.attempts.map((a: any) => a.error_kind)).toEqual(["rate_limit", null]);
    const usage = (await infra.pool.query("SELECT provider, success, prompt_tokens FROM api_usage WHERE job_id = $1 ORDER BY id", [body.job_id])).rows;
    expect(usage).toEqual([{ provider: "groq", success: false, prompt_tokens: 0 }, { provider: "gemini", success: true, prompt_tokens: 11 }]);
    expect(Number(await infra.redis.get(`${infra.prefix}:llm:cooldown:groq`))).toBeGreaterThan(Date.now() / 1000 + 20);
    // the next job in a *different* invocation skips Groq entirely
    infra.llm.calls = [];
    const second = await call(req("POST", "/webhook", { body: { type: "llm.complete", payload: { prompt: "again" } },
      headers: { "x-api-key": c.api_key } }));
    expect((await job(second.body.job_id, c.api_key)).result.provider).toBe("gemini");
    expect(infra.llm.callsTo("groq")).toBe(0);
    await infra.redis.del(`${infra.prefix}:llm:cooldown:groq`);
  });

  it("retry with backoff via the delayed queue, then success", async () => {
    const c = await newClient("retry-co");
    const { body } = await call(req("POST", "/webhook", { body: { type: "echo", payload: { fail_times: 1 } }, headers: { "x-api-key": c.api_key } }));
    let j = await job(body.job_id, c.api_key);
    expect([j.status, j.attempts]).toEqual(["retrying", 1]);
    expect(await infra.redis.zscore(`${infra.prefix}:delayed`, body.job_id)).not.toBeNull();
    await new Promise((r) => setTimeout(r, 200));
    const summary = await runCron(infra.deps(), Date.now());
    expect((summary.drain as any).succeeded).toBe(1);
    j = await job(body.job_id, c.api_key);
    expect([j.status, j.attempts]).toEqual(["succeeded", 2]);
  });

  it("permanent errors and exhausted attempts fail without endless retries", async () => {
    const c = await newClient("fail-co");
    const key = { "x-api-key": c.api_key };
    const a = await call(req("POST", "/webhook", { body: { type: "llm.complete", payload: {} }, headers: key }));
    const ja = await job(a.body.job_id, c.api_key);
    expect([ja.status, ja.attempts]).toEqual(["failed", 1]);
    expect(ja.error).toMatch(/PermanentError|prompt/);
    const b = await call(req("POST", "/webhook", { body: { type: "echo", payload: { fail_times: 9 }, max_attempts: 1 }, headers: key }));
    expect((await job(b.body.job_id, c.api_key)).status).toBe("failed");
  });

  it("idempotency keys return the first job and create no extra rows", async () => {
    const c = await newClient("idem-co");
    const h = { "x-api-key": c.api_key, "idempotency-key": "order-42" };
    const a = await call(req("POST", "/webhook", { body: { type: "echo" }, headers: h }));
    const b = await call(req("POST", "/webhook", { body: { type: "echo" }, headers: h }));
    expect([b.body.duplicate, b.body.job_id]).toEqual([true, a.body.job_id]);
    const n = (await infra.pool.query("SELECT count(*)::int AS n FROM jobs j JOIN clients c ON c.id = j.client_id WHERE c.slug = 'idem-co'")).rows[0].n;
    expect(n).toBe(1);
  });

  it("signed webhooks: HMAC verified on the raw body", async () => {
    const c = await newClient("signed-co");
    const raw = JSON.stringify({ action: "opened", llm_prompt: "summarize" });
    const sig = "sha256=" + (await hmacSha256Hex(c.webhook_secret, new TextEncoder().encode(raw)));
    expect((await call(req("POST", "/webhook/signed-co", { raw, headers: { "x-signature-256": "sha256=deadbeef" } }))).res.status).toBe(401);
    expect((await call(req("POST", "/webhook/nobody", { raw, headers: { "x-signature-256": sig } }))).res.status).toBe(401);
    const ok = await call(req("POST", "/webhook/signed-co", { raw, headers: { "x-hub-signature-256": sig, "x-github-event": "pull_request" } }));
    expect(ok.res.status).toBe(202);
    const j = await job(ok.body.job_id, c.api_key);
    expect(j.result).toMatchObject({ event: "pull_request", keys: ["action", "llm_prompt"], llm: { provider: "groq" } });
  });

  it("cron endpoints: auth, maintenance + provider probe as jobs", async () => {
    expect((await call(req("POST", "/cron/maintenance"))).res.status).toBe(401);
    expect((await call(req("POST", "/cron/unknown", { headers: CRON }))).res.status).toBe(404);
    const m = await call(req("POST", "/cron/maintenance", { headers: CRON }));
    expect(m.res.status).toBe(202);
    const mj = (await call(req("GET", `/cron/jobs/${m.body.job_id}`, { headers: CRON }))).body;
    expect(mj.status).toBe("succeeded");
    expect(Object.keys(mj.result)).toEqual(expect.arrayContaining(["stuck", "requeued", "promoted", "pruned"]));
    const p = await call(req("POST", "/cron/provider_probe", { headers: ADMIN }));
    const pj = (await call(req("GET", `/cron/jobs/${p.body.job_id}`, { headers: CRON }))).body;
    expect(pj.result.providers.groq.ok && pj.result.providers.gemini.ok).toBe(true);
  });

  it("scheduled run: maintenance at :17 recovers a job whose id never reached Redis", async () => {
    const c = await newClient("stuck-co");
    await infra.pool.query(`INSERT INTO jobs (id, client_id, type, payload, status, attempts, max_attempts, source, created_at, run_at)
      SELECT 'stuck000000000000000000000000001', id, 'echo', '{}'::json, 'queued', 0, 3, 'test', now(), now() - interval '30 minutes'
      FROM clients WHERE slug = 'stuck-co'`);
    const t = new Date();
    t.setUTCMinutes(17);
    const summary: any = await runCron(infra.deps(), t.getTime());
    expect(summary.maintenance.requeued).toBeGreaterThanOrEqual(1);
    expect(summary.drain.succeeded).toBeGreaterThanOrEqual(1);
    expect((await job("stuck000000000000000000000000001", c.api_key)).status).toBe("succeeded");
    const hb = (await infra.pool.query("SELECT worker_id, status FROM worker_health")).rows;
    expect(hb).toEqual([{ worker_id: "cloudflare-worker", status: "alive" }]);
  });

  it("the subrequest budget stops the drain cleanly; leftovers are picked up later", async () => {
    const c = await newClient("budget-co");
    const deps = infra.deps({}, 12); // tiny budget, like an exhausted invocation
    const ids: string[] = [];
    for (let i = 0; i < 3; i++) {
      const r = await call(req("POST", "/webhook", { body: { type: "echo", delay_seconds: 0 }, headers: { "x-api-key": c.api_key } }));
      ids.push(r.body.job_id);
    }
    // all three already ran inline; now queue three more without inline processing and drain on a small budget
    const more = [];
    for (let i = 0; i < 3; i++) {
      more.push((await infra.pool.query(`INSERT INTO jobs (id, client_id, type, payload, status, attempts, max_attempts, source, created_at, run_at)
        SELECT md5(random()::text), id, 'echo', '{}'::json, 'queued', 0, 3, 'test', now(), now() FROM clients WHERE slug = 'budget-co' RETURNING id`)).rows[0].id);
    }
    for (const id of more) await infra.redis.lpush(`${infra.prefix}:pending`, id);
    const out = await drain(deps, { maxJobs: 10, deadline: Date.now() + 10_000 });
    expect(out.succeeded).toBe(1); // budget allowed one job, then stopped without crashing
    const rest = await drain(infra.deps(), { maxJobs: 10, deadline: Date.now() + 10_000 });
    expect(rest.succeeded).toBe(2);
  });

  it("body size limit (Content-Length and streamed)", async () => {
    const big = "x".repeat(20_000);
    // a real client sends Content-Length: rejected before auth, like the FastAPI middleware
    expect((await call(req("POST", "/webhook", { raw: big, headers: { "content-type": "application/json", "content-length": String(big.length) } }),
      { MAX_BODY_BYTES: "10000" })).res.status).toBe(413);
    // authenticated client, oversized JSON without a usable length: capped while reading
    const c = await newClient("big-co");
    expect((await call(req("POST", "/webhook", { raw: JSON.stringify({ type: "echo", payload: { x: big } }), headers: { "x-api-key": c.api_key, "content-type": "application/json" } }),
      { MAX_BODY_BYTES: "10000" })).res.status).toBe(413);
    const stream = new ReadableStream({
      start(ctl) {
        for (let i = 0; i < 30; i++) ctl.enqueue(new TextEncoder().encode("x".repeat(1000)));
        ctl.close();
      },
    });
    expect((await call(req("POST", "/webhook/acme", { raw: stream, headers: { "x-signature-256": "sha256=00" } }), { MAX_BODY_BYTES: "10000" })).res.status).toBe(413);
  });

  it("metrics: locked in production, Prometheus + JSON", async () => {
    expect((await call(req("GET", "/metrics"), { APP_ENV: "production" })).res.status).toBe(401);
    const m = await call(req("GET", "/metrics", { headers: ADMIN }), { APP_ENV: "production" });
    expect(m.res.status).toBe(200);
    expect(m.body).toContain('autoplatform_jobs{status="succeeded"}');
    expect(m.body).toContain('autoplatform_worker_up{worker_id="cloudflare-cron"}');
    const j = await call(req("GET", "/metrics?format=json", { headers: ADMIN }), { APP_ENV: "production" });
    expect(j.body.jobs.succeeded).toBeGreaterThan(0);
    expect(j.body.llm_usage_24h.length).toBeGreaterThan(0);
  });

  it("public-endpoint guards: junk keys/signatures never reach the DB; healthz is cached", async () => {
    let queries = 0;
    const counting = (env = {}) => {
      const d = infra.deps(env);
      const inner = d.db;
      d.db = { query: (t, p) => (queries++, inner.query(t, p)), transaction: (s) => (queries++, inner.transaction(s)) };
      return d;
    };
    const run = async (r: Request, env = {}) => handle(r, counting(env), new TestCtx());
    expect((await run(req("POST", "/webhook", { body: { type: "echo" }, headers: { "x-api-key": "ap_short" } }))).status).toBe(401);
    expect((await run(req("GET", "/tasks", { headers: { "x-api-key": "x".repeat(46) } }))).status).toBe(401);
    expect((await run(req("POST", "/webhook/acme", { raw: "{}", headers: { "x-signature-256": "sha256=nothex" } }))).status).toBe(401);
    expect(queries).toBe(0);
    const first = await run(req("GET", "/healthz"), { HEALTHZ_CACHE_SECONDS: "15" });
    const before = queries;
    const second = await run(req("GET", "/healthz"), { HEALTHZ_CACHE_SECONDS: "15" });
    expect(second.headers.get("x-cache")).toBe("hit");
    expect(queries).toBe(before);
    expect(await second.json()).toEqual(await first.json());
  });

  it("unknown routes and methods", async () => {
    expect((await call(req("GET", "/nope"))).res.status).toBe(404);
    expect((await call(req("PUT", "/webhook"))).res.status).toBe(405);
  });
});
