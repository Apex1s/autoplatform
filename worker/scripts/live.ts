// Runs the Worker code in Node against REAL services (Neon HTTPS, Upstash REST, Groq, Gemini):
// HTTP server -> worker.fetch(), plus worker.scheduled() every minute, like Cloudflare does.
// Usage: NODE_USE_ENV_PROXY=1 npx tsx scripts/live.ts [port]   (reads ../.env)
import { readFileSync } from "node:fs";
import http from "node:http";
import worker from "../src/index";
import type { Env } from "../src/env";

function dotenv(path: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of readFileSync(path, "utf8").split("\n")) {
    const m = /^([A-Z0-9_]+)=(.*)$/.exec(line.trim());
    if (m) out[m[1]] = m[2].replace(/^"(.*)"$/, "$1");
  }
  return out;
}

const PORT = Number(process.argv[2] ?? 8787);
const file = dotenv(new URL("../../.env", import.meta.url).pathname);
const env: Env = {
  DATABASE_URL: file.DATABASE_URL_POOLED || file.DATABASE_URL,
  UPSTASH_REDIS_URL: file.UPSTASH_REDIS_URL,
  GROQ_API_KEY: file.GROQ_API_KEY,
  GEMINI_API_KEY: file.GEMINI_API_KEY,
  ADMIN_TOKEN: file.ADMIN_TOKEN,
  CRON_TOKEN: file.CRON_TOKEN,
  APP_ENV: "production",
  QUEUE_PREFIX: process.env.QUEUE_PREFIX ?? "livetest",
  GROQ_BASE_URL: process.env.GROQ_BASE_URL,
};

const ctx = {
  waitUntil: (p: Promise<unknown>) => void p.catch((e) => console.error("waitUntil", e)),
  passThroughOnException: () => {},
  props: {},
} as unknown as ExecutionContext;

http.createServer(async (nreq, nres) => {
  const chunks: Buffer[] = [];
  for await (const c of nreq) chunks.push(c as Buffer);
  const headers = new Headers();
  for (const [k, v] of Object.entries(nreq.headers)) if (typeof v === "string") headers.set(k, v);
  const method = nreq.method ?? "GET";
  const request = new Request(`http://127.0.0.1:${PORT}${nreq.url}`, {
    method, headers, body: ["GET", "HEAD"].includes(method) ? undefined : Buffer.concat(chunks),
  });
  const res = await worker.fetch(request, env, ctx);
  nres.writeHead(res.status, Object.fromEntries(res.headers));
  nres.end(Buffer.from(await res.arrayBuffer()));
}).listen(PORT, () => console.log(`live worker on http://127.0.0.1:${PORT} (prefix ${env.QUEUE_PREFIX})`));

const tick = () =>
  worker.scheduled({ scheduledTime: Date.now(), cron: "* * * * *", noRetry() {} } as unknown as ScheduledController, env, ctx)
    .catch((e) => console.error("cron", e));
await tick();
setInterval(tick, 60_000);
